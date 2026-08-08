"""The one path that creates claims.

Session extraction, connectors, consumer requests, and curator entry all write
through here. That is not tidiness: every invariant a claim carries -- it
conforms to the ontology, its value matches the predicate's declared type, its
subject resolves to a real entity, it has provenance, it cannot be more visible
than the thing it describes -- holds only because there is exactly one place
that can create one. A second writer would satisfy none of them while producing
rows that look identical.

A lint gate enforces the same rule structurally, the way the visibility
chokepoint is enforced: nothing outside this module (and its sibling
`claim_curator_actions.py`, composed into the same class below) may INSERT
into `memory_claims`.

**Why this file, and not one `claims.py`.** The module that used to hold this
class, plus the resolution/authority logic it depends on and the two curator
decisions, crossed the line-count ceiling this program enforces on service
modules. It splits along its one real seam: this file is `ClaimService`
itself -- construction, the machine/system write path (`stage_claim` and the
confidence-scoring helpers it calls), and the lifecycle operations that don't
need a curator (`stage_confirmation`, `close_superseded`, `mark_consolidated`,
`set_promotion_state`, `merge_provenance`). `claim_authority.py` holds the
read-only resolution/authority/value-conformance checks both this file and
`claim_curator_actions.py` depend on. `claim_curator_actions.py` holds the two
decisions a curator makes about a claim that already exists (`link_subject`,
`discard`). `claim_erasure_writes.py` holds the actor-erasure participant's
claims-table writer, which is not a `ClaimService` method at all -- it lives
in this package only because this package is the one writer `memory_claims`
and `memory_claim_provenance` have.

`ClaimService` is assembled here as `_ClaimResolutionMixin` (resolution) plus
`_ClaimCuratorActionsMixin` (curator decisions) plus the methods defined
directly below (the write/scoring path). A mixin composition rather than a
package with a re-exporting `__init__` or a composed helper object, because it
is the one shape that moves every method's body unchanged: every internal
`self.foo(...)` call in the code below already worked by looking up `foo` on
whatever class the running instance actually is, and that lookup does not care
which file defined `foo` -- so no call site needed to change to make the split
work, only where each method's definition physically lives.

**Validation order is deliberate.** Cheap structural checks first, then the
subject resolution that costs a query. A malformed value should not pay for a
lookup, and more importantly a rejection reason should describe the first thing
wrong rather than whichever check happened to run.

**A claim is never silently dropped.** An unresolvable subject is stored
`unlinked` and queued for a human; every other rejection raises with a
categorized reason. Silent rejection is the failure mode where extraction
quietly stops producing and nobody notices for a month.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK
from contextplane.service.memory.claim_authority import (
    EVIDENCE_CONNECTOR_RUN,
    EVIDENCE_CURATOR,
    REJECT_INTERVAL,
    STATUS_STAGED,
    STATUS_UNLINKED,
    UNCALIBRATED,
    ClaimRejected,
    Evidence,
    StagedClaim,
    _ClaimResolutionMixin,
    _maybe_uuid,
)
from contextplane.service.memory.claim_curator_actions import _ClaimCuratorActionsMixin
from contextplane.service.memory.confidence import (
    SCORER_VERSION,
    ConfidencePolicy,
    EvidenceClass,
)
from contextplane.service.memory.confidence import (
    score as score_confidence,
)
from contextplane.service.memory.confidence_decay import half_life_days
from contextplane.service.memory.confidence_read import subject_change_profile
from contextplane.service.memory.contest import ContestOutcome, detect_for_claim
from contextplane.service.retrieval.embedding_index import project_claim
from contextplane.types import Clock, JSONValue, TenantContext

# Why a claim stopped being current. A status of `superseded` says a claim is no
# longer current without saying whether it lost a conflict, was a duplicate, or was
# replaced by a person -- and a reviewer asking what changed wants the difference.
SUPERSEDED_REASONS = frozenset({"lost_conflict", "cluster_collapsed", "human_confirmed", "curator_replaced"})

_STAGED = Counter(
    "contextplane_claim_staged_total",
    "Claims written, by status and derived source authority.",
    ["status", "source_authority"],
)

# Counted separately from the status label because the rate is the signal, not
# the total: a rising share of unresolved subjects means extraction is drifting
# off the entity model, which no absolute count makes visible.
_UNLINKED = Counter(
    "contextplane_claim_unresolved_subject_total",
    "Claims stored unlinked because the subject did not resolve.",
)


class ClaimService(_ClaimResolutionMixin, _ClaimCuratorActionsMixin):
    """Stages claims. The only creator of `memory_claims` rows."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def stage_claim(
        self,
        ctx: TenantContext,
        *,
        subject_reference: str,
        predicate: str,
        value: JSONValue,
        evidence: tuple[Evidence, ...],
        asserted_valid_from: datetime.datetime | None = None,
        asserted_valid_to: datetime.datetime | None = None,
        visibility: str | None = None,
        token_count: int | None = None,
        tokenizer_id: str | None = None,
        # The provider's own number, on whatever scale it used. Stored and unused:
        # nothing has checked what it predicts, and using it would launder an
        # unexamined figure into an authoritative-looking signal. Kept because a
        # mapping can only ever be fitted from raw scores paired with judged
        # outcomes, so discarding them would make that state permanent.
        provider_confidence: float | None = None,
        namespace: str | None = None,
        strategy_id: str | None = None,
    ) -> StagedClaim:
        """Validate, resolve, and stage one claim.

        Evidence is required. A claim with no provenance cannot be checked by
        anyone later, and an unverifiable assertion in a store whose whole
        premise is trust is worse than no assertion.
        """
        if not evidence:
            msg = "a claim requires provenance; an assertion nobody can check is not evidence"
            raise ValidationError(msg)

        now = self._clock.now()
        valid_from = asserted_valid_from if asserted_valid_from is not None else now
        if asserted_valid_to is not None and asserted_valid_to <= valid_from:
            raise ClaimRejected(REJECT_INTERVAL, "asserted_valid_to must be after asserted_valid_from")

        async with self._session_factory() as session, session.begin():
            declared = await self._resolve_predicate(session, ctx, predicate)
            self._validate_value(value, declared)

            subject = await self._resolve_subject(session, ctx, subject_reference)
            resolved_visibility = await self._derive_visibility(session, subject, requested=visibility)
            # An entity reference in the value position is resolved here, through
            # the same chokepoint as the subject, and the result is stored. That
            # is what lets two claims naming one entity by different names be
            # recognised as agreeing without a query at comparison time -- and
            # resolving an arbitrary reference outside the chokepoint would answer
            # "does this exist" for every entity in the deployment.
            value_entity_id: uuid.UUID | None = None
            if declared.value_type == "entity_ref" and isinstance(value, str):
                referenced = await self._resolve_subject(session, ctx, value)
                value_entity_id = referenced.entity_id

            authority, derivations = await self._derive_authority(session, ctx, subject, evidence)
            status = STATUS_UNLINKED if subject.entity_id is None else STATUS_STAGED
            contest = ContestOutcome(detected=(), neighbourhood_size=0, truncated=False)

            claim_id = uuid.uuid4()
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))

            # Scored twice, and deliberately. The row must carry a score from the
            # moment it exists -- a claim with a resolved subject and no score is
            # refused by the schema, because such a claim would be invisible to
            # every path that filters on confidence. The base is knowable here,
            # since it comes from the authority tier alone. Corroboration and any
            # disagreement refine it once the provenance rows exist and the
            # neighbourhood has been compared, both of which need this row first.
            policy = ConfidencePolicy()
            initial = score_confidence(authority=authority, policy=policy)

            # How fast this subject actually changes, read from the canonical
            # graph's own history. The category sets the rate and the subject
            # modifies it: two capabilities can hold the same kind of claim and
            # deserve different half-lives, because one of them moves weekly and
            # the other has not changed in a year.
            median_change, observations = (None, 0)
            if subject.entity_id is not None:
                median_change, observations = await subject_change_profile(
                    session, entity_id=subject.entity_id, now=now
                )
            half_life = half_life_days(
                declared.claim_category,
                subject_median_change_days=median_change,
                subject_change_observations=observations,
                tenant_multiplier=policy.decay_multiplier,
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id,"
                    "  subject_entity_id, subject_reference, predicate, value_type,"
                    "  claim_category, value_jsonb, asserted_valid_from, asserted_valid_to,"
                    "  status, visibility, source_authority, size_bytes, token_count,"
                    "  tokenizer_id, namespace, strategy_id, value_cardinality,"
                    "  value_entity_id, confidence, confidence_scored_at,"
                    "  confidence_inputs, scorer_version, calibration_version,"
                    "  decay_half_life_days, provider_confidence, created_at"
                    ") VALUES (:cid, :owner, :author, :actor, :subject, :ref, :pred, :vtype,"
                    "          :cat, CAST(:val AS JSONB), :vfrom, :vto, :status, :vis, :auth,"
                    "          :size, :tokens, :tokenizer, :ns, :strat, :card, :ventity,"
                    "          CAST(:conf AS NUMERIC), :conf_at, CAST(:conf_in AS JSONB),"
                    "          :scorer, :calib, CAST(:half_life AS NUMERIC),"
                    "          CAST(:prov_conf AS NUMERIC), :now)"
                ),
                {
                    "cid": claim_id,
                    "owner": subject.owning_tenant_id,
                    "author": ctx.tenant_id,
                    "actor": ctx.actor_id,
                    "subject": subject.entity_id,
                    "ref": subject_reference,
                    "pred": predicate,
                    "vtype": declared.value_type,
                    "cat": declared.claim_category,
                    "val": canonical,
                    "vfrom": valid_from,
                    "vto": asserted_valid_to,
                    "status": status,
                    "vis": resolved_visibility,
                    "auth": authority,
                    "size": len(canonical.encode("utf-8")),
                    "tokens": token_count,
                    "tokenizer": tokenizer_id,
                    # Namespaces group and scope retrieval, so the value travels
                    # with the thing retrieved. Both NULL for a claim with no
                    # strategy -- a connector's or a curator's -- because a
                    # synthetic namespace would imply a grouping nobody chose.
                    "ns": namespace,
                    "strat": strategy_id,
                    # Copied from the vocabulary onto the row so the sweep that
                    # looks for disagreements never re-reads the ontology to learn
                    # whether a predicate can disagree with itself.
                    "card": declared.value_cardinality,
                    "ventity": value_entity_id,
                    # Null together for an unlinked claim, which is excluded from
                    # scoring entirely: a number there would assert a
                    # determination nobody made.
                    "conf": initial.value if initial is not None else None,
                    "conf_at": now if initial is not None else None,
                    "conf_in": (json.dumps(initial.inputs.as_json(), sort_keys=True) if initial is not None else None),
                    "scorer": SCORER_VERSION if initial is not None else None,
                    # Nothing has checked what a provider's self-report predicts,
                    # so the token says so rather than naming a mapping.
                    "calib": UNCALIBRATED if initial is not None else None,
                    "half_life": half_life if initial is not None else None,
                    "prov_conf": provider_confidence,
                    "now": now,
                },
            )

            for item in evidence:
                independence_key, independence_group = await self._independence(session, ctx, item, claim_id=claim_id)
                await session.execute(
                    text(
                        "INSERT INTO memory_claim_provenance "
                        "  (claim_id, evidence_kind, evidence_ref, evidence_excerpt, "
                        "   derivation, independence_key, independence_group, recorded_at) "
                        "VALUES (:cid, :kind, :ref, :excerpt, :deriv, :ikey, :igroup, :now) "
                        "ON CONFLICT (claim_id, evidence_kind, evidence_ref) DO NOTHING"
                    ),
                    {
                        "cid": claim_id,
                        "kind": item.kind,
                        "ref": item.ref,
                        "excerpt": item.excerpt,
                        # Which row set the floor. Authority is a minimum over
                        # the set, so without this an auditor cannot
                        # reconstruct why a claim scored as it did.
                        "deriv": derivations[(item.kind, item.ref)],
                        # Which source this evidence traces to. Repetition through
                        # one source is not corroboration, so scoring counts
                        # sources rather than rows.
                        "ikey": independence_key,
                        "igroup": independence_group,
                        "now": now,
                    },
                )

            if status == STATUS_STAGED:
                # Same transaction as the claim, so a claim and the disagreements
                # it creates commit together. A separate transaction could leave a
                # claim staged and uncontested while it conflicts with something
                # already stored -- and the promotion gate reads that flag.
                contest = await detect_for_claim(session, claim_id=claim_id, now=now)

            # Scored after detection, because a disagreement lowers the score and
            # the claim must not be stored briefly holding a number that ignores
            # a conflict already found.
            await self._rescore(
                session,
                claim_id=claim_id,
                status=status,
                authority=authority,
                is_contested=contest.is_contested,
                policy=policy,
                now=now,
            )

            # The claims already stored that this one disagrees with are rescored
            # too. A disagreement lowers both sides, and only one of them is the
            # claim being written -- leaving the other on its uncontested score
            # would show a conflicted pair where one side still looks confident.
            for other in contest.counterparties(claim_id):
                await self.rescore_existing(session, claim_id=other, policy=policy, now=now)

        _STAGED.labels(status=status, source_authority=authority).inc()
        if subject.entity_id is None:
            _UNLINKED.inc()

        return StagedClaim(
            claim_id=claim_id,
            subject_entity_id=subject.entity_id,
            predicate=predicate,
            value=value,
            status=status,
            visibility=resolved_visibility,
            owning_tenant_id=subject.owning_tenant_id,
            source_authority=authority,
            is_contested=contest.is_contested,
        )

    async def _independence(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        item: Evidence,
        *,
        claim_id: uuid.UUID,
    ) -> tuple[str | None, str | None]:
        """Which source a piece of evidence traces to, as a digest.

        Two turns of one conversation share a source, and so do two runs of one
        connector over one artefact -- re-running a parse that is a pure function
        of the fetched bytes is a recomputation, not a second observation. Scoring
        counts sources, so the class has to be resolved once and stored.

        Digested rather than stored raw. A session's events are physically removed
        by an erasure request while the claims derived from them survive, and a raw
        key would leave an actor and a session identifier on a row that outlives
        them. Equality is all scoring needs, and a digest compares just as well.
        """
        raw_key: str | None = None
        raw_group: str | None = None

        if item.kind == EVIDENCE_CURATOR:
            # The authenticated actor, never the supplied reference -- the ref is
            # caller-controlled and nothing validates it.
            raw_key = f"actor:{ctx.actor_id}"
            raw_group = f"actor:{ctx.actor_id}"

        elif item.kind == EVIDENCE_CONNECTOR_RUN:
            run = _maybe_uuid(item.ref)
            if run is not None:
                row = (
                    await session.execute(
                        text("SELECT source_id FROM sync_runs " "WHERE sync_run_id = :rid AND tenant_id = :tid"),
                        {"rid": run, "tid": ctx.tenant_id},
                    )
                ).one_or_none()
                if row is not None:
                    # The source, not the run. Two runs against one source are one
                    # source however many times it was fetched.
                    raw_key = f"connector_source:{row.source_id}"
                    raw_group = f"connector_source:{row.source_id}"

        elif item.kind == "session_event":
            event = _maybe_uuid(item.ref)
            if event is not None:
                row = (
                    await session.execute(
                        text(
                            "SELECT tenant_id, actor_id, session_id " "FROM memory_session_events WHERE event_id = :eid"
                        ),
                        {"eid": event},
                    )
                ).one_or_none()
                if row is not None:
                    # The whole triple, not the session string alone: a session id
                    # is caller-supplied text, unique only within one actor, so two
                    # actors can collide on "main".
                    raw_key = f"session:{row.tenant_id}:{row.actor_id}:{row.session_id}"
                    raw_group = f"actor:{row.actor_id}"

        else:
            # A document revision, commit, or work item. No producer exists to
            # group these, so each distinct reference is its own source.
            raw_key = f"evidence:{item.kind}:{item.ref}"
            raw_group = f"evidence:{item.kind}:{item.ref}"

        if raw_key is None or raw_group is None:
            # An unresolvable pointer traces to nothing, so it corroborates
            # nothing. Left null rather than made unique, which would let a bad
            # reference buy a corroboration class.
            return None, None

        return _digest(raw_key), _digest(raw_group)

    async def rescore_existing(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        policy: ConfidencePolicy,
        now: datetime.datetime,
    ) -> None:
        """Rescore a claim already stored, reading its own authority and state.

        Used when something else changed what this claim's score should be -- a
        newly detected disagreement, most often. Reads the tier and contested flag
        from the row rather than taking them as arguments, so a caller cannot
        rescore a claim under an authority it does not have.
        """
        row = (
            await session.execute(
                text("SELECT status, source_authority, is_contested " "FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim_id},
            )
        ).one_or_none()
        if row is None or row.status == STATUS_UNLINKED:
            return

        await self._rescore(
            session,
            claim_id=claim_id,
            status=row.status,
            authority=row.source_authority,
            is_contested=row.is_contested,
            policy=policy,
            now=now,
        )

    async def _rescore(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        status: str,
        authority: str,
        is_contested: bool,
        policy: ConfidencePolicy,
        now: datetime.datetime,
    ) -> None:
        """Refine the score now that provenance exists and the neighbourhood is known.

        The row already carries a base score from its authority tier. This adds
        what could not be known before it existed: how many independent sources
        agree, and whether anything disagrees.

        An unlinked claim is left alone. Such a claim is excluded from scoring,
        consolidation, promotion and serving.
        """
        if status == STATUS_UNLINKED:
            return

        classes = [
            EvidenceClass(
                key=row.independence_key,
                group=row.independence_group,
                authority_rank=SOURCE_AUTHORITY_RANK[authority],
            )
            for row in (
                await session.execute(
                    text(
                        "SELECT independence_key, independence_group "
                        "FROM memory_claim_provenance "
                        "WHERE claim_id = :cid AND independence_key IS NOT NULL"
                    ),
                    {"cid": claim_id},
                )
            ).all()
        ]

        # A claim's own evidence establishes it rather than corroborating it, so
        # one source is what produced the base score and only *additional*
        # independent sources count as agreement.
        #
        # Deduplicated by source before dropping one, not after. Dropping a row
        # first would leave several rows from the same conversation behind, and
        # they would then collapse to one class and read as agreement -- which is
        # exactly the repetition the independence rule exists to exclude.
        by_key: dict[str, EvidenceClass] = {}
        for item in classes:
            if item.key not in by_key:
                by_key[item.key] = item
        distinct = list(by_key.values())
        corroborators = distinct[1:]

        scored = score_confidence(
            authority=authority,
            corroborators=corroborators,
            is_contested=is_contested,
            policy=policy,
        )
        if scored is None:
            return

        await session.execute(
            text(
                "UPDATE memory_claims SET "
                "  confidence = CAST(:conf AS NUMERIC), "
                "  confidence_scored_at = CAST(:now AS TIMESTAMPTZ), "
                "  confidence_inputs = CAST(:inputs AS JSONB), "
                "  scorer_version = :scorer "
                "WHERE claim_id = :cid"
            ),
            {
                "cid": claim_id,
                "conf": scored.value,
                "now": now,
                "inputs": json.dumps(scored.inputs.as_json(), sort_keys=True),
                "scorer": SCORER_VERSION,
            },
        )

    async def stage_confirmation(
        self,
        session: AsyncSession,
        *,
        confirms_claim_id: uuid.UUID,
        authority: str,
        confidence: float,
        confidence_inputs: str,
        hold_until: datetime.datetime,
        confirming_tenant_id: uuid.UUID,
        confirming_actor_id: uuid.UUID | None,
        now: datetime.datetime,
    ) -> uuid.UUID:
        """Create the claim a human confirmation produces.

        Lives here because creating a claim is what this module does, and a second
        module inserting rows would be a second writer however careful it was. The
        invariants hold by construction: every field describing *what* is asserted
        -- predicate, value, type, category, subject, cardinality, visibility -- is
        copied from a row that already passed validation, and nothing is taken from
        a caller. What the caller supplies is only what confirmation changes.

        That distinction is the reason this is a method rather than an allowlist
        entry. A future edit taking a value from a parameter would be visible right
        next to the validation it bypassed, instead of in another file the write-path
        gate has been told to ignore.

        Runs in the caller's transaction so the new claim, its provenance, and the
        supersession marker on the original commit together.
        """
        new_claim_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id,"
                "  subject_entity_id, subject_reference, predicate, value_type,"
                "  claim_category, value_jsonb, asserted_valid_from, asserted_valid_to,"
                "  status, visibility, source_authority, size_bytes, namespace,"
                "  strategy_id, value_cardinality, value_entity_id, confidence,"
                "  confidence_scored_at, confidence_inputs, scorer_version,"
                "  calibration_version, decay_half_life_days, confidence_hold_until,"
                "  confirms_claim_id, confirmed_by, confirmed_at, created_at"
                ") SELECT :new_cid, owning_tenant_id, :author, :actor,"
                "         subject_entity_id, subject_reference, predicate, value_type,"
                "         claim_category, value_jsonb, asserted_valid_from,"
                "         asserted_valid_to, status, visibility, :auth, size_bytes,"
                "         namespace, strategy_id, value_cardinality, value_entity_id,"
                "         CAST(:conf AS NUMERIC), CAST(:now AS TIMESTAMPTZ),"
                "         CAST(:inputs AS JSONB), :scorer, calibration_version,"
                "         decay_half_life_days, CAST(:hold AS TIMESTAMPTZ),"
                "         :cid, :actor, CAST(:now AS TIMESTAMPTZ), CAST(:now AS TIMESTAMPTZ) "
                "  FROM memory_claims WHERE claim_id = :cid"
            ),
            {
                "new_cid": new_claim_id,
                "cid": confirms_claim_id,
                "author": confirming_tenant_id,
                "actor": confirming_actor_id,
                "auth": authority,
                "conf": confidence,
                "inputs": confidence_inputs,
                "scorer": SCORER_VERSION,
                "hold": hold_until,
                "now": now,
            },
        )

        # The original's provenance is copied, not moved. What the machine saw is
        # still why the claim was first made, and the confirmation adds a human act
        # on top rather than replacing the trail.
        await session.execute(
            text(
                "INSERT INTO memory_claim_provenance "
                "  (claim_id, evidence_kind, evidence_ref, evidence_excerpt, derivation, "
                "   independence_key, independence_group, recorded_at) "
                "SELECT :new_cid, evidence_kind, evidence_ref, evidence_excerpt, "
                "       derivation, independence_key, independence_group, recorded_at "
                "FROM memory_claim_provenance WHERE claim_id = :cid "
                "ON CONFLICT (claim_id, evidence_kind, evidence_ref) DO NOTHING"
            ),
            {"new_cid": new_claim_id, "cid": confirms_claim_id},
        )
        if confirming_actor_id is not None:
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance "
                    "  (claim_id, evidence_kind, evidence_ref, evidence_excerpt, derivation, "
                    "   independence_key, independence_group, recorded_at) "
                    "VALUES (:new_cid, 'curator', :ref, NULL, 'human', NULL, NULL, "
                    "        CAST(:now AS TIMESTAMPTZ)) "
                    "ON CONFLICT (claim_id, evidence_kind, evidence_ref) DO NOTHING"
                ),
                {"new_cid": new_claim_id, "ref": str(confirming_actor_id), "now": now},
            )

        # The original is closed, not merely pointed at a successor. Leaving it
        # `staged` would leave a weaker claim live in the same neighbourhood as its
        # own confirmation -- and a later machine claim of equal rank to the original
        # would then supersede it on recency, which is precisely the outcome a
        # confirmation is supposed to prevent.
        await self.close_superseded(
            session,
            claim_id=confirms_claim_id,
            survivor=new_claim_id,
            reason="human_confirmed",
            now=now,
        )
        return new_claim_id

    async def close_superseded(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        survivor: uuid.UUID,
        reason: str,
        now: datetime.datetime,
    ) -> None:
        """Close a claim in favour of another. Lifecycle only.

        Touches `status`, the invalidation timestamp, the successor pointer, and the
        reason -- never a field describing what is asserted. That is the line: a
        caller may retire a claim, and may not change what it said on the way out.

        The `staged` predicate is what makes a repeated sweep a no-op rather than a
        second closure, so a claim cannot be closed twice in favour of different
        survivors and no confidence drifts.
        """
        if reason not in SUPERSEDED_REASONS:
            msg = f"unknown supersession reason {reason!r}; expected one of {sorted(SUPERSEDED_REASONS)}"
            raise ValidationError(msg)
        await session.execute(
            text(
                "UPDATE memory_claims "
                "SET status = 'superseded', "
                "    t_invalidated_at = CAST(:now AS TIMESTAMPTZ), "
                "    superseded_by = :survivor, "
                "    superseded_reason = :reason "
                "WHERE claim_id = :cid AND status = 'staged'"
            ),
            {"cid": claim_id, "survivor": survivor, "reason": reason, "now": now},
        )
        # A closed claim is no longer servable, so its vectors have to go. Left behind
        # they cannot produce a wrong answer -- the read arms refuse them -- but every
        # dead vector occupies a candidate slot in an ANN search, which is a silent
        # recall loss on the queries that do matter.
        await project_claim(session, claim_id=claim_id, now=now)

    async def mark_consolidated(self, session: AsyncSession, *, claim_id: uuid.UUID, now: datetime.datetime) -> None:
        """Record that a claim has been reconciled against its neighbourhood.

        Reconciliation is what makes a claim servable, so it is also what makes it
        indexable. The projection hook is here rather than in the caller because this is
        the one place the timestamp is set -- a hook in `ConsolidationService` would be
        missed by any future path that consolidates by another route.
        """
        await session.execute(
            text("UPDATE memory_claims SET consolidated_at = CAST(:now AS TIMESTAMPTZ) " "WHERE claim_id = :cid"),
            {"cid": claim_id, "now": now},
        )
        await project_claim(session, claim_id=claim_id, now=now)

    async def set_promotion_state(self, session: AsyncSession, *, claim_id: uuid.UUID, state: str) -> None:
        """Record where a claim stands with respect to becoming canonical.

        A separate axis from `status`: a claim rejected for promotion is still
        staged, still readable, and still serves. Folding the two together would
        force a rejected claim to stop being a claim, and the rejection itself is
        meant to become evidence about it.

        Here rather than in the promotion service because this table has one writer.
        A second module able to flip promotion state could mark a claim promoted
        without anything having been written to the graph.
        """
        if state not in {"proposed", "promoted", "rejected", "reversed"}:
            raise ValueError(f"unknown promotion state {state!r}")
        await session.execute(
            text("UPDATE memory_claims SET promotion_state = :state WHERE claim_id = :cid"),
            {"cid": claim_id, "state": state},
        )

    async def merge_provenance(self, session: AsyncSession, *, survivor: uuid.UUID, collapsed: uuid.UUID) -> None:
        """Attribute a collapsed duplicate's evidence to the claim that survived.

        Here rather than in the caller because attributing evidence to a claim raises
        its corroboration, and corroboration raises its confidence. A module that
        could insert provenance freely could inflate any claim's score by citing
        anything, which is why this table has one writer.

        Copied, not moved: the closed claim keeps its own trail so the supersession
        chain stays readable. What the survivor gains is the knowledge that another
        source said the same thing -- which is exactly what makes it more credible.
        """
        await session.execute(
            text(
                "INSERT INTO memory_claim_provenance "
                "  (claim_id, evidence_kind, evidence_ref, evidence_excerpt, derivation, "
                "   independence_key, independence_group, recorded_at) "
                "SELECT :survivor, evidence_kind, evidence_ref, evidence_excerpt, "
                "       derivation, independence_key, independence_group, recorded_at "
                "FROM memory_claim_provenance WHERE claim_id = :collapsed "
                "ON CONFLICT (claim_id, evidence_kind, evidence_ref) DO NOTHING"
            ),
            {"survivor": survivor, "collapsed": collapsed},
        )


def _digest(raw: str) -> str:
    """A stable, non-reversible identity for a corroboration source.

    Salted with nothing on purpose: this is not a secret, it is a way of comparing
    two sources for equality without carrying the identifiers they were derived
    from onto a row that outlives them.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


__all__ = ["ClaimService"]
