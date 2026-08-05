"""Challenge-nonce derivation.

A challenge's nonce is never stored. Only its digest is, and the raw value is
re-derived on demand from the challenge's immutable ID under a versioned key.

That indirection buys a specific property. An exact challenge retry has to return
the *same* nonce — a caller that retries with an identical payload must not get a
second, different challenge — but storing a recoverable nonce to satisfy that
would put the secret at rest in the table, where a database read is enough to
forge an attestation. Deriving instead means the table holds only a digest, and
reproducing a nonce additionally requires the derivation key.

Rotation is why the key ID is stored alongside the digest: a nonce derived under
a retired key must stay reproducible for the five-minute challenge window plus
clock skew, so a rotation cannot invalidate challenges already in flight.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc import metrics
from registry.arc.models import ArcContextChallenge
from registry.arc.service.signing import (
    KeyPurpose,
    KeyRecord,
    KeyUnavailableError,
    PurposeBoundKeyProvider,
)
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, RegistryError
from registry.types import Clock

# Versioned so a derivation change is explicit rather than silently producing
# different nonces for the same inputs.
NONCE_DERIVATION_PROFILE = "arc_challenge_nonce_v1"

NONCE_BYTES = 32

# The challenge lifetime, plus the skew allowance a retired derivation key must
# remain available for. A rotation must not invalidate in-flight challenges.
CHALLENGE_TTL = datetime.timedelta(minutes=5)
ROTATION_SKEW_ALLOWANCE = datetime.timedelta(minutes=1)
RETIRED_KEY_RETENTION = CHALLENGE_TTL + ROTATION_SKEW_ALLOWANCE


def nonce_digest(nonce: bytes) -> str:
    """The value stored in `arc_context_challenges.arc_nonce_digest`."""
    return hashlib.sha256(nonce).hexdigest()


class ChallengeNonceDeriver(PurposeBoundKeyProvider):
    """Derives a challenge nonce from its immutable ID under a versioned key.

    Deterministic by design: the same challenge ID and bindings under the same key
    always yield the same nonce, which is what makes exact retry work without
    persisting the secret.
    """

    purpose = KeyPurpose.CHALLENGE_NONCE_DERIVATION

    def __init__(
        self,
        secrets: dict[str, bytes],
        *,
        active_key_id: str | None,
        records: dict[str, KeyRecord] | None = None,
    ) -> None:
        self._secrets = secrets
        self._active_key_id = active_key_id
        self._records = records or {}

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        if self._active_key_id is None:
            msg = (
                "no active ARC challenge-nonce derivation key is configured; "
                "refusing to issue challenges whose nonce cannot be reproduced"
            )
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def derive(
        self,
        challenge_id: uuid.UUID,
        *,
        host_id: str,
        session_id: str,
        manifest_claims_digest: str,
        key_id: str | None = None,
    ) -> bytes:
        """Derive the nonce for a challenge.

        The authenticated bindings are part of the derivation input, not just of
        the surrounding row. A nonce that depended on the challenge ID alone would
        be identical across two challenges that differ only in host or claims,
        which is exactly the substitution the binding exists to prevent.
        """
        resolved = key_id or self.active_key_id
        secret = self._secrets.get(resolved)
        if secret is None:
            msg = (
                f"no derivation secret for key {resolved!r}; a nonce derived under "
                "a key that is no longer held cannot be reproduced"
            )
            raise KeyUnavailableError(msg)

        # Length-prefixed so no two different field splits can produce the same
        # byte string. Plain concatenation would let ("ab", "c") and ("a", "bc")
        # collide.
        parts = (
            NONCE_DERIVATION_PROFILE.encode("ascii"),
            challenge_id.bytes,
            host_id.encode("utf-8"),
            session_id.encode("utf-8"),
            manifest_claims_digest.encode("ascii"),
        )
        message = b"".join(len(p).to_bytes(4, "big") + p for p in parts)
        return hmac.new(secret, message, hashlib.sha256).digest()[:NONCE_BYTES]

    def retired_key_is_still_required(self, retired_at: datetime.datetime, now: datetime.datetime) -> bool:
        """Whether a rotated-out key must still be held to reproduce nonces.

        Dropping it sooner breaks exact retry for challenges issued just before
        the rotation.
        """
        return now - retired_at <= RETIRED_KEY_RETENTION


def idempotency_key_digest(idempotency_key: str) -> str:
    """The value stored in `arc_context_challenges.idempotency_key_digest`.

    Only the digest is persisted. The raw key is a caller-chosen string that
    may carry meaning to the caller (a request UUID, a trace ID); there is no
    reason to keep a recoverable copy of it at rest.
    """
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class IssuedChallenge:
    """What issuance hands back, whether freshly created or resumed by retry."""

    challenge_id: uuid.UUID
    arc_nonce: bytes
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    manifest_claims_digest: str


class ChallengeValidationError(RegistryError):
    """A presented challenge failed a single-use, binding, or freshness check.

    Deliberately one type for all six checks (missing, consumed, expired, and
    a mismatch on host, session, or claims digest). The caller's response is
    identical either way -- abort without a receipt -- so the difference
    between them matters only for the message, not for control flow.
    """


class ChallengeConsumptionError(RegistryError):
    """Consuming a challenge affected a row count other than exactly one.

    Zero means it was already consumed by the time this ran despite the
    caller holding the `FOR UPDATE` lock from validation -- a correctness bug
    in the caller's transaction handling, not a business outcome to recover
    from. The caller's transaction must not commit past this.
    """


@dataclasses.dataclass(frozen=True)
class ValidatedChallenge:
    """A challenge that has passed every check and is locked in the caller's
    ambient transaction.

    Carries no nonce -- validation is the last thing that needs it. Consuming
    the row (`consumed_at`) is a separate step so it can happen atomically
    with receipt creation rather than here.
    """

    challenge_id: uuid.UUID
    tenant_id: uuid.UUID
    host_id: str
    session_id: str
    manifest_claims_digest: str


class ChallengeService:
    """Issues ARC context challenges under the `(host, session, key)` identity.

    Each call acquires its own session, matching the session-per-call pattern
    used across the service layer rather than holding one open across a
    request.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        deriver: ChallengeNonceDeriver,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._deriver = deriver
        self._clock = clock

    async def issue_challenge(
        self,
        ctx: ArcRequestContext,
        *,
        session_id: str,
        manifest_claims_digest: str,
        idempotency_key: str,
    ) -> IssuedChallenge:
        """Issue a challenge, or resume the one an identical retry already made.

        `(tenant_id, host_id, session_id, idempotency_key)` is a permanent
        identity — the database's unique index never expires it, so a second
        insert under the same key is never possible, not even after the
        original challenge's five minutes have passed. That makes resumption
        rather than reissuance the only path once a row exists:

        - Same claims digest: rederive the nonce under whichever key the
          original was issued under (not necessarily the currently active
          one, so a rotation between the two calls can't break the retry) and
          return it. This is unconditional on expiry — issuance's job is only
          to answer "does this key already identify a challenge," not "is it
          still usable." A caller that retries after the window closed gets
          back a truthful, already-expired challenge, and consumption is
          where "too late" is actually enforced.
        - Different claims digest: the caller is reusing a key for what is,
          semantically, a different request. That is exactly what an
          idempotency key exists to catch, so it is never resolved
          automatically.
        """
        if ctx.host_id is None:
            msg = "challenge issuance requires an authenticated host identity"
            raise ValueError(msg)
        host_id = ctx.host_id
        key_digest = idempotency_key_digest(idempotency_key)

        async with self._session_factory() as session:
            existing = await self._find_by_key(
                session,
                tenant_id=ctx.tenant_id,
                host_id=host_id,
                session_id=session_id,
                key_digest=key_digest,
            )
            if existing is not None:
                return self._resume(
                    existing, host_id=host_id, session_id=session_id, manifest_claims_digest=manifest_claims_digest
                )

            challenge_id = uuid.uuid4()
            key_id = self._deriver.active_key_id
            nonce = self._deriver.derive(
                challenge_id,
                host_id=host_id,
                session_id=session_id,
                manifest_claims_digest=manifest_claims_digest,
                key_id=key_id,
            )
            issued_at = self._clock.now()
            expires_at = issued_at + CHALLENGE_TTL

            session.add(
                ArcContextChallenge(
                    challenge_id=challenge_id,
                    tenant_id=ctx.tenant_id,
                    host_id=host_id,
                    session_id=session_id,
                    manifest_claims_digest=manifest_claims_digest,
                    arc_nonce_digest=nonce_digest(nonce),
                    nonce_derivation_key_id=key_id,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    idempotency_key_digest=key_digest,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # Either a concurrent request won the same race, or a bound
                # column rejected the input (e.g. an over-length host_id). The
                # two are distinguished by whether the row now exists: if it
                # does, resolve against the winner exactly as a sequential
                # retry would; if not, this was never an idempotency race and
                # the original error is the real one.
                await session.rollback()
                existing = await self._find_by_key(
                    session,
                    tenant_id=ctx.tenant_id,
                    host_id=host_id,
                    session_id=session_id,
                    key_digest=key_digest,
                )
                if existing is None:
                    raise
                return self._resume(
                    existing, host_id=host_id, session_id=session_id, manifest_claims_digest=manifest_claims_digest
                )

            # Counted only on a genuinely new challenge. A resumed retry is
            # the same challenge, and counting it again would make the
            # issued-vs-consumed ratio look like leakage that is not there.
            metrics.observe_challenge_issued()
            return IssuedChallenge(
                challenge_id=challenge_id,
                arc_nonce=nonce,
                issued_at=issued_at,
                expires_at=expires_at,
                manifest_claims_digest=manifest_claims_digest,
            )

    def _resume(
        self,
        existing: ArcContextChallenge,
        *,
        host_id: str,
        session_id: str,
        manifest_claims_digest: str,
    ) -> IssuedChallenge:
        if existing.manifest_claims_digest != manifest_claims_digest:
            msg = (
                f"idempotency key already identifies a challenge for host={host_id!r} "
                f"session={session_id!r} with a different manifest claims digest"
            )
            raise ConflictError(msg)

        nonce = self._deriver.derive(
            existing.challenge_id,
            host_id=host_id,
            session_id=session_id,
            manifest_claims_digest=manifest_claims_digest,
            key_id=existing.nonce_derivation_key_id,
        )
        return IssuedChallenge(
            challenge_id=existing.challenge_id,
            arc_nonce=nonce,
            issued_at=existing.issued_at,
            expires_at=existing.expires_at,
            manifest_claims_digest=existing.manifest_claims_digest,
        )

    async def _find_by_key(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        host_id: str,
        session_id: str,
        key_digest: str,
    ) -> ArcContextChallenge | None:
        stmt = select(ArcContextChallenge).where(
            ArcContextChallenge.tenant_id == tenant_id,
            ArcContextChallenge.host_id == host_id,
            ArcContextChallenge.session_id == session_id,
            ArcContextChallenge.idempotency_key_digest == key_digest,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def validate_challenge(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        host_id: str,
        session_id: str,
        manifest_claims_digest: str,
        arc_nonce: bytes,
    ) -> ValidatedChallenge:
        """Validate and lock the challenge the presented nonce identifies.

        Takes the caller's own session rather than opening one, and must run
        inside that caller's open transaction: `FOR UPDATE` only delivers the
        single-use guarantee if the lock it takes is held across the
        resolution attempt that follows, not released immediately after this
        call returns.

        Scoping the lookup to `tenant_id` in the query itself, rather than
        fetching by nonce digest alone and comparing after, means a
        cross-tenant nonce never locks a row that belongs to another
        tenant -- it is indistinguishable from no challenge existing at all.

        Does not set `consumed_at`; call `consume_challenge` for that, inside
        the same transaction as the receipt it is consumed for.
        """
        now = self._clock.now()
        digest = nonce_digest(arc_nonce)
        stmt = (
            select(ArcContextChallenge)
            .where(ArcContextChallenge.tenant_id == tenant_id, ArcContextChallenge.arc_nonce_digest == digest)
            .with_for_update()
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            msg = "no challenge matches the presented nonce for this tenant"
            raise ChallengeValidationError(msg)
        if row.consumed_at is not None:
            msg = f"challenge {row.challenge_id} was already consumed"
            raise ChallengeValidationError(msg)
        if row.expires_at <= now:
            msg = f"challenge {row.challenge_id} expired at {row.expires_at.isoformat()}"
            raise ChallengeValidationError(msg)
        if row.host_id != host_id:
            msg = f"challenge {row.challenge_id} is bound to a different host"
            raise ChallengeValidationError(msg)
        if row.session_id != session_id:
            msg = f"challenge {row.challenge_id} is bound to a different session"
            raise ChallengeValidationError(msg)
        if row.manifest_claims_digest != manifest_claims_digest:
            msg = f"challenge {row.challenge_id} is bound to a different manifest claims digest"
            raise ChallengeValidationError(msg)

        return ValidatedChallenge(
            challenge_id=row.challenge_id,
            tenant_id=row.tenant_id,
            host_id=row.host_id,
            session_id=row.session_id,
            manifest_claims_digest=row.manifest_claims_digest,
        )

    async def consume_challenge(self, session: AsyncSession, challenge_id: uuid.UUID) -> None:
        """Mark a validated challenge consumed.

        Executes the `UPDATE` against the caller's session without
        committing -- the caller commits once, together with the receipt
        this consumption belongs to, so the two either land together or not
        at all. Requires exactly one affected row.
        """
        now = self._clock.now()
        result = await session.execute(
            update(ArcContextChallenge)
            .where(ArcContextChallenge.challenge_id == challenge_id, ArcContextChallenge.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            msg = f"expected to consume exactly one challenge for {challenge_id}, affected {result.rowcount}"  # type: ignore[attr-defined]
            raise ChallengeConsumptionError(msg)
