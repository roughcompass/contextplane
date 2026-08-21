"""Unit tests for `contextplane/arc/service/source_admission.py`.

No database: `queries.source_admission`'s functions are monkeypatched with
an in-memory fake that mimics the five tables' relational shape (recheck by
scope digest, insert body before evidence before status, read the row back)
closely enough to exercise the service's real branches — unknown authority,
verifier/media-type allowlist refusal, claim/digest mismatch, expiry, exact
retry versus changed-payload conflict, and the `IntegrityError` fallback
path. What a fake session cannot prove is that the advisory lock actually
serializes two concurrent callers; that proof is
`tests/integration/test_arc_source_admission.py`'s race test, against a
real Postgres.

Also covered without any fake at all, because these are pure functions or
use a real `httpx.MockTransport`: the streaming byte-ceiling abort, digest
computation, idempotency-digest determinism, and the redirect-chain host
escape.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from contextplane.arc.service import source_admission as sa
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.queries.source_admission import (
    BodyRow,
    ConnectorRow,
    EvidenceRow,
    StatusRow,
    UploadPolicyRow,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.exceptions import ConflictError, NotFoundError
from contextplane.types import TenantContext

# No `pytestmark = pytest.mark.asyncio` here: `asyncio_mode = "auto"`
# (pyproject.toml) already runs async tests without the marker, and adding
# it anyway makes pytest-asyncio warn on every synchronous test in this
# file, which is most of the pure-function ones below.

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"


class _FakeClock:
    def __init__(self, moment: datetime.datetime) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment


class _AllowAll:
    """A permissive `CapabilityVisibility` -- authorization tests use the
    real `ArcAuthorizationService`, not a mock of it, so what is under test
    is this module's own scope-building, not a stand-in for the chokepoint.
    """

    async def visible_entity_ids(self, ctx: object, entity_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(entity_ids)


def _ctx(
    *, tenant_id: uuid.UUID | None = None, subject: str = "operator", roles: list[str] | None = None
) -> ArcRequestContext:
    return ArcRequestContext(
        tenant=TenantContext(
            tenant_id=tenant_id or uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=roles or ["admin"],
            oidc_subject=subject,
        ),
        oidc_issuer=_ISSUER,
    )


def _claim(**overrides: object) -> dict[str, Any]:
    body: dict[str, Any] = {
        "profile": "arc_source_approval_claim_v1",
        "source_system": "confluence",
        "source_revision_locator": "conf://space/page@3",
        "source_content_digest_algorithm": "sha256",
        "source_content_digest": "0" * 64,
        "source_content_type": "text/markdown",
        "approval_locator": "https://confluence.example/approvals/1",
        "approving_authority_issuer": "https://idp.example.test",
        "approving_authority_subject": "owner",
        "approval_scope": "space:eng",
        "approved_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def _proof() -> sa.ApprovalProof:
    return sa.ApprovalProof(
        verification_method="detached_signature",
        signature_algorithm="Ed25519",
        signature_base64="c2lnbmF0dXJl",
    )


async def _bytes_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def begin(self) -> _NoopTransactionCM:
        return _NoopTransactionCM()


class _SessionCM:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeQueries:
    """An in-memory stand-in for `queries.source_admission`'s module-level
    functions, faithful enough to the real relational shape (insert order,
    recheck-by-scope-digest, read-your-own-write) to exercise
    `source_admission.py`'s real branches without a database.
    """

    def __init__(self) -> None:
        self.connectors: dict[str, ConnectorRow] = {}
        self.policies: dict[str, UploadPolicyRow] = {}
        self.bodies: dict[uuid.UUID, BodyRow] = {}
        self.evidence: dict[uuid.UUID, EvidenceRow] = {}
        self.evidence_by_scope: dict[str, uuid.UUID] = {}
        self.status: dict[uuid.UUID, StatusRow] = {}
        self.raise_integrity_error_once = False
        self._raised = False
        # Armed alongside `raise_integrity_error_once`: makes the *first*
        # `find_evidence_by_scope_digest` call (the one before the insert
        # attempt) report "not found" even though a row already exists,
        # simulating the row having been invisible under this transaction's
        # own snapshot right up until the concurrent insert it collides
        # with commits. Without this, the pre-insert recheck alone would
        # already resolve the call and `insert_body` would never run.
        self.suppress_next_find = False

    def seed_connector(self, row: ConnectorRow) -> None:
        self.connectors[row.connector_id] = row

    def seed_policy(self, row: UploadPolicyRow) -> None:
        self.policies[row.policy_id] = row

    # -- connectors / policies --------------------------------------------

    async def load_connector(self, _session: object, connector_id: str) -> ConnectorRow | None:
        return self.connectors.get(connector_id)

    async def insert_connector(self, _session: object, **kwargs: Any) -> None:
        row = ConnectorRow(
            connector_id=kwargs["connector_id"],
            owning_scope=kwargs["owning_scope"],
            tenant_id=kwargs["tenant_id"],
            allowed_schemes=tuple(kwargs["allowed_schemes"]),
            allowed_hosts=tuple(kwargs["allowed_hosts"]),
            allowed_media_types=tuple(kwargs["allowed_media_types"]),
            allowed_verifier_ids=tuple(kwargs["allowed_verifier_ids"]),
            max_bytes=kwargs["max_bytes"],
            credential_ref=kwargs["credential_ref"],
            registered_at=kwargs["registered_at"],
        )
        self.connectors[row.connector_id] = row

    async def load_upload_policy(self, _session: object, policy_id: str) -> UploadPolicyRow | None:
        return self.policies.get(policy_id)

    async def insert_upload_policy(self, _session: object, **kwargs: Any) -> None:
        row = UploadPolicyRow(
            policy_id=kwargs["policy_id"],
            owning_scope=kwargs["owning_scope"],
            tenant_id=kwargs["tenant_id"],
            allowed_media_types=tuple(kwargs["allowed_media_types"]),
            allowed_verifier_ids=tuple(kwargs["allowed_verifier_ids"]),
            max_bytes=kwargs["max_bytes"],
            registered_at=kwargs["registered_at"],
        )
        self.policies[row.policy_id] = row

    # -- idempotency -------------------------------------------------------

    async def acquire_scope_lock(self, _session: object, _scope_digest: str) -> None:
        # No real concurrency in a unit test; the lock's actual holding
        # power is proven against real Postgres in the integration suite.
        return None

    async def find_evidence_by_scope_digest(self, _session: object, scope_digest: str) -> EvidenceRow | None:
        if self.suppress_next_find:
            self.suppress_next_find = False
            return None
        eid = self.evidence_by_scope.get(scope_digest)
        return self.evidence.get(eid) if eid is not None else None

    # -- bodies / evidence / status ----------------------------------------

    async def insert_body(self, _session: object, **kwargs: Any) -> None:
        if self.raise_integrity_error_once and not self._raised:
            self._raised = True
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        row = BodyRow(**kwargs)
        self.bodies[row.source_evidence_id] = row

    async def load_body(self, _session: object, source_evidence_id: uuid.UUID) -> BodyRow | None:
        return self.bodies.get(source_evidence_id)

    async def insert_evidence(self, _session: object, **kwargs: Any) -> None:
        kwargs = dict(kwargs)
        kwargs.pop("created_at")
        row = EvidenceRow(**kwargs)
        self.evidence[row.source_evidence_id] = row
        self.evidence_by_scope[row.idempotency_scope_digest] = row.source_evidence_id

    async def load_evidence(self, _session: object, source_evidence_id: uuid.UUID) -> EvidenceRow | None:
        return self.evidence.get(source_evidence_id)

    async def insert_status(self, _session: object, **kwargs: Any) -> None:
        row = StatusRow(**kwargs)
        self.status[row.source_evidence_id] = row

    async def load_status(self, _session: object, source_evidence_id: uuid.UUID) -> StatusRow | None:
        return self.status.get(source_evidence_id)


def _build_service(fake: FakeQueries, *, clock_at: datetime.datetime = _NOW) -> sa.SourceAdmissionService:
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, "operator"),))
    return sa.SourceAdmissionService(
        lambda: _SessionCM(_NullSession()),
        authorization=authorization,
        clock=_FakeClock(clock_at),
    )


@pytest.fixture(autouse=True)
def _patch_queries(monkeypatch: pytest.MonkeyPatch) -> FakeQueries:
    fake = FakeQueries()
    for name in (
        "load_connector",
        "insert_connector",
        "load_upload_policy",
        "insert_upload_policy",
        "acquire_scope_lock",
        "find_evidence_by_scope_digest",
        "insert_body",
        "load_body",
        "insert_evidence",
        "load_evidence",
        "insert_status",
        "load_status",
    ):
        monkeypatch.setattr(sa.queries, name, getattr(fake, name))
    return fake


# ---------------------------------------------------------------------------
# Pure helpers: streaming ceiling + digest (never trust a caller's digest)
# ---------------------------------------------------------------------------


class TestStreamAndHash:
    async def test_hashes_and_sizes_correctly(self) -> None:
        data = b"hello world"
        body, digest, size = await sa._stream_and_hash(_bytes_iter([data[:5], data[5:]]), max_bytes=1024)
        assert body == data
        assert digest == _digest_of(data)
        assert size == len(data)

    async def test_aborts_the_instant_the_ceiling_is_exceeded(self) -> None:
        """Proof of the hard 10 MiB (here, tiny) streaming ceiling: the
        abort happens on the chunk that crosses the limit, before any
        further chunk is read."""
        read_chunks: list[bytes] = []

        async def chunks() -> AsyncIterator[bytes]:
            read_chunks.append(b"a" * 5)
            yield b"a" * 5
            read_chunks.append(b"b" * 5)
            yield b"b" * 5
            # This third chunk must never be read: the ceiling (8) is
            # already exceeded by the first two chunks combined (10).
            read_chunks.append(b"c" * 5)
            yield b"c" * 5

        with pytest.raises(sa.SourceAdmissionRefused, match="8-byte ceiling"):
            await sa._stream_and_hash(chunks(), max_bytes=8)

        assert read_chunks == [b"aaaaa", b"bbbbb"], "a third chunk was read past the ceiling"

    async def test_the_real_10mib_ceiling_constant_is_exceeded_and_refused(self) -> None:
        """Same proof, at the actual production ceiling rather than a
        tiny stand-in, so the constant itself is exercised."""
        oversized = sa.HARD_BYTE_CEILING + 1

        async def one_big_chunk() -> AsyncIterator[bytes]:
            yield b"x" * oversized

        with pytest.raises(sa.SourceAdmissionRefused, match=f"{sa.HARD_BYTE_CEILING}-byte ceiling"):
            await sa._stream_and_hash(one_big_chunk(), max_bytes=sa.HARD_BYTE_CEILING)


class TestDigestNeverTrustsTheCaller:
    def test_matching_digest_is_accepted_and_a_mismatch_is_still_refused(self) -> None:
        """Both directions in one test, deliberately: a guard reduced to
        `pass` would still let the first half look green, but fails the
        second; a guard that over-refuses fails the first. Neither half
        alone can distinguish a working check from a vacuous one."""
        data = b"trusted bytes"
        sa._assert_digest_matches(_claim(source_content_digest=_digest_of(data)), _digest_of(data))
        with pytest.raises(sa.SourceAdmissionRefused, match="does not match"):
            sa._assert_digest_matches(_claim(source_content_digest=_digest_of(data)), _digest_of(b"different bytes"))

    def test_mismatched_digest_is_refused(self) -> None:
        claim = _claim(source_content_digest="1" * 64)
        with pytest.raises(sa.SourceAdmissionRefused, match="does not match"):
            sa._assert_digest_matches(claim, _digest_of(b"different bytes"))

    def test_case_folding_accepts_same_content_but_still_refuses_a_real_mismatch(self) -> None:
        """Proves case-*folding*, not blanket acceptance of any uppercase
        value: an uppercase digest of the *same* bytes passes, but an
        uppercase digest of *different* bytes still does not."""
        data = b"case"
        sa._assert_digest_matches(_claim(source_content_digest=_digest_of(data).upper()), _digest_of(data))
        with pytest.raises(sa.SourceAdmissionRefused, match="does not match"):
            sa._assert_digest_matches(
                _claim(source_content_digest=_digest_of(b"other bytes").upper()), _digest_of(data)
            )


class TestIterUploadFile:
    async def test_yields_chunks_until_empty_read(self) -> None:
        reads = [b"ab", b"cd", b""]

        async def fake_read(_size: int) -> bytes:
            return reads.pop(0)

        collected = [chunk async for chunk in sa.iter_upload_file(fake_read, chunk_size=2)]
        assert collected == [b"ab", b"cd"]


# ---------------------------------------------------------------------------
# Redirect-chain host escape (every connector hop re-validated)
# ---------------------------------------------------------------------------


class TestFetchViaConnector:
    def _service(self, handler: Any) -> sa.SourceAdmissionService:
        fake = FakeQueries()
        service = _build_service(fake)
        service._http_client_factory = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        return service

    async def test_direct_fetch_within_allowlist_succeeds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "good.example"
            return httpx.Response(200, content=b"document body")

        service = self._service(handler)
        body, digest, size = await service._fetch_via_connector(
            locator="https://good.example/doc",
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=frozenset({"good.example"}),
            max_bytes=1024,
            credential_ref=None,
        )
        assert body == b"document body"
        assert digest == _digest_of(b"document body")
        assert size == len(b"document body")

    async def test_a_redirect_to_an_allowlisted_host_is_followed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "good.example":
                return httpx.Response(302, headers={"location": "https://good-cdn.example/doc"})
            return httpx.Response(200, content=b"cdn body")

        service = self._service(handler)
        body, _digest, _size = await service._fetch_via_connector(
            locator="https://good.example/doc",
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=frozenset({"good.example", "good-cdn.example"}),
            max_bytes=1024,
            credential_ref=None,
        )
        assert body == b"cdn body"

    async def test_a_redirect_that_escapes_the_allowlist_is_refused(self) -> None:
        """The proof this task calls for: a redirect chain that lands
        somewhere it should not is refused, not silently followed."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "good.example":
                return httpx.Response(302, headers={"location": "https://evil.example/payload"})
            return httpx.Response(200, content=b"should never be admitted")

        service = self._service(handler)
        with pytest.raises(sa.SourceAdmissionRefused, match="not in the connector's allowlist"):
            await service._fetch_via_connector(
                locator="https://good.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"good.example"}),  # evil.example is NOT here
                max_bytes=1024,
                credential_ref=None,
            )

    async def test_every_hop_is_rechecked_not_just_the_first(self) -> None:
        """Two hops inside the allowlist, then a third that escapes it --
        proves the check runs on every hop, not only the first redirect."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "hop1.example":
                return httpx.Response(302, headers={"location": "https://hop2.example/doc"})
            if request.url.host == "hop2.example":
                return httpx.Response(302, headers={"location": "https://evil.example/payload"})
            return httpx.Response(200, content=b"unreachable")

        service = self._service(handler)
        with pytest.raises(sa.SourceAdmissionRefused, match="not in the connector's allowlist"):
            await service._fetch_via_connector(
                locator="https://hop1.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"hop1.example", "hop2.example"}),
                max_bytes=1024,
                credential_ref=None,
            )

    async def test_a_redirect_with_no_location_header_is_refused(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        service = self._service(handler)
        with pytest.raises(sa.SourceAdmissionRefused, match="no Location header"):
            await service._fetch_via_connector(
                locator="https://good.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"good.example"}),
                max_bytes=1024,
                credential_ref=None,
            )

    async def test_too_many_redirect_hops_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": str(request.url)})

        fake = FakeQueries()
        service = _build_service(fake)
        service._max_redirects = 2
        service._http_client_factory = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        with pytest.raises(sa.SourceAdmissionRefused, match="exceeded 2 redirect"):
            await service._fetch_via_connector(
                locator="https://good.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"good.example"}),
                max_bytes=1024,
                credential_ref=None,
            )

    async def test_a_non_200_non_redirect_status_is_refused(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        service = self._service(handler)
        with pytest.raises(sa.SourceAdmissionRefused, match="status 500"):
            await service._fetch_via_connector(
                locator="https://good.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"good.example"}),
                max_bytes=1024,
                credential_ref=None,
            )

    async def test_the_response_body_is_streamed_through_the_ceiling_too(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 20)

        service = self._service(handler)
        with pytest.raises(sa.SourceAdmissionRefused, match="byte ceiling"):
            await service._fetch_via_connector(
                locator="https://good.example/doc",
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=frozenset({"good.example"}),
                max_bytes=10,
                credential_ref=None,
            )


# ---------------------------------------------------------------------------
# Idempotency digests: deterministic, and sensitive to every named input
# ---------------------------------------------------------------------------


class TestIdempotencyScopeDigest:
    def test_deterministic_for_the_same_inputs(self) -> None:
        a = sa.idempotency_scope_digest(issuer="i", subject="s", source_system="sys", idempotency_key="k")
        b = sa.idempotency_scope_digest(issuer="i", subject="s", source_system="sys", idempotency_key="k")
        assert a == b

    @pytest.mark.parametrize(
        "changed",
        [
            {"issuer": "different"},
            {"subject": "different"},
            {"source_system": "different"},
            {"idempotency_key": "different"},
        ],
    )
    def test_sensitive_to_every_field(self, changed: dict[str, str]) -> None:
        base = {"issuer": "i", "subject": "s", "source_system": "sys", "idempotency_key": "k"}
        other = {**base, **changed}
        assert sa.idempotency_scope_digest(**base) != sa.idempotency_scope_digest(**other)

    def test_field_splits_do_not_collide(self) -> None:
        """Length-prefixing exists precisely so ("ab", "c") and ("a", "bc")
        do not hash identically."""
        a = sa.idempotency_scope_digest(issuer="ab", subject="c", source_system="sys", idempotency_key="k")
        b = sa.idempotency_scope_digest(issuer="a", subject="bc", source_system="sys", idempotency_key="k")
        assert a != b


# ---------------------------------------------------------------------------
# admit_upload
# ---------------------------------------------------------------------------


def _seed_policy(fake: FakeQueries, **overrides: object) -> UploadPolicyRow:
    row = UploadPolicyRow(
        policy_id="policy-1",
        owning_scope="global",
        tenant_id=None,
        allowed_media_types=("text/markdown",),
        allowed_verifier_ids=("verifier-1",),
        max_bytes=1024,
        registered_at=_NOW,
    )
    row = dataclasses.replace(row, **overrides)  # type: ignore[arg-type]
    fake.seed_policy(row)
    return row


def _upload_admission(**overrides: object) -> sa.UploadAdmission:
    body = dict(
        policy_id="policy-1",
        source_system="confluence",
        source_revision_locator="conf://space/page@3",
        source_content_type="text/markdown",
        claim=_claim(),
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key="key-1",
    )
    body.update(overrides)
    return sa.UploadAdmission(**body)  # type: ignore[arg-type]


class TestAdmitUpload:
    async def test_unknown_policy_is_refused(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="unknown upload policy"):
            await service.admit_upload(_ctx(), _upload_admission(), _bytes_iter([b"data"]))

    async def test_verifier_not_in_the_allowlist_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="not permitted by this policy"):
            await service.admit_upload(_ctx(), _upload_admission(verifier_id="not-registered"), _bytes_iter([b"data"]))

    async def test_media_type_not_in_the_allowlist_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        forbidden_type = "application/pdf"
        with pytest.raises(sa.SourceAdmissionRefused, match="media type"):
            await service.admit_upload(
                _ctx(),
                _upload_admission(source_content_type=forbidden_type, claim=_claim(source_content_type=forbidden_type)),
                _bytes_iter([b"data"]),
            )

    async def test_source_system_mismatch_against_the_claim_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="source_system"):
            await service.admit_upload(
                _ctx(), _upload_admission(source_system="different-system"), _bytes_iter([b"data"])
            )

    async def test_a_caller_supplied_digest_that_disagrees_is_refused(self, _patch_queries: FakeQueries) -> None:
        """Security-critical: the claim's own digest is compared against
        the recomputed one, never trusted."""
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        wrong_digest_claim = _claim(source_content_digest=_digest_of(b"not the real bytes"))
        with pytest.raises(sa.SourceAdmissionRefused, match="does not match"):
            await service.admit_upload(
                _ctx(), _upload_admission(claim=wrong_digest_claim), _bytes_iter([b"actual bytes"])
            )

    async def test_an_expired_claim_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        expired_claim = _claim(expires_at="2025-01-01T00:00:00Z")
        service = _build_service(_patch_queries, clock_at=_NOW)
        data = b"data"
        expired_claim["source_content_digest"] = _digest_of(data)
        with pytest.raises(sa.SourceAdmissionRefused, match="expired"):
            await service.admit_upload(_ctx(), _upload_admission(claim=expired_claim), _bytes_iter([data]))

    async def test_exceeding_the_policy_byte_ceiling_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries, max_bytes=4)
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="byte ceiling"):
            await service.admit_upload(_ctx(), _upload_admission(), _bytes_iter([b"way too much data"]))

    async def test_a_valid_admission_is_recorded_and_returned(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        data = b"document body"
        claim = _claim(source_content_digest=_digest_of(data))

        evidence = await service.admit_upload(_ctx(), _upload_admission(claim=claim), _bytes_iter([data]))

        assert evidence.source_content_digest == _digest_of(data)
        assert evidence.source_content_bytes == len(data)
        assert evidence.admission_method == "authorized_upload"
        assert evidence.verification_method == "detached_signature"
        assert evidence.status == "current"
        assert evidence.policy_id == "policy-1"
        assert evidence.connector_id is None
        # Persisted in canonical vocabulary, translated back to wire
        # vocabulary at the response boundary.
        stored = _patch_queries.evidence[evidence.source_evidence_id]
        assert stored.admission_method == "authorized_upload"
        assert stored.verification_method == "source_signed"

    async def test_tenant_scoped_policy_requires_a_tenant_admin(self, _patch_queries: FakeQueries) -> None:
        tenant_id = uuid.uuid4()
        _seed_policy(_patch_queries, owning_scope="tenant", tenant_id=tenant_id)
        service = _build_service(_patch_queries)
        data = b"data"
        claim = _claim(source_content_digest=_digest_of(data))
        non_admin_same_tenant = _ctx(tenant_id=tenant_id, roles=["producer"])
        with pytest.raises(Exception, match="may not write"):
            await service.admit_upload(non_admin_same_tenant, _upload_admission(claim=claim), _bytes_iter([data]))

    async def test_exact_retry_returns_the_first_evidence(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        data = b"document body"
        claim = _claim(source_content_digest=_digest_of(data))
        ctx = _ctx()  # same actor across both calls, matching the global operator allowlist

        first = await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))
        second = await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))

        assert first.source_evidence_id == second.source_evidence_id
        assert len(_patch_queries.evidence) == 1

    async def test_a_changed_retry_under_the_same_key_is_a_conflict(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        data = b"document body"
        claim = _claim(source_content_digest=_digest_of(data))
        ctx = _ctx()  # same actor across both calls, matching the global operator allowlist

        await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))

        other_data = b"a completely different document"
        other_claim = _claim(source_content_digest=_digest_of(other_data))
        with pytest.raises(sa.SourceIdempotencyConflict):
            await service.admit_upload(ctx, _upload_admission(claim=other_claim), _bytes_iter([other_data]))

    async def test_the_race_fallback_resolves_like_a_sequential_retry(self, _patch_queries: FakeQueries) -> None:
        """Deterministically drives the `IntegrityError` recovery branch
        `_finish_admission` falls back to if the advisory lock were ever
        bypassed -- the logic half of the race proof; the concurrency half
        is the integration suite's."""
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        data = b"document body"
        claim = _claim(source_content_digest=_digest_of(data))
        ctx = _ctx()  # same actor across both calls, matching the global operator allowlist

        # The "winner": an ordinary first admission, inserted normally.
        winner = await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))

        # Now simulate the race: a second, identical request whose
        # pre-insert recheck still sees no row (suppress_next_find), whose
        # insert then collides on the UNIQUE scope-digest constraint (as
        # if the advisory lock had somehow let two transactions in at
        # once), and whose post-IntegrityError recheck finds the winner
        # above under that same scope digest.
        _patch_queries.raise_integrity_error_once = True
        _patch_queries._raised = False
        _patch_queries.suppress_next_find = True

        resolved = await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))
        assert resolved.source_evidence_id == winner.source_evidence_id


# ---------------------------------------------------------------------------
# admit_connector_fetch
# ---------------------------------------------------------------------------


def _seed_connector(fake: FakeQueries, **overrides: object) -> ConnectorRow:
    row = ConnectorRow(
        connector_id="connector-1",
        owning_scope="global",
        tenant_id=None,
        allowed_schemes=("https",),
        allowed_hosts=("good.example",),
        allowed_media_types=("text/markdown",),
        allowed_verifier_ids=("verifier-1",),
        max_bytes=1024,
        credential_ref=None,
        registered_at=_NOW,
    )
    row = dataclasses.replace(row, **overrides)  # type: ignore[arg-type]
    fake.seed_connector(row)
    return row


def _connector_fetch_admission(**overrides: object) -> sa.ConnectorFetchAdmission:
    body = dict(
        connector_id="connector-1",
        source_revision_locator="https://good.example/doc",
        claim=_claim(source_revision_locator="https://good.example/doc"),
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key="key-1",
    )
    body.update(overrides)
    return sa.ConnectorFetchAdmission(**body)  # type: ignore[arg-type]


class TestAdmitConnectorFetch:
    def _service_with_handler(self, fake: FakeQueries, handler: Any) -> sa.SourceAdmissionService:
        service = _build_service(fake)
        service._http_client_factory = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        return service

    async def test_unknown_connector_is_refused(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="unknown connector"):
            await service.admit_connector_fetch(_ctx(), _connector_fetch_admission())

    async def test_verifier_not_in_the_allowlist_is_refused(self, _patch_queries: FakeQueries) -> None:
        _seed_connector(_patch_queries)
        service = _build_service(_patch_queries)
        with pytest.raises(sa.SourceAdmissionRefused, match="not permitted by this connector"):
            await service.admit_connector_fetch(_ctx(), _connector_fetch_admission(verifier_id="unknown"))

    async def test_a_fetch_target_outside_the_allowlist_is_refused_before_any_network_call(
        self, _patch_queries: FakeQueries
    ) -> None:
        _seed_connector(_patch_queries)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not be called: the locator is outside the allowlist")

        service = self._service_with_handler(_patch_queries, handler)
        escaped_locator = "https://evil.example/doc"
        with pytest.raises(sa.SourceAdmissionRefused, match="not in the connector's allowlist"):
            await service.admit_connector_fetch(
                _ctx(),
                _connector_fetch_admission(
                    source_revision_locator=escaped_locator,
                    claim=_claim(source_revision_locator=escaped_locator),
                ),
            )

    async def test_a_valid_fetch_is_recorded_and_returned(self, _patch_queries: FakeQueries) -> None:
        _seed_connector(_patch_queries)
        data = b"fetched document"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=data)

        service = self._service_with_handler(_patch_queries, handler)
        claim = _claim(source_content_digest=_digest_of(data), source_revision_locator="https://good.example/doc")
        evidence = await service.admit_connector_fetch(_ctx(), _connector_fetch_admission(claim=claim))

        assert evidence.source_content_digest == _digest_of(data)
        assert evidence.admission_method == "connector_fetch"
        assert evidence.connector_id == "connector-1"
        assert evidence.policy_id is None

    async def test_fetched_bytes_disagreeing_with_the_claim_digest_are_refused(
        self, _patch_queries: FakeQueries
    ) -> None:
        _seed_connector(_patch_queries)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"bytes that were never signed for")

        service = self._service_with_handler(_patch_queries, handler)
        claim = _claim(source_content_digest=_digest_of(b"something else entirely"))
        with pytest.raises(sa.SourceAdmissionRefused, match="does not match"):
            await service.admit_connector_fetch(_ctx(), _connector_fetch_admission(claim=claim))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    async def test_registering_a_connector_round_trips(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        registration = sa.ConnectorRegistration(
            connector_id="c-1",
            owning_scope="global",
            tenant_id=None,
            allowed_schemes=("https",),
            allowed_hosts=("example.com",),
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("v-1",),
            max_bytes=1024,
        )
        row = await service.register_connector(_ctx(), registration)
        assert row.connector_id == "c-1"
        assert _patch_queries.connectors["c-1"] is row

    async def test_registering_a_duplicate_connector_is_a_conflict(self, _patch_queries: FakeQueries) -> None:
        _seed_connector(_patch_queries)
        service = _build_service(_patch_queries)
        registration = sa.ConnectorRegistration(
            connector_id="connector-1",
            owning_scope="global",
            tenant_id=None,
            allowed_schemes=("https",),
            allowed_hosts=("example.com",),
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("v-1",),
            max_bytes=1024,
        )
        with pytest.raises(ConflictError):
            await service.register_connector(_ctx(), registration)

    async def test_registering_an_upload_policy_round_trips(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        registration = sa.UploadPolicyRegistration(
            policy_id="p-1",
            owning_scope="global",
            tenant_id=None,
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("v-1",),
            max_bytes=1024,
        )
        row = await service.register_upload_policy(_ctx(), registration)
        assert row.policy_id == "p-1"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    async def test_get_evidence_not_found(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        with pytest.raises(NotFoundError):
            await service.get_evidence(_ctx(), uuid.uuid4())

    async def test_get_body_not_found(self, _patch_queries: FakeQueries) -> None:
        service = _build_service(_patch_queries)
        with pytest.raises(NotFoundError):
            await service.get_body(_ctx(), uuid.uuid4())

    async def test_get_evidence_and_body_after_admission(self, _patch_queries: FakeQueries) -> None:
        _seed_policy(_patch_queries)
        service = _build_service(_patch_queries)
        data = b"document body"
        claim = _claim(source_content_digest=_digest_of(data))
        ctx = _ctx()
        admitted = await service.admit_upload(ctx, _upload_admission(claim=claim), _bytes_iter([data]))

        fetched = await service.get_evidence(ctx, admitted.source_evidence_id)
        assert fetched.source_evidence_id == admitted.source_evidence_id

        body, content_type = await service.get_body(ctx, admitted.source_evidence_id)
        assert body == data
        assert content_type == "text/markdown"
