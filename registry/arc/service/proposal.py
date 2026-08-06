"""Artifact families, proposal threads, and the ADR 040 proposal-version
state machine.

An artifact family (`arc_artifacts`) is the stable identity of a versioned
ARC artifact -- created once, referenced by every revision. A proposal
thread (`arc_authoring_proposals`) is the sequence of attempts to create or
revise one family member: stable identity and sequence coordination only,
exactly one thread per family. A proposal version
(`arc_authoring_proposal_versions`) is where all state actually lives --
each version is frozen once submitted and carries its own state, never the
thread's.

**The bijection and the one-live-candidate rule are database constraints,
not application checks.** `UNIQUE (revision_id)` on the version table is the
`(proposal_id, proposal_version) <-> revision_id` bijection (nothing sets
`revision_id` yet -- that is `submit`'s job, a later task); the partial
`UNIQUE INDEX ... WHERE state IN ('open','submitted','approved')` is what
makes "at most one nonterminal version per thread" true even when two
callers race to open one. This module's `open_proposal` still checks the
rule itself first, so a caller usually gets a clean `ProposalStateConflict`
message rather than a raw `IntegrityError` -- but the constraint, not the
check, is what makes the rule actually hold.

**Lock order.** `open_proposal` locks the artifact-family row before the
thread row, matching ADR 040's global order ("artifact-family row" precedes
"proposal-version row"); the thread row is locked immediately alongside it,
because get-or-create-thread and the nonterminal-version check both need to
serialize against a concurrent opener before anything is inserted. Every
transition (`withdraw`/`reject`/`supersede`) is a single-row compare-and-swap
that needs no separate lock -- the `UPDATE ... WHERE state = ANY(...)`
clause itself is the atomic decision.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService, ArtifactScope
from registry.arc.service.queries import proposal as queries
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.exceptions import ConflictError, NotFoundError, RegistryError
from registry.types import Clock

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProposalStateConflict(RegistryError):
    """Compare-and-swap lost, or the transition is not in the ADR 040 table
    (`arc_proposal_state_conflict`, 409)."""


# ---------------------------------------------------------------------------
# The closed ADR 040 state machine
# ---------------------------------------------------------------------------

#: The eight `ProposalState` literals, in the order the wire enum declares
#: them. `activated`, `rejected`, `stale`, `superseded`, `withdrawn` are
#: terminal: no outward transition exists for any of them at the version
#: level -- recovery from a terminal version is always a new version on the
#: same thread, never a transition on the terminal one.
NONTERMINAL_STATES: frozenset[str] = frozenset({"open", "submitted", "approved"})

#: Legal next-states per current state. `stale` (server-only, drift
#: detection) and `activated`/`approved`-only transitions (`approve`,
#: `activate`) are listed because they are part of the closed state machine
#: this response describes, even though no writer for them exists until
#: later tasks land -- `allowed_transitions` is the state machine's shape,
#: not a promise that every route exists yet.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("submitted", "withdrawn"),
    "submitted": ("approved", "rejected", "stale", "superseded"),
    "approved": ("activated", "stale", "superseded"),
    "activated": (),
    "rejected": (),
    "stale": (),
    "superseded": (),
    "withdrawn": (),
}

#: Which `AvailableAction` members this task's own writers back, per
#: current state. Only `withdraw`/`reject`/`supersede` are real routes as of
#: this module; the remaining `AvailableAction` members (`edit`, `validate`,
#: `submit`, ...) become available once the tasks that implement their
#: routes extend this table -- advertising one today would be actively
#: wrong, since calling it would 404 rather than refuse meaningfully.
_AVAILABLE_ACTIONS: dict[str, tuple[str, ...]] = {
    "open": ("withdraw",),
    "submitted": ("reject", "supersede"),
    "approved": ("supersede",),
    "activated": (),
    "rejected": (),
    "stale": (),
    "superseded": (),
    "withdrawn": (),
}

# reason_code is intentionally an open string, not a closed vocabulary here:
# `ReasonCode` stayed `str` at the wire layer (see
# `registry/api/schemas/arc_authoring.py`'s own docstring) because ADR 040's
# named source for it states transition authorities and effects in prose,
# not an enumerated code list, and no task has materialized concrete
# `reason_code` string constants yet. Inventing a plausible-looking list
# here would be exactly that failure. `reason_codes` on every response is
# therefore always empty until whichever task closes the vocabulary.
_REASON_CODES: tuple[str, ...] = ()

_REASON_NOTE_MAX_LEN = 2000


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ArtifactFamily:
    artifact_id: uuid.UUID
    slug: str
    kind: str
    owning_scope: str
    target_tenant_id: uuid.UUID | None
    title: str
    active_revision_id: uuid.UUID | None
    created_at: datetime.datetime
    created_by_issuer: str
    created_by_subject: str


@dataclasses.dataclass(frozen=True)
class ProposalVersion:
    proposal_id: uuid.UUID
    proposal_version: int
    artifact_id: uuid.UUID
    state: str
    revision_id: uuid.UUID | None
    source_evidence_id: uuid.UUID
    reviewed_baseline_revision_id: uuid.UUID | None
    risk_classification: str | None
    risk_algorithm_version: str | None
    allowed_transitions: tuple[str, ...]
    available_actions: tuple[str, ...]
    reason_codes: tuple[str, ...]
    operational_integrity_state: str
    created_at: datetime.datetime
    frozen_at: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class ProposalThread:
    proposal_id: uuid.UUID
    artifact_id: uuid.UUID
    latest_version: int
    versions: tuple[ProposalVersion, ...]


@dataclasses.dataclass(frozen=True)
class ProposalPage:
    items: tuple[ProposalVersion, ...]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


def _owning_scope(tenant_id: uuid.UUID | None) -> str:
    return "global" if tenant_id is None else "tenant"


def _operational_integrity_state(revision_id: uuid.UUID | None) -> str:
    """`unavailable` until a revision exists to report on; `pending` from
    materialisation onward until the checkpoint is durable (a later task's
    concern -- see this module's own docstring on the bijection)."""
    return "unavailable" if revision_id is None else "pending"


def _encode_cursor(row: queries.VersionRow) -> str:
    return f"{row.created_at.isoformat()}|{row.proposal_id}|{row.proposal_version}"


def _decode_cursor(cursor: str) -> tuple[datetime.datetime, uuid.UUID, int]:
    try:
        created_at_str, proposal_id_str, version_str = cursor.split("|")
        return (
            datetime.datetime.fromisoformat(created_at_str),
            uuid.UUID(proposal_id_str),
            int(version_str),
        )
    except (ValueError, TypeError) as exc:
        msg = "the cursor is not one this service issued"
        raise RegistryError(msg) from exc


def _require_reason(reason_code: str, note: str | None) -> None:
    if not reason_code or not reason_code.strip():
        msg = "reason_code is required"
        raise RegistryError(msg)
    if note is not None and len(note) > _REASON_NOTE_MAX_LEN:
        msg = f"note exceeds the {_REASON_NOTE_MAX_LEN}-character bound"
        raise RegistryError(msg)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProposalService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    # -- artifact families ----------------------------------------------------

    async def create_family(
        self,
        ctx: ArcRequestContext,
        *,
        slug: str,
        kind: str,
        owning_scope: str,
        target_tenant_id: uuid.UUID | None,
        title: str,
    ) -> ArtifactFamily:
        tenant_id = target_tenant_id if owning_scope == "tenant" else None
        self._authorization.assert_can_write_artifact(ctx, _scope(tenant_id))

        now = self._clock.now()
        artifact_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            try:
                await queries.insert_family(
                    session,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    slug=slug,
                    kind=kind,
                    title=title,
                    created_at=now,
                    created_by_issuer=ctx.oidc_issuer,
                    created_by_subject=ctx.oidc_subject,
                )
            except IntegrityError as exc:
                # The unique (scope, slug) index is what makes "one family
                # per slug per scope" real; a collision is a conflict, not
                # a crash.
                msg = f"an artifact family with slug {slug!r} already exists in this scope"
                raise ConflictError(msg) from exc
            # The requesting tenant, not the family's own scope: who acted,
            # matching `ArtifactService.activate_revision`'s own convention
            # for audit rows on artifacts and revisions that may be global.
            # `emit_global` exists for operations with no natural requesting
            # tenant at all (verifier enrollment, approval-trust
            # administration) -- this is not that; a tenant admin creating a
            # global family is still acting from a real tenant.
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_FAMILY_CREATED,
                payload={"artifact_id": str(artifact_id), "slug": slug, "kind": kind, "owning_scope": owning_scope},
            )

        return ArtifactFamily(
            artifact_id=artifact_id,
            slug=slug,
            kind=kind,
            owning_scope=owning_scope,
            target_tenant_id=tenant_id,
            title=title,
            active_revision_id=None,
            created_at=now,
            created_by_issuer=ctx.oidc_issuer,
            created_by_subject=ctx.oidc_subject,
        )

    async def get_family(self, ctx: ArcRequestContext, artifact_id: uuid.UUID) -> ArtifactFamily:
        async with self._session_factory() as session:
            family = await queries.load_family(session, artifact_id)
        if family is None:
            raise NotFoundError(f"artifact family {artifact_id} not found")
        self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
        return _family_result(family)

    # -- proposal threads and versions -----------------------------------------

    async def open_proposal(
        self,
        ctx: ArcRequestContext,
        *,
        artifact_id: uuid.UUID,
        source_evidence_id: uuid.UUID,
        reviewed_baseline_revision_id: uuid.UUID | None = None,
    ) -> ProposalVersion:
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            # Lock order: artifact-family row before proposal-version row.
            family = await queries.load_family_for_update(session, artifact_id)
            if family is None:
                raise NotFoundError(f"artifact family {artifact_id} not found")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))

            baseline = reviewed_baseline_revision_id
            if baseline is None:
                baseline = family.active_revision_id

            proposal_id = await queries.get_or_create_thread(session, artifact_id=artifact_id, created_at=now)
            await queries.lock_thread(session, proposal_id)

            latest = await queries.load_latest_version(session, proposal_id)
            if latest is not None and latest.state in NONTERMINAL_STATES:
                msg = f"proposal {proposal_id} already has a live version ({latest.proposal_version}, {latest.state!r})"
                raise ProposalStateConflict(msg)
            next_version = 1 if latest is None else latest.proposal_version + 1

            try:
                await queries.insert_version(
                    session,
                    proposal_id=proposal_id,
                    proposal_version=next_version,
                    artifact_id=artifact_id,
                    tenant_id=family.tenant_id,
                    source_evidence_id=source_evidence_id,
                    reviewed_baseline_revision_id=baseline,
                    opened_by_issuer=ctx.oidc_issuer,
                    opened_by_subject=ctx.oidc_subject,
                    created_at=now,
                )
            except IntegrityError as exc:
                # The partial unique index is the backstop this check above
                # should have made unreachable; if it still fires (two
                # concurrent openers both read no nonterminal version), this
                # resolves the loser exactly as the check would have.
                msg = f"a nonterminal version already exists for proposal {proposal_id}"
                raise ProposalStateConflict(msg) from exc

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_PROPOSAL_OPENED,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": next_version,
                    "artifact_id": str(artifact_id),
                    "source_evidence_id": str(source_evidence_id),
                },
            )
            version = await queries.load_version(session, proposal_id, next_version)
            if version is None:
                raise RegistryError(f"proposal version {proposal_id}/{next_version} vanished immediately after insert")
            can_write = self._authorization.can_write_artifact(ctx, _scope(family.tenant_id))
            return _version_result(version, can_write=can_write)

    async def get_thread(self, ctx: ArcRequestContext, proposal_id: uuid.UUID) -> ProposalThread:
        async with self._session_factory() as session:
            thread = await queries.load_thread(session, proposal_id)
            if thread is None:
                raise NotFoundError(f"proposal {proposal_id} not found")
            family = await queries.load_family(session, thread.artifact_id)
            if family is None:
                raise RegistryError(f"proposal {proposal_id} references a vanished artifact family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            can_write = self._authorization.can_write_artifact(ctx, _scope(family.tenant_id))
            versions = await queries.list_versions_for_thread(session, proposal_id)
        if not versions:
            raise RegistryError(f"proposal {proposal_id} has no versions")
        results = tuple(_version_result(v, can_write=can_write) for v in versions)
        return ProposalThread(
            proposal_id=proposal_id,
            artifact_id=thread.artifact_id,
            latest_version=results[-1].proposal_version,
            versions=results,
        )

    async def get_version(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> ProposalVersion:
        async with self._session_factory() as session:
            version = await queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            can_write = self._authorization.can_write_artifact(ctx, _scope(family.tenant_id))
        return _version_result(version, can_write=can_write)

    async def list_proposals(
        self,
        ctx: ArcRequestContext,
        tenant_id: uuid.UUID | None,
        *,
        artifact_id: uuid.UUID | None = None,
        state: str | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> ProposalPage:
        self._authorization.assert_can_read_artifact(ctx, _scope(tenant_id))
        can_write = self._authorization.can_write_artifact(ctx, _scope(tenant_id))
        cursor_created_at: datetime.datetime | None = None
        cursor_proposal_id: uuid.UUID | None = None
        cursor_proposal_version: int | None = None
        if cursor is not None:
            cursor_created_at, cursor_proposal_id, cursor_proposal_version = _decode_cursor(cursor)

        async with self._session_factory() as session:
            rows = await queries.list_versions(
                session,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                state=state,
                cursor_created_at=cursor_created_at,
                cursor_proposal_id=cursor_proposal_id,
                cursor_proposal_version=cursor_proposal_version,
                page_size=page_size,
            )
        items = tuple(_version_result(row, can_write=can_write) for row in rows)
        next_cursor = _encode_cursor(rows[-1]) if len(rows) == page_size and rows else None
        return ProposalPage(items=items, next_cursor=next_cursor)

    # -- transitions ------------------------------------------------------------

    async def withdraw(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        reason_code: str,
        note: str | None = None,
    ) -> ProposalVersion:
        # ADR 040: "open -> withdrawn: submitter before submission" -- the
        # one transition restricted to a specific identity rather than
        # generic write authority, so it is checked in addition to (not
        # instead of) the scope check every transition needs.
        return await self._transition(
            ctx,
            proposal_id,
            proposal_version,
            from_states=("open",),
            to_state="withdrawn",
            reason_code=reason_code,
            note=note,
            event_type=actions.ARC_PROPOSAL_WITHDRAWN,
            require_submitter=True,
        )

    async def reject(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        reason_code: str,
        note: str | None = None,
    ) -> ProposalVersion:
        return await self._transition(
            ctx,
            proposal_id,
            proposal_version,
            from_states=("submitted",),
            to_state="rejected",
            reason_code=reason_code,
            note=note,
            event_type=actions.ARC_PROPOSAL_REJECTED,
        )

    async def supersede(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        reason_code: str,
        note: str | None = None,
    ) -> ProposalVersion:
        return await self._transition(
            ctx,
            proposal_id,
            proposal_version,
            from_states=("submitted", "approved"),
            to_state="superseded",
            reason_code=reason_code,
            note=note,
            event_type=actions.ARC_PROPOSAL_SUPERSEDED,
        )

    async def _transition(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        from_states: Sequence[str],
        to_state: str,
        reason_code: str,
        note: str | None,
        event_type: str,
        require_submitter: bool = False,
    ) -> ProposalVersion:
        _require_reason(reason_code, note)
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            current = await queries.load_version(session, proposal_id, proposal_version)
            if current is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await queries.load_family(session, current.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
            submitter = (current.opened_by_issuer, current.opened_by_subject)
            if require_submitter and (ctx.oidc_issuer, ctx.oidc_subject) != submitter:
                msg = "only the proposal's submitter may withdraw it before submission"
                raise ArcAuthorizationError(msg)

            updated = await queries.transition_version(
                session,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                from_states=from_states,
                to_state=to_state,
                reason_code=reason_code,
                note=note,
                actor_issuer=ctx.oidc_issuer,
                actor_subject=ctx.oidc_subject,
                now=now,
            )
            if updated is None:
                # Distinguish "wrong state" from "vanished between the reads
                # above and now" only for the message -- both are the same
                # refusal code.
                msg = f"proposal version {proposal_id}/{proposal_version} is not in a state this transition accepts"
                raise ProposalStateConflict(msg)

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=event_type,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "from_state": current.state,
                    "to_state": to_state,
                    "reason_code": reason_code,
                },
            )
            can_write = True  # the caller just proved write authority above
            return _version_result(updated, can_write=can_write)


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _family_result(family: queries.FamilyRow) -> ArtifactFamily:
    return ArtifactFamily(
        artifact_id=family.artifact_id,
        slug=family.slug,
        kind=family.kind,
        owning_scope=_owning_scope(family.tenant_id),
        target_tenant_id=family.tenant_id,
        title=family.title,
        active_revision_id=family.active_revision_id,
        created_at=family.created_at,
        created_by_issuer=family.created_by_issuer,
        created_by_subject=family.created_by_subject,
    )


def _version_result(version: queries.VersionRow, *, can_write: bool) -> ProposalVersion:
    available = _AVAILABLE_ACTIONS[version.state] if can_write else ()
    return ProposalVersion(
        proposal_id=version.proposal_id,
        proposal_version=version.proposal_version,
        artifact_id=version.artifact_id,
        state=version.state,
        revision_id=version.revision_id,
        source_evidence_id=version.source_evidence_id,
        reviewed_baseline_revision_id=version.reviewed_baseline_revision_id,
        risk_classification=version.risk_classification,
        risk_algorithm_version=version.risk_algorithm_version,
        allowed_transitions=_ALLOWED_TRANSITIONS[version.state],
        available_actions=available,
        reason_codes=_REASON_CODES,
        operational_integrity_state=_operational_integrity_state(version.revision_id),
        created_at=version.created_at,
        frozen_at=version.frozen_at,
    )


__all__ = [
    "NONTERMINAL_STATES",
    "ArtifactFamily",
    "ProposalPage",
    "ProposalService",
    "ProposalStateConflict",
    "ProposalThread",
    "ProposalVersion",
]
