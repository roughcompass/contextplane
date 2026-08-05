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

from prometheus_client import Counter
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.security.pii_scanner import PiiScanner, build_builtin_scanner
from registry.service.catalog import attribute_writes
from registry.service.governance.authority import SOURCE_AUTHORITY_RANK
from registry.service.memory import promotion_eligibility as elig
from registry.service.memory import promotion_targets
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.promotion_targets import TARGET_ATTRIBUTE
from registry.types import Clock, JSONValue

# One counter per arrow the review loop can take, so a dashboard can show the
# funnel -- proposed, accepted, rejected, reversed -- without joining the audit
# log. `accepted` alone carries a label: it is the one arrow with two distinct
# origins (a person reviewing, or the sweep auto-accepting under an allowlisted
# guardrail), and collapsing them would make an operator unable to tell "the
# queue is being worked" from "nothing is being reviewed at all".
_PROPOSED = Counter(
    "registry_claim_promotion_proposed_total",
    "Promotion proposals created from an eligible, consolidated claim.",
)

_ACCEPTED = Counter(
    "registry_claim_promotion_accepted_total",
    "Promotion proposals accepted and written to the canonical graph, by "
    "whether the sweep auto-accepted it or a person reviewed it.",
    ["auto_promoted"],
)

_REJECTED = Counter(
    "registry_claim_promotion_rejected_total",
    "Promotion proposals refused by the tenant that owns the subject.",
)

_REVERSED = Counter(
    "registry_claim_promotion_reversed_total",
    "Promotions undone, restoring what the canonical graph said before them.",
)

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


_UNSET: Final[_Unset] = _Unset()


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
    # Optional and defaulted so `propose()`'s in-transaction construction (which
    # already knows the state is 'open') and every existing caller keep working
    # unchanged; the read path (`get_proposal`, `proposals_for`) always fills
    # both from the row it loaded.
    state: str = STATE_OPEN
    created_at: datetime.datetime | None = None

    @property
    def high_impact(self) -> bool:
        return bool(self.high_impact_reasons)


@dataclasses.dataclass(frozen=True)
class JournalEntry:
    """One promotion's ledger row: what it wrote, what it closed, and whether
    it has since been reversed.

    The reversal handle a reviewer needs -- `reverse()` takes a `promotion_id`,
    not a `claim_id`, and a claim promoted, reversed, and promoted again has
    more than one of these, so finding the still-live one is the point of
    reading the list rather than a single row.
    """

    promotion_id: uuid.UUID
    proposal_id: uuid.UUID
    claim_id: uuid.UUID
    tenant_id: uuid.UUID
    target_kind: str
    created_row_id: uuid.UUID
    superseded_row_id: uuid.UUID | None
    superseded_valid_to: datetime.datetime | None
    promoted_at: datetime.datetime
    promoted_by: uuid.UUID | None
    reversed_at: datetime.datetime | None
    reversed_by: uuid.UUID | None
    reversal_reason: str | None

    @property
    def is_reversed(self) -> bool:
        return self.reversed_at is not None


def value_digest(value: JSONValue) -> str:
    """A canonical digest of an asserted value.

    Used to key a rejection by *what was asserted* rather than by which row asserted
    it. Claims are immutable, so the same assertion arriving again is a new row --
    keyed by row id a rejection could be defeated by simply repeating.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Shared by every read path (`get_proposal`, `proposals_for`) so the column list
# and the row-to-dataclass mapping can only drift from each other, not from
# themselves across two call sites.
_PROPOSAL_SELECT = (
    "SELECT proposal_id, claim_id, owner_tenant_id, author_tenant_id, "
    "       subject_entity_id, predicate, target_kind, target_key, "
    "       current_value, proposed_value, valid_from, valid_to, "
    "       high_impact_reasons, state, created_at "
    "  FROM memory_promotion_proposal "
)


def _to_proposal(row: RowMapping) -> Proposal:
    return Proposal(
        proposal_id=row["proposal_id"],
        claim_id=row["claim_id"],
        owner_tenant_id=row["owner_tenant_id"],
        author_tenant_id=row["author_tenant_id"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        target_kind=row["target_kind"],
        target_key=row["target_key"],
        current_value=row["current_value"],
        proposed_value=row["proposed_value"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        high_impact_reasons=tuple(row["high_impact_reasons"] or ()),
        state=row["state"],
        created_at=row["created_at"],
    )


class PromotionService:
    """Proposals, review, the canonical write, and reversal."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        claims: ClaimService,
        clock: Clock,
        pii_scanner: PiiScanner | None = None,
    ) -> None:
        self._factory = factory
        self._claims = claims
        self._clock = clock
        # Optional so a deployment can supply its configured scanner, but never
        # absent: promotion is the moment a value stops being a private observation
        # and becomes something other systems read.
        #
        # What it *does* on a match follows the scanner's policy, exactly as every
        # other write path in the platform does -- advisory reports, block refuses.
        # Honouring that policy is the point; a promotion path that blocked where
        # the rest of the platform advises would be enforcing a rule nobody
        # configured, and one that advised where the platform blocks would be the
        # bypass this is here to prevent.
        self._pii = pii_scanner if pii_scanner is not None else build_builtin_scanner()

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
            # assess_eligibility (above) already appends INELIGIBLE_NO_TARGET and
            # returns early whenever target_for(predicate) is None, so this can only
            # be reached with a real target. Asserted rather than re-checked so a
            # future change to that invariant fails loudly here instead of silently
            # reintroducing a None that this function would otherwise swallow.
            assert target is not None  # noqa: S101 - narrows a real, already-enforced invariant; not runtime validation of untrusted input

            radius = await elig.blast_radius_for(session, claim["subject_entity_id"])
            impact = await elig.assess_impact(session, claim, policy, blast_radius=radius)
            current = await self._current_canonical_value(session, claim, target)

            proposal_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO memory_promotion_proposal "
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
            _PROPOSED.inc()

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
                state=STATE_OPEN,
                created_at=now,
            )

    # --- reviewing ------------------------------------------------------------

    async def accept(
        self,
        proposal_id: uuid.UUID,
        *,
        actor_tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: frozenset[str],
        amended_value: JSONValue | _Unset = _UNSET,
        auto_promoted: bool = False,
    ) -> uuid.UUID:
        """Accept a proposal, optionally amending the value, and write the graph.

        Returns the promotion id, which is the handle reversal takes.

        `auto_promoted` is an explicit signal from the caller, not something
        inferred from `roles` here: the sweep's system-curator identity and a
        human admin can both present `roles={"admin"}` (`_assert_may_review`
        only checks for membership, not identity), so guessing from roles would
        misattribute a human admin's review as automatic. The sweep is the one
        caller that passes `auto_promoted=True`; every other caller's default
        is correct because every other caller is a person.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            proposal = await self._load_open_proposal(session, proposal_id)
            self._assert_may_review(proposal, actor_tenant_id, roles)

            # `isinstance` rather than `is not _UNSET` so the branch below
            # narrows `amended_value` to `JSONValue`, not just to "not the
            # sentinel" -- the two are equivalent here since `_Unset` is
            # private to this module and `_UNSET` is its only instance.
            if isinstance(amended_value, _Unset):
                amended = False
                value: JSONValue = proposal["proposed_value"]
            else:
                amended = True
                value = amended_value

            created_id, superseded_id, superseded_valid_to = await self._write_canonical(
                session, proposal, value, actor_id=actor_id, now=now
            )

            promotion_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO memory_promotion_journal "
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
                    "UPDATE memory_promotion_proposal "
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
            _ACCEPTED.labels(auto_promoted=str(auto_promoted).lower()).inc()
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
            raise ValidationError(f"rejection reason must be one of {sorted(REJECTION_REASONS)}")
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            proposal = await self._load_open_proposal(session, proposal_id)
            self._assert_may_review(proposal, actor_tenant_id, roles)
            claim_authority = (
                await session.execute(
                    text("SELECT source_authority FROM memory_claims WHERE claim_id = :c"),
                    {"c": proposal["claim_id"]},
                )
            ).scalar_one()

            await session.execute(
                text(
                    "UPDATE memory_promotion_proposal "
                    "   SET state = 'rejected', decided_by = :actor, decided_at = :now, "
                    "       decision_reason = :reason "
                    " WHERE proposal_id = :pid"
                ),
                {"actor": actor_id, "now": now, "reason": reason, "pid": proposal_id},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_promotion_rejection "
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
            _REJECTED.inc()

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
                            "  FROM memory_promotion_journal WHERE promotion_id = :pid "
                            "   FOR UPDATE"
                        ),
                        {"pid": promotion_id},
                    )
                )
                .mappings()
                .first()
            )
            if journal is None:
                raise NotFoundError("no such promotion")
            if journal["reversed_at"] is not None:
                raise ConflictError("promotion was already reversed")
            if journal["tenant_id"] != actor_tenant_id or not (roles & REVIEW_ROLES):
                raise PermissionError("only the owning tenant may reverse a promotion")

            # Closed two-value set, never caller input: every f-string below that
            # interpolates `table`/`id_column` is safe for the same reason -- there
            # is no third option and nothing here comes from a request.
            table = "attributes" if journal["target_kind"] == TARGET_ATTRIBUTE else "edges"
            id_column = "attr_id" if table == "attributes" else "edge_id"

            # Two ways this promotion can have been built on since. The row may have
            # been closed outright, or a later promotion may have superseded it --
            # which narrows its interval without invalidating it, so checking only
            # for invalidation misses exactly the stacked case this guards against.
            still_live = (
                await session.execute(
                    text(f"SELECT 1 FROM {table} " f" WHERE {id_column} = :rid AND t_invalidated_at IS NULL"),  # noqa: S608 - table/id_column are the closed set named above, not caller input
                    {"rid": journal["created_row_id"]},
                )
            ).first()
            built_on = (
                await session.execute(
                    text(
                        "SELECT 1 FROM memory_promotion_journal "
                        " WHERE superseded_row_id = :rid AND reversed_at IS NULL"
                    ),
                    {"rid": journal["created_row_id"]},
                )
            ).first()
            if still_live is None or built_on is not None:
                raise ConflictError(
                    "the canonical row this promotion created is no longer live; " "reverse the later change first"
                )

            # Close what the promotion wrote. Not a delete: an `as_of` query before
            # the reversal must still see that the promotion happened.
            await session.execute(
                text(f"UPDATE {table} SET t_invalidated_at = :now " f" WHERE {id_column} = :rid"),  # noqa: S608 - table/id_column are the closed set named above, not caller input
                {"now": now, "rid": journal["created_row_id"]},
            )
            if journal["superseded_row_id"] is not None:
                # Restore the predecessor's interval, not merely its value. A
                # predecessor left closed would make the reversal visible as a gap.
                await session.execute(
                    text(f"UPDATE {table} SET t_valid_to = :vt, t_invalidated_at = NULL " f" WHERE {id_column} = :rid"),  # noqa: S608 - table/id_column are the closed set named above, not caller input
                    {"vt": journal["superseded_valid_to"], "rid": journal["superseded_row_id"]},
                )

            await session.execute(
                text(
                    "UPDATE memory_promotion_journal "
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
            _REVERSED.inc()

    # --- reading ----------------------------------------------------------------

    async def get_proposal(self, proposal_id: uuid.UUID) -> Proposal | None:
        """One proposal, whatever its current state, or None if the id names
        nothing.

        No tenancy filter in the query -- the same shape `_load_open_proposal`
        already has. Whether the caller is entitled to see what it names is a
        question the caller answers by comparing `owner_tenant_id` against its
        own context, exactly as `accept`/`reject`/`reverse` already do after
        their own loads; duplicating that check here would just be a second
        place it could drift from the first.
        """
        async with self._factory() as session:
            row = (
                (await session.execute(text(_PROPOSAL_SELECT + " WHERE proposal_id = :pid"), {"pid": proposal_id}))
                .mappings()
                .first()
            )
        return _to_proposal(row) if row is not None else None

    async def proposals_for(
        self,
        tenant_id: uuid.UUID,
        *,
        state: str = STATE_OPEN,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 50,
    ) -> tuple[Proposal, ...]:
        """Proposals owned by this tenant, oldest first.

        Oldest first because this is a review queue, not a feed -- the same
        drain-from-the-front convention the curation queue and the capability
        request queue (`for_owner`) both use. Keyset-paginated on
        `(created_at, proposal_id)` rather than limit/offset, which degrades as
        the queue grows and re-shows or skips rows when proposals are decided
        between pages a caller fetches.

        Fetches `page_size + 1` rows so the caller can tell whether another
        page exists without a separate count query -- the same convention
        `queries.query_audit_log` uses; cursor decode/encode and the
        page-size truncation stay with the caller, not here.
        """
        conditions = ["owner_tenant_id = :tid", "state = CAST(:state AS TEXT)"]
        params: dict[str, Any] = {"tid": tenant_id, "state": state, "limit": page_size + 1}
        if cursor is not None:
            cursor_created_at, cursor_proposal_id = cursor
            conditions.append("(created_at, proposal_id) > (:cursor_created_at, :cursor_proposal_id)")
            params["cursor_created_at"] = cursor_created_at
            params["cursor_proposal_id"] = cursor_proposal_id

        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"{_PROPOSAL_SELECT} WHERE {' AND '.join(conditions)} "
                            " ORDER BY created_at, proposal_id "
                            " LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_proposal(r) for r in rows)

    async def journal_for(self, claim_id: uuid.UUID) -> tuple[JournalEntry, ...]:
        """Every promotion this claim has been through, oldest first -- the
        reversal handle a reviewer needs.

        `reverse()` takes a `promotion_id`, not a `claim_id`: a claim promoted,
        reversed, and promoted again has more than one journal row, and this is
        how a reviewer finds which one is still live to reverse.
        """
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT promotion_id, proposal_id, claim_id, tenant_id, target_kind, "
                            "       created_row_id, superseded_row_id, superseded_valid_to, "
                            "       promoted_at, promoted_by, reversed_at, reversed_by, reversal_reason "
                            "  FROM memory_promotion_journal WHERE claim_id = :cid "
                            " ORDER BY promoted_at"
                        ),
                        {"cid": claim_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            JournalEntry(
                promotion_id=r["promotion_id"],
                proposal_id=r["proposal_id"],
                claim_id=r["claim_id"],
                tenant_id=r["tenant_id"],
                target_kind=r["target_kind"],
                created_row_id=r["created_row_id"],
                superseded_row_id=r["superseded_row_id"],
                superseded_valid_to=r["superseded_valid_to"],
                promoted_at=r["promoted_at"],
                promoted_by=r["promoted_by"],
                reversed_at=r["reversed_at"],
                reversed_by=r["reversed_by"],
                reversal_reason=r["reversal_reason"],
            )
            for r in rows
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
            raise PermissionError("only the tenant that owns the subject may act on this proposal")
        if not (roles & REVIEW_ROLES):
            raise PermissionError("acting on a proposal requires the producer or admin role")

    async def _load_claim(self, session: AsyncSession, claim_id: uuid.UUID) -> dict[str, Any] | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT claim_id, subject_entity_id, predicate, value_jsonb AS value, "
                        "       owning_tenant_id, author_tenant_id, author_actor_id, status, "
                        "       is_contested, confidence, source_authority, consolidated_at, "
                        "       promotion_state, asserted_valid_from, asserted_valid_to "
                        "  FROM memory_claims WHERE claim_id = :cid AND t_invalidated_at IS NULL"
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
                        "  FROM memory_promotion_proposal WHERE proposal_id = :pid FOR UPDATE"
                    ),
                    {"pid": proposal_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError("no such proposal")
        if row["state"] != STATE_OPEN:
            raise ConflictError(f"proposal is already {row['state']}")
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
                        "SELECT rejected_authority FROM memory_promotion_rejection "
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
    ) -> JSONValue:
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
        value: JSONValue,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        """Write the canonical row, closing whatever it replaces.

        The claim's asserted interval becomes the canonical row's validity, so the
        graph records when the fact holds rather than when somebody got around to
        promoting it.
        """
        self._assert_no_pii(proposal, value)
        if proposal["target_kind"] == TARGET_ATTRIBUTE:
            return await self._write_attribute(session, proposal, value, actor_id=actor_id, now=now)
        return await self._write_edge(session, proposal, value, actor_id=actor_id, now=now)

    def _assert_no_pii(self, proposal: dict[str, Any], value: JSONValue) -> None:
        """Scan on the way into the canonical graph, not on the way into staging.

        A claim can carry an email address or an account number quite legitimately
        while it is a private observation about one tenant's session. What it must
        not do is cross into the shared graph, where a different audience reads it
        under different rules. So the check belongs at the boundary being crossed.

        Scanning the amended value rather than the proposed one matters: a reviewer
        correcting a value must not be able to introduce PII that was never in the
        claim, and a review path that scanned only what the machine proposed would
        let exactly that through.
        """
        if not isinstance(value, str):
            return
        response = self._pii.scan(value, field_type=f"memory_claim.{proposal['predicate']}")
        if response.action_taken == "block":
            categories = sorted({match.category for match in response.matched_patterns})
            raise ValidationError(
                "the proposed value carries content that may not enter the canonical " f"graph: {', '.join(categories)}"
            )

    async def _write_attribute(
        self,
        session: AsyncSession,
        proposal: dict[str, Any],
        value: JSONValue,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        """Delegate the canonical write to the one module every catalog write
        (claim-promotion or otherwise) into `attributes` goes through -- it
        revalidates the target key against the vocabulary before writing, which
        this call site no longer has to (and, before this module existed, did
        not)."""
        return await attribute_writes.write_attribute(
            session,
            tenant_id=proposal["owner_tenant_id"],
            entity_id=proposal["subject_entity_id"],
            key=proposal["target_key"],
            value=value,
            valid_from=proposal["valid_from"],
            valid_to=proposal["valid_to"],
            actor_id=actor_id,
            now=now,
        )

    async def _write_edge(
        self,
        session: AsyncSession,
        proposal: dict[str, Any],
        value: JSONValue,
        *,
        actor_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
        """Resolve the claim's value into a destination entity, then delegate.

        Parsing stays here because it is specific to a claim's value shape,
        never something the canonical-write module should need to know about;
        everything after resolution -- vocabulary revalidation, destination
        existence, the tenant boundary, the write itself -- is the one write
        path's job.
        """
        try:
            dst = uuid.UUID(str(value))
        except ValueError as exc:
            raise ValidationError("an edge-valued claim must name a resolved entity") from exc

        return await attribute_writes.write_edge(
            session,
            tenant_id=proposal["owner_tenant_id"],
            src_entity_id=proposal["subject_entity_id"],
            rel=proposal["target_key"],
            dst_entity_id=dst,
            valid_from=proposal["valid_from"],
            valid_to=proposal["valid_to"],
            actor_id=actor_id,
            now=now,
        )

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
                "VALUES (:audit_id, :tid, :aid, :action, 'memory_claim', :target, NULL, "
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


async def oldest_open_proposal_created_at(session: AsyncSession) -> datetime.datetime | None:
    """`created_at` of the longest-waiting open proposal across every tenant, or
    `None` when no proposal is open.

    A bare function taking the caller's own session, not a `PromotionService`
    method: its one caller (the operational-health console reading) already
    holds an open session and reads across every tenant at once, the opposite
    of every other read on this module, which is scoped to one tenant's own
    queue and opens its own session through the service's factory.
    """
    return (
        await session.execute(text("SELECT min(created_at) FROM memory_promotion_proposal WHERE state = 'open'"))
    ).scalar_one_or_none()


async def erase_promotion_artifacts(session: AsyncSession, claim_ids: list[uuid.UUID]) -> dict[str, int]:
    """Physically remove everything promotion wrote for these claims, for erasure.

    `PromotionService.reverse` deliberately *closes* the canonical row so an
    `as_of` query still sees that the promotion happened — history-preserving
    by design. An actor erasure is the one caller that must override exactly
    that: a promoted value derived from an erased person's sessions is their
    data carried forward, and a closed-but-present row still holds it. So this
    deletes where reversal closes, the same way event erasure deletes where
    ordinary removal soft-invalidates.

    Runs inside the caller's transaction (no `session.begin()` here): erasure
    is all-or-nothing across claims, embeddings, and these rows, and a partial
    commit would orphan what the retry can no longer find.

    The stacked case — a later promotion built on the row being deleted — keeps
    the later row as the live head and leaves the predecessor closed; only the
    erased person's row vanishes from the middle of the chain. A later reversal
    of that stacked promotion will find no predecessor row to reopen and
    no-op on it, which is the correct reading: the row it would restore no
    longer exists to restore.

    Lives here rather than in the erasure participant so knowledge of the
    journal's row shapes stays in the module that writes them.
    """
    if not claim_ids:
        return {
            "canonical_rows_deleted": 0,
            "canonical_rows_reopened": 0,
            "journal_rows_deleted": 0,
            "proposals_deleted": 0,
            "rejections_deleted": 0,
        }

    journals = (
        (
            await session.execute(
                text(
                    "SELECT promotion_id, target_kind, created_row_id, "
                    "       superseded_row_id, superseded_valid_to "
                    "  FROM memory_promotion_journal WHERE claim_id = ANY(:cids) "
                    "   FOR UPDATE"
                ),
                {"cids": claim_ids},
            )
        )
        .mappings()
        .all()
    )

    deleted = 0
    reopened = 0
    for journal in journals:
        # Closed two-value set, never caller input -- see the reversal path above
        # for the same derivation and the same reasoning.
        table = "attributes" if journal["target_kind"] == TARGET_ATTRIBUTE else "edges"
        id_column = "attr_id" if table == "attributes" else "edge_id"

        # A later, un-reversed promotion built on this row means the slot is
        # occupied: the later row is someone else's claim and stays the head.
        occupied = (
            await session.execute(
                text(
                    "SELECT 1 FROM memory_promotion_journal "
                    " WHERE superseded_row_id = :rid AND reversed_at IS NULL "
                    "   AND promotion_id <> :pid"
                ),
                {"rid": journal["created_row_id"], "pid": journal["promotion_id"]},
            )
        ).first()

        result = await session.execute(
            text(f"DELETE FROM {table} WHERE {id_column} = :rid"),  # noqa: S608 - table/id_column are the closed set named above, not caller input
            {"rid": journal["created_row_id"]},
        )
        deleted += result.rowcount or 0  # type: ignore[attr-defined]

        if journal["superseded_row_id"] is not None and occupied is None:
            # Same restoration reversal performs: the predecessor becomes the
            # live row again, its interval reopened to what the promotion had
            # narrowed it to.
            result = await session.execute(
                text(f"UPDATE {table} SET t_valid_to = :vt, t_invalidated_at = NULL " f" WHERE {id_column} = :rid"),  # noqa: S608 - table/id_column are the closed set named above, not caller input
                {"vt": journal["superseded_valid_to"], "rid": journal["superseded_row_id"]},
            )
            reopened += result.rowcount or 0  # type: ignore[attr-defined]

    rejections = await session.execute(
        text(
            "DELETE FROM memory_promotion_rejection WHERE proposal_id IN "
            "  (SELECT proposal_id FROM memory_promotion_proposal WHERE claim_id = ANY(:cids))"
        ),
        {"cids": claim_ids},
    )
    journal_rows = await session.execute(
        text("DELETE FROM memory_promotion_journal WHERE claim_id = ANY(:cids)"),
        {"cids": claim_ids},
    )
    proposals = await session.execute(
        text("DELETE FROM memory_promotion_proposal WHERE claim_id = ANY(:cids)"),
        {"cids": claim_ids},
    )

    return {
        "canonical_rows_deleted": deleted,
        "canonical_rows_reopened": reopened,
        "journal_rows_deleted": journal_rows.rowcount or 0,  # type: ignore[attr-defined]
        "proposals_deleted": proposals.rowcount or 0,  # type: ignore[attr-defined]
        "rejections_deleted": rejections.rowcount or 0,  # type: ignore[attr-defined]
    }
