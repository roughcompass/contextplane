"""Unit tests for `registry/arc/service/receipt_read.py` — `ReceiptReader`.

This module had no dedicated unit suite before this file: unit-scope
coverage was 38% (48 statements, 30 missed), entirely from the constructor
running incidentally wherever something imports the module. Every read
method's own body -- the authorization gate, the not-found/not-yours
indistinguishability the module's own docstring names as a rule, and the
per-directive audience redaction -- was untested at the unit tier.

Coverage:
- get_receipt         — full shape, including nested `selected` rows
- get_receipt         — missing receipt -> NotFoundError
- get_receipt         — authorized-tenant-but-wrong-reader -> NotFoundError,
  with the same message shape as "missing" (the module's own stated
  invariant: existence is not something an unauthorized caller learns)
- explain             — full shape, including the event chain
- _selected (via get_receipt's `selected` field) / `_audience_permits` —
  all three `DetailAudience` values, permitted and denied, plus the
  closed-set fallback for a value that matches none of them
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.arc.service.receipt_read import ReceiptReader, _audience_permits
from registry.arc.types import ArcRequestContext, DetailAudience
from registry.exceptions import NotFoundError
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_RECEIPT_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


def _arc_ctx(*, roles: list[str] | None = None, mcp_session_id: str | None = None) -> ArcRequestContext:
    return ArcRequestContext(
        tenant=tenant_context(roles=roles),
        oidc_issuer="https://idp.example.test",
        mcp_session_id=mcp_session_id,
    )


def _receipt_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        receipt_id=_RECEIPT_ID,
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        host_id="host-1",
        session_id=None,
        manifest_fingerprint="fp-1",
        attestation_id=None,
        resolution_status="resolved",
        selection_engine_version="v1",
        registry_build_revision="rev-1",
        canonical_profile_versions={"default": "1"},
        selection_config_digest="digest-1",
        evaluated_at=_NOW,
        freshness_basis="live",
        blocked_reasons=None,
        degraded_reasons=None,
        mandatory_directive_count=1,
        rendered_content_bytes=100,
        budget_limit_bytes=1000,
        integrity_state="intact",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _selected_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        artifact_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        directive_id=uuid.uuid4(),
        is_mandatory=True,
        was_omitted=False,
        omission_reason=None,
        source_locator="s3://bucket/key",
        source_revision_locator="s3://bucket/key@rev-1",
        content_digest="sha256:abc",
        detail_audience=DetailAudience.ALL_MATCHED_ACTORS.value,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _event_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        sequence=1,
        event_type="resolution_created",
        event_source="selection_engine",
        event_payload={"k": "v"},
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResult:
    """Stand-in for the SQLAlchemy `Result` returned by `session.execute`."""

    def __init__(self, *, one: object = None, rows: list[object] | None = None) -> None:
        self._one = one
        self._rows = list(rows or [])

    def one_or_none(self) -> object:
        return self._one

    def all(self) -> list[object]:
        return self._rows


def _build_reader(
    *,
    receipt_row: SimpleNamespace | None,
    selected_rows: list[SimpleNamespace] | None = None,
    event_rows: list[SimpleNamespace] | None = None,
    can_read: bool = True,
) -> tuple[ReceiptReader, MagicMock]:
    """A `ReceiptReader` wired with a session whose `execute` routes on the
    SQL text -- there is no DB here, only three distinct queries this
    module issues, keyed by the table name each one names."""

    async def _execute(stmt: object, _params: object = None) -> _FakeResult:
        sql = str(stmt)
        if "arc_receipt_selected_directives" in sql:
            return _FakeResult(rows=selected_rows)
        if "arc_receipt_events" in sql:
            return _FakeResult(rows=event_rows)
        if "FROM arc_receipts" in sql:
            return _FakeResult(one=receipt_row)
        msg = f"unexpected query in test double: {sql}"
        raise AssertionError(msg)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session_cm)

    authorization = MagicMock()
    authorization.can_read_receipt = MagicMock(return_value=can_read)

    reader = ReceiptReader(session_factory, authorization=authorization)
    return reader, authorization


# ---------------------------------------------------------------------------
# get_receipt
# ---------------------------------------------------------------------------


class TestGetReceipt:
    async def test_returns_the_full_shape_with_selected_rows(self) -> None:
        row = _receipt_row()
        selected = _selected_row()
        reader, authorization = _build_reader(receipt_row=row, selected_rows=[selected])
        ctx = _arc_ctx()

        result = await reader.get_receipt(ctx, _RECEIPT_ID)

        assert result["receipt_id"] == str(_RECEIPT_ID)
        assert result["tenant_id"] == str(_TENANT_ID)
        assert result["resolution_status"] == "resolved"
        assert result["evaluated_at"] == _NOW.isoformat()
        assert result["blocked_reasons"] == []
        assert len(result["selected"]) == 1
        assert result["selected"][0]["artifact_id"] == str(selected.artifact_id)
        authorization.can_read_receipt.assert_called_once_with(
            ctx,
            receipt_tenant_id=_TENANT_ID,
            receipt_actor_id=_ACTOR_ID,
        )

    async def test_missing_receipt_raises_not_found(self) -> None:
        reader, authorization = _build_reader(receipt_row=None)

        with pytest.raises(NotFoundError, match=f"receipt {_RECEIPT_ID} not found"):
            await reader.get_receipt(_arc_ctx(), _RECEIPT_ID)

        authorization.can_read_receipt.assert_not_called()

    async def test_unauthorized_reader_raises_not_found_with_the_same_message_as_missing(self) -> None:
        """The module's own stated rule: a receipt that exists but that this
        caller may not read raises the identical `NotFoundError` a missing
        receipt would -- existence itself is not disclosed."""
        row = _receipt_row()
        reader, authorization = _build_reader(receipt_row=row, can_read=False)

        with pytest.raises(NotFoundError) as denied_exc:
            await reader.get_receipt(_arc_ctx(), _RECEIPT_ID)

        reader_missing, _ = _build_reader(receipt_row=None)
        with pytest.raises(NotFoundError) as missing_exc:
            await reader_missing.get_receipt(_arc_ctx(), _RECEIPT_ID)

        assert str(denied_exc.value) == str(missing_exc.value)
        authorization.can_read_receipt.assert_called_once()


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    async def test_returns_explanation_with_budget_and_event_chain(self) -> None:
        row = _receipt_row()
        event = _event_row(sequence=2, event_type="detail_denied")
        reader, _ = _build_reader(receipt_row=row, selected_rows=[], event_rows=[event])

        result = await reader.explain(_arc_ctx(), _RECEIPT_ID)

        assert result["receipt_id"] == str(_RECEIPT_ID)
        assert result["budget"] == {"rendered_content_bytes": 100, "budget_limit_bytes": 1000}
        assert result["selected"] == []
        assert len(result["events"]) == 1
        assert result["events"][0]["sequence"] == 2
        assert result["events"][0]["event_type"] == "detail_denied"
        assert result["events"][0]["created_at"] == _NOW.isoformat()


# ---------------------------------------------------------------------------
# Audience redaction — DetailAudience x permitted/denied
# ---------------------------------------------------------------------------


class TestAudienceRedaction:
    async def test_all_matched_actors_is_never_redacted(self) -> None:
        row = _receipt_row()
        selected = _selected_row(detail_audience=DetailAudience.ALL_MATCHED_ACTORS.value)
        reader, _ = _build_reader(receipt_row=row, selected_rows=[selected])

        result = await reader.get_receipt(_arc_ctx(roles=["consumer"]), _RECEIPT_ID)

        item = result["selected"][0]
        assert item["audience_redacted"] is False
        assert item["source_locator"] == selected.source_locator
        assert item["content_digest"] == selected.content_digest

    async def test_tenant_admin_auditor_permitted_for_admin_role(self) -> None:
        row = _receipt_row()
        selected = _selected_row(detail_audience=DetailAudience.TENANT_ADMIN_AUDITOR.value)
        reader, _ = _build_reader(receipt_row=row, selected_rows=[selected])

        result = await reader.get_receipt(_arc_ctx(roles=["admin"]), _RECEIPT_ID)

        item = result["selected"][0]
        assert item["audience_redacted"] is False
        assert item["source_revision_locator"] == selected.source_revision_locator

    async def test_tenant_admin_auditor_denied_for_consumer_role(self) -> None:
        row = _receipt_row()
        selected = _selected_row(detail_audience=DetailAudience.TENANT_ADMIN_AUDITOR.value)
        reader, _ = _build_reader(receipt_row=row, selected_rows=[selected])

        result = await reader.get_receipt(_arc_ctx(roles=["consumer"]), _RECEIPT_ID)

        item = result["selected"][0]
        assert item["audience_redacted"] is True
        assert item["source_locator"] is None
        assert item["source_revision_locator"] is None
        assert item["content_digest"] is None
        # Non-source fields are unaffected by redaction.
        assert item["artifact_id"] == str(selected.artifact_id)

    async def test_registered_gateway_only_permitted_for_mcp_session(self) -> None:
        row = _receipt_row()
        selected = _selected_row(detail_audience=DetailAudience.REGISTERED_GATEWAY_ONLY.value)
        reader, _ = _build_reader(receipt_row=row, selected_rows=[selected])

        result = await reader.get_receipt(_arc_ctx(mcp_session_id="sess-1"), _RECEIPT_ID)

        assert result["selected"][0]["audience_redacted"] is False

    async def test_registered_gateway_only_denied_without_mcp_session(self) -> None:
        row = _receipt_row()
        selected = _selected_row(detail_audience=DetailAudience.REGISTERED_GATEWAY_ONLY.value)
        reader, _ = _build_reader(receipt_row=row, selected_rows=[selected])

        result = await reader.get_receipt(_arc_ctx(mcp_session_id=None), _RECEIPT_ID)

        assert result["selected"][0]["audience_redacted"] is True


class TestAudiencePermitsFallback:
    def test_unknown_audience_value_defaults_to_denied(self) -> None:
        """`_audience_permits` matches by identity against the three closed
        `DetailAudience` members. Anything else -- defensively, since the
        enum is closed and a real row can't produce one -- falls through to
        the final `return False`, not an unhandled case that reads as
        permitted."""
        sentinel = object()
        assert _audience_permits(_arc_ctx(roles=["admin"], mcp_session_id="sess-1"), sentinel) is False  # type: ignore[arg-type]
