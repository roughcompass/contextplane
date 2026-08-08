"""The legacy evidence-activation bypass is gone, symbol and behavior.

Two things used to let a revision activate without real projection-approval
verification: a constructor flag on `ArtifactService` that a deployment
could leave enabled, and an attach route that would bind any evidence to a
revision regardless of its type. `! git grep -n
"approval_verification_enabled"` proves the flag's *name* is gone
everywhere; the tests below prove the *behavior* is gone too, including
from the one place a symbol-exact grep cannot see: the constructor's
actual call signature.
"""

from __future__ import annotations

import inspect
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.api.routers import arc_admin
from registry.arc.service.artifact import ArtifactService, EvidenceTypeNotWritableError
from registry.arc.service.artifact_integrity import ATTACHABLE_EVIDENCE_TYPES, _assert_evidence_approves
from registry.arc.types import ArcRequestContext
from registry.exceptions import NotFoundError, ValidationError
from registry.types import TenantContext

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_REVISION_ID = uuid.uuid4()
_EVIDENCE_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Symbol-exact: the constructor no longer accepts the flag, under any name
# ---------------------------------------------------------------------------


def test_artifact_service_constructor_has_no_verification_flag() -> None:
    """A grep proves the string is gone; this proves the *parameter* is gone
    -- a rename to some other flag name would still pass the grep but not
    this. `ArtifactService.__init__` takes exactly its three collaborators
    and nothing that could disable a trust check.
    """
    params = set(inspect.signature(ArtifactService.__init__).parameters)
    assert params == {"self", "session_factory", "authorization", "clock"}


def test_constructing_with_the_old_flag_name_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        ArtifactService(  # type: ignore[call-arg]
            MagicMock(),
            authorization=MagicMock(),
            clock=MagicMock(),
            approval_verification_enabled=True,
        )


# ---------------------------------------------------------------------------
# attach_approval_evidence: the write-side half of the bypass
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for the SQLAlchemy `Result` returned by `session.execute`.

    Every accessor method returns the same configured row -- callers here
    only ever use one of them per query, and never need more than one row.
    """

    def __init__(self, *, row: object = None) -> None:
        self._row = row

    def one_or_none(self) -> object:
        return self._row

    def one(self) -> object:
        if self._row is None:
            raise AssertionError("one() called with no row configured")
        return self._row


def _artifacts_service(*, evidence_row: object) -> ArtifactService:
    """An `ArtifactService` whose session answers exactly the queries
    `attach_approval_evidence` issues before it reaches the evidence-type
    check: the revision lock/lookup (three queries -- see `_lock_family`),
    the artifact lookup, and then the evidence lookup itself -- routed by
    which table and clause each query names, matching this repo's
    SQL-string-keyed test-double convention."""

    _artifact_id = uuid.uuid4()

    async def _execute(stmt: object, _params: object = None) -> _FakeResult:
        sql = str(stmt)
        if "SELECT artifact_id FROM arc_revisions WHERE revision_id" in sql:
            return _FakeResult(row=SimpleNamespace(artifact_id=_artifact_id))
        if "FOR UPDATE" in sql:
            return _FakeResult()
        if "SELECT revision_id, artifact_id, lifecycle_state" in sql:
            return _FakeResult(
                row=SimpleNamespace(
                    revision_id=_REVISION_ID,
                    artifact_id=_artifact_id,
                    lifecycle_state="draft",
                    review_expires_at=None,
                    approval_evidence_id=None,
                )
            )
        if "FROM arc_artifacts" in sql:
            return _FakeResult(row=SimpleNamespace(artifact_id=_artifact_id, tenant_id=_TENANT_ID))
        if "FROM arc_approval_evidence" in sql:
            return _FakeResult(row=evidence_row)
        msg = f"unexpected query in test double: {sql}"
        raise AssertionError(msg)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    # `session.begin()` is used as its own async context manager alongside
    # `session_factory()` -- `async with factory() as session, session.begin():`
    # -- so it needs to return one, not the coroutine an AsyncMock's
    # auto-mocked attribute would return when called.
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session_cm)

    authorization = MagicMock()
    authorization.assert_can_write_artifact = MagicMock(return_value=None)

    return ArtifactService(session_factory, authorization=authorization, clock=MagicMock())


def _ctx() -> ArcRequestContext:
    tenant = TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["admin"], oidc_subject="s")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"})


class TestAttachApprovalEvidence:
    async def test_refuses_artifact_activation_evidence(self) -> None:
        row = SimpleNamespace(evidence_type="artifact_activation", approved_revision_id=_REVISION_ID)
        service = _artifacts_service(evidence_row=row)

        with pytest.raises(EvidenceTypeNotWritableError, match="artifact_activation"):
            await service.attach_approval_evidence(_ctx(), _REVISION_ID, _EVIDENCE_ID)

    async def test_refuses_gateway_emergency_bypass_evidence_too(self) -> None:
        """Not a single-value special case: every type outside the
        allowlist is refused, not just the one this task names."""
        row = SimpleNamespace(evidence_type="gateway_emergency_bypass", approved_revision_id=None)
        service = _artifacts_service(evidence_row=row)

        with pytest.raises(EvidenceTypeNotWritableError):
            await service.attach_approval_evidence(_ctx(), _REVISION_ID, _EVIDENCE_ID)

    async def test_exception_approval_passes_the_type_check(self) -> None:
        """The positive control: `exception_approval` is not refused by the
        type gate -- it fails the *next* check instead (this evidence does
        not name `_REVISION_ID`), proving the gate is exactly this one type
        restriction and not a blanket refusal of the whole route."""
        row = SimpleNamespace(evidence_type="exception_approval", approved_revision_id=None)
        service = _artifacts_service(evidence_row=row)

        with pytest.raises(Exception) as excinfo:
            await service.attach_approval_evidence(_ctx(), _REVISION_ID, _EVIDENCE_ID)
        assert not isinstance(excinfo.value, EvidenceTypeNotWritableError)

    async def test_unknown_evidence_id_is_not_found_before_the_type_check(self) -> None:
        service = _artifacts_service(evidence_row=None)

        with pytest.raises(NotFoundError):
            await service.attach_approval_evidence(_ctx(), _REVISION_ID, _EVIDENCE_ID)

    def test_attachable_evidence_types_is_exactly_exception_approval(self) -> None:
        assert ATTACHABLE_EVIDENCE_TYPES == frozenset({"exception_approval"})


class TestTheOneAttachableTypeStillCannotActivate:
    """Why removing the flag did not open a path to activation.

    Deleting `approval_verification_enabled` left `approval_evidence_id IS
    NOT NULL` as the gate a draft must pass to activate, and
    `attach_approval_evidence` permits exactly one `evidence_type` through:
    `exception_approval`. Read together, those two facts look like a way in
    -- attach the one permitted type, then activate.

    It is closed, but by something narrower than either check: the only
    production writer of `exception_approval` (`ExceptionService`) populates
    `approved_exception_id` and never `approved_revision_id`, so the column
    activation binds on is NULL on every such row, and
    `_assert_evidence_approves` refuses it.

    That is a *contingent* guarantee -- it holds because of what one INSERT
    statement in another module happens to list. Adding
    `approved_revision_id` to that statement would make activation reachable
    with no projection approval and none of the ten trust predicates, and
    nothing else in the tree would notice. These tests pin the link so that
    change has to break a test rather than a deployment.
    """

    async def test_evidence_that_names_no_revision_is_refused(self) -> None:
        """The load-bearing step: NULL `approved_revision_id` cannot approve."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        with pytest.raises(NotFoundError):
            await _assert_evidence_approves(session, _EVIDENCE_ID, _REVISION_ID)

    async def test_evidence_naming_a_different_revision_is_refused(self) -> None:
        """And borrowing another revision's approval is refused too, so the
        refusal above is not the only thing standing here."""
        other = uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=other)))

        with pytest.raises(ValidationError):
            await _assert_evidence_approves(session, _EVIDENCE_ID, _REVISION_ID)

    def test_the_exception_writer_does_not_populate_the_activation_binding(self) -> None:
        """Pins the contingent fact itself, at its source.

        If `ExceptionService`'s INSERT ever names `approved_revision_id`,
        this fails -- and whoever makes that change is then required to
        reckon with activation reachability rather than discover it later.
        """
        source = (
            pathlib.Path(__file__).resolve().parents[2] / "registry" / "arc" / "service" / "approved_exceptions.py"
        ).read_text(encoding="utf-8")
        insert_start = source.index("INSERT INTO arc_approval_evidence")
        statement = source[insert_start : source.index(")", source.index("VALUES", insert_start))]
        assert "approved_revision_id" not in statement, (
            "ExceptionService now writes approved_revision_id. An exception_approval row can "
            "therefore satisfy _assert_evidence_approves, and with attach permitting that type, "
            "activation becomes reachable without projection approval or the ten predicates."
        )


# ---------------------------------------------------------------------------
# Request test: the legacy route refused end to end
# ---------------------------------------------------------------------------


class _RefusingArtifacts:
    async def attach_approval_evidence(self, ctx: object, revision_id: object, evidence_id: object) -> None:
        msg = f"evidence_type {'artifact_activation'!r} has no first-party writer in this deployment"
        raise EvidenceTypeNotWritableError(msg)


def _fake_request() -> SimpleNamespace:
    services = SimpleNamespace(arc_artifacts=_RefusingArtifacts())
    return SimpleNamespace(
        state=SimpleNamespace(oidc_claims={"iss": "https://idp.example.test"}),
        app=SimpleNamespace(state=SimpleNamespace(services=services)),
    )


@pytest.mark.asyncio
async def test_a_direct_artifact_activation_write_through_the_legacy_route_is_refused() -> None:
    """The request-level proof: a caller naming `artifact_activation`
    evidence on `POST .../revisions/{revision_id}/approval-evidence` gets
    `arc_evidence_type_not_writable` back, not a 500 and not a success.
    """
    request = _fake_request()
    ctx = TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["admin"], oidc_subject="s")
    body = arc_admin.AttachEvidenceRequest(evidence_id=_EVIDENCE_ID)

    with pytest.raises(Exception) as excinfo:
        await arc_admin.attach_approval_evidence(request, body, ctx, _REVISION_ID)  # type: ignore[arg-type]

    refusal = excinfo.value
    assert getattr(refusal, "status_code", None) == 409
    assert refusal.detail[0]["code"] == "arc_evidence_type_not_writable"  # type: ignore[union-attr]


def test_the_route_still_accepts_exception_approval_shaped_requests() -> None:
    """Non-goal check: this task must not remove `exception_approval` or
    its writer. The route's request body still closes over a bare
    `evidence_id` -- callers were never able to name `evidence_type` on
    this route, so nothing about the exception path's request shape moved.
    """
    body = arc_admin.AttachEvidenceRequest(evidence_id=_EVIDENCE_ID)
    assert body.evidence_id == _EVIDENCE_ID
