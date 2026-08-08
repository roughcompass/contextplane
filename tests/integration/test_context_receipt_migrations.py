"""Receipt tables: what the migration builds, and what it refuses.

A receipt is only worth keeping if it is checkable, so the constraints here are
the ones that make a stored receipt readable later: an arm that degraded says
why, an item carries the contract's stable id rather than a position, and a
degraded answer cannot be recorded as cacheable.

The parity test follows the precedent set by the task-memory and external-
reference suites: a column in the migration but not the ORM is invisible to
service code, and one in the ORM but not the database fails at query time
rather than at import.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from contextplane.context.models_receipt import ContextReceipt, ContextReceiptArm, ContextReceiptItem
from contextplane.context.schemas.envelope import BLOCK_NAMES
from contextplane.context.schemas.trust import ReceiptItemIdV1

_MODELS = (ContextReceipt, ContextReceiptArm, ContextReceiptItem)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    tid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n) ON CONFLICT DO NOTHING"),
            {"t": tid, "s": f"rc-{tid.hex[:8]}", "n": "receipt test"},
        )
    return tid


def _receipt(
    conn: object,
    tenant: uuid.UUID,
    *,
    state: str = "complete",
    cacheable: bool = True,
    task_id: uuid.UUID | None = None,
) -> uuid.UUID:
    rid = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO context_receipts (receipt_id, tenant_id, task_id, state, cacheable, requested_by)
            VALUES (:rid, :tid, :task, :state, :cacheable, 'actor:alice')
            """
        ),
        {"rid": rid, "tid": tenant, "task": task_id, "state": state, "cacheable": cacheable},
    )
    return rid


def _arm(conn: object, receipt: uuid.UUID, block: str, state: str, reason: str | None = None) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO context_receipt_arms (arm_id, receipt_id, block, state, reason)
            VALUES (:aid, :rid, :b, :s, :r)
            """
        ),
        {"aid": uuid.uuid4(), "rid": receipt, "b": block, "s": state, "r": reason},
    )


def _item(
    conn: object,
    receipt: uuid.UUID,
    *,
    block: str = "arc",
    source: str = "github",
    item_key: str = "k1",
    receipt_item_id: str | None = None,
) -> str:
    identity = receipt_item_id or ReceiptItemIdV1(block=block, source=source, item_key=item_key).value()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO context_receipt_items (item_row_id, receipt_id, receipt_item_id, block, source, item_key)
            VALUES (:row, :rid, :iid, :b, :src, :key)
            """
        ),
        {"row": uuid.uuid4(), "rid": receipt, "iid": identity, "b": block, "src": source, "key": item_key},
    )
    return identity


# --- fresh install --------------------------------------------------------------


@pytest.mark.parametrize("table", ["context_receipts", "context_receipt_arms", "context_receipt_items"])
def test_the_migration_creates_every_table(sync_engine: Engine, table: str) -> None:
    assert inspect(sync_engine).has_table(table)


def test_the_orm_and_the_database_agree_column_for_column(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    for model in _MODELS:
        live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        declared = {column.name for column in model.__table__.columns}
        assert declared == live, (
            f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, "
            f"database-only {sorted(live - declared)}"
        )


@pytest.mark.parametrize(
    ("table", "index"),
    [
        ("context_receipts", "ix_receipt_tenant_time"),
        ("context_receipts", "ix_receipt_task"),
        ("context_receipt_arms", "uq_receipt_arm"),
        ("context_receipt_items", "uq_receipt_item"),
        ("context_receipt_items", "ix_receipt_item_identity"),
        ("context_receipt_items", "ix_receipt_item_block"),
    ],
)
def test_the_join_paths_have_their_indexes(sync_engine: Engine, table: str, index: str) -> None:
    with sync_engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})
        }
    assert index in names, f"missing {index}; have {sorted(names)}"


# --- the receipt as a whole -------------------------------------------------------


def test_a_degraded_answer_cannot_be_recorded_as_cacheable(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Caching it would outlive the failure that caused it, and the cached copy
    carries no sign it was degraded when it was taken."""
    with pytest.raises(Exception, match="ck_receipt_degraded_is_not_cacheable"), sync_engine.begin() as conn:
        _receipt(conn, tenant_id, state="degraded", cacheable=True)


def test_a_blocked_answer_cannot_be_recorded_as_cacheable(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_receipt_degraded_is_not_cacheable"), sync_engine.begin() as conn:
        _receipt(conn, tenant_id, state="blocked", cacheable=True)


def test_a_degraded_answer_is_storable_when_it_says_it_is_not_cacheable(
    sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The rule is about the pair, not about degraded answers being unstorable —
    a degraded resolution is exactly the one worth having a receipt for."""
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id, state="degraded", cacheable=False)
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT state, cacheable FROM context_receipts WHERE receipt_id = :r"), {"r": receipt}
        ).one()
    assert stored == ("degraded", False)


def test_an_unknown_envelope_state_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_receipt_state"), sync_engine.begin() as conn:
        _receipt(conn, tenant_id, state="mostly-fine", cacheable=False)


def test_a_receipt_records_who_asked(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """An unattributed receipt cannot answer the question it exists for."""
    with pytest.raises(Exception, match="ck_receipt_requested_by_present"), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, requested_by)
                VALUES (:rid, :tid, 'complete', TRUE, '')
                """
            ),
            {"rid": uuid.uuid4(), "tid": tenant_id},
        )


# --- per-arm state ----------------------------------------------------------------


def test_every_block_of_the_envelope_is_a_legal_arm(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The closed set here and the four blocks the envelope contract fixes must
    be the same four, or a legal response cannot be recorded."""
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        for block in BLOCK_NAMES:
            _arm(conn, receipt, block, "empty")
    with sync_engine.connect() as conn:
        stored = {
            row[0]
            for row in conn.execute(
                text("SELECT block FROM context_receipt_arms WHERE receipt_id = :r"), {"r": receipt}
            )
        }
    assert stored == set(BLOCK_NAMES)


def test_an_unknown_arm_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_receipt_arm_block"), sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _arm(conn, receipt, "extra_block", "empty")


@pytest.mark.parametrize("state", ["degraded", "failed"])
def test_an_arm_that_did_not_succeed_must_say_why(sync_engine: Engine, tenant_id: uuid.UUID, state: str) -> None:
    """A degraded arm with no reason is a dead end for whoever has to explain
    the response."""
    with pytest.raises(Exception, match="ck_receipt_arm_reason_when_not_ok"), sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id, state="degraded", cacheable=False)
        _arm(conn, receipt, "arc", state, None)


@pytest.mark.parametrize("state", ["success", "empty"])
def test_an_arm_that_succeeded_needs_no_reason(sync_engine: Engine, tenant_id: uuid.UUID, state: str) -> None:
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _arm(conn, receipt, "canonical", state, None)
    with sync_engine.connect() as conn:
        reason = conn.execute(
            text("SELECT reason FROM context_receipt_arms WHERE receipt_id = :r"), {"r": receipt}
        ).scalar_one()
    assert reason is None


def test_one_row_per_arm_per_receipt(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Two rows for one arm would leave the response's own state underivable
    from its record."""
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _arm(conn, receipt, "workspace", "empty")
    with pytest.raises(Exception, match="uq_receipt_arm"), sync_engine.begin() as conn:
        _arm(conn, receipt, "workspace", "success")


# --- receipt items -----------------------------------------------------------------


def test_an_item_carries_the_contracts_stable_id(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The digest of block, source and item key — so the same item resolved
    twice gets the same line, and a citation into a receipt does not rot."""
    expected = ReceiptItemIdV1(block="arc", source="github", item_key="k1").value()
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        stored_id = _item(conn, receipt, block="arc", source="github", item_key="k1")
    assert stored_id == expected
    with sync_engine.connect() as conn:
        found = conn.execute(
            text("SELECT receipt_item_id FROM context_receipt_items WHERE receipt_id = :r"), {"r": receipt}
        ).scalar_one()
    assert found == expected


def test_one_line_per_item_per_receipt(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The same item twice in one resolution is a duplicate, and a reader
    counting sources would over-weight it."""
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _item(conn, receipt)
    with pytest.raises(Exception, match="uq_receipt_item"), sync_engine.begin() as conn:
        _item(conn, receipt)


def test_two_receipts_may_return_the_same_item(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Which is the whole point of a stable id: it is what makes a receipt
    checkable across resolutions rather than only within one."""
    # A key unique to this test: the default one is shared by every other test
    # in the module, and they all write to the same database.
    key = f"shared-{uuid.uuid4().hex[:8]}"
    with sync_engine.begin() as conn:
        first = _receipt(conn, tenant_id)
        second = _receipt(conn, tenant_id)
        identity = _item(conn, first, item_key=key)
        _item(conn, second, item_key=key)
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM context_receipt_items WHERE receipt_item_id = :i"), {"i": identity}
        ).scalar_one()
    assert count == 2


def test_an_item_without_identity_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_receipt_item_identity_present"), sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _item(conn, receipt, source="", receipt_item_id="deadbeef")


# --- deletion ------------------------------------------------------------------------


def test_deleting_a_receipt_removes_its_arms_and_items(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        _arm(conn, receipt, "canonical", "success")
        _item(conn, receipt)
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM context_receipts WHERE receipt_id = :r"), {"r": receipt})
    with sync_engine.connect() as conn:
        arms = conn.execute(
            text("SELECT count(*) FROM context_receipt_arms WHERE receipt_id = :r"), {"r": receipt}
        ).scalar_one()
        items = conn.execute(
            text("SELECT count(*) FROM context_receipt_items WHERE receipt_id = :r"), {"r": receipt}
        ).scalar_one()
    assert (arms, items) == (0, 0)


def test_deleting_a_receipt_leaves_external_references_alone(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Other receipts and other checkpoints cite the same external things. A
    cascade from a resolution to the things it happened to name would delete
    evidence that was never this resolution's to own."""
    reference = uuid.uuid4()
    with sync_engine.begin() as conn:
        receipt = _receipt(conn, tenant_id)
        conn.execute(
            text(
                """
                INSERT INTO context_external_references
                    (reference_id, tenant_id, source_system, source_namespace, kind, external_id,
                     classification, external_authority, collision_key)
                VALUES (:rid, :tid, 'github', 'ns', 'issue', 'keep-me', 'internal', 'authority', :key)
                """
            ),
            {"rid": reference, "tid": tenant_id, "key": uuid.uuid4().hex},
        )
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM context_receipts WHERE receipt_id = :r"), {"r": receipt})
    with sync_engine.connect() as conn:
        survived = conn.execute(
            text("SELECT count(*) FROM context_external_references WHERE reference_id = :r"), {"r": reference}
        ).scalar_one()
    assert survived == 1


# --- downgrade --------------------------------------------------------------------------


def test_the_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """On a throwaway database, for the same reason the sibling suites use one:
    downgrading the shared one would drop tables out from under every other
    integration module in the session."""
    scratch = f"rc_downgrade_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_sync_url(pg_container), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))

        scratch_url = pg_container.rsplit("/", 1)[0] + "/" + scratch
        env = {**os.environ, "DATABASE_URL": scratch_url}
        run = lambda *args: subprocess.run(  # noqa: E731
            [sys.executable, "-m", "alembic", *args],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        up = run("upgrade", "head")
        assert up.returncode == 0, f"upgrade head failed: {up.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_receipts")

        down = run("downgrade", "0031_external_references")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        for table in ("context_receipt_items", "context_receipt_arms", "context_receipts"):
            assert not after.has_table(table), f"{table} survived the downgrade"
        # The predecessor is intact: this revision's downgrade must not take the
        # reference tables with it.
        assert after.has_table("context_external_references"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_receipt_items")
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": scratch},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        admin.dispose()
