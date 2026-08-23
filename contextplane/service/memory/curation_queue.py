"""One queue for everything needing a person, and what kind of attention each needs.

Four things arrive here, and they are not interchangeable. An unlinked claim needs
somebody to say what it is about. A contested one needs somebody to decide which of two
assertions holds. A below-floor claim needs somebody to judge whether it is worth
keeping. A high-impact proposal needs its *owner* specifically, and nobody else will do.

Collapsing them into one undifferentiated list would be the easy implementation and the
wrong one: a curator picking up an item has to know what is being asked of them before
they can act, and "there are 40 things" is not a queue anybody works.

**What a machine extracted and what a person stands behind are distinguished on every
row.** That distinction is the audit the curator role exists to perform. A queue that
showed only the current value would make a machine's guess and a person's decision look
the same at a glance, which is the one confusion this surface must never create.

**A contradiction case is the fifth thing, and it is the only one this module writes.**
The claim-shaped items above are read-only projections; a case is a row of its own,
recording that a contradiction was routed to a named person and what they eventually
decided. Writing cases here does not make this a second write path into claims -- no
statement in this module touches `memory_claims`, and a disposition is a *proposal*,
never the write it proposes. The surfaces that perform canonical, runbook, and agent-
readiness writes each have their own approval contract, and a curator recording
`propose_canonical` has asked for a promotion rather than performed one. Collapsing
those two events would make "decided" and "written" the same moment, which is exactly
the confusion an accountable owner is there to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.curation_ranking import (
    _RANK_JOIN,
    _RANK_ORDER,
    ESCALATION_AGE_DAYS,
    QueueCursor,
    SystemClock,
)
from contextplane.types import Clock, TenantContext

REASON_UNLINKED: Final[str] = "unlinked"
REASON_CONTESTED: Final[str] = "contested"
REASON_BELOW_FLOOR: Final[str] = "below_floor"
REASON_AWAITING_OWNER: Final[str] = "awaiting_owner"

# A `containment_refused` reason existed here once, for a candidate a containment
# check turned away. Removed rather than given a SQL arm: nothing in this codebase
# writes a queryable record of that refusal -- the direct-assertion route refuses
# synchronously and never stages or queues the attempt, and extraction's own
# refusal path only counts and logs. A vocabulary entry with no row it could ever
# match is a dead branch, not a feature waiting on wiring.

# What a curator can do about each, so the surface can offer the right action rather
# than every action. An item whose only offered action is wrong for it is how a queue
# gets worked incorrectly rather than slowly.
ACTIONS_BY_REASON: Final[dict[str, tuple[str, ...]]] = {
    REASON_UNLINKED: ("link", "discard"),
    REASON_CONTESTED: ("confirm", "discard", "escalate"),
    REASON_BELOW_FLOOR: ("confirm", "discard"),
    # Deliberately not "accept" or "reject": those belong to the owner's review path,
    # which checks tenancy and role. Offering them here would put a second door on
    # a decision that has an owner.
    REASON_AWAITING_OWNER: ("escalate",),
}

# Where a contradiction case is in its life. `open` means nobody owns it yet, which
# is a state worth naming rather than defaulting to the first curator who looks:
# a contradiction that reaches nobody is a contradiction that stays.
CASE_OPEN: Final[str] = "open"
CASE_ROUTED: Final[str] = "routed"
CASE_RESOLVED: Final[str] = "resolved"

# What an owner may decide. The first three settle the disagreement itself; the last
# three ask a different surface to write something, and asking is all they do.
DISPOSITION_CONFIRM: Final[str] = "confirm"
DISPOSITION_REJECT: Final[str] = "reject"
DISPOSITION_SUPERSEDE: Final[str] = "supersede"
DISPOSITION_PROPOSE_CANONICAL: Final[str] = "propose_canonical"
DISPOSITION_PROPOSE_RUNBOOK: Final[str] = "propose_runbook"
DISPOSITION_PROPOSE_ARC: Final[str] = "propose_arc"

# The three things a promotion proposal can ask for. Carried as a named kind rather
# than inferred from the disposition string so a reader of a stored case can tell
# what was asked for without parsing a verb.
TARGET_CANONICAL_FACT: Final[str] = "canonical_fact"
TARGET_RUNBOOK: Final[str] = "runbook"
TARGET_ARC_ARTIFACT: Final[str] = "arc_artifact"


@dataclasses.dataclass(frozen=True)
class DispositionPolicy:
    """What one disposition commits somebody to, and on whose authority.

    Every field here is a property of the *target*, not of the claim, and the three
    proposal targets deliberately disagree on all of them. A canonical fact is one
    row about one subject that a later reversal can close from the promotion journal;
    a runbook is a procedure a person follows, so a single observation is not enough
    evidence and rolling back means reinstating a revision; an agent-readiness
    artifact changes what every agent reading it will do, which is why it needs
    attested evidence and why its rollback is a revocation rather than an edit.

    Collapsing them into one "propose" disposition with a free-text note would make
    all three look equally consequential in the queue, and the one that reaches
    every agent is not.
    """

    disposition: str
    #: Who may approve the write this disposition asks for -- recorded on the case
    #: at disposition time, because a decision whose approver is decided afterwards
    #: is a decision nobody is accountable for.
    approval_authority: str
    #: What that approver requires before approving.
    evidence_threshold: str
    #: How far the proposed write reaches.
    scope: str
    #: What happens to what the target said before.
    supersession: str
    #: How the write is undone if it turns out wrong.
    rollback: str
    #: The audit vocabulary term the disposition emits.
    audit_action: str
    #: The target a proposal asks to write, or None for a disposition that settles
    #: the disagreement without asking for a write.
    target_kind: str | None = None


# Settling the disagreement, and asking for a write, are different acts with different
# authorities. The first three name the curator's own review authority; only the last
# three name an approver outside curation.
DISPOSITIONS: Final[dict[str, DispositionPolicy]] = {
    DISPOSITION_CONFIRM: DispositionPolicy(
        disposition=DISPOSITION_CONFIRM,
        approval_authority="curation_owner",
        evidence_threshold="one attributable source the owner accepts",
        scope="the contested claim only",
        supersession="none: the counterpart claim is retained and stays visible",
        rollback="record a further disposition on a new case for the same axis",
        audit_action=actions.CLAIM_ADJUDICATED,
    ),
    DISPOSITION_REJECT: DispositionPolicy(
        disposition=DISPOSITION_REJECT,
        approval_authority="curation_owner",
        evidence_threshold="a stated reason the assertion does not hold",
        scope="the contested claim only",
        supersession="none: the rejected claim is retained, never deleted",
        rollback="record a further disposition on a new case for the same axis",
        audit_action=actions.CLAIM_ADJUDICATED,
    ),
    DISPOSITION_SUPERSEDE: DispositionPolicy(
        disposition=DISPOSITION_SUPERSEDE,
        approval_authority="curation_owner",
        evidence_threshold="a newer assertion from a source of at least equal authority",
        scope="the contested claim only",
        supersession="the older claim stops being current and is kept as history",
        rollback="record a further disposition on a new case for the same axis",
        audit_action=actions.CLAIM_ADJUDICATED,
    ),
    DISPOSITION_PROPOSE_CANONICAL: DispositionPolicy(
        disposition=DISPOSITION_PROPOSE_CANONICAL,
        approval_authority="catalog_owner",
        evidence_threshold="a settled, uncontested claim above the tenant confidence floor",
        scope="one subject and predicate in the canonical graph",
        supersession="the canonical row it replaces is closed at the asserted interval",
        rollback="reverse the promotion from its journal entry",
        audit_action=actions.CLAIM_PROMOTION_PROPOSED,
        target_kind=TARGET_CANONICAL_FACT,
    ),
    DISPOSITION_PROPOSE_RUNBOOK: DispositionPolicy(
        disposition=DISPOSITION_PROPOSE_RUNBOOK,
        approval_authority="operations_owner",
        evidence_threshold="two independent outcome signals, not one observation",
        scope="one documented procedure a person follows",
        supersession="a new revision supersedes the previous one, which is retained",
        rollback="reinstate the previous revision",
        audit_action=actions.CLAIM_PROMOTION_PROPOSED,
        target_kind=TARGET_RUNBOOK,
    ),
    DISPOSITION_PROPOSE_ARC: DispositionPolicy(
        disposition=DISPOSITION_PROPOSE_ARC,
        approval_authority="arc_approver",
        evidence_threshold="an attested source plus recorded human judgment",
        scope="every agent that resolves the artifact",
        supersession="a new revision activates; the previous revision is retained",
        rollback="revoke the activated revision",
        audit_action=actions.CLAIM_PROMOTION_PROPOSED,
        target_kind=TARGET_ARC_ARTIFACT,
    ),
}


def policy_for(disposition: str) -> DispositionPolicy:
    """The policy a disposition commits to, or a refusal naming the vocabulary.

    Refuses rather than defaulting: an unknown disposition stored with a borrowed
    authority would read afterwards as a decision somebody was accountable for.
    """
    policy = DISPOSITIONS.get(disposition)
    if policy is None:
        msg = f"unknown disposition {disposition!r}; expected one of {sorted(DISPOSITIONS)}"
        raise ValidationError(msg)
    return policy


@dataclasses.dataclass(frozen=True)
class CurationCase:
    """One contradiction, who owns it, and what was decided about it."""

    case_id: uuid.UUID
    tenant_id: uuid.UUID
    subject_reference: str
    predicate: str
    status: str
    created_at: datetime.datetime
    raised_by_derivation_id: uuid.UUID | None = None
    owner_id: str | None = None
    routed_at: datetime.datetime | None = None
    disposition: str | None = None
    approval_authority: str | None = None
    evidence_threshold: str | None = None
    resolved_at: datetime.datetime | None = None

    @property
    def target_kind(self) -> str | None:
        """What a resolved case asked to be written, or None when it asked for nothing."""
        if self.disposition is None:
            return None
        return DISPOSITIONS[self.disposition].target_kind


@dataclasses.dataclass(frozen=True)
class QueueItem:
    """One claim awaiting curation, with reason and available actions."""

    claim_id: uuid.UUID
    reason: str
    subject_reference: str
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    confidence: float | None
    created_at: datetime.datetime
    # Whether a person stands behind this row, as against a machine having extracted
    # or inferred it. Read from the claim's own authority tier rather than from
    # whether a confirmation points at it: a confirmation closes what it confirms, so
    # a "has been confirmed" flag would be false on every row that is still in the
    # queue -- a field that reads as informative and can never vary.
    human_backed: bool
    proposal_id: uuid.UUID | None = None

    #: Why this row sits where it does. The consequence preview belongs on the
    #: item rather than behind a second call: a rank a reviewer cannot
    #: interrogate is a rank they learn to ignore, and these three are exactly
    #: the ordering's inputs.
    dependant_count: int = 0
    sampling_priority: int = 0
    #: Past the governed escalation age, so ordered ahead of everything younger
    #: whatever its leverage. This is the flag that makes the queue's
    #: reachability promise visible to the person relying on it.
    escalated: bool = False

    @property
    def available_actions(self) -> tuple[str, ...]:
        """Actions the current reason permits; empty when no action applies."""
        return ACTIONS_BY_REASON.get(self.reason, ())

    def cursor(self) -> QueueCursor:
        """Where a next page should resume from, if this is the last row read."""
        return QueueCursor(
            escalation_rank=0 if self.escalated else 1,
            neg_dependants=-self.dependant_count,
            neg_sampling=-self.sampling_priority,
            created_at=self.created_at,
            claim_id=self.claim_id,
        )


class CurationQueueService:
    """Reads only. Acting on an item goes through the service that owns that
    decision, so the queue cannot become a second write path into claims."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, clock: Clock | None = None) -> None:
        self._factory = factory
        # Optional so the existing construction sites keep working: every method
        # but the ranked read is timeless, and `items_for` accepts an explicit
        # `now` for callers that hold one. A service that demanded a clock for
        # nine methods that never ask the time would be a worse contract than
        # this narrow default.
        self._clock = clock or SystemClock()

    async def items_for(
        self,
        tenant_id: uuid.UUID,
        *,
        cursor: QueueCursor | None = None,
        page_size: int = 100,
        now: datetime.datetime | None = None,
    ) -> tuple[QueueItem, ...]:
        """Everything needing attention in one tenant, most consequential first.

        This was arrival order, and the docstring here argued for it: ranking
        "means the tail is never reached". That was right about ranking and
        wrong only in assuming ranking has to starve. `curation_ranking` holds
        why it does not have to, what the ordering is, and why confidence is
        deliberately absent from it.

        Keyset-paginated on the whole sort tuple. `cursor` is the decoded
        `QueueCursor` from the last row of the previous page; the caller owns
        encoding it, the same split `admin_audit.py`'s query route uses. Fetches
        `page_size + 1` rows so the caller can tell whether another page follows
        without a second query.
        """
        moment = now if now is not None else self._clock.now()
        params: dict[str, Any] = {
            "escalation_cutoff": moment - datetime.timedelta(days=ESCALATION_AGE_DAYS),
            "limit": page_size + 1,
            "tid": tenant_id,
        }
        # A CTE, so the cursor can compare the *computed* sort columns rather
        # than recomputing a correlated subquery inside its own predicate.
        sql = f"WITH ranked AS ({_QUEUE_BASE}{_RANK_JOIN}\n) SELECT * FROM ranked"  # noqa: S608 - fixed module constants; every value is bound
        if cursor is not None:
            sql += (
                " WHERE (escalation_rank, neg_dependants, neg_sampling, created_at, claim_id)"
                " > (:cursor_escalation, :cursor_dependants, :cursor_sampling,"
                "    :cursor_created_at, :cursor_claim_id)"
            )
            params["cursor_claim_id"] = cursor.claim_id
            params["cursor_created_at"] = cursor.created_at
            params["cursor_dependants"] = cursor.neg_dependants
            params["cursor_escalation"] = cursor.escalation_rank
            params["cursor_sampling"] = cursor.neg_sampling
        sql += _RANK_ORDER + " LIMIT :limit"

        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(sql),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            QueueItem(
                claim_id=row["claim_id"],
                reason=row["reason"],
                subject_reference=row["subject_reference"],
                subject_entity_id=row["subject_entity_id"],
                predicate=row["predicate"],
                value=row["value"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                created_at=row["created_at"],
                human_backed=bool(row["human_backed"]),
                proposal_id=row["proposal_id"],
                # The consequence preview, from the same read rather than a
                # traversal per row: a rank a reviewer cannot interrogate is a
                # rank they learn to ignore.
                dependant_count=-int(row["neg_dependants"]),
                sampling_priority=-int(row["neg_sampling"]),
                escalated=int(row["escalation_rank"]) == 0,
            )
            for row in rows
        )

    async def counts_for(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """How much of each kind is waiting.

        Separate from the item list because the number a curator needs to see before
        opening the queue is not the same as the page they open.
        """
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT reason, count(*) AS n FROM ({_QUEUE_BASE}) q GROUP BY reason"  # noqa: S608 - _QUEUE_BASE is a fixed module-level query constant, not caller input; :tid is the only actual value and is bound below
                        ),
                        {"tid": tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return {row["reason"]: int(row["n"]) for row in rows}

    # -- contradiction cases -------------------------------------------------

    async def open_case(
        self,
        ctx: TenantContext,
        *,
        subject_reference: str,
        predicate: str,
        now: datetime.datetime,
        raised_by_derivation_id: uuid.UUID | None = None,
    ) -> CurationCase:
        """Open a case for one contradiction axis, or return the one already open.

        Idempotent on `(tenant, subject_reference, predicate)` while a case on that
        axis is unresolved. A second row would split one disagreement across two
        queue entries and let two owners decide it differently, which is the state
        this surface exists to make impossible -- and re-detection of the same
        contradiction is the normal case, not the rare one.

        Opening a case writes nothing about the claims it is about. Grouping decides
        *that* they disagree; this decides that a person has to look.
        """
        if not subject_reference or not predicate:
            msg = "a case names the subject and predicate it is about"
            raise ValidationError(msg)

        async with self._factory() as session, session.begin():
            existing = (
                (
                    await session.execute(
                        text(
                            f"SELECT {_CASE_COLUMNS} FROM curation_cases "  # noqa: S608 - _CASE_COLUMNS is a fixed module-level column list, not caller input
                            "WHERE tenant_id = :tid AND subject_reference = :subj "
                            "  AND predicate = :pred AND status <> 'resolved' "
                            "ORDER BY created_at LIMIT 1"
                        ),
                        {"tid": ctx.tenant_id, "subj": subject_reference, "pred": predicate},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return _to_case(existing)

            case_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO curation_cases "
                    "  (case_id, tenant_id, subject_reference, predicate, "
                    "   raised_by_derivation_id, status, created_at) "
                    "VALUES (:cid, :tid, :subj, :pred, :did, 'open', CAST(:now AS TIMESTAMPTZ))"
                ),
                {
                    "cid": case_id,
                    "tid": ctx.tenant_id,
                    "subj": subject_reference,
                    "pred": predicate,
                    "did": raised_by_derivation_id,
                    "now": now,
                },
            )
            await _audit(
                session,
                action=actions.CLAIM_CONTESTED,
                ctx=ctx,
                case_id=case_id,
                payload={"subject_reference": subject_reference, "predicate": predicate, "status": CASE_OPEN},
                now=now,
            )
            return CurationCase(
                case_id=case_id,
                tenant_id=ctx.tenant_id,
                subject_reference=subject_reference,
                predicate=predicate,
                status=CASE_OPEN,
                created_at=now,
                raised_by_derivation_id=raised_by_derivation_id,
            )

    async def route_case(
        self,
        ctx: TenantContext,
        *,
        case_id: uuid.UUID,
        owner_id: str,
        now: datetime.datetime,
    ) -> CurationCase:
        """Name the person accountable for deciding this case.

        Re-routing an already-routed case is allowed and audited: escalation is a
        real move, and a queue that could not hand a case on would strand it on
        whoever it reached first.

        A resolved case is refused rather than re-opened. Routing it would suggest
        the decision is still to be made when it has been made and audited.
        """
        if not owner_id.strip():
            msg = "a case is routed to a named owner"
            raise ValidationError(msg)

        async with self._factory() as session, session.begin():
            current = await self._locked_case(session, ctx, case_id)
            if current.status == CASE_RESOLVED:
                msg = f"case {case_id} is resolved; routing it would reopen a decision that was made"
                raise ConflictError(msg)

            await session.execute(
                text(
                    "UPDATE curation_cases "
                    "SET owner_id = :owner, routed_at = CAST(:now AS TIMESTAMPTZ), status = 'routed' "
                    "WHERE case_id = :cid"
                ),
                {"cid": case_id, "owner": owner_id, "now": now},
            )
            await _audit(
                session,
                action=actions.CLAIM_PROPOSAL_ROUTED,
                ctx=ctx,
                case_id=case_id,
                payload={
                    "owner_id": owner_id,
                    "previous_owner_id": current.owner_id,
                    "subject_reference": current.subject_reference,
                    "predicate": current.predicate,
                },
                now=now,
            )
            return dataclasses.replace(current, status=CASE_ROUTED, owner_id=owner_id, routed_at=now)

    async def record_disposition(
        self,
        ctx: TenantContext,
        *,
        case_id: uuid.UUID,
        disposition: str,
        now: datetime.datetime,
    ) -> CurationCase:
        """Record what the accountable owner decided, and on whose authority.

        Three checks, in this order, and each one refuses without writing:

        1. the case is in the caller's tenant (an absent case and one belonging to
           somebody else answer identically, so a case id is not an existence oracle);
        2. it has been routed, because a disposition on an unrouted case is a
           decision with no accountable owner behind it;
        3. the caller *is* that owner. Being able to see a case is not authority to
           decide it, and this is the one check that makes "routed to an owner" mean
           anything at all.

        The write is a compare-and-swap on the routed, unresolved state, so two
        owners racing the same case leave one decision rather than the last writer's.
        Nothing here writes what the disposition proposes -- the surface that owns
        the target does that, under the authority recorded on this row.
        """
        policy = policy_for(disposition)

        async with self._factory() as session, session.begin():
            current = await self._locked_case(session, ctx, case_id)
            if current.status != CASE_ROUTED:
                msg = f"case {case_id} is {current.status}; a disposition needs an accountable owner"
                raise ConflictError(msg)
            if current.owner_id != _actor_key(ctx):
                msg = f"case {case_id} is routed to another owner"
                raise PermissionError(msg)

            updated = (
                await session.execute(
                    text(
                        "UPDATE curation_cases "
                        "SET status = 'resolved', disposition = :disp, "
                        "    approval_authority = :authority, evidence_threshold = :threshold, "
                        "    resolved_at = CAST(:now AS TIMESTAMPTZ) "
                        "WHERE case_id = :cid AND status = 'routed' AND resolved_at IS NULL "
                        "RETURNING case_id"
                    ),
                    {
                        "cid": case_id,
                        "disp": policy.disposition,
                        "authority": policy.approval_authority,
                        "threshold": policy.evidence_threshold,
                        "now": now,
                    },
                )
            ).one_or_none()
            if updated is None:
                msg = f"case {case_id} was decided by another writer"
                raise ConflictError(msg)

            await _audit(
                session,
                action=policy.audit_action,
                ctx=ctx,
                case_id=case_id,
                payload={
                    "disposition": policy.disposition,
                    "target_kind": policy.target_kind,
                    "approval_authority": policy.approval_authority,
                    "evidence_threshold": policy.evidence_threshold,
                    "scope": policy.scope,
                    "supersession": policy.supersession,
                    "rollback": policy.rollback,
                    "owner_id": current.owner_id,
                },
                now=now,
            )
            return dataclasses.replace(
                current,
                status=CASE_RESOLVED,
                disposition=policy.disposition,
                approval_authority=policy.approval_authority,
                evidence_threshold=policy.evidence_threshold,
                resolved_at=now,
            )

    async def case(self, ctx: TenantContext, case_id: uuid.UUID) -> CurationCase:
        """One case, if it belongs to the caller's tenant.

        A case in another tenant answers exactly as a case that does not exist,
        so reading one cannot confirm that a contradiction is under review
        somewhere else.
        """
        async with self._factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {_CASE_COLUMNS} FROM curation_cases "  # noqa: S608 - _CASE_COLUMNS is a fixed module-level column list, not caller input
                            "WHERE case_id = :cid AND tenant_id = :tid"
                        ),
                        {"cid": case_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            msg = f"curation case {case_id} not found"
            raise NotFoundError(msg)
        return _to_case(row)

    async def cases_for(
        self,
        tenant_id: uuid.UUID,
        *,
        status: str | None = None,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 100,
    ) -> tuple[CurationCase, ...]:
        """Cases in one tenant, oldest first, optionally narrowed to one status.

        Same keyset shape and same drain-from-the-front order as `items_for`, on
        `(created_at, case_id)`: an aged contradiction is the one worth surfacing,
        and a queue whose tail is never reached is a queue that queued for nothing.
        Fetches `page_size + 1` so the caller can tell whether another page follows.
        """
        if status is not None and status not in _CASE_STATUSES:
            msg = f"unknown case status {status!r}; expected one of {sorted(_CASE_STATUSES)}"
            raise ValidationError(msg)

        params: dict[str, Any] = {"tid": tenant_id, "limit": page_size + 1}
        sql = f"SELECT {_CASE_COLUMNS} FROM curation_cases WHERE tenant_id = :tid"  # noqa: S608 - _CASE_COLUMNS is a fixed module-level column list, not caller input
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        if cursor is not None:
            sql += " AND (created_at, case_id) > (:cursor_created_at, :cursor_case_id)"
            params["cursor_created_at"] = cursor[0]
            params["cursor_case_id"] = cursor[1]
        sql += " ORDER BY created_at, case_id LIMIT :limit"

        async with self._factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
        return tuple(_to_case(row) for row in rows)

    async def _locked_case(self, session: AsyncSession, ctx: TenantContext, case_id: uuid.UUID) -> CurationCase:
        """The case under a row lock, or the same refusal a missing one gets.

        Locked because both mutations read the case, decide on what they read, and
        then write: without the lock, two owners can both see `routed` and both
        proceed. The compare-and-swap in `record_disposition` is the second guard
        rather than the only one, so a lost race refuses instead of overwriting.
        """
        row = (
            (
                await session.execute(
                    text(
                        f"SELECT {_CASE_COLUMNS} FROM curation_cases "  # noqa: S608 - _CASE_COLUMNS is a fixed module-level column list, not caller input
                        "WHERE case_id = :cid AND tenant_id = :tid FOR UPDATE"
                    ),
                    {"cid": case_id, "tid": ctx.tenant_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            msg = f"curation case {case_id} not found"
            raise NotFoundError(msg)
        return _to_case(row)


def _actor_key(ctx: TenantContext) -> str | None:
    """The caller's identity as `owner_id` stores it.

    `owner_id` is text rather than an actor foreign key because an accountable
    owner can be a rota or a team address that has no actor row -- so the
    comparison is on the string form, and an unauthenticated context (no actor)
    matches nobody rather than matching an unrouted case.
    """
    return None if ctx.actor_id is None else str(ctx.actor_id)


def _to_case(row: RowMapping) -> CurationCase:
    return CurationCase(
        case_id=row["case_id"],
        tenant_id=row["tenant_id"],
        subject_reference=row["subject_reference"],
        predicate=row["predicate"],
        status=row["status"],
        created_at=row["created_at"],
        raised_by_derivation_id=row["raised_by_derivation_id"],
        owner_id=row["owner_id"],
        routed_at=row["routed_at"],
        disposition=row["disposition"],
        approval_authority=row["approval_authority"],
        evidence_threshold=row["evidence_threshold"],
        resolved_at=row["resolved_at"],
    )


async def _audit(
    session: AsyncSession,
    *,
    action: str,
    ctx: TenantContext,
    case_id: uuid.UUID,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> None:
    """Audit in the case's own transaction, targeting the case rather than a claim.

    In-transaction rather than through the fire-and-forget emitter every read-side
    surface uses: a routed case with no routing row, or a resolved one with no
    recorded authority, is precisely the state the disposition rules exist to make
    unreachable, so the audit row commits with the decision or neither does.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
            "   before_jsonb, after_jsonb, ts, request_id, error_code) "
            "VALUES (:audit_id, :tid, :aid, :action, 'curation_case', :target, NULL, "
            "        CAST(:after AS JSONB), CAST(:now AS TIMESTAMPTZ), NULL, NULL)"
        ),
        {
            "audit_id": uuid.uuid4(),
            "tid": ctx.tenant_id,
            "aid": ctx.actor_id,
            "action": action,
            "target": case_id,
            "after": json.dumps(payload, sort_keys=True),
            "now": now,
        },
    )


_CASE_STATUSES: Final[frozenset[str]] = frozenset({CASE_OPEN, CASE_ROUTED, CASE_RESOLVED})

# Every column `_to_case` reads, in one place: a SELECT that forgot one would fail
# at row-mapping time in whichever of the four read paths ran first.
_CASE_COLUMNS: Final[str] = (
    "case_id, tenant_id, subject_reference, predicate, raised_by_derivation_id, "
    "status, owner_id, routed_at, disposition, approval_authority, "
    "evidence_threshold, resolved_at, created_at"
)


def backlog_predicate(*, tenant_filter: bool) -> str:
    """The FROM/JOIN/WHERE that decides whether a claim is in the curation backlog.

    A claim is backlogged for the first reason that applies -- unlinked, contested,
    waiting on a high-impact proposal's owner, or below the tenant's confidence
    floor -- which is also the CASE order `_QUEUE_BASE` below uses to label it.

    `operational_health.py`'s cluster-wide backlog gauge calls this the same way
    (`tenant_filter=False`) rather than keeping its own copy of the CASE/JOIN
    logic. One function both callers run through means a change to what counts
    as "backlog" cannot update one and silently leave the other disagreeing --
    the failure mode a hand-copied second query invites by construction.
    """
    tenant_clause = "COALESCE(c.owning_tenant_id, c.author_tenant_id) = :tid\n   AND " if tenant_filter else ""
    return f"""
  FROM memory_claims c
  LEFT JOIN memory_promotion_proposal p
         ON p.claim_id = c.claim_id AND p.state = 'open'
        AND p.high_impact_reasons <> '[]'::JSONB
  LEFT JOIN memory_promotion_policy pol
         ON pol.tenant_id = COALESCE(c.owning_tenant_id, c.author_tenant_id)
 WHERE {tenant_clause}c.t_invalidated_at IS NULL
   AND c.status <> 'superseded'
   AND (
       c.status = 'unlinked'
       OR c.is_contested
       OR p.proposal_id IS NOT NULL
       OR c.confidence < COALESCE(pol.confidence_floor, 0)
   )
"""


# A claim reaches the queue for the first reason that applies, so one claim never
# appears twice under different headings. The order runs from "we do not know what this
# is about" outward, because an unlinked claim cannot be usefully judged on any of the
# later grounds anyway.
_QUEUE_BASE = """
SELECT c.claim_id,
       CASE
           WHEN c.status = 'unlinked' THEN 'unlinked'
           WHEN c.is_contested THEN 'contested'
           WHEN p.proposal_id IS NOT NULL THEN 'awaiting_owner'
           WHEN c.confidence < COALESCE(pol.confidence_floor, 0) THEN 'below_floor'
       END AS reason,
       c.subject_reference,
       c.subject_entity_id,
       c.predicate,
       c.value_jsonb AS value,
       c.confidence,
       c.created_at,
       (c.source_authority IN ('owner_human', 'observer_human')
        OR c.confirms_claim_id IS NOT NULL) AS human_backed,
       p.proposal_id""" + backlog_predicate(tenant_filter=True)
