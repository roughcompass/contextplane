"""Source admission: the door from an approved upstream revision to an
admitted, evidence-backed candidate.

ADR 039 defines two closed admission authorities and this module implements
both: a configured connector fetches bytes from a registered, allowlisted
location; an authorized upload accepts bytes the caller sends directly and
never a URL. Either path streams the admitted bytes through a hard 10 MiB
ceiling while hashing SHA-256, and the digest that ends up on the evidence
row is always the one this deployment computed — never the caller's or the
signed claim's own assertion, which is checked *against* the computed value
rather than trusted in its place.

**What this module does not verify.** `authoring_profiles.py` draws this
line for the profile-shape layer: signature verification is deliberately
out of scope there, because that module knows how one instance is shaped,
not whether the world around it agrees with it. This service layer is
where that line points, and it does not yet close the gap either: a
verifier is authorized by allowlist membership on the admitting connector
or policy (its own test coverage's "verifier allowlist enforcement"), not
by cryptographic verification against an enrolled key or attestation
provider. `proof.signature_base64` / `proof.assertion_base64` are validated
for shape and stored, never returned, but not verified — there is no
enrolled-verifier trust-root table among this surface's own five tables to
verify against yet. A named, not a hidden, residual gap.

**Idempotency.** Per ADR 039: the scope digest is
`sha256(canonical{issuer, subject, source_system, idempotency_key})`. The
admission transaction acquires a Postgres advisory lock keyed by that
digest, then rechecks for an existing evidence row before any insert — the
lock is what makes two concurrent identical requests resolve to one row
instead of racing into the UNIQUE constraint, the final guard rather than
the mechanism. An exact retry (same scope digest, same payload digest)
returns the first evidence; a changed one is `SourceIdempotencyConflict`.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas import authoring_profiles
from contextplane.arc.schemas.authoring_profile_shapes import SOURCE_APPROVAL_CLAIM_PROFILE
from contextplane.arc.service import source_admission_vocab as vocab
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.queries import source_admission as queries
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError
from contextplane.ingest.connector import resolve_credential
from contextplane.types import Clock

# The one ceiling every admission path enforces, regardless of what a
# connector or policy registered for itself — registration is additionally
# capped at this value by a CHECK constraint, but the streaming reader
# enforces it independently rather than trusting that constraint alone.
HARD_BYTE_CEILING = 10_485_760

_CHUNK_SIZE = 65_536

# Redirect hops a configured-connector fetch will follow before refusing.
# Every hop re-validates scheme and host against the connector's own
# allowlist — the caller never supplies either.
_DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

# Both directions of the wire <-> persisted-profile translation live in
# `source_admission_vocab`, shared with the graph-promotion admission
# service. Imported under the existing private names so every call site in
# this module reads exactly as it did when the maps were declared here.
_ADMISSION_METHOD_TO_CANONICAL = vocab.ADMISSION_METHOD_TO_CANONICAL
_ADMISSION_METHOD_FROM_CANONICAL = vocab.ADMISSION_METHOD_FROM_CANONICAL
_VERIFICATION_METHOD_TO_CANONICAL = vocab.VERIFICATION_METHOD_TO_CANONICAL
_VERIFICATION_METHOD_FROM_CANONICAL = vocab.VERIFICATION_METHOD_FROM_CANONICAL


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceAdmissionRefused(RegistryError):
    """Size, media type, locator, scope, or verifier mismatch (`arc_source_admission_refused`, 400).

    One type for every such refusal: which check failed only matters for
    the message an operator reads, never for the caller's response shape.
    """


class SourceIdempotencyConflict(RegistryError):
    """Same idempotency scope, changed request payload (`arc_idempotency_conflict`, 409)."""


# ---------------------------------------------------------------------------
# Inputs and results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApprovalProof:
    """One `ApprovalProof` variant, in the service's own plain shape.

    The router adapts the wire's discriminated `DetachedSignatureProof` /
    `VerifierAttestationProof` union into this before calling the service,
    so nothing here depends on the pydantic request models.
    """

    verification_method: str  # "detached_signature" | "verifier_attestation"
    signature_algorithm: str | None = None
    signature_base64: str | None = None
    provider_id: str | None = None
    assertion_format: str | None = None
    assertion_base64: str | None = None


@dataclasses.dataclass(frozen=True)
class UploadAdmission:
    policy_id: str
    source_system: str
    source_revision_locator: str
    source_content_type: str
    claim: Mapping[str, Any]
    verifier_id: str
    proof: ApprovalProof
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class ConnectorFetchAdmission:
    connector_id: str
    source_revision_locator: str
    claim: Mapping[str, Any]
    verifier_id: str
    proof: ApprovalProof
    idempotency_key: str


@dataclasses.dataclass(frozen=True)
class ConnectorRegistration:
    connector_id: str
    owning_scope: str
    tenant_id: uuid.UUID | None
    allowed_schemes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allowed_media_types: tuple[str, ...]
    allowed_verifier_ids: tuple[str, ...]
    max_bytes: int
    credential_ref: str | None = None


@dataclasses.dataclass(frozen=True)
class UploadPolicyRegistration:
    policy_id: str
    owning_scope: str
    tenant_id: uuid.UUID | None
    allowed_media_types: tuple[str, ...]
    allowed_verifier_ids: tuple[str, ...]
    max_bytes: int


@dataclasses.dataclass(frozen=True)
class SourceEvidence:
    """Mirrors `SourceEvidenceResponse`. Never carries signature bytes,
    credentials, or the claim's raw proof — see that model's own docstring.
    """

    source_evidence_id: uuid.UUID
    source_system: str
    source_revision_locator: str
    source_content_digest: str
    source_content_type: str
    source_content_bytes: int
    admission_method: str  # wire vocabulary: connector_fetch | authorized_upload
    connector_id: str | None
    policy_id: str | None
    verification_method: str  # wire vocabulary: detached_signature | verifier_attestation
    verifier_id: str
    admitted_at: datetime.datetime
    verified_at: datetime.datetime
    expires_at: datetime.datetime | None
    status: str
    status_checked_at: datetime.datetime
    next_check_at: datetime.datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rfc3339(moment: datetime.datetime) -> str:
    """UTC, `Z`-suffixed — the exact form `authoring_profile_shapes`'s
    timestamp pattern requires, matching the wire convention's own rule
    that a numeric offset is never used."""
    return moment.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _length_prefixed(*parts: str) -> bytes:
    """Concatenate with each part's byte length prefixed, so no two
    different field splits can collide on the same digest input."""
    return b"".join(len(p.encode("utf-8")).to_bytes(4, "big") + p.encode("utf-8") for p in parts)


def idempotency_scope_digest(*, issuer: str, subject: str, source_system: str, idempotency_key: str) -> str:
    """`sha256(canonical{issuer, subject, source_system, idempotency_key})` per ADR 039 §1."""
    return hashlib.sha256(_length_prefixed(issuer, subject, source_system, idempotency_key)).hexdigest()


def _admission_request_payload_digest(
    *,
    claim_canonical_bytes: bytes,
    verification_method: str,
    verifier_id: str,
    proof: ApprovalProof,
    admission_method: str,
    connector_or_policy_id: str,
) -> str:
    """What ADR 039 §1 names as the retry-equivalence input: the claim,
    verification method, verifier id, signature or attestation, admission
    method, and connector id (or policy id for an upload) — computed before
    any server-generated field exists, so two calls that would produce
    identical evidence hash identically regardless of which one runs first.
    """
    proof_material = proof.signature_base64 or proof.assertion_base64 or ""
    material = _length_prefixed(
        verification_method,
        verifier_id,
        proof_material,
        admission_method,
        connector_or_policy_id,
    )
    return hashlib.sha256(claim_canonical_bytes + material).hexdigest()


async def _stream_and_hash(chunks: AsyncIterator[bytes], max_bytes: int) -> tuple[bytes, str, int]:
    """Stream *chunks*, hashing incrementally, aborting the instant the
    running total exceeds *max_bytes* — before any further chunk is read,
    which is what makes this a ceiling on the stream rather than a check
    performed after the fact on however much was already buffered.

    `aclosing` matters here, not just style: an early abort leaves
    whatever produced *chunks* (an `UploadFile.read` wrapper, an httpx
    response's `aiter_bytes()`) holding its underlying socket or file
    until the generator is closed. `async for` alone only does that on
    normal exhaustion, not on the exception this function raises.
    """
    hasher = hashlib.sha256()
    buffer = bytearray()
    total = 0
    try:
        async for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise SourceAdmissionRefused(
                    f"admitted content exceeds the {max_bytes}-byte ceiling; the stream was aborted "
                    "before any excess was retained"
                )
            hasher.update(chunk)
            buffer.extend(chunk)
    finally:
        # `AsyncIterator` does not guarantee `.aclose()` in its type, but
        # every real producer here (an async generator, httpx's
        # `aiter_bytes()`) is one and has it — duck-typed rather than
        # `contextlib.aclosing`, which the type checker cannot accept for
        # a bare `AsyncIterator[bytes]` parameter.
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            await aclose()
    return bytes(buffer), hasher.hexdigest(), total


class _AsyncChunkReader(Protocol):
    """The one method `iter_upload_file` needs from Starlette's
    `UploadFile.read` — narrow on purpose, so a test double needs no other
    method to stand in for one."""

    async def __call__(self, size: int) -> bytes: ...


async def iter_upload_file(read: _AsyncChunkReader, *, chunk_size: int = _CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Adapt an object exposing an async `.read(n)` (Starlette's
    `UploadFile.read`, or a test double) into the plain byte-chunk iterator
    the service consumes. Kept here rather than in the router so unit tests
    can drive it without a real multipart request.
    """
    while True:
        chunk = await read(chunk_size)
        if not chunk:
            return
        yield chunk


def _validate_claim(claim: Mapping[str, Any]) -> None:
    try:
        authoring_profiles.validate_source_approval_claim_v1(dict(claim))
    except authoring_profiles.AuthoringProfileError as exc:
        raise SourceAdmissionRefused(f"claim failed closed-schema validation: {exc}") from exc
    if claim.get("profile") != SOURCE_APPROVAL_CLAIM_PROFILE:
        raise SourceAdmissionRefused("claim carries the wrong profile literal")


def _canonical_claim_bytes(claim: Mapping[str, Any]) -> bytes:
    return authoring_profiles.canonicalize_source_approval_claim_v1(dict(claim))


def _proof_signature_or_attestation(proof: ApprovalProof) -> tuple[str | None, dict[str, Any] | None]:
    # Dispatched on each method rather than signature-or-else. A trailing
    # `else` meant every non-signature method built an attestation dict, so
    # `graph_promotion` -- which carries no proof at all -- produced one with
    # three null fields instead of NULL, and
    # `ck_arc_source_evidence_representation` rejected the insert. A third
    # method is exactly the case an else-branch cannot describe.
    if proof.verification_method == "detached_signature":
        return proof.signature_base64, None
    if proof.verification_method == "graph_promotion":
        return None, None
    return None, {
        "provider_id": proof.provider_id,
        "assertion_format": proof.assertion_format,
        "assertion_base64": proof.assertion_base64,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SourceAdmissionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._max_redirects = max_redirects
        # Injectable so a test can swap in an `httpx.MockTransport`-backed
        # client and drive the redirect-chain logic without a real socket.
        # `follow_redirects=False` is load-bearing on whatever factory is
        # given: the whole point of `_fetch_via_connector`'s own loop is to
        # re-validate scheme and host on every hop, which a client that
        # followed redirects itself would skip.
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30.0))
        )

    # -- registration -----------------------------------------------------
    #
    # Deliberately no authorization check in either method below, matching
    # `VerifierRegistry.register`: registering an authority decides what
    # counts as trusted, blast radius every future admission it accepts,
    # so it takes the same deployment-wide operator gate, held by the
    # router regardless of the body's own declared scope. A scope-based
    # check here would even be wrong: an operator registering a
    # tenant-scoped connector on that tenant's behalf is not its admin.
    # Admission itself, below, is the scope-aware action ADR 039 actually
    # describes that way ("tenant uploads require a tenant admin").

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

    async def admit_upload(
        self,
        ctx: ArcRequestContext,
        admission: UploadAdmission,
        body: AsyncIterator[bytes],
    ) -> SourceEvidence:
        async with self._session_factory() as session:
            policy = await queries.load_upload_policy(session, admission.policy_id)
        if policy is None:
            raise SourceAdmissionRefused(f"unknown upload policy {admission.policy_id!r}")
        if policy.revoked_at is not None:
            raise SourceAdmissionRefused(
                f"upload policy {admission.policy_id!r} was withdrawn at "
                f"{policy.revoked_at.isoformat()} and admits nothing further"
            )

        self._authorization.assert_can_write_artifact(ctx, _scope(policy.owning_scope, policy.tenant_id))

        if admission.verifier_id not in policy.allowed_verifier_ids:
            raise SourceAdmissionRefused(f"verifier {admission.verifier_id!r} is not permitted by this policy")

        claim = admission.claim
        _validate_claim(claim)
        if claim.get("source_content_type") not in policy.allowed_media_types:
            raise SourceAdmissionRefused(f"media type {claim.get('source_content_type')!r} is not permitted")
        if claim.get("source_system") != admission.source_system:
            raise SourceAdmissionRefused("source_system does not match the claim")
        if claim.get("source_revision_locator") != admission.source_revision_locator:
            raise SourceAdmissionRefused("source_revision_locator does not match the claim")
        if claim.get("source_content_type") != admission.source_content_type:
            raise SourceAdmissionRefused("source_content_type does not match the claim")

        max_bytes = min(policy.max_bytes, HARD_BYTE_CEILING)
        content_bytes, computed_digest, size = await _stream_and_hash(body, max_bytes)
        _assert_digest_matches(claim, computed_digest)

        return await self.finish_admission(
            ctx,
            claim=claim,
            verifier_id=admission.verifier_id,
            proof=admission.proof,
            idempotency_key=admission.idempotency_key,
            content_bytes=content_bytes,
            content_digest=computed_digest,
            content_bytes_len=size,
            admission_method="authorized_upload",
            connector_id=None,
            policy_id=admission.policy_id,
            owning_scope=policy.owning_scope,
            tenant_id=policy.tenant_id,
        )

    async def admit_connector_fetch(
        self,
        ctx: ArcRequestContext,
        admission: ConnectorFetchAdmission,
    ) -> SourceEvidence:
        async with self._session_factory() as session:
            connector = await queries.load_connector(session, admission.connector_id)
        if connector is None:
            raise SourceAdmissionRefused(f"unknown connector {admission.connector_id!r}")
        # Checked before the scope and verifier checks below, because a revoked
        # connector is not a permission question: it grants nothing to anybody,
        # and reporting it as a scope or verifier failure would send an operator
        # to look at the wrong thing.
        if connector.revoked_at is not None:
            raise SourceAdmissionRefused(
                f"connector {admission.connector_id!r} was withdrawn at "
                f"{connector.revoked_at.isoformat()} and admits nothing further"
            )

        self._authorization.assert_can_write_artifact(ctx, _scope(connector.owning_scope, connector.tenant_id))

        if admission.verifier_id not in connector.allowed_verifier_ids:
            raise SourceAdmissionRefused(f"verifier {admission.verifier_id!r} is not permitted by this connector")

        claim = admission.claim
        _validate_claim(claim)
        if claim.get("source_content_type") not in connector.allowed_media_types:
            raise SourceAdmissionRefused(f"media type {claim.get('source_content_type')!r} is not permitted")
        # No cross-check against connector_id's own text: a connector id is
        # an operator-chosen label, not a parseable source-system encoding.
        # The claim's source_system has nothing on the request to compare
        # against here (unlike an upload's top-level source_system field) —
        # the locator match below is what binds this claim to this fetch.
        if claim.get("source_revision_locator") != admission.source_revision_locator:
            raise SourceAdmissionRefused("source_revision_locator does not match the claim")

        max_bytes = min(connector.max_bytes, HARD_BYTE_CEILING)
        content_bytes, computed_digest, size = await self._fetch_via_connector(
            locator=admission.source_revision_locator,
            allowed_schemes=frozenset(connector.allowed_schemes),
            allowed_hosts=frozenset(connector.allowed_hosts),
            max_bytes=max_bytes,
            credential_ref=connector.credential_ref,
        )
        _assert_digest_matches(claim, computed_digest)

        return await self.finish_admission(
            ctx,
            claim=claim,
            verifier_id=admission.verifier_id,
            proof=admission.proof,
            idempotency_key=admission.idempotency_key,
            content_bytes=content_bytes,
            content_digest=computed_digest,
            content_bytes_len=size,
            admission_method="connector_fetch",
            connector_id=admission.connector_id,
            policy_id=None,
            owning_scope=connector.owning_scope,
            tenant_id=connector.tenant_id,
        )

    async def _fetch_via_connector(
        self,
        *,
        locator: str,
        allowed_schemes: frozenset[str],
        allowed_hosts: frozenset[str],
        max_bytes: int,
        credential_ref: str | None,
    ) -> tuple[bytes, str, int]:
        """Fetch *locator*, re-validating scheme and host on every hop.

        A caller names only a registered connector and locator; it cannot
        supply a fetch host, credential, or redirect target. What decides
        whether a hop is followed is always the connector's own allowlist,
        never anything in the response.
        """
        headers: dict[str, str] = {}
        if credential_ref:
            headers["Authorization"] = f"Bearer {resolve_credential(credential_ref)}"

        url = locator
        for _hop in range(self._max_redirects + 1):
            parsed = httpx.URL(url)
            if parsed.scheme not in allowed_schemes or parsed.host not in allowed_hosts:
                raise SourceAdmissionRefused(
                    f"fetch target {parsed.scheme}://{parsed.host} is not in the connector's allowlist"
                )
            async with (
                self._http_client_factory() as client,
                client.stream("GET", url, headers=headers) as response,
            ):
                if response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceAdmissionRefused("connector redirect carried no Location header")
                    url = str(httpx.URL(url).join(location))
                    continue
                if response.status_code != 200:
                    raise SourceAdmissionRefused(f"connector fetch failed with status {response.status_code}")
                body, digest, size = await _stream_and_hash(response.aiter_bytes(), max_bytes)
                return body, digest, size
        raise SourceAdmissionRefused(f"connector fetch exceeded {self._max_redirects} redirect hop(s)")

    # -- shared admission transaction ---------------------------------------

    async def finish_admission(
        self,
        ctx: ArcRequestContext,
        *,
        claim: Mapping[str, Any],
        verifier_id: str,
        proof: ApprovalProof,
        idempotency_key: str,
        content_bytes: bytes,
        content_digest: str,
        content_bytes_len: int,
        admission_method: str,
        connector_id: str | None,
        policy_id: str | None,
        owning_scope: str,
        tenant_id: uuid.UUID | None,
    ) -> SourceEvidence:
        expires_at = _parse_rfc3339(str(claim["expires_at"]))
        now = self._clock.now()
        if expires_at <= now:
            raise SourceAdmissionRefused(f"claim expired at {expires_at.isoformat()}")

        claim_bytes = _canonical_claim_bytes(claim)
        claim_digest = hashlib.sha256(claim_bytes).hexdigest()
        canonical_verification_method = _VERIFICATION_METHOD_TO_CANONICAL[proof.verification_method]
        canonical_admission_method = _ADMISSION_METHOD_TO_CANONICAL[admission_method]
        signature, verifier_attestation = _proof_signature_or_attestation(proof)

        scope_digest = idempotency_scope_digest(
            issuer=ctx.oidc_issuer,
            subject=ctx.oidc_subject,
            source_system=str(claim["source_system"]),
            idempotency_key=idempotency_key,
        )
        payload_digest = _admission_request_payload_digest(
            claim_canonical_bytes=claim_bytes,
            verification_method=canonical_verification_method,
            verifier_id=verifier_id,
            proof=proof,
            admission_method=canonical_admission_method,
            connector_or_policy_id=connector_id or policy_id or "",
        )

        # One session, no explicit `session.begin()`: SQLAlchemy autobegins
        # on the lock statement below and holds the transaction open until
        # the explicit commit/rollback further down — matching the same
        # commit-then-catch-IntegrityError-then-resolve shape
        # `ChallengeService.issue_challenge` uses for the same kind of race.
        async with self._session_factory() as session:
            # ADR 039 §1: acquire a database lock keyed by the scope digest,
            # then recheck, before ever inserting. The lock is what makes
            # two concurrent identical requests resolve to one row instead
            # of racing each other into the UNIQUE constraint below.
            await queries.acquire_scope_lock(session, scope_digest)
            existing = await queries.find_evidence_by_scope_digest(session, scope_digest)
            if existing is not None:
                await session.commit()
                if existing.admission_request_payload_digest != payload_digest:
                    raise SourceIdempotencyConflict("idempotency key already identifies a different admission request")
                return await self._response_for(session, existing)

            source_evidence_id = uuid.uuid4()
            checked_at = now
            next_check_at = min(checked_at + datetime.timedelta(seconds=300), expires_at)

            # The whole insert sequence is inside this `try`, not just
            # `commit()`: Postgres checks the non-deferrable UNIQUE
            # constraint on `idempotency_scope_digest` at statement-
            # execution time, so a colliding `insert_evidence` raises
            # right there — waiting for `commit()` would miss it.
            try:
                await queries.insert_body(
                    session,
                    source_evidence_id=source_evidence_id,
                    content_digest=content_digest,
                    content_bytes=content_bytes_len,
                    body=content_bytes,
                    created_at=now,
                )
                await queries.insert_evidence(
                    session,
                    source_evidence_id=source_evidence_id,
                    owning_scope=owning_scope,
                    tenant_id=tenant_id,
                    source_system=str(claim["source_system"]),
                    source_revision_locator=str(claim["source_revision_locator"]),
                    source_content_type=str(claim["source_content_type"]),
                    source_content_digest=content_digest,
                    claim=dict(claim),
                    claim_digest=claim_digest,
                    verification_method=canonical_verification_method,
                    verifier_id=verifier_id,
                    signature=signature,
                    verifier_attestation=verifier_attestation,
                    admission_method=canonical_admission_method,
                    connector_id=connector_id,
                    policy_id=policy_id,
                    admitted_at=now,
                    admitted_by_issuer=ctx.oidc_issuer,
                    admitted_by_subject=ctx.oidc_subject,
                    verified_at=now,
                    expires_at=expires_at,
                    idempotency_key_digest=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                    admission_request_payload_digest=payload_digest,
                    idempotency_scope_digest=scope_digest,
                    created_at=now,
                )
                await queries.insert_status(
                    session,
                    source_evidence_id=source_evidence_id,
                    status="current",
                    checked_at=checked_at,
                    next_check_at=next_check_at,
                    status_source="admission",
                    status_evidence_digest=None,
                )
                await session.commit()
            except IntegrityError:
                # The unique scope-digest constraint is the final race
                # guard the lock above should have made unreachable; if it
                # still fires, resolve exactly as a sequential retry would,
                # in the same session — rollback() leaves it usable for a
                # fresh read, it does not close it.
                await session.rollback()
                resolved = await queries.find_evidence_by_scope_digest(session, scope_digest)
                if resolved is None:
                    raise
                if resolved.admission_request_payload_digest != payload_digest:
                    raise SourceIdempotencyConflict(
                        "idempotency key already identifies a different admission request"
                    ) from None
                return await self._response_for(session, resolved)

            evidence = await queries.load_evidence(session, source_evidence_id)
            if evidence is None:
                raise RegistryError(f"evidence {source_evidence_id} vanished immediately after commit")
            return await self._response_for(session, evidence)

    # -- reads ----------------------------------------------------------------

    async def get_evidence(self, ctx: ArcRequestContext, source_evidence_id: uuid.UUID) -> SourceEvidence:
        async with self._session_factory() as session:
            evidence = await queries.load_evidence(session, source_evidence_id)
            if evidence is None:
                raise NotFoundError(f"source evidence {source_evidence_id} not found")
            self._authorization.assert_can_read_artifact(ctx, _scope(evidence.owning_scope, evidence.tenant_id))
            return await self._response_for(session, evidence)

    async def get_body(self, ctx: ArcRequestContext, source_evidence_id: uuid.UUID) -> tuple[bytes, str]:
        async with self._session_factory() as session:
            evidence = await queries.load_evidence(session, source_evidence_id)
            if evidence is None:
                raise NotFoundError(f"source evidence {source_evidence_id} not found")
            self._authorization.assert_can_read_artifact(ctx, _scope(evidence.owning_scope, evidence.tenant_id))
            body = await queries.load_body(session, source_evidence_id)
            if body is None:
                raise NotFoundError(f"source body {source_evidence_id} not found")
            return body.body, evidence.source_content_type

    async def _response_for(self, session: AsyncSession, evidence: queries.EvidenceRow) -> SourceEvidence:
        status = await queries.load_status(session, evidence.source_evidence_id)
        if status is None:
            # Every evidence row is inserted in the same transaction as its
            # status sibling; a miss here means that invariant broke, not
            # that the status is merely stale.
            raise NotFoundError(f"source status for {evidence.source_evidence_id} not found")
        body = await queries.load_body(session, evidence.source_evidence_id)
        if body is None:
            # Same invariant as above: the body is inserted before its
            # evidence sibling in the same transaction, so a miss here is a
            # broken invariant, not a stale read.
            raise NotFoundError(f"source body for {evidence.source_evidence_id} not found")
        return SourceEvidence(
            source_evidence_id=evidence.source_evidence_id,
            source_system=evidence.source_system,
            source_revision_locator=evidence.source_revision_locator,
            source_content_digest=evidence.source_content_digest,
            source_content_type=evidence.source_content_type,
            source_content_bytes=body.content_bytes,
            admission_method=_ADMISSION_METHOD_FROM_CANONICAL[evidence.admission_method],
            connector_id=evidence.connector_id,
            policy_id=evidence.policy_id,
            verification_method=_VERIFICATION_METHOD_FROM_CANONICAL[evidence.verification_method],
            verifier_id=evidence.verifier_id,
            admitted_at=evidence.admitted_at,
            verified_at=evidence.verified_at,
            expires_at=evidence.expires_at,
            status=status.status,
            status_checked_at=status.checked_at,
            next_check_at=status.next_check_at,
        )


def _scope(owning_scope: str, tenant_id: uuid.UUID | None) -> ArtifactScope:
    return ArtifactScope(scope=AuthorityScope(owning_scope), tenant_id=tenant_id)


def _assert_digest_matches(claim: Mapping[str, Any], computed_digest: str) -> None:
    """Never trust a caller's content digest — this is the check that
    makes that true. The claim's own `source_content_digest` is a signed
    assertion about what the upstream bytes hash to; it is compared against
    what this deployment actually streamed and hashed, and a mismatch is
    refused rather than recorded.
    """
    claimed = str(claim.get("source_content_digest", "")).lower()
    if claimed != computed_digest.lower():
        raise SourceAdmissionRefused(
            "the claim's source_content_digest does not match the digest computed over the admitted bytes"
        )


__all__ = [
    "HARD_BYTE_CEILING",
    "ApprovalProof",
    "ConnectorFetchAdmission",
    "ConnectorRegistration",
    "SourceAdmissionRefused",
    "SourceAdmissionService",
    "SourceEvidence",
    "SourceIdempotencyConflict",
    "UploadAdmission",
    "UploadPolicyRegistration",
    "idempotency_scope_digest",
    "iter_upload_file",
]
