"""Staging becomes truth here, and only through a person who owns the subject.

Everything before this phase happened inside a store nothing else could see. A wrong
claim was a wrong row. From here a wrong claim is a wrong entry in the canonical graph,
which other systems read and act on. Three properties make that acceptable, and each is
enforced rather than assumed:

**Nothing promotes automatically unless a tenant said so, per predicate.** The
allowlist is empty on a fresh deployment. There is no wildcard entry and no global
switch, so the safe posture does not depend on an operator knowing to turn something
off.

**Nothing consequential promotes automatically at all.** A high-impact claim needs a
person regardless of the allowlist, and confidence is not an input to that
classification. Being certain a capability is about to be withdrawn is a reason to make
sure its owner sees it.

**Every promotion is reversible, exactly.** The journal records the canonical row each
promotion created and the row it closed, by id. Reversal restores the predecessor and
its interval, so an `as_of` query spanning the promotion sees what it saw before. This
is what makes machine-originated writes to a shared graph defensible at all: the cost
of being wrong is one audited operation rather than an archaeology project.

**A claim about another tenant's capability never writes to their graph.** It becomes a
proposal addressed to them. This is the whole mechanism behind cross-team claims
routing to the owner, and it holds at every authority tier -- a human at the wrong
tenant is still not the owner.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.service import promotion_eligibility as elig
from registry.service import promotion_targets
from registry.service.authority import SOURCE_AUTHORITY_RANK
from registry.service.claims import ClaimService
from registry.service.promotion_targets import TARGET_ATTRIBUTE

STATE_OPEN: Final[str] = "open"
STATE_ACCEPTED: Final[str] = "accepted"
STATE_AMENDED: Final[str] = "amended"
STATE_REJECTED: Final[str] = "rejected"

# The roles that may act on a proposal, in the owner tenant only. This follows the
# established precedent for annotation triage rather than inventing a parallel notion
# of who speaks for a capability.
REVIEW_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})

REJECTION_REASONS: Final[frozenset[str]] = frozenset(
    {"incorrect", "already_known", "not_actionable", "wrong_subject", "superseded_by_other"}
)


class _Unset:
    """Distinguishes "no amendment" from "amended to null".

    A sentinel rather than None, because None is a value a reviewer might legitimately
    want to promote, and the two must not collapse.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET: Final[Any] = _Unset()


class PromotionError(RuntimeError):
    """A promotion or review was refused. The message is shown to the caller, so it
    says what was refused and why, never what the internal state happens to be."""


@dataclasses.dataclass(frozen=True)
class Proposal:
    proposal_id: uuid.UUID
    claim_id: uuid.UUID
    owner_tenant_id: uuid.UUID
    author_tenant_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    target_kind: str
    target_key: str
    current_value: Any
    proposed_value: Any
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    high_impact_reasons: tuple[str, ...]

    @property
    def high_impact(self) -> bool:
        return bool(self.high_impact_reasons)


def value_digest(value: Any) -> str:
    """A canonical digest of an asserted value.

    Used to key a rejection by *what was asserted* rather than by which row asserted
    it. Claims are immutable, so the same assertion arriving again is a new row --
    keyed by row id a rejection could be defeated by simply repeating.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PromotionService:
    """Proposals, review, the canonical write, and reversal."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        claims: ClaimService,
        clock: Any,
    ) -> None:
        self._factory = factory
        self._claims = claims
        self._clock = clock

    # --- proposing ------------------------------------------------------------

    async def propose(self, claim_id: uuid.UUID) -> Proposal | None:
        """Build a proposal for a claim, or return None if it is not eligible.

        None is an ordinary outcome. Most claims are not promotable at any given
        moment, and treating that as an error would make the sweep that walks them
        log a failure per claim.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            claim = await self._load_claim(session, claim_id)
            if claim is None:
                return None

            policy = await elig.load_policy(session, claim["owning_tenant_id"])
            eligibility = elig.assess_eligibility(claim, policy)
            if not eligibility.eligible:
                return None

            if await self._is_rejected_already(session, claim):
                # The same assertion has been refused before. Re-queueing it would
                # let repetition wear down a decision that was already made.
                return None

            target = promotion_targets.target_for(claim["predicate"])
            if target is None:
                return None

            radius = await elig.blast_radius_for(session, claim["subject_entity_id"])
            impact = await elig.assess_impact(session, claim, policy, blast_radius=radius)
            current = await self._current_canonical_value(session, claim, target)

            proposal_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO lmm_promotion_proposal "
                    "  (proposal_id, claim_id, owner_tenant_id, author_tenant_id, "
                    "   subject_entity_id, predicate, target_kind, target_key, "
                    "   mapping_version, current_value, proposed_value, valid_from, "
                    "   valid_to, high_impact_reasons, state, created_at) "
                    "VALUES (:pid, :cid, :owner, :author, :sid, :pred, :kind, :key, "
                    "        :ver, CAST(:cur AS JSONB), CAST(:prop AS JSONB), :vf, "
                    "        :vt, CAST(:reasons AS JSONB), 'open', :now)"
                ),
                {
                    "pid": proposal_id,
                    "cid": claim_id,
                    "owner": claim["owning_tenant_id"],
                    "author": claim["author_tenant_id"],
                    "sid": claim["subject_entity_id"],
                    "pred": claim["predicate"],
                    "kind": target.kind,
                    "key": target.key,
                    "ver": promotion_targets.MAPPING_VERSION,
                    "cur": json.dumps(current) if current is not None else None,
                    "prop": json.dumps(claim["value"]),
                    "vf": claim["asserted_valid_from"],
                    "vt": claim["asserted_valid_to"],
                    "reasons": json.dumps(list(impact.reasons)),
                    "now": now,
                },
            )
            await self._claims.set_promotion_state(session, claim_id=claim_id, state="proposed")

            action = (
                actions.CLAIM_PROPOSAL_ROUTED
                if claim["author_tenant_id"] != claim["owning_tenant_id"]
                else actions.CLAIM_PROMOTION_PROPOSED
            )
            await self._audit(
                session,
                action=action,
                tenant_id=claim["owning_tenant_id"],
                actor_id=claim["author_actor_id"],
                target_id=claim_id,
                payload={
                    "proposal_id": str(proposal_id),
                    "high_impact": impact.high_impact,
                    "high_impact_reasons": list(impact.reasons),
                    "surface_evaluated": impact.surface_evaluated,
                    "blast_radius": radius,
                },
                now=now,
            )

            return Proposal(
                proposal_id=proposal_id,
                claim_id=claim_id,
                owner_tenant_id=claim["owning_tenant_id"],
                author_tenant_id=claim["author_tenant_id"],
                subject_entity_id=claim["subject_entity_id"],
                predicate=claim["predicate"],
                target_kind=target.kind,
                target_key=target.key,
                current_value=current,
                proposed_value=claim["value"],
                valid_from=claim["asserted_valid_from"],
                valid_to=claim["asserted_valid_to"],
                high_impact_reasons=impact.reasons,
            )

    # --- reviewing ------------------------------------------------------------

    async def accept(
        self,
        proposal_id: uuid.UUID,
        *,
        actor_tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: frozenset[str],
        amended_value: Any = _UNSET,
    ) -> uuid.UUID:
        """Accept a proposal, optionally amending the value, and write the graph.

        Returns the promotion id, which is the handle reversal takes.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            proposal = await self._load_open_proposal(session, proposal_id)
            self._assert_may_review(proposal, actor_tenant_id, roles)

            amended = amended_value is not _UNSET
            value = amended_value if amended else proposal["proposed_value"]

            created_id, superseded_id, superseded_valid_to = await self._write_canonical(
                session, proposal, value, actor_id=actor_id, now=now
            )

            promotion_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO lmm_promotion_journal "
                    "  (promotion_id, proposal_id, claim_id, tenant_id, target_kind, "
                    "   created_row_id, superseded_row_id, superseded_valid_to, "
                    "   promoted_at, promoted_by) "
                    "VALUES (:pid, :prop, :cid, :tid, :kind, :created, :superseded, "
                    "        :sv, :now, :actor)"
                ),
                {
                    "pid": promotion_id,
                    "prop": proposal_id,
                    "cid": proposal["claim_id"],
                    "tid": proposal["owner_tenant_id"],
                    "kind": proposal["target_kind"],
                    "created": created_id,
                    "superseded": superseded_id,
                    "sv": superseded_valid_to,
                    "now": now,
                    "actor": actor_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE lmm_promotion_proposal "
                    "   SET state = :state, decided_by = :actor, decided_at = :now, "
                    "       amended_value = CAST(:amended AS JSONB) "
                    " WHERE proposal_id = :pid"
                ),
                {
                    "state": STATE_AMENDED if amended else STATE_ACCEPTED,
                    "actor": actor_id,
                    "now": now,
                    "amended": json.dumps(value) if amended else None,
                    "pid": proposal_id,
                },
            )
            await self._claims.set_promotion_state(session, claim_id=proposal["claim_id"], state="promoted")
            await self._audit(
                session,
                action=actions.CLAIM_PROMOTED,
                tenant_id=proposal["owner_tenant_id"],
                actor_id=actor_id,
                target_id=proposal["claim_id"],
                payload={
                    "promotion_id": str(promotion_id),
                    "proposal_id": str(proposal_id),
                    "amended": amended,
                    "target_kind": proposal["target_kind"],
                    "created_row_id": str(created_id),
                },
                now=now,
            )
            return promotion_id

    async def reject(
        self,
        proposal_id: uuid.UUID,
        *,
        actor_tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: frozenset[str],
        reason: str,
    ) -> None:
        """Refuse a proposal, and record what was refused so repeating it does not
        silently re-queue.

        The claim is not deleted and does not leave staging. It stays readable, it
        still serves, and the rejection itself becomes evidence about it.
        """
        if reason not in REJECTION_REASONS:
            raise PromotionError(f"rejection reason must be one of {sorted(REJECTION_REASONS)}")
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            proposal = await self._load_open_proposal(session, proposal_id)
            self._assert_may_review(proposal, actor_tenant_id, roles)
            claim_authority = (
                await session.execute(
                    text("SELECT source_authority FROM lmm_claims WHERE claim_id = :c"),
                    {"c": proposal["claim_id"]},
                )
            ).scalar_one()

            await session.execute(
                text(
                    "UPDATE lmm_promotion_proposal "
                    "   SET state = 'rejected', decided_by = :actor, decided_at = :now, "
                    "       decision_reason = :reason "
                    " WHERE proposal_id = :pid"
                ),
                {"actor": actor_id, "now": now, "reason": reason, "pid": proposal_id},
            )
            await session.execute(
                text(
                    "INSERT INTO lmm_promotion_rejection "
                    "  (rejection_id, tenant_id, subject_entity_id, predicate, "
                    "   value_digest, rejected_authority, reason, proposal_id, "
                    "   rejected_at, rejected_by) "
                    "VALUES (:rid, :tid, :sid, :pred, :digest, :auth, :reason, :pid, "
                    "        :now, :actor) "
                    "ON CONFLICT (tenant_id, subject_entity_id, predicate, value_digest) "
                    "DO NOTHING"
                ),
                {
                    "rid": uuid.uuid4(),
                    "tid": proposal["owner_tenant_id"],
                    "sid": proposal["subject_entity_id"],
                    "pred": proposal["predicate"],
                    "digest": value_digest(proposal["proposed_value"]),
                    "auth": claim_authority,
                    "reason": reason,
                    "pid": proposal_id,
                    "now": now,
                    "actor": actor_id,
                },
            )
            await self._claims.set_promotion_state(session, claim_id=proposal["claim_id"], state="rejected")
            await self._audit(
                session,
                action=actions.CLAIM_PROMOTION_REJECTED,
                tenant_id=proposal["owner_tenant_id"],
                actor_id=actor_id,
                target_id=proposal["claim_id"],
                payload={"proposal_id": str(proposal_id), "reason": reason},
                now=now,
            )

    # --- reversal -------------------------------------------------------------

    async def reverse(
        self,
        promotion_id: uuid.UUID,
        *,
        actor_tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: frozenset[str],
        reason: str,
    ) -> None:
        """Undo a promotion, restoring what the graph said before it.

        Refuses when the row this promotion created is no longer the live one. That
        is not a limitation to work around -- it is the condition under which
        restoring the predecessor is sound. If a later promotion has already changed
        the same target, then "the state before this promotion" and "the state to
        restore now" are different things, and writing the old value back would
        silently destroy the later change. The later promotion must be reversed
        first.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            journal = (
                (
                    await session.execute(
                        text(
                            "SELECT promotion_id, claim_id, tenant_id, target_kind, "
                            "       created_row_id, superseded_row_id, superseded_valid_to, "
                            "       reversed_at "
                            "  FROM lmm_promotion_journal WHERE promotion_id = :pid "
                            "   FOR UPDATE"
                        ),
                        {"pid": promotion_id},
                    )
                )
                .mappings()
                .first()
            )
            if journal is None:
                raise PromotionError("no such promotion")
            if journal["reversed_at"] is not None:
                raise PromotionError("promotion was already reversed")
            if journal["tenant_id"] != actor_tenant_id or not (roles & REVIEW_ROLES):
                raise PromotionError("only the owning tenant may reverse a promotion")

            table = "attributes" if journal["target_kind"] == TARGET_ATTRIBUTE else "edges"
            id_column = "attr_id" if table == "attributes" else "edge_id"

            # Two ways this promotion can have been built on since. The row may have
            # been closed outright, or a later promotion may have superseded it --
            # which narrows its interval without invalidating it, so checking only
            # for invalidation misses exactly the stacked case this guards against.
            still_live = (
                await session.execute(
                    text(
                        f"SELECT 1 FROM {table} "  # noqa: S608 - table from a closed set
                        f" WHERE {id_column} = :rid AND t_invalidated_at IS NULL"
                    ),
                    {"rid": journal["created_row_id"]},
                )
            ).first()
            built_on = (
                await session.execute(
                    text(
                        "SELECT 1 FROM lmm_promotion_journal " " WHERE superseded_row_id = :rid AND reversed_at IS NULL"
                    ),
                    {"rid": journal["created_row_id"]},
                )
            ).first()
            if still_live is None or built_on is not None:
                raise PromotionError(
                    "the canonical row this promotion created is no longer live; " "reverse the later change first"
                )

            # Close what the promotion wrote. Not a delete: an `as_of` query before
            # the reversal must still see that the promotion happened.
            await session.execute(
                text(
                    f"UPDATE {table} SET t_invalidated_at = :now "  # noqa: S608
                    f" WHERE {id_column} = :rid"
                ),
                {"now": now, "rid": journal["created_row_id"]},
            )
            if journal["superseded_row_id"] is not None:
                # Restore the predecessor's interval, not merely its value. A
                # predecessor left closed would make the reversal visible as a gap.
                await session.execute(
                    text(
                        f"UPDATE {table} SET t_valid_to = :vt, t_invalidated_at = NULL "  # noqa: S608
                        f" WHERE {id_column} = :rid"
                    ),
                    {"vt": journal["superseded_valid_to"], "rid": journal["superseded_row_id"]},
                )

            await session.execute(
                text(
                    "UPDATE lmm_promotion_journal "
                    "   SET reversed_at = :now, reversed_by = :actor, reversal_reason = :reason "
                    " WHERE promotion_id = :pid"
                ),
                {"now": now, "actor": actor_id, "reason": reason, "pid": promotion_id},
            )
            # Back to eligible-but-unpromoted, which is where it was before review.
            await self._claims.set_promotion_state(session, claim_id=journal["claim_id"], state="reversed")
            await self._audit(
                session,
                action=actions.CLAIM_PROMOTION_REVERSED,
                tenant_id=journal["tenant_id"],
                actor_id=actor_id,
                target_id=journal["claim_id"],
                payload={"promotion_id": str(promotion_id), "reason": reason},
                now=now,
            )

    # --- internals ------------------------------------------------------------

    def _assert_may_review(self, proposal: dict[str, Any], actor_tenant_id: uuid.UUID, roles: frozenset[str]) -> None:
        """Authority to act follows the subject's owner, never the claim's author.

        Checked as two separate conditions on purpose: the right tenant with the
        wrong role and the right role in the wrong tenant are different refusals,
        and collapsing them into one check makes it possible to satisfy the
        combination by accident.
        """
        if proposal["owner_tenant_id"] != actor_tenant_id:
            raise PromotionError("only the tenant that owns the subject may act on this proposal")
        if not (roles & REVIEW_ROLES):
            raise PromotionError("acting on a proposal requires the producer or admin role")

    async def _load_claim(self, session: AsyncSession, claim_id: uuid.UUID) -> dict[str, Any] | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT claim_id, subject_entity_id, predicate, value_jsonb AS value, "
                        "       owning_tenant_id, author_tenant_id, author_actor_id, status, "
                        "       is_contested, confidence, source_authority, consolidated_at, "
                        "       promotion_state, asserted_valid_from, asserted_valid_to "
                        "  FROM lmm_claims WHERE claim_id = :cid AND t_invalidated_at IS NULL"
                    ),
                    {"cid": claim_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    async def _load_open_proposal(self, session: AsyncSession, proposal_id: uuid.UUID) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT proposal_id, claim_id, owner_tenant_id, author_tenant_id, "
                        "       subject_entity_id, predicate, target_kind, target_key, "
                        "       proposed_value, valid_from, valid_to, state "
                        "  FROM lmm_promotion_proposal WHERE proposal_id = :pid FOR UPDATE"
                    ),
                    {"pid": proposal_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PromotionError("no such proposal")
        if row["state"] != STATE_OPEN:
            raise PromotionError(f"proposal is already {row['state']}")
        return dict(row)

    async def _is_rejected_already(self, session: AsyncSession, claim: dict[str, Any]) -> bool:
        """Has this assertion been refused by someone whose decision still stands?

        Keyed on the assertion rather than on the row or the moment: a restatement is
        always a new row and always carries a later timestamp, so either of those
        keys would let repetition win.

        The escape is standing, not persistence. A claim carrying strictly stronger
        authority than the one that was refused may be proposed again -- that is how
        an owner overturns a rejection of a stranger's claim, or a human overturns
        one of a machine's. Anything at or below the refused tier is the same
        assertion from no better source, which is what the record exists to stop.
        """
        row = (
            (
                await session.execute(
                    text(
                        "SELECT rejected_authority FROM lmm_promotion_rejection "
                        " WHERE tenant_id = :tid AND subject_entity_id = :sid "
                        "   AND predicate = CAST(:pred AS TEXT) "
                        "   AND value_digest = CAST(:digest AS TEXT)"
                    ),
                    {
                        "tid": claim["owning_tenant_id"],
                        "sid": claim["subject_entity_id"],
                        "pred": claim["predicate"],
                        "digest": value_digest(claim["value"]),
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        refused_rank = SOURCE_AUTHORITY_RANK.get(row["rejected_authority"], len(SOURCE_AUTHORITY_RANK))
        arriving_rank = SOURCE_AUTHORITY_RANK.get(str(claim["source_authority"]), len(SOURCE_AUTHORITY_RANK))
        # Rank 0 is strongest, so a lower number is stronger standing.
        return arriving_rank >= refused_rank

    async def _current_canonical_value(
        self, session: AsyncSession, claim: dict[str, Any], target: promotion_targets.PromotionTarget
    ) -> Any:
        """What the graph says now, so a reviewer sees the change and not just the
        proposal."""
        if target.kind == TARGET_ATTRIBUTE:
            row = (
                await session.execute(
                    text(
                        "SELECT value FROM attributes "
                        " WHERE entity_id = :eid AND key = CAST(:key AS TEXT) "
                        "   AND t_invalidated_at IS NULL "
                        " ORDER BY t_valid_from DESC LIMIT 1"
                    ),
                    {"eid": claim["subject_entity_id"], "key": target.key},
                )
            ).first()
            return row[0] if row is not None else None
        row = (
            await session.execute(
                text(
                    "SELECT dst_entity_id FROM edges "
                    " WHERE src_entity_id = :eid AND rel = CAST(:rel AS TEXT) "
                    "   AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1"
                ),
                {"eid": claim["subject_entity_id"], "rel": target.key},
            )
        ).first()
        return str(row[0]) if row is not None else None

    async def _write_canonical(
        self,
        session: AsyncSession,
        proposal: dict[str, Any],
        value: Any,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        """Write the canonical row, closing whatever it replaces.

        The claim's asserted interval becomes the canonical row's validity, so the
        graph records when the fact holds rather than when somebody got around to
        promoting it.
        """
        if proposal["target_kind"] == TARGET_ATTRIBUTE:
            return await self._write_attribute(session, proposal, value, actor_id=actor_id, now=now)
        return await self._write_edge(session, proposal, value, actor_id=actor_id, now=now)

    async def _write_attribute(
        self,
        session: AsyncSession,
        proposal: dict[str, Any],
        value: Any,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        prior = (
            (
                await session.execute(
                    text(
                        "SELECT attr_id, t_valid_to FROM attributes "
                        " WHERE entity_id = :eid AND key = CAST(:key AS TEXT) "
                        "   AND t_invalidated_at IS NULL "
                        " ORDER BY t_valid_from DESC LIMIT 1 FOR UPDATE"
                    ),
                    {"eid": proposal["subject_entity_id"], "key": proposal["target_key"]},
                )
            )
            .mappings()
            .first()
        )

        superseded_id: uuid.UUID | None = None
        superseded_valid_to: datetime.datetime | None = None
        if prior is not None:
            superseded_id = prior["attr_id"]
            superseded_valid_to = prior["t_valid_to"]
            await session.execute(
                text("UPDATE attributes SET t_valid_to = :vf WHERE attr_id = :aid"),
                {"vf": proposal["valid_from"], "aid": superseded_id},
            )

        attr_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO attributes "
                "  (attr_id, tenant_id, entity_id, key, value, t_valid_from, "
                "   t_valid_to, t_ingested_at, created_by) "
                "VALUES (:aid, :tid, :eid, :key, CAST(:val AS JSONB), :vf, :vt, :now, :actor)"
            ),
            {
                "aid": attr_id,
                "tid": proposal["owner_tenant_id"],
                "eid": proposal["subject_entity_id"],
                "key": proposal["target_key"],
                "val": json.dumps(value),
                "vf": proposal["valid_from"],
                "vt": proposal["valid_to"],
                "now": now,
                "actor": actor_id,
            },
        )
        return attr_id, superseded_id, superseded_valid_to

    async def _write_edge(
        self,
        session: AsyncSession,
        proposal: dict[str, Any],
        value: Any,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        try:
            dst = uuid.UUID(str(value))
        except ValueError as exc:
            raise PromotionError("an edge-valued claim must name a resolved entity") from exc

        # Cross-tenant edges are not written here even with the owner's consent: the
        # destination belongs to somebody who has not been asked.
        dst_tenant = (
            await session.execute(text("SELECT tenant_id FROM entities WHERE entity_id = :eid"), {"eid": dst})
        ).scalar_one_or_none()
        if dst_tenant is None:
            raise PromotionError("the edge destination does not exist")
        if dst_tenant != proposal["owner_tenant_id"]:
            raise PromotionError("an edge may not be written across a tenant boundary")

        prior = (
            (
                await session.execute(
                    text(
                        "SELECT edge_id, t_valid_to FROM edges "
                        " WHERE src_entity_id = :eid AND rel = CAST(:rel AS TEXT) "
                        "   AND dst_entity_id = :dst AND t_invalidated_at IS NULL "
                        " ORDER BY t_valid_from DESC LIMIT 1 FOR UPDATE"
                    ),
                    {"eid": proposal["subject_entity_id"], "rel": proposal["target_key"], "dst": dst},
                )
            )
            .mappings()
            .first()
        )

        superseded_id: uuid.UUID | None = None
        superseded_valid_to: datetime.datetime | None = None
        if prior is not None:
            superseded_id = prior["edge_id"]
            superseded_valid_to = prior["t_valid_to"]
            await session.execute(
                text("UPDATE edges SET t_valid_to = :vf WHERE edge_id = :eid"),
                {"vf": proposal["valid_from"], "eid": superseded_id},
            )

        edge_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO edges "
                "  (edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                "   t_valid_from, t_valid_to, t_ingested_at, created_by) "
                "VALUES (:eid, :tid, :src, :rel, :dst, :vf, :vt, :now, :actor)"
            ),
            {
                "eid": edge_id,
                "tid": proposal["owner_tenant_id"],
                "src": proposal["subject_entity_id"],
                "rel": proposal["target_key"],
                "dst": dst,
                "vf": proposal["valid_from"],
                "vt": proposal["valid_to"],
                "now": now,
                "actor": actor_id,
            },
        )
        return edge_id, superseded_id, superseded_valid_to

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'lmm_claim', :target, NULL, "
                "        CAST(:after AS JSONB), :now, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": actor_id,
                "action": action,
                "target": target_id,
                "after": json.dumps(payload, sort_keys=True),
                "now": now,
            },
        )
