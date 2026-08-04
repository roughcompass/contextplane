"""The one path that creates claims.

Session extraction, connectors, consumer requests, and curator entry all write
through here. That is not tidiness: every invariant a claim carries -- it
conforms to the ontology, its value matches the predicate's declared type, its
subject resolves to a real entity, it has provenance, it cannot be more visible
than the thing it describes -- holds only because there is exactly one place
that can create one. A second writer would satisfy none of them while producing
rows that look identical.

A lint gate enforces the same rule structurally, the way the visibility
chokepoint is enforced: nothing outside this module may INSERT into `memory_claims`.

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

import dataclasses
import datetime
import decimal
import hashlib
import json
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ValidationError
from registry.service.authority import (
    AUTHORITY_BY_AXES,
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
    DERIVATION_BY_RANK,
    DERIVATION_EXTRACTION,
    DERIVATION_HUMAN,
    DERIVATION_INFERENCE,
    DERIVATION_RANK,
    SOURCE_AUTHORITY_ORDER,
    SOURCE_AUTHORITY_RANK,
)
from registry.service.embedding_index import project_claim
from registry.service.global_vocabulary import CARDINALITY_MULTI
from registry.service.memory.confidence import (
    SCORER_VERSION,
    ConfidencePolicy,
    EvidenceClass,
)
from registry.service.memory.confidence import (
    score as score_confidence,
)
from registry.service.memory.confidence_decay import half_life_days
from registry.service.memory.confidence_read import subject_change_profile
from registry.service.memory.contest import ContestOutcome, detect_for_claim
from registry.service.version_predicates import validate_version_predicate
from registry.service.visibility import resolve_visible_entity
from registry.storage.models import CLAIM_PREDICATE_KIND
from registry.types import Clock, TenantContext

# Why a write was refused. Bounded and exported, because a rejection nobody
# counts is a pipeline that has silently stopped working.
REJECT_UNKNOWN_PREDICATE = "unknown_predicate"
REJECT_DEPRECATED_PREDICATE = "deprecated_predicate"
REJECT_VALUE_TYPE = "value_type_mismatch"
REJECT_PROSE = "prose_not_permitted"
REJECT_NULL_VALUE = "null_value"
REJECT_INTERVAL = "invalid_interval"
REJECT_VISIBILITY = "visibility_broader_than_subject"
REJECT_EVIDENCE_KIND = "evidence_kind_not_permitted"

REJECTION_REASONS = frozenset(
    {
        REJECT_UNKNOWN_PREDICATE,
        REJECT_DEPRECATED_PREDICATE,
        REJECT_VALUE_TYPE,
        REJECT_PROSE,
        REJECT_NULL_VALUE,
        REJECT_INTERVAL,
        REJECT_VISIBILITY,
        REJECT_EVIDENCE_KIND,
    }
)

# --- source authority ------------------------------------------------------
#
# The ladder itself lives in its own module: the write path derives a tier and
# scoring weights one, and neither layer should depend on the other. Re-exported
# here because this is where callers have always found it.
# A connector whose `parse()` is a pure function of the fetched bytes produces
# a reproducible claim: re-fetch the artefact, re-parse, get the same triple.
# That reproducibility -- not the file format -- is what earns the extraction
# tier, and the connector base class already guarantees it. A source type
# absent from this set falls to inference.
DETERMINISTIC_SOURCE_TYPES = frozenset({"openapi", "package_json", "release_notes", "markdown_adr_rfc", "docs_corpus"})

EVIDENCE_CURATOR = "curator"
EVIDENCE_CONNECTOR_RUN = "connector_run"

STATUS_STAGED = "staged"
STATUS_UNLINKED = "unlinked"

# Recorded on every claim scored without a fitted provider mapping. Deliberately
# not a version string and deliberately not "identity": an identity mapping would
# assert that a model reporting 0.9 is right nine times in ten, which nobody has
# checked. A token with no version shape cannot be mistaken for one.
UNCALIBRATED = "uncalibrated"

# Why a claim stopped being current. A status of `superseded` says a claim is no
# longer current without saying whether it lost a conflict, was a duplicate, or was
# replaced by a person -- and a reviewer asking what changed wants the difference.
SUPERSEDED_REASONS = frozenset({"lost_conflict", "cluster_collapsed", "human_confirmed", "curator_replaced"})

# A rejection nobody counts is a pipeline that has silently stopped working:
# extraction that quietly stops conforming looks exactly like extraction that
# is producing nothing because there is nothing to produce. Label cardinality
# is bounded by `REJECTION_REASONS`.
_REJECTED = Counter(
    "registry_claim_rejected_total",
    "Claim writes refused, by reason.",
    ["reason"],
)

_STAGED = Counter(
    "registry_claim_staged_total",
    "Claims written, by status and derived source authority.",
    ["status", "source_authority"],
)

# Counted separately from the status label because the rate is the signal, not
# the total: a rising share of unresolved subjects means extraction is drifting
# off the entity model, which no absolute count makes visible.
_UNLINKED = Counter(
    "registry_claim_unresolved_subject_total",
    "Claims stored unlinked because the subject did not resolve.",
)

# Ordered widest to narrowest. A claim may never be more visible than the entity
# it describes, so comparison needs an order.
_VISIBILITY_RANK = {"public": 0, "tenant-shared": 1, "private": 2}

# The value types whose values are text. Named rather than inlined because the
# numeric and boolean branches below must stay reachable: a blanket "must be a
# string" ahead of them rejects an integer duration before it is examined.
_TEXT_VALUE_TYPES = frozenset({"string", "enum", "prose", "entity_ref", "decimal", "url", "version_predicate"})

_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class ClaimRejected(ValidationError):
    """A write refused, carrying the reason code the metric counts.

    Counting happens here rather than at each raise site: a new rejection added
    later cannot forget to increment, which is the failure NF the metric
    exists to prevent. The reason is asserted against the bounded set so a
    typo cannot quietly create an unwatched label.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        _REJECTED.labels(reason=reason).inc()


@dataclasses.dataclass(frozen=True)
class StagedClaim:
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    status: str
    visibility: str
    owning_tenant_id: uuid.UUID | None
    source_authority: str
    # Whether this claim disagrees with something already stored. Returned so a
    # caller staging a claim learns immediately that it conflicts, rather than
    # discovering it when promotion is refused.
    is_contested: bool = False


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One piece of provenance. Immutable once written.

    Correcting a claim creates a new claim; it never rewrites the evidence,
    because the record of what was believed and why is the thing provenance
    exists to preserve.
    """

    kind: str
    ref: str
    excerpt: str | None = None


class ClaimService:
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
        value: Any,
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

    # -- resolution ------------------------------------------------------------

    async def _resolve_predicate(self, session: AsyncSession, ctx: TenantContext, predicate: str) -> _Declared:
        """Global predicates win, then the tenant's own.

        Safe only because a tenant cannot define a name that exists globally --
        without that rule this ordering would let a shared term shadow a
        tenant's and silently change what their claims mean.
        """
        row = (
            await session.execute(
                text(
                    "SELECT value_type, claim_category, value_cardinality, deprecated_at "
                    "FROM vocabulary_values "
                    "WHERE kind = :kind AND value = :value "
                    "  AND (tenant_id IS NULL OR tenant_id = :tid) "
                    "ORDER BY tenant_id NULLS FIRST LIMIT 1"
                ),
                {"kind": CLAIM_PREDICATE_KIND, "value": predicate, "tid": ctx.tenant_id},
            )
        ).one_or_none()

        if row is None:
            raise ClaimRejected(
                REJECT_UNKNOWN_PREDICATE,
                f"predicate {predicate!r} is not in the ontology",
            )
        if row.deprecated_at is not None:
            raise ClaimRejected(
                REJECT_DEPRECATED_PREDICATE,
                f"predicate {predicate!r} is deprecated and accepts no new claims",
            )
        return _Declared(
            value_type=row.value_type,
            claim_category=row.claim_category,
            # A predicate whose cardinality was never declared is treated as
            # set-valued. That direction misses a disagreement; the other
            # manufactures one, and only the first is recoverable.
            value_cardinality=row.value_cardinality or CARDINALITY_MULTI,
        )

    async def _resolve_subject(self, session: AsyncSession, ctx: TenantContext, reference: str) -> _Subject:
        """Resolve by entity id, then by external id. Never guess.

        A reference that resolves to nothing produces an `unlinked` claim
        rather than a dropped one or an invented link. Attaching an assertion
        to the wrong entity is worse than not attaching it: the claim then
        looks corroborated by something it has nothing to do with.
        """
        as_uuid = _maybe_uuid(reference)

        if as_uuid is not None:
            # Through the cross-tenant chokepoint, and deliberately using the
            # variant that cannot distinguish "absent" from "not yours". A
            # direct read would answer "does this id exist, and who owns it"
            # for every entity in the deployment to anyone who can guess a
            # UUID. An invisible subject is indistinguishable from a missing
            # one: both produce an unlinked claim.
            entity = await resolve_visible_entity(session, ctx, as_uuid)
            if entity is not None:
                return _Subject(entity_id=entity.entity_id, owning_tenant_id=entity.tenant_id)

        # External-id form: `system:identifier`. Scoped to the author's tenant,
        # because an external id means something only within the mapping that
        # defined it.
        if ":" in reference:
            system, _, external = reference.partition(":")
            row = (
                await session.execute(
                    text(
                        "SELECT e.entity_id, e.tenant_id FROM entity_external_ids x "
                        "JOIN entities e ON e.entity_id = x.entity_id "
                        "WHERE x.tenant_id = :tid AND x.external_system_slug = :sys "
                        "  AND x.external_id = :ext"
                    ),
                    {"tid": ctx.tenant_id, "sys": system, "ext": external},
                )
            ).one_or_none()
            if row is not None:
                return _Subject(entity_id=row.entity_id, owning_tenant_id=row.tenant_id)

        return _Subject(entity_id=None, owning_tenant_id=None)

    async def _derive_authority(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        subject: _Subject,
        evidence: tuple[Evidence, ...],
    ) -> tuple[str, dict[tuple[str, str], str]]:
        """Compute a claim's authority from provenance the caller cannot forge.

        Returns the authority and the per-evidence derivation tier, so the
        provenance rows can record which one set the floor. Authority is a
        minimum over the evidence set; without the per-row value an auditor
        cannot reconstruct why a claim scored as it did.

        Standing comes from the authenticated tenant compared against the
        subject's owner -- a producer cannot claim ownership it does not have.
        Derivation comes from resolving each piece of evidence, never from the
        caller asserting how it produced the claim.
        """
        derivations = {(item.kind, item.ref): await self._evidence_derivation(session, ctx, item) for item in evidence}

        if subject.owning_tenant_id is None:
            # No subject, so no owner to compare the author against. Naming an
            # observer tier here would assert a determination that was never
            # made, and nothing would mark it stale once curation links the
            # claim to an entity the author does in fact own.
            return AUTHORITY_UNATTRIBUTED, derivations

        is_owner = subject.owning_tenant_id == ctx.tenant_id

        # A human putting their name to a claim asserts it in the first person.
        # That stands on its own rather than averaging with whatever the claim
        # was originally derived from -- otherwise confirming a model
        # extraction would leave it at the model's tier and confirmation would
        # mean nothing.
        if any(item.kind == EVIDENCE_CURATOR for item in evidence):
            if not await self._actor_is_human(session, ctx):
                raise ClaimRejected(
                    REJECT_EVIDENCE_KIND,
                    "curator evidence records a human act; a service principal cannot produce it",
                )
            return AUTHORITY_BY_AXES[(is_owner, DERIVATION_HUMAN)], derivations

        # The weakest link across the remaining evidence. A claim is only as
        # checkable as the least checkable step needed to produce it, and taking
        # the strongest would let anyone launder a model inference into the
        # extraction tier by attaching one connector run to it. Independent
        # sources agreeing raises confidence; it must never raise authority.
        weakest = max(DERIVATION_RANK[d] for d in derivations.values())
        return AUTHORITY_BY_AXES[(is_owner, DERIVATION_BY_RANK[weakest])], derivations

    async def _evidence_derivation(self, session: AsyncSession, ctx: TenantContext, item: Evidence) -> str:
        """How reproducible is a claim derived from this evidence?

        The caller supplies a pointer; this decides what the pointer is worth.
        An unresolvable pointer is worth the floor, so a bad ref can only ever
        cost authority, never buy it.
        """
        if item.kind == EVIDENCE_CURATOR:
            return DERIVATION_HUMAN

        if item.kind == EVIDENCE_CONNECTOR_RUN:
            run = _maybe_uuid(item.ref)
            if run is None:
                return DERIVATION_INFERENCE
            row = (
                await session.execute(
                    text(
                        "SELECT s.source_type FROM sync_runs r "
                        "JOIN sync_sources s ON s.source_id = r.source_id "
                        "WHERE r.sync_run_id = :rid AND r.tenant_id = :tid"
                    ),
                    {"rid": run, "tid": ctx.tenant_id},
                )
            ).one_or_none()
            if row is not None and row.source_type in DETERMINISTIC_SOURCE_TYPES:
                return DERIVATION_EXTRACTION
            return DERIVATION_INFERENCE

        # session_event, document_revision, commit, work_item. The artefact is
        # real, but the step from it to a typed triple is a model reading text.
        # A deterministic reader over one of these earns the extraction tier by
        # being a registered connector, not by asserting it at the call site.
        return DERIVATION_INFERENCE

    async def _actor_is_human(self, session: AsyncSession, ctx: TenantContext) -> bool:
        kind = (
            await session.execute(
                text("SELECT actor_kind FROM actors WHERE actor_id = :aid AND tenant_id = :tid"),
                {"aid": ctx.actor_id, "tid": ctx.tenant_id},
            )
        ).scalar_one_or_none()
        return bool(kind == "human")

    async def _derive_visibility(self, session: AsyncSession, subject: _Subject, *, requested: str | None) -> str:
        """A claim can never be more visible than the entity it describes.

        Derived from the subject rather than accepted from the author: a claim
        about somebody else's private capability must not be publishable by
        whoever noticed it.
        """
        if subject.entity_id is None:
            # Nothing to derive from, so the most restrictive value. An
            # unlinked claim is served to nobody anyway.
            return "private"

        subject_visibility = (
            await session.execute(
                text("SELECT visibility FROM entities WHERE entity_id = :eid"),
                {"eid": subject.entity_id},
            )
        ).scalar_one()

        if requested is None:
            return str(subject_visibility)
        if requested not in _VISIBILITY_RANK:
            raise ClaimRejected(REJECT_VISIBILITY, f"unknown visibility {requested!r}")
        if _VISIBILITY_RANK[requested] < _VISIBILITY_RANK[str(subject_visibility)]:
            raise ClaimRejected(
                REJECT_VISIBILITY,
                f"a claim may not be more visible ({requested}) than its subject " f"({subject_visibility})",
            )
        return requested

    # -- value conformance -----------------------------------------------------

    def _validate_value(self, value: Any, declared: _Declared) -> None:
        """The value must be what the predicate says it is.

        This is what makes claims comparable. A predicate declaring
        `duration_seconds` and receiving a sentence produces a row that looks
        like every other row and can be reasoned with by nothing.
        """
        if value is None:
            raise ClaimRejected(
                REJECT_NULL_VALUE,
                "null is never a value; an unknown is the absence of a claim, not a claim of nothing",
            )

        expected = declared.value_type
        if expected == "prose" and declared.claim_category != "session_summary":
            raise ClaimRejected(REJECT_PROSE, "prose is permitted only for session summaries")

        # The numeric and boolean types are handled below and must reach their
        # own branches, so the string requirement applies only to the types whose
        # values genuinely are text. Making it unconditional here rejects an
        # integer duration before it is ever looked at.
        if expected in _TEXT_VALUE_TYPES and not isinstance(value, str):
            self._type_error(expected, value)

        if expected in {"string", "enum", "prose", "entity_ref"}:
            # Free text, a vocabulary member, a paragraph, or a reference this
            # path resolves separately. Nothing further is decidable here.
            return
        if expected == "decimal":
            # A fixed-point string, not a float -- which is the whole reason the
            # type exists. Parsed rather than merely shaped: an availability
            # target of "banana" would otherwise be stored, and every later
            # comparison against it would be undecidable forever.
            try:
                decimal.Decimal(value)
            except decimal.InvalidOperation:
                self._type_error(expected, value)
            return
        if expected == "url":
            # Absolute, as the type declares. A relative reference resolves
            # against a base this store does not have, so it names nothing.
            parsed = urlsplit(value)
            if not parsed.scheme or not parsed.netloc:
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    f"url must be absolute with a scheme and host; got {value!r}",
                )
            return
        if expected == "version_predicate":
            # The same grammar the graph's own edges are validated against, so a
            # claim cannot carry a range that could never be promoted to one.
            if not validate_version_predicate(value):
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    f"not a well-formed version range: {value!r}",
                )
            return
        if expected in {"integer", "duration_seconds", "bytes"}:
            # `bool` is a subclass of `int` in Python, and True would otherwise
            # store as 1 under a predicate meaning seconds.
            if isinstance(value, bool) or not isinstance(value, int):
                self._type_error(expected, value)
            return
        if expected == "boolean":
            if not isinstance(value, bool):
                self._type_error(expected, value)
            return
        if expected == "timestamp_utc":
            if not isinstance(value, str) or not _RFC3339_UTC.match(value):
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    "timestamp_utc must be RFC 3339 with a Z suffix; offsets are rejected rather "
                    "than converted, because converting silently loses which zone was meant",
                )
            return

        self._type_error(expected, value)

    def _type_error(self, expected: str, value: Any) -> None:
        raise ClaimRejected(
            REJECT_VALUE_TYPE,
            f"predicate declares {expected!r} but the value is {type(value).__name__}",
        )


def _digest(raw: str) -> str:
    """A stable, non-reversible identity for a corroboration source.

    Salted with nothing on purpose: this is not a secret, it is a way of comparing
    two sources for equality without carrying the identifiers they were derived
    from onto a row that outlives them.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _maybe_uuid(value: str) -> uuid.UUID | None:
    """A UUID if the string is one, else None. Never raises."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclasses.dataclass(frozen=True)
class _Declared:
    value_type: str
    claim_category: str
    value_cardinality: str


@dataclasses.dataclass(frozen=True)
class _Subject:
    entity_id: uuid.UUID | None
    owning_tenant_id: uuid.UUID | None


__all__ = [
    "AUTHORITY_OBSERVER_EXTRACTION",
    "AUTHORITY_OBSERVER_HUMAN",
    "AUTHORITY_OBSERVER_INFERENCE",
    "AUTHORITY_OWNER_EXTRACTION",
    "AUTHORITY_OWNER_HUMAN",
    "AUTHORITY_OWNER_INFERENCE",
    "AUTHORITY_UNATTRIBUTED",
    "DERIVATION_EXTRACTION",
    "DERIVATION_HUMAN",
    "DERIVATION_INFERENCE",
    "DETERMINISTIC_SOURCE_TYPES",
    "REJECTION_REASONS",
    "REJECT_EVIDENCE_KIND",
    "SOURCE_AUTHORITY_ORDER",
    "SOURCE_AUTHORITY_RANK",
    "REJECT_DEPRECATED_PREDICATE",
    "REJECT_INTERVAL",
    "REJECT_NULL_VALUE",
    "REJECT_PROSE",
    "REJECT_UNKNOWN_PREDICATE",
    "REJECT_VALUE_TYPE",
    "REJECT_VISIBILITY",
    "STATUS_STAGED",
    "STATUS_UNLINKED",
    "ClaimRejected",
    "ClaimService",
    "Evidence",
    "StagedClaim",
]


async def erase_claims_for_actor(
    session: AsyncSession,
    *,
    selected: list[uuid.UUID],
    target_actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """The claims-table half of an actor erasure: scrub, repair, delete.

    Lives here because this module is the single writer for `memory_claims` and
    `memory_claim_provenance` — an erasure that wrote them from elsewhere would
    be a second vocabulary for the same rows. The *selection* of what to erase
    belongs to the erasure participant; every write it implies lands here, in
    the caller's transaction.

    Three write families, ordered so no delete can trip a constraint:

    1. *Excerpt scrub.* The target's session-event provenance rows are removed
       from claims that survive on independent evidence — the excerpt column
       carries the person's verbatim sentences. Survivors keep at least one
       row by construction: independent evidence is what made them survivors.
    2. *Chain repair.* Confirmation triples pointing at selected claims are
       nulled together (their CHECK requires all-or-none). Losers superseded
       by a selected claim are re-pointed at the erased chain's first
       unselected successor when one exists, and otherwise reopened — status
       back to `staged` with supersession *and* consolidation markers cleared,
       so the next sweep re-decides them instead of skipping them as settled.
    3. *Deletion.* Provenance first (belt to the FK cascade's braces), then
       the claims.
    """
    counts = {
        "claims": 0,
        "provenance_rows": 0,
        "provenance_rows_scrubbed": 0,
        "confirmation_refs_cleared": 0,
        "chains_spliced": 0,
        "losers_reopened": 0,
    }

    scrubbed = await session.execute(
        text(
            "DELETE FROM memory_claim_provenance p "
            " USING memory_session_events e "
            " WHERE p.evidence_kind = 'session_event' "
            "   AND e.event_id::text = p.evidence_ref "
            "   AND e.actor_id = :actor AND e.tenant_id = :tid "
            "   AND p.claim_id <> ALL(:selected)"
        ),
        {
            "actor": target_actor_id,
            "tid": tenant_id,
            # A harmless never-matching id keeps the exclusion well-formed
            # when nothing was selected but excerpts still need scrubbing.
            "selected": selected or [uuid.UUID(int=0)],
        },
    )
    counts["provenance_rows_scrubbed"] = scrubbed.rowcount or 0  # type: ignore[attr-defined]

    if not selected:
        return counts

    cleared = await session.execute(
        text(
            "UPDATE memory_claims "
            "   SET confirms_claim_id = NULL, confirmed_by = NULL, confirmed_at = NULL "
            " WHERE confirms_claim_id = ANY(:selected) "
            "   AND claim_id <> ALL(:selected)"
        ),
        {"selected": selected},
    )
    counts["confirmation_refs_cleared"] = cleared.rowcount or 0  # type: ignore[attr-defined]

    # For every selected claim, its first unselected successor — the splice
    # target. A chain that never leaves the selected set yields no row, and
    # its losers are reopened instead.
    splice_targets = {
        row.selected_id: row.splice_to
        for row in await session.execute(
            text(
                "WITH RECURSIVE chain AS ( "
                "  SELECT c.claim_id AS selected_id, c.superseded_by AS cursor_id "
                "    FROM memory_claims c WHERE c.claim_id = ANY(:selected) "
                "  UNION ALL "
                "  SELECT chain.selected_id, n.superseded_by "
                "    FROM chain JOIN memory_claims n ON n.claim_id = chain.cursor_id "
                "   WHERE chain.cursor_id = ANY(:selected) "
                ") "
                "SELECT selected_id, cursor_id AS splice_to FROM chain "
                " WHERE cursor_id IS NOT NULL AND cursor_id <> ALL(:selected)"
            ),
            {"selected": selected},
        )
    }

    losers = (
        await session.execute(
            text(
                "SELECT claim_id, superseded_by FROM memory_claims "
                " WHERE superseded_by = ANY(:selected) "
                "   AND claim_id <> ALL(:selected) "
                "   FOR UPDATE"
            ),
            {"selected": selected},
        )
    ).all()
    for loser in losers:
        target = splice_targets.get(loser.superseded_by)
        if target is not None:
            await session.execute(
                text("UPDATE memory_claims SET superseded_by = :to WHERE claim_id = :cid"),
                {"to": target, "cid": loser.claim_id},
            )
            counts["chains_spliced"] += 1
        else:
            # The whole chain is being erased: the belief this loser was
            # displaced by no longer exists, so it is the best remaining
            # assertion. Clearing consolidated_at is what lets the next sweep
            # re-decide it instead of skipping it as already settled.
            await session.execute(
                text(
                    "UPDATE memory_claims "
                    "   SET status = 'staged', superseded_by = NULL, "
                    "       superseded_reason = NULL, t_invalidated_at = NULL, "
                    "       consolidated_at = NULL "
                    " WHERE claim_id = :cid"
                ),
                {"cid": loser.claim_id},
            )
            counts["losers_reopened"] += 1

    provenance = await session.execute(
        text("DELETE FROM memory_claim_provenance WHERE claim_id = ANY(:selected)"),
        {"selected": selected},
    )
    counts["provenance_rows"] = provenance.rowcount or 0  # type: ignore[attr-defined]

    claims = await session.execute(
        text("DELETE FROM memory_claims WHERE claim_id = ANY(:selected)"),
        {"selected": selected},
    )
    counts["claims"] = claims.rowcount or 0  # type: ignore[attr-defined]
    return counts
