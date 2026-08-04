"""Workspace PII detections reach the compliance log.

WorkspaceService.create_entry and update_entry funnel every PII scan through
scan_for_pii, and scan_for_pii is the one place that writes pii_detection_log
rows. This module proves that funnel holds against real Postgres:

  - detection rows land for every write path (create/update x body/references),
    including outcomes that never surface a client-visible signal (advisory);
  - a block outcome still logs the detection before it raises 422, and the
    workspace_entries row is never inserted;
  - a pii_field_policies row keyed by a pattern's real UUID actually changes
    the resolved policy — the regression case for the field-policy keying
    bug (field_policies used to be built as "field_type:<uuid>" while policy
    resolution looks up "field_type:<name>"; the two could never match).

WorkspaceService is exercised directly against a pg_container-backed
session_factory — no HTTP layer, no auth harness. exc.detail is inspected as
the dict the service raises, not the envelope FastAPI's error handler would
wrap it in.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.workspace import WorkspaceEntryRef, WorkspaceService
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)

# Visa test number -- passes Luhn, well-known in PCI test suites (matches the
# fixture tests/integration/test_pii_block.py uses for the same pattern).
_VISA_TEST_CC = "4111111111111111"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_actor(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh tenant and one actor in it. Returns (tenant_id, actor_id)."""
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tenant_id, "slug": f"ws-pii-{tenant_id.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'ws-pii-actor', :sub, :now)"
            ),
            {"aid": actor_id, "tid": tenant_id, "sub": f"sub-{actor_id.hex[:8]}", "now": _NOW},
        )
    return tenant_id, actor_id


async def _seed_pattern(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    category: str,
    policy_override: str | None,
) -> uuid.UUID:
    """Insert a tenant-scoped pii_patterns row for a built-in pattern name.

    Built-in pii_patterns rows are seeded only for the baseline migration's
    default tenant — every other tenant starts with none, so a per-tenant
    policy override (or a field policy targeting a specific pattern) has to
    be seeded explicitly to take effect. The regex column here is a
    placeholder: the built-in scanner detects matches with its own compiled
    pattern regardless of what this row's regex says. Only (name,
    policy_override) and, for field policies, this row's pattern_id are
    read back by scan_for_pii.
    """
    pattern_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO pii_patterns "
                "(pattern_id, tenant_id, name, category, regex, is_system, "
                " policy_override, is_enabled, created_at, created_by) "
                "VALUES (:pid, :tid, :name, :cat, '__sentinel__', FALSE, "
                "        :override, TRUE, :now, :aid)"
            ),
            {
                "pid": pattern_id,
                "tid": tenant_id,
                "name": name,
                "cat": category,
                "override": policy_override,
                "now": _NOW,
                "aid": actor_id,
            },
        )
    return pattern_id


async def _seed_field_policy(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    field_type: str,
    pattern_id: uuid.UUID,
    policy: str,
) -> None:
    """Insert a pii_field_policies row keyed to a pattern's real DB UUID.

    This is exactly the shape the field-policy keying fix targets: the row's
    pattern_id is the pattern's actual UUID, and scan_for_pii must translate
    it to the pattern's name before _resolve_policy's name-keyed lookup can
    ever match it.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO pii_field_policies "
                "(policy_id, tenant_id, field_type, pattern_id, policy, created_at) "
                "VALUES (:pid, :tid, :ft, :pattern_id, :policy, :now)"
            ),
            {
                "pid": uuid.uuid4(),
                "tid": tenant_id,
                "ft": field_type,
                "pattern_id": pattern_id,
                "policy": policy,
                "now": _NOW,
            },
        )


async def _count_detection_log(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    pattern_name: str,
) -> int:
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM pii_detection_log WHERE tenant_id = :tid AND pattern_name = :pname"),
            {"tid": tenant_id, "pname": pattern_name},
        )
        return int(result.scalar_one())


async def _count_entries(factory: async_sessionmaker[AsyncSession], *, workspace_id: uuid.UUID) -> int:
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM workspace_entries WHERE workspace_id = :wid"),
            {"wid": workspace_id},
        )
        return int(result.scalar_one())


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _service(factory: async_sessionmaker[AsyncSession]) -> WorkspaceService:
    """WorkspaceService wired to a real session_factory.

    visibility_svc is a stub: none of the methods this module exercises call
    it directly (visibility is enforced inline via role checks against
    TenantContext.roles inside get_workspace), so nothing here depends on
    its behaviour. audit_writer is a no-op stub for the same reason — audit
    emission is not what this module proves.
    """
    audit_writer = MagicMock()
    audit_writer.emit = AsyncMock(return_value=None)
    return WorkspaceService(
        session_factory=factory,
        visibility_svc=MagicMock(),
        audit_writer=audit_writer,
        clock=FakeClock(_NOW),
    )


async def _make_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID:
    ws = await _service(factory).create_workspace(_ctx(tenant_id, actor_id), name="pii-log-ws", owner_kind="actor")
    return ws.workspace_id


# ---------------------------------------------------------------------------
# Advisory outcomes still reach the compliance log — all four write paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_body_advisory_writes_detection_log(factory: async_sessionmaker[AsyncSession]) -> None:
    """create_entry body_md: an advisory match still lands in pii_detection_log."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory, tenant_id=tenant_id, actor_id=actor_id, name="email", category="CONTACT", policy_override=None
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)

    ref = await svc.create_entry(
        _ctx(tenant_id, actor_id),
        workspace_id=workspace_id,
        kind="note",
        body_md="Contact person: alice@example.com",
        reference_ids=[],
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is None, "advisory outcome must carry no client-visible signal"
    count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="email")
    assert count >= 1, "advisory outcome must still write a pii_detection_log row"


@pytest.mark.asyncio
async def test_create_references_advisory_writes_detection_log(factory: async_sessionmaker[AsyncSession]) -> None:
    """create_entry references_jsonb: an advisory match still lands in pii_detection_log."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory, tenant_id=tenant_id, actor_id=actor_id, name="email", category="CONTACT", policy_override=None
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)

    ref = await svc.create_entry(
        _ctx(tenant_id, actor_id),
        workspace_id=workspace_id,
        kind="saved_query",
        body_md="Clean body, no PII here.",
        reference_ids=[],
        references_jsonb={"contact": "bob@example.com"},
    )

    assert ref.warnings is None
    count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="email")
    assert count >= 1, "advisory outcome on references_jsonb must still write a pii_detection_log row"


@pytest.mark.asyncio
async def test_update_body_advisory_writes_detection_log(factory: async_sessionmaker[AsyncSession]) -> None:
    """update_entry body_md: an advisory match still lands in pii_detection_log."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory, tenant_id=tenant_id, actor_id=actor_id, name="ssn", category="GOVERNMENT_ID", policy_override=None
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)
    created = await svc.create_entry(
        _ctx(tenant_id, actor_id),
        workspace_id=workspace_id,
        kind="note",
        body_md="Clean body.",
        reference_ids=[],
    )

    ref = await svc.update_entry(
        _ctx(tenant_id, actor_id),
        entry_id=created.entry_id,
        body_md="SSN on file: 123-45-6789",
    )

    assert ref.warnings is None
    count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="ssn")
    assert count >= 1, "advisory outcome on update body_md must still write a pii_detection_log row"


@pytest.mark.asyncio
async def test_update_references_advisory_writes_detection_log(factory: async_sessionmaker[AsyncSession]) -> None:
    """update_entry references_jsonb: an advisory match still lands in pii_detection_log."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory, tenant_id=tenant_id, actor_id=actor_id, name="phone", category="CONTACT", policy_override=None
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)
    created = await svc.create_entry(
        _ctx(tenant_id, actor_id),
        workspace_id=workspace_id,
        kind="saved_query",
        body_md="Clean query body.",
        reference_ids=[],
    )

    ref = await svc.update_entry(
        _ctx(tenant_id, actor_id),
        entry_id=created.entry_id,
        references_jsonb={"phone": "+1-800-555-0100"},
    )

    assert ref.warnings is None
    count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="phone")
    assert count >= 1, "advisory outcome on update references_jsonb must still write a pii_detection_log row"


# ---------------------------------------------------------------------------
# Block outcome: detection log + 422, entry never inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_body_block_logs_detection_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """create_entry body_md block: detection row written, 422 raised, no entry row."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        name="credit_card",
        category="FINANCIAL",
        policy_override="block",
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            _ctx(tenant_id, actor_id),
            workspace_id=workspace_id,
            kind="note",
            body_md=f"Card on file: {_VISA_TEST_CC}.",
            reference_ids=[],
        )

    exc = exc_info.value
    assert exc.status_code == 422
    detail = exc.detail
    assert isinstance(detail, dict), f"exc.detail must be the service's dict, not an HTTP envelope; got {detail!r}"
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "workspace_entry.body"
    assert "FINANCIAL" in detail["categories"]

    log_count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="credit_card")
    assert log_count >= 1, "a blocked write must still log the detection"

    entry_count = await _count_entries(factory, workspace_id=workspace_id)
    assert entry_count == 0, "no workspace_entries row must be written when the scan blocks"


@pytest.mark.asyncio
async def test_update_body_block_logs_detection_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """update_entry body_md block: detection row written, 422 raised, row left untouched."""
    tenant_id, actor_id = await _seed_actor(factory)
    await _seed_pattern(
        factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        name="credit_card",
        category="FINANCIAL",
        policy_override="block",
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)
    created = await svc.create_entry(
        _ctx(tenant_id, actor_id),
        workspace_id=workspace_id,
        kind="note",
        body_md="Original clean body.",
        reference_ids=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        await svc.update_entry(
            _ctx(tenant_id, actor_id),
            entry_id=created.entry_id,
            body_md=f"Replacement card on file: {_VISA_TEST_CC}.",
        )

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "workspace_entry.body"

    log_count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="credit_card")
    assert log_count >= 1, "a blocked update must still log the detection"

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT body_md FROM workspace_entries WHERE entry_id = :eid"),
                {"eid": created.entry_id},
            )
        ).first()
    assert row is not None
    assert row[0] == "Original clean body.", "a blocked update must not modify the existing row"


# ---------------------------------------------------------------------------
# Regression: pii_field_policies keyed by a pattern's real UUID must fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_policy_keyed_by_pattern_id_blocks_write(factory: async_sessionmaker[AsyncSession]) -> None:
    """A pii_field_policies row keyed by a pattern's real UUID must take effect.

    Before the fix, field_policies was built as f"{field_type}:{pattern_id}"
    (a UUID) while _resolve_policy looks up f"{field_type}:{pattern_name}" —
    the two could never match, so this field policy was silently ignored and
    the write below would have gone through as advisory (tenant default).
    With the id -> name translation in place, the block policy actually
    resolves and fires.
    """
    tenant_id, actor_id = await _seed_actor(factory)
    pattern_id = await _seed_pattern(
        factory, tenant_id=tenant_id, actor_id=actor_id, name="phone", category="CONTACT", policy_override=None
    )
    await _seed_field_policy(
        factory,
        tenant_id=tenant_id,
        field_type="workspace_entry.body",
        pattern_id=pattern_id,
        policy="block",
    )
    workspace_id = await _make_workspace(factory, tenant_id=tenant_id, actor_id=actor_id)
    svc = _service(factory)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            _ctx(tenant_id, actor_id),
            workspace_id=workspace_id,
            kind="note",
            body_md="Call me at +1-800-555-0100.",
            reference_ids=[],
        )

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "workspace_entry.body"
    assert "CONTACT" in detail["categories"]

    log_count = await _count_detection_log(factory, tenant_id=tenant_id, pattern_name="phone")
    assert log_count >= 1

    entry_count = await _count_entries(factory, workspace_id=workspace_id)
    assert entry_count == 0, "the field-policy block must prevent the INSERT"
