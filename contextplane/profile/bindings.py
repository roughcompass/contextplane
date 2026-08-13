"""Which profile governs a tenant, over which interval, and how it moves.

A binding is the only row in this area meant to change, and every later
validator resolves through it: "what may this tenant write?" is answered by
finding its active binding and compiling what that binding names. Three things
follow, and they shape the whole module.

**The tenant is never a parameter the caller chooses.** Every function here
takes `tenant_id` from the authenticated context its router resolved. A request
body that could name a tenant — or omit one and have a default filled in —
would let a caller be governed by somebody else's profile, which is the
bypass this design exists to prevent. There is no code path here that reads a
tenant from a payload.

**One active binding per tenant, enforced by the database.** The exclusion
constraint `ex_profile_bindings_one_active_per_tenant` refuses two `active`
rows whose effective intervals overlap. This module closes the outgoing
binding's interval in the same transaction that opens the incoming one, so the
constraint is a backstop for concurrency rather than the primary mechanism —
but it is the thing that makes two simultaneous activations impossible rather
than merely unlikely.

**A rollback restores prior behaviour without erasing what happened under the
new binding.** Rolling back reopens the previous binding and marks the current
one `rolled_back`; it does not delete rows, and it does not touch identifiers,
aliases or mappings that were expanded while the newer profile was active.
Those expansions are data the tenant now owns, and a rollback that reclaimed
them would lose writes rather than restore behaviour.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock

#: The states the database CHECK constraint admits, and the only transitions
#: this service performs. Spelled as a map rather than as scattered `if`s so
#: the machine is readable in one place -- a transition table that lives in six
#: call sites is one nobody can audit.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"validating", "retired"}),
    "validating": frozenset({"active", "retired"}),
    "active": frozenset({"rollback_pending", "retired"}),
    "rollback_pending": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
    "retired": frozenset(),
}


class BindingError(RuntimeError):
    """A binding cannot move as asked."""


class InvalidTransition(BindingError):
    """The requested transition is not one this state may take.

    Carries both states because "cannot activate" is unactionable on its own:
    the caller needs to know it is sitting on `planned` and owes a validation
    pass first.
    """

    def __init__(self, *, binding_id: uuid.UUID, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        allowed = ", ".join(sorted(_ALLOWED_TRANSITIONS.get(current, frozenset()))) or "nothing"
        super().__init__(
            f"binding {binding_id} is {current!r} and cannot become {requested!r}; "
            f"from {current!r} it may become: {allowed}"
        )


class BindingNotFound(BindingError):
    """No such binding for this tenant.

    Deliberately not distinguished from "exists but belongs to another tenant".
    Every lookup here is tenant-scoped, and an error that told a caller a
    binding exists under someone else's tenant would answer a question it is
    not entitled to ask.
    """


class RollbackNotReady(BindingError):
    """A rollback was requested against a target nobody has checked.

    `rollback_ready` is a separate column from `rollback_target_binding_id`
    precisely so this case is representable: a target with no readiness is a
    plan, not a rollback.
    """


class ConcurrentActivation(BindingError):
    """Two activations raced and the database refused the second."""


@dataclasses.dataclass(frozen=True)
class Binding:
    """One binding row, as this service hands it out."""

    binding_id: uuid.UUID
    tenant_id: uuid.UUID
    profile_revision_id: uuid.UUID
    extension_set_digest: str
    state: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    migration_run_id: uuid.UUID | None
    rollback_target_binding_id: uuid.UUID | None
    rollback_ready: bool
    actor: str
    reason: str
    audit_reference: str | None
    recorded_at: datetime.datetime


def extension_set_digest(extension_revision_ids: Sequence[uuid.UUID]) -> str:
    """A stable digest over the *set* of extensions a binding activates.

    Sorted before hashing, so the digest is a property of which extensions are
    bound and not of the order a caller happened to list them in. Two bindings
    naming the same extensions must produce the same digest or the value is
    useless for deciding whether a tenant's governance actually changed.

    The empty set has a digest too, rather than being NULL or an empty string:
    "this tenant is bound to core with no extensions" is a real, checkable
    configuration, and giving it a value means the column never has to be read
    as three-valued.
    """
    ordered = sorted(str(identifier) for identifier in set(extension_revision_ids))
    return hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()


class BindingService:
    """Plans, validates, activates and rolls back tenant profile bindings."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- Planning ---------------------------------------------------------

    async def plan_binding(
        self,
        *,
        tenant_id: uuid.UUID,
        profile_revision_id: uuid.UUID,
        extension_revision_ids: Sequence[uuid.UUID] = (),
        effective_from: datetime.datetime,
        actor: str,
        reason: str,
        audit_reference: str | None = None,
        migration_run_id: uuid.UUID | None = None,
    ) -> Binding:
        """Draft a binding without governing anything yet.

        Several `planned` bindings may exist over the same window -- the
        exclusion constraint deliberately applies only to `active` -- because
        drafting alternatives is a normal part of planning a migration, and
        only promotion has to be exclusive.
        """
        binding_id = uuid.uuid4()
        now = self._clock.now()
        digest = extension_set_digest(extension_revision_ids)

        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO profile_bindings ("
                    "  binding_id, tenant_id, profile_revision_id, extension_set_digest, state,"
                    "  effective_from, effective_to, migration_run_id,"
                    "  rollback_target_binding_id, rollback_ready,"
                    "  actor, reason, audit_reference, recorded_at"
                    ") VALUES (:bid, :tenant, :revision, :digest, 'planned',"
                    "          :effective_from, NULL, :migration_run,"
                    "          :rollback_target, FALSE,"
                    "          :actor, :reason, :audit, :now)"
                ),
                {
                    "bid": binding_id,
                    "tenant": tenant_id,
                    "revision": profile_revision_id,
                    "digest": digest,
                    "effective_from": effective_from,
                    "migration_run": migration_run_id,
                    "rollback_target": await self._current_active_id(session, tenant_id=tenant_id),
                    "actor": actor,
                    "reason": reason,
                    "audit": audit_reference,
                    "now": now,
                },
            )
            await session.commit()

        binding = await self.get_binding(tenant_id=tenant_id, binding_id=binding_id)
        if binding is None:  # pragma: no cover - the row was just committed
            msg = f"binding {binding_id} vanished immediately after insert"
            raise BindingError(msg)
        return binding

    # -- Transitions ------------------------------------------------------

    async def start_validation(
        self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID, actor: str, reason: str
    ) -> Binding:
        """`planned` -> `validating`. Still governs nothing."""
        return await self._transition(
            tenant_id=tenant_id, binding_id=binding_id, requested="validating", actor=actor, reason=reason
        )

    async def activate(
        self,
        *,
        tenant_id: uuid.UUID,
        binding_id: uuid.UUID,
        actor: str,
        reason: str,
        audit_reference: str | None = None,
    ) -> Binding:
        """`validating` -> `active`, closing whatever was active before.

        Both writes happen in one transaction. Closing the old interval in a
        separate transaction would leave a window with either two active
        bindings or none, and "none" is worse than it sounds: a governed write
        arriving in that window resolves no binding at all.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            current = await self._load(session, tenant_id=tenant_id, binding_id=binding_id)
            _require_transition(binding_id=binding_id, current=current.state, requested="active")

            outgoing = await self._current_active_id(session, tenant_id=tenant_id)
            if outgoing is not None:
                await session.execute(
                    text(
                        "UPDATE profile_bindings SET effective_to = :now, state = 'retired' "
                        " WHERE binding_id = :bid AND tenant_id = :tenant"
                    ),
                    {"now": now, "bid": outgoing, "tenant": tenant_id},
                )

            try:
                await session.execute(
                    text(
                        "UPDATE profile_bindings"
                        "   SET state = 'active', effective_from = :now, effective_to = NULL,"
                        "       rollback_target_binding_id = :rollback_target,"
                        "       rollback_ready = :ready,"
                        "       actor = :actor, reason = :reason, audit_reference = :audit,"
                        "       recorded_at = :now"
                        " WHERE binding_id = :bid AND tenant_id = :tenant"
                    ),
                    {
                        "now": now,
                        "bid": binding_id,
                        "tenant": tenant_id,
                        "rollback_target": outgoing,
                        # A rollback is only ready when there is somewhere to
                        # roll back *to*. The first binding a tenant ever
                        # activates has no predecessor, and claiming readiness
                        # for it would be a promise nothing could keep.
                        "ready": outgoing is not None,
                        "actor": actor,
                        "reason": reason,
                        "audit": audit_reference,
                    },
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                msg = (
                    f"another binding for tenant {tenant_id} became active over an overlapping "
                    "interval; the database refused a second one"
                )
                raise ConcurrentActivation(msg) from error

        return await self._must_get(tenant_id=tenant_id, binding_id=binding_id)

    async def begin_rollback(self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID, actor: str, reason: str) -> Binding:
        """`active` -> `rollback_pending`, refusing an unchecked target."""
        async with self._session_factory() as session:
            current = await self._load(session, tenant_id=tenant_id, binding_id=binding_id)
            _require_transition(binding_id=binding_id, current=current.state, requested="rollback_pending")
            if current.rollback_target_binding_id is None or not current.rollback_ready:
                msg = (
                    f"binding {binding_id} has no ready rollback target; a target without readiness "
                    "is a plan nobody has checked, and rolling back onto one restores nothing"
                )
                raise RollbackNotReady(msg)

        return await self._transition(
            tenant_id=tenant_id, binding_id=binding_id, requested="rollback_pending", actor=actor, reason=reason
        )

    async def complete_rollback(
        self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID, actor: str, reason: str
    ) -> Binding:
        """`rollback_pending` -> `rolled_back`, reopening the target.

        What is restored is *governance*: the target binding becomes active
        again and subsequent writes are validated against it. What is not
        touched is anything expanded while this binding was active -- ids,
        aliases and mappings created under the newer profile stay exactly where
        they are. Reclaiming them would be losing writes, not restoring
        behaviour, and a tenant that rolled back a profile did not ask to lose
        the data it recorded meanwhile.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            current = await self._load(session, tenant_id=tenant_id, binding_id=binding_id)
            _require_transition(binding_id=binding_id, current=current.state, requested="rolled_back")
            target_id = current.rollback_target_binding_id
            if target_id is None:
                msg = f"binding {binding_id} is rolling back with no target recorded"
                raise RollbackNotReady(msg)

            await session.execute(
                text(
                    "UPDATE profile_bindings"
                    "   SET state = 'rolled_back', effective_to = :now,"
                    "       actor = :actor, reason = :reason, recorded_at = :now"
                    " WHERE binding_id = :bid AND tenant_id = :tenant"
                ),
                {"now": now, "bid": binding_id, "tenant": tenant_id, "actor": actor, "reason": reason},
            )
            try:
                await session.execute(
                    text(
                        "UPDATE profile_bindings"
                        "   SET state = 'active', effective_from = :now, effective_to = NULL,"
                        "       actor = :actor, reason = :reason, recorded_at = :now"
                        " WHERE binding_id = :target AND tenant_id = :tenant"
                    ),
                    {"now": now, "target": target_id, "tenant": tenant_id, "actor": actor, "reason": reason},
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                msg = f"restoring binding {target_id} would overlap another active binding for tenant {tenant_id}"
                raise ConcurrentActivation(msg) from error

        return await self._must_get(tenant_id=tenant_id, binding_id=binding_id)

    # -- Reads ------------------------------------------------------------

    async def active_binding(self, *, tenant_id: uuid.UUID) -> Binding | None:
        """The binding governing this tenant now, if any."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(f"{_SELECT} WHERE tenant_id = :tenant AND state = 'active' ORDER BY effective_from DESC LIMIT 1"),
                {"tenant": tenant_id},
            )
            row = result.first()
        return _row_to_binding(row) if row is not None else None

    async def get_binding(self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID) -> Binding | None:
        """One binding of this tenant's, in whatever state it currently holds."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(f"{_SELECT} WHERE binding_id = :bid AND tenant_id = :tenant"),
                {"bid": binding_id, "tenant": tenant_id},
            )
            row = result.first()
        return _row_to_binding(row) if row is not None else None

    # -- Internals --------------------------------------------------------

    async def _must_get(self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID) -> Binding:
        binding = await self.get_binding(tenant_id=tenant_id, binding_id=binding_id)
        if binding is None:
            raise BindingNotFound(f"no binding {binding_id} for this tenant")
        return binding

    async def _load(self, session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: uuid.UUID) -> Binding:
        result = await session.execute(
            text(f"{_SELECT} WHERE binding_id = :bid AND tenant_id = :tenant"),
            {"bid": binding_id, "tenant": tenant_id},
        )
        row = result.first()
        if row is None:
            raise BindingNotFound(f"no binding {binding_id} for this tenant")
        return _row_to_binding(row)

    async def _current_active_id(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> uuid.UUID | None:
        result = await session.execute(
            text(
                "SELECT binding_id FROM profile_bindings"
                " WHERE tenant_id = :tenant AND state = 'active'"
                " ORDER BY effective_from DESC LIMIT 1"
            ),
            {"tenant": tenant_id},
        )
        row = result.first()
        return uuid.UUID(str(row[0])) if row is not None else None

    async def _transition(
        self, *, tenant_id: uuid.UUID, binding_id: uuid.UUID, requested: str, actor: str, reason: str
    ) -> Binding:
        now = self._clock.now()
        async with self._session_factory() as session:
            current = await self._load(session, tenant_id=tenant_id, binding_id=binding_id)
            _require_transition(binding_id=binding_id, current=current.state, requested=requested)
            await session.execute(
                text(
                    "UPDATE profile_bindings"
                    "   SET state = :state, actor = :actor, reason = :reason, recorded_at = :now"
                    " WHERE binding_id = :bid AND tenant_id = :tenant"
                ),
                {
                    "state": requested,
                    "actor": actor,
                    "reason": reason,
                    "now": now,
                    "bid": binding_id,
                    "tenant": tenant_id,
                },
            )
            await session.commit()
        return await self._must_get(tenant_id=tenant_id, binding_id=binding_id)


_SELECT = (
    "SELECT binding_id, tenant_id, profile_revision_id, extension_set_digest, state,"
    "       effective_from, effective_to, migration_run_id,"
    "       rollback_target_binding_id, rollback_ready,"
    "       actor, reason, audit_reference, recorded_at"
    "  FROM profile_bindings"
)


def _require_transition(*, binding_id: uuid.UUID, current: str, requested: str) -> None:
    if requested not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(binding_id=binding_id, current=current, requested=requested)


def _row_to_binding(row: Row[Any]) -> Binding:
    values: tuple[Any, ...] = tuple(row)
    return Binding(
        binding_id=values[0],
        tenant_id=values[1],
        profile_revision_id=values[2],
        extension_set_digest=values[3],
        state=values[4],
        effective_from=values[5],
        effective_to=values[6],
        migration_run_id=values[7],
        rollback_target_binding_id=values[8],
        rollback_ready=values[9],
        actor=values[10],
        reason=values[11],
        audit_reference=values[12],
        recorded_at=values[13],
    )


__all__ = [
    "Binding",
    "BindingError",
    "BindingNotFound",
    "BindingService",
    "ConcurrentActivation",
    "InvalidTransition",
    "RollbackNotReady",
    "extension_set_digest",
]
