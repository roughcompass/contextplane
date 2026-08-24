"""The curation-case lifecycle: opening a contradiction, routing it, deciding it.

Split out of `curation_queue.py`, whose own class docstring says the thing this
module exists to make true:

    Reads only. Acting on an item goes through the service that owns that
    decision, so the queue cannot become a second write path into claims.

The case lifecycle *is* that second write path, and it lived in the same file as
the sentence denying it. The 800-line ceiling is what pointed at the seam, but
the seam predates the line that crossed it: a read-only queue and a read-write
lifecycle are two concerns however many lines they take.

**The queue answers "what needs a person"; this answers "what did the person
decide".** They share a subject and nothing else — no state, no transaction, no
table. `CurationQueueService` never opens a case and `CurationCaseService` never
reads the queue, which is a property a reader can now check by opening one file
instead of reasoning about a class with nine methods and two jobs.
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
from contextplane.types import TenantContext

# Where a contradiction case is in its life. `open` means nobody owns it yet, which
# is a state worth naming rather than defaulting to the first curator who looks:
# a contradiction that reaches nobody is a contradiction that stays.
CASE_OPEN: Final[str] = "open"
CASE_ROUTED: Final[str] = "routed"
CASE_RESOLVED: Final[str] = "resolved"

# What kind of thing decided. A first-class field rather than something worked
# out from `owner_id`: telling a policy's actor from a person's means knowing
# which service accounts are automation, which lives outside this table and is
# wrong for exactly the deployment that adds a new one.
DISPOSITION_BY_HUMAN: Final[str] = "human"
DISPOSITION_BY_POLICY: Final[str] = "policy"
DISPOSITION_ACTOR_KINDS: Final[frozenset[str]] = frozenset({DISPOSITION_BY_HUMAN, DISPOSITION_BY_POLICY})

#: What a *person* may record, which is not the whole vocabulary.
#:
#: `migrated_canonical` is written by `MigrationAcceptanceService` and by nothing
#: else. Offering it on the operator surface would let somebody mark a claim
#: migrated without a lot, without a sample and without the halt -- the
#: self-marking batch ADR 0022 exists to refuse, reachable by one POST. The
#: transports build their accepted set from this rather than from `DISPOSITIONS`,
#: and a conformance test holds them equal.
#:
#: Defined below the vocabulary it subsets; see `OPERATOR_DISPOSITIONS`.

# What an owner may decide. The first three settle the disagreement itself; the last
# three ask a different surface to write something, and asking is all they do.
DISPOSITION_CONFIRM: Final[str] = "confirm"
DISPOSITION_REJECT: Final[str] = "reject"
DISPOSITION_SUPERSEDE: Final[str] = "supersede"
DISPOSITION_PROPOSE_CANONICAL: Final[str] = "propose_canonical"
DISPOSITION_PROPOSE_RUNBOOK: Final[str] = "propose_runbook"
DISPOSITION_PROPOSE_ARC: Final[str] = "propose_arc"

# The seventh, and the only one a policy performs. ADR 0022: a migration is a
# lot, the lot is accepted on a sample a *person* inspected, and this records the
# same outcome across the uninspected remainder. It asks the promotion surface
# like the three above it -- nothing here writes canon.
DISPOSITION_MIGRATED_CANONICAL: Final[str] = "migrated_canonical"

# The three things a promotion proposal can ask for. Carried as a named kind rather
# than inferred from the disposition string so a reader of a stored case can tell
# what was asked for without parsing a verb.
TARGET_CANONICAL_FACT: Final[str] = "canonical_fact"
TARGET_RUNBOOK: Final[str] = "runbook"
TARGET_ARC_ARTIFACT: Final[str] = "arc_artifact"


#: The six a person may record. `DISPOSITIONS` minus the policy-only one.
OPERATOR_DISPOSITIONS: Final[tuple[str, ...]] = (
    DISPOSITION_CONFIRM,
    DISPOSITION_REJECT,
    DISPOSITION_SUPERSEDE,
    DISPOSITION_PROPOSE_CANONICAL,
    DISPOSITION_PROPOSE_RUNBOOK,
    DISPOSITION_PROPOSE_ARC,
)

#: The dispositions no transport offers, because a service writes them. Stated as
#: its own name so "why is this one missing from the endpoint" has an answer in
#: the code rather than in a reviewer's memory.
POLICY_ONLY_DISPOSITIONS: Final[frozenset[str]] = frozenset({DISPOSITION_MIGRATED_CANONICAL})


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
    DISPOSITION_MIGRATED_CANONICAL: DispositionPolicy(
        disposition=DISPOSITION_MIGRATED_CANONICAL,
        # Not a new signatory. `catalog_owner` already answers "who may put a
        # fact in the canonical graph", and a migration is exactly the moment
        # somebody would want to give that question a second answer.
        approval_authority="catalog_owner",
        # The one dimension that is this disposition's own. Every other
        # disposition's threshold is a statement about one claim; this is a
        # statement about a lot, which is what makes it a migration rather than
        # a promotion somebody read.
        evidence_threshold="a lot whose review sample a person inspected to its category's policy floor",
        # The remaining three are the target's, and match `propose_canonical`
        # exactly. A migration that wrote canon by different rules would be a
        # second canonical graph.
        scope="one subject and predicate in the canonical graph",
        supersession="the canonical row it replaces is closed at the asserted interval",
        rollback="reverse the promotion from its journal entry",
        audit_action=actions.CLAIM_PROMOTION_PROPOSED,
        target_kind=TARGET_CANONICAL_FACT,
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
    #: `human` or `policy`, set with the disposition or not at all.
    disposition_actor_kind: str | None = None
    approval_authority: str | None = None
    evidence_threshold: str | None = None
    resolved_at: datetime.datetime | None = None

    @property
    def target_kind(self) -> str | None:
        """What a resolved case asked to be written, or None when it asked for nothing."""
        if self.disposition is None:
            return None
        return DISPOSITIONS[self.disposition].target_kind


class CurationCaseService:
    """Opens, routes and resolves curation cases. The write half of curation.

    Separate from `CurationQueueService` rather than a second set of methods on
    it: that class promises reads only, and a class cannot promise that while
    also holding `record_disposition`.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

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
        actor_kind: str = DISPOSITION_BY_HUMAN,
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

        `actor_kind` says whether a person or a policy decided, and it is stored
        rather than inferred. It defaults to `human` because every caller today
        is a transport carrying a person's request past the owner check above; a
        policy path that arrives later has to say so, which is the point.

        **A policy disposition is not an inspected sample.** Acceptance sampling
        assumes the item was looked at, so counting an automated disposal toward
        a reviewer's sample would raise the measured quality of a queue nobody
        read. `inspected_dispositions` excludes them, which keeps the human
        sample requirement unchanged by automation -- a more aggressive policy
        cannot quietly shrink what a person still has to review.
        """
        if actor_kind not in DISPOSITION_ACTOR_KINDS:
            msg = f"unknown disposition actor kind {actor_kind!r}; expected one of {sorted(DISPOSITION_ACTOR_KINDS)}"
            raise ValidationError(msg)
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
                        "    disposition_actor_kind = :actor_kind, "
                        "    approval_authority = :authority, evidence_threshold = :threshold, "
                        "    resolved_at = CAST(:now AS TIMESTAMPTZ) "
                        "WHERE case_id = :cid AND status = 'routed' AND resolved_at IS NULL "
                        "RETURNING case_id"
                    ),
                    {
                        "cid": case_id,
                        "disp": policy.disposition,
                        "actor_kind": actor_kind,
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
                    "disposition_actor_kind": actor_kind,
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
                disposition_actor_kind=actor_kind,
                approval_authority=policy.approval_authority,
                evidence_threshold=policy.evidence_threshold,
                resolved_at=now,
            )

    async def inspected_dispositions(
        self,
        ctx: TenantContext,
        *,
        since: datetime.datetime,
    ) -> int:
        """How many dispositions since `since` were made by a person.

        **The number acceptance sampling is entitled to use.** Sampling assumes
        the sampled item was inspected; a policy disposed of nothing it looked
        at, so counting automated disposals here would raise the measured quality
        of a queue nobody read — the failure where the number keeps looking fine
        while the evidence behind it disappears.

        Excluding them, rather than giving automation its own acceptance
        criteria, has one property worth the choice: the human sample
        requirement is **unchanged by automation**. A more aggressive policy
        cannot shrink what a person still has to review, so automating disposal
        can never improve the figure by reducing the evidence behind it. The
        alternative needed a defect tolerance and a consumer's risk for automated
        disposal that nobody has measured, which is a governance fact invented to
        make an automated path look governed.
        """
        async with self._factory() as session:
            return int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM curation_cases "
                            "WHERE tenant_id = :tid AND resolved_at >= :since "
                            "  AND disposition_actor_kind = :human"
                        ),
                        {"tid": ctx.tenant_id, "since": since, "human": DISPOSITION_BY_HUMAN},
                    )
                ).scalar_one()
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
        disposition_actor_kind=row["disposition_actor_kind"],
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
    "status, owner_id, routed_at, disposition, disposition_actor_kind, approval_authority, "
    "evidence_threshold, resolved_at, created_at"
)
