"""Registering a source grant, and withdrawing one.

Split from `source_admission.py`, which held both this and the admission path
and crossed the 800-line ceiling when withdrawal arrived. The seam is not the
line count: **registering a grant is an operator action on configuration, and
admitting material is a runtime action that uses it.** Different actor, different
frequency, and — until E14-T2 — different completeness, because the grant
lifecycle was missing its second half entirely.

## What a withdrawal reaches

Forward only, and that is the decision rather than an omission. Material already
admitted through a grant was *validly* admitted: the grant was in force at the
time, and rewriting that would make the record describe a history that did not
happen. `revoked_at` is what lets an auditor place any admission on one side of
it or the other.

The codebase already made this move twice — a revoked ARC revision tombstones
rather than disappearing, and a withheld receipt is marked rather than deleted.
Both say "this was fine then and is not now" without editing the past.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.queries import source_admission as queries
from contextplane.arc.service.source_admission import (
    ConnectorRegistration,
    UploadPolicyRegistration,
    _scope,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError, ValidationError
from contextplane.types import Clock


class SourceGrantService:
    """The lifecycle of a source connector and an upload policy: both halves."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        authorization: ArcAuthorizationService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._authorization = authorization

    async def register_connector(
        self, ctx: ArcRequestContext, registration: ConnectorRegistration
    ) -> queries.ConnectorRow:
        registered_at = self._clock.now()
        async with self._session_factory() as session, session.begin():
            existing = await queries.load_connector(session, registration.connector_id)
            if existing is not None:
                raise ConflictError(f"connector {registration.connector_id!r} is already registered")
            await queries.insert_connector(
                session,
                connector_id=registration.connector_id,
                owning_scope=registration.owning_scope,
                tenant_id=registration.tenant_id,
                allowed_schemes=list(registration.allowed_schemes),
                allowed_hosts=list(registration.allowed_hosts),
                allowed_media_types=list(registration.allowed_media_types),
                allowed_verifier_ids=list(registration.allowed_verifier_ids),
                max_bytes=registration.max_bytes,
                credential_ref=registration.credential_ref,
                registered_at=registered_at,
            )
            # Read back inside the same transaction — the row is already
            # visible to this session's own connection, so no second
            # round-trip after commit is needed.
            row = await queries.load_connector(session, registration.connector_id)
            if row is None:
                raise RegistryError(f"connector {registration.connector_id!r} vanished immediately after insert")
            return row

    #: The floor a revocation reason has to clear, matching the database CHECK.
    #: Refused here first so a caller gets a sentence naming the field rather
    #: than a constraint violation.
    MIN_REVOCATION_REASON = 20

    async def revoke_connector(self, ctx: ArcRequestContext, *, connector_id: str, reason: str) -> queries.ConnectorRow:
        """Withdraw a connector. It admits nothing further; what it admitted stands.

        **Revocation reaches forward only, and that is the decision rather than
        an omission.** Material already admitted through this connector was
        validly admitted — the grant was in force at the time — and rewriting
        that would make the record describe a history that did not happen. The
        `revoked_at` instant is what lets an auditor place any admission on one
        side of it or the other, which is the same move a tombstoned revision and
        a withheld receipt both make: say "this was fine then and is not now"
        without editing the past.

        Refuses a second revocation rather than overwriting. The first decision
        and the first reason are the ones somebody acted on.
        """
        cleaned = reason.strip()
        if len(cleaned) < self.MIN_REVOCATION_REASON:
            raise ValidationError(
                f"a revocation reason must be at least {self.MIN_REVOCATION_REASON} characters; "
                "a withdrawn grant with no stated cause is unreviewable afterwards"
            )
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            existing = await queries.load_connector(session, connector_id)
            if existing is None:
                raise NotFoundError(f"unknown connector {connector_id!r}")
            self._authorization.assert_can_write_artifact(ctx, _scope(existing.owning_scope, existing.tenant_id))
            changed = await queries.revoke_connector(
                session, connector_id=connector_id, actor_id=ctx.actor_id, reason=cleaned, now=now
            )
            if not changed:
                raise ConflictError(
                    f"connector {connector_id!r} was already withdrawn; "
                    "a second withdrawal would replace the first reason"
                )
            row = await queries.load_connector(session, connector_id)
            if row is None:
                raise RegistryError(f"connector {connector_id!r} vanished during revocation")
            return row

    async def revoke_upload_policy(
        self, ctx: ArcRequestContext, *, policy_id: str, reason: str
    ) -> queries.UploadPolicyRow:
        """Withdraw an upload policy. Same rule, same reach — see `revoke_connector`."""
        cleaned = reason.strip()
        if len(cleaned) < self.MIN_REVOCATION_REASON:
            raise ValidationError(
                f"a revocation reason must be at least {self.MIN_REVOCATION_REASON} characters; "
                "a withdrawn grant with no stated cause is unreviewable afterwards"
            )
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            existing = await queries.load_upload_policy(session, policy_id)
            if existing is None:
                raise NotFoundError(f"unknown upload policy {policy_id!r}")
            self._authorization.assert_can_write_artifact(ctx, _scope(existing.owning_scope, existing.tenant_id))
            changed = await queries.revoke_upload_policy(
                session, policy_id=policy_id, actor_id=ctx.actor_id, reason=cleaned, now=now
            )
            if not changed:
                raise ConflictError(
                    f"upload policy {policy_id!r} was already withdrawn; "
                    "a second withdrawal would replace the first reason"
                )
            row = await queries.load_upload_policy(session, policy_id)
            if row is None:
                raise RegistryError(f"upload policy {policy_id!r} vanished during revocation")
            return row

    async def register_upload_policy(
        self, ctx: ArcRequestContext, registration: UploadPolicyRegistration
    ) -> queries.UploadPolicyRow:
        registered_at = self._clock.now()
        async with self._session_factory() as session, session.begin():
            existing = await queries.load_upload_policy(session, registration.policy_id)
            if existing is not None:
                raise ConflictError(f"upload policy {registration.policy_id!r} is already registered")
            await queries.insert_upload_policy(
                session,
                policy_id=registration.policy_id,
                owning_scope=registration.owning_scope,
                tenant_id=registration.tenant_id,
                allowed_media_types=list(registration.allowed_media_types),
                allowed_verifier_ids=list(registration.allowed_verifier_ids),
                max_bytes=registration.max_bytes,
                registered_at=registered_at,
            )
            row = await queries.load_upload_policy(session, registration.policy_id)
            if row is None:
                raise RegistryError(f"upload policy {registration.policy_id!r} vanished immediately after insert")
            return row
