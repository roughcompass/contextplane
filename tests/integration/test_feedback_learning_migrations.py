"""The signal ledger's schema, checked against a real Postgres rather than a reading of it.

Every assertion here runs against the migration as applied. The constraints live
in the migration and nowhere else, so a test that re-derived them from the ORM
would agree with itself and prove nothing; these drive the database and read what
it actually refused.
"""

from __future__ import annotations

import datetime
import os
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, bindparam, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, IntegrityError

from contextplane.signals.models import ExternalSignal
from contextplane.signals.models_feedback import ContextFeedback

_MODELS = (ExternalSignal,)

# A real captured shape from this repo's own CI, projected to the allowlist the
# adapter decision fixes. Kept as a literal rather than a fixture file because
# what is under test is the schema's treatment of it, not the projection.
_PAYLOAD: dict[str, Any] = {
    "repository": "roughcompass/contextplane",
    "workflow_name": "ci",
    "run_id": 17_209_331_845,
    "run_attempt": 1,
    "head_sha": "fd9df6c0f4e1a2b3c4d5e6f708192a3b4c5d6e7f",
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
}


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
            {"t": tid, "s": f"sig-{tid.hex[:8]}", "n": "external signal test"},
        )
    return tid


def _signal(
    conn: Any,
    tenant: uuid.UUID,
    *,
    source_system: str = "github-actions",
    producer_id: str = "signal-producer:github-actions:roughcompass/contextplane",
    producer_type: str = "external",
    source_event_id: str | None = None,
    idempotency_key: str | None = None,
    content_digest: str = "sha256:e3b0c44298fc1c149afbf4c8996fb924",
    authority: str = "github-actions:workflow-conclusion",
    classification: str = "internal",
    event_time: datetime.datetime | None = None,
    observed_time: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    schema_version: str = "external-signal-envelope/v1",
    payload: dict[str, Any] | None = _PAYLOAD,
    evidence_handle: str | None = None,
    **extra: Any,
) -> uuid.UUID:
    """Insert one signal and return its id, defaulting every field to a valid row."""
    unique = uuid.uuid4().hex[:12]
    signal_id = uuid.uuid4()
    columns: dict[str, Any] = {
        "signal_id": signal_id,
        "tenant_id": tenant,
        "source_system": source_system,
        "producer_id": producer_id,
        "producer_type": producer_type,
        # `is None`, not `or`: an empty string is falsy, and defaulting it would
        # make every "an empty key is refused" case silently insert a valid key
        # and pass whether or not the constraint exists.
        "source_event_id": (
            f"github:workflow_run:roughcompass/contextplane:{unique}:1" if source_event_id is None else source_event_id
        ),
        "idempotency_key": f"delivery-{unique}" if idempotency_key is None else idempotency_key,
        "content_digest": content_digest,
        "authority": authority,
        "classification": classification,
        "event_time": event_time,
        "observed_time": observed_time,
        "expires_at": expires_at,
        "schema_version": schema_version,
        "payload": payload,
        "evidence_handle": evidence_handle,
        **extra,
    }
    names = ", ".join(columns)
    binds = ", ".join(f":{name}" for name in columns)
    # `payload` is bound as JSONB explicitly: psycopg2 has no adapter for a bare
    # dict, and letting it infer would fail on the one column whose type is the
    # point of several of these tests. A `None` payload is bound *without* the
    # type, because binding None as JSONB sends the JSON literal `null` rather
    # than SQL NULL -- a distinction the schema takes seriously enough to have
    # its own constraint, and one this helper must not paper over.
    statement = text(f"INSERT INTO external_signals ({names}) VALUES ({binds})").bindparams(
        *[
            bindparam(key, value, type_=JSONB) if key == "payload" and value is not None else bindparam(key, value)
            for key, value in columns.items()
        ]
    )
    conn.execute(statement)
    return signal_id


def test_the_migration_creates_the_signal_ledger(sync_engine: Engine) -> None:
    assert inspect(sync_engine).has_table("external_signals")


def test_the_signal_orm_and_the_database_agree_column_for_column(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    for model in _MODELS:
        live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        declared = {column.name for column in model.__table__.columns}
        assert declared == live, (
            f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, "
            f"database-only {sorted(live - declared)}"
        )


@pytest.mark.parametrize(
    "index",
    [
        "uq_external_signal_source_event",
        "uq_external_signal_idempotency",
        "ix_external_signal_recent",
        "ix_external_signal_expiry",
    ],
)
def test_the_signal_read_and_uniqueness_paths_have_their_indexes(sync_engine: Engine, index: str) -> None:
    names = {i["name"] for i in inspect(sync_engine).get_indexes("external_signals")}
    assert index in names, f"missing {index}; present: {sorted(names)}"


def test_one_signal_per_source_event_per_producer(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The same external occurrence, reported twice, is one row."""
    event = "github:workflow_run:roughcompass/contextplane:9001:1"
    with sync_engine.begin() as conn:
        _signal(conn, tenant_id, source_event_id=event)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _signal(conn, tenant_id, source_event_id=event)


def test_one_signal_per_idempotency_key_per_producer(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The same submission, replayed, is one row."""
    key = "delivery-1f4c2b7a-e2ec-11ee-8c1a-000000000001"
    with sync_engine.begin() as conn:
        _signal(conn, tenant_id, idempotency_key=key)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _signal(conn, tenant_id, idempotency_key=key)


def test_a_rerun_is_a_new_signal_not_a_duplicate(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Attempt-qualification is what makes a re-run a second event rather than a mutation.

    Both runs happened; the ledger has to be able to hold both, or the earlier
    one has to be overwritten to record the later one.
    """
    base = "github:workflow_run:roughcompass/contextplane:9002"
    with sync_engine.begin() as conn:
        first = _signal(conn, tenant_id, source_event_id=f"{base}:1")
        second = _signal(conn, tenant_id, source_event_id=f"{base}:2")
    assert first != second
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT count(*) FROM external_signals WHERE source_event_id LIKE :p"),
            {"p": f"{base}:%"},
        ).scalar_one()
    assert stored == 2


def test_two_tenants_may_each_hold_a_signal_for_one_external_run(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Uniqueness is scoped per tenant: one external thing seen by two tenants is not a conflict."""
    other = uuid.uuid4()
    event = "github:workflow_run:roughcompass/contextplane:9003:1"
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": other, "s": f"sig-{other.hex[:8]}", "n": "second observer"},
        )
        _signal(conn, tenant_id, source_event_id=event)
        _signal(conn, other, source_event_id=event)
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM external_signals WHERE source_event_id = :e"), {"e": event}
            ).scalar_one()
            == 2
        )


def test_two_producers_may_each_hold_a_signal_for_one_event_id(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Uniqueness is producer-scoped, because one producer's id space is its own."""
    event = "github:workflow_run:roughcompass/contextplane:9004:1"
    with sync_engine.begin() as conn:
        _signal(conn, tenant_id, source_event_id=event, producer_id="signal-producer:a")
        _signal(conn, tenant_id, source_event_id=event, producer_id="signal-producer:b")
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM external_signals WHERE source_event_id = :e"), {"e": event}
            ).scalar_one()
            == 2
        )


def test_the_server_assigns_the_signal_ingestion_time(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """`ingested_at` is the audit anchor, so it cannot be absent even when the other two are."""
    before = datetime.datetime.now(tz=datetime.UTC)
    with sync_engine.begin() as conn:
        signal_id = _signal(conn, tenant_id, event_time=None, observed_time=None)
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT event_time, observed_time, ingested_at FROM external_signals WHERE signal_id = :s"),
            {"s": signal_id},
        ).one()
    assert row.event_time is None
    assert row.observed_time is None
    assert row.ingested_at is not None
    assert row.ingested_at >= before - datetime.timedelta(minutes=5)


def test_a_signal_stores_three_distinct_instants(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Lag between the source, the producer and this server is only visible if none is derived from another."""
    happened = datetime.datetime(2026, 8, 9, 10, 0, 0, tzinfo=datetime.UTC)
    learned = datetime.datetime(2026, 8, 9, 10, 0, 30, tzinfo=datetime.UTC)
    with sync_engine.begin() as conn:
        signal_id = _signal(conn, tenant_id, event_time=happened, observed_time=learned)
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT event_time, observed_time, ingested_at FROM external_signals WHERE signal_id = :s"),
            {"s": signal_id},
        ).one()
    assert row.event_time == happened
    assert row.observed_time == learned
    assert row.ingested_at > learned
    assert len({row.event_time, row.observed_time, row.ingested_at}) == 3


def test_a_signal_carries_an_observation_or_a_handle_to_one(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The handle-only shape is legal, and the payload stays SQL NULL rather than JSON null."""
    handle = "evidence://authorized/9005"
    with sync_engine.begin() as conn:
        signal_id = _signal(conn, tenant_id, payload=None, evidence_handle=handle)
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT payload, evidence_handle FROM external_signals WHERE signal_id = :s"),
            {"s": signal_id},
        ).one()
    assert row.payload is None
    assert row.evidence_handle == handle


@pytest.mark.parametrize(
    ("payload", "handle"),
    [
        pytest.param(None, None, id="neither"),
        pytest.param(_PAYLOAD, "evidence://authorized/9006", id="both"),
    ],
)
def test_a_signal_carrying_neither_or_both_bodies_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, payload: dict[str, Any] | None, handle: str | None
) -> None:
    """Neither is a row asserting nothing; both is two copies of one observation, free to drift."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, payload=payload, evidence_handle=handle)


def test_a_signal_with_a_json_null_payload_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """JSON `null` is a value, and `num_nonnulls` alone would have admitted it.

    This is the spelling an ordinary ORM call produces by accident: binding a
    Python `None` through a JSONB-typed parameter sends the JSON literal rather
    than SQL NULL. Without the `jsonb_typeof` half of the constraint, such a row
    satisfies "exactly one body" while carrying no observation whatsoever.
    """
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type,"
                " source_event_id, idempotency_key, content_digest, authority, classification, schema_version,"
                " payload, evidence_handle)"
                " VALUES (:sid, :tid, 'github-actions', 'signal-producer:x', 'external', :ev, :idk,"
                " 'sha256:abc', 'github-actions:workflow-conclusion', 'internal', 'v1', 'null'::jsonb, NULL)"
            ),
            {
                "sid": uuid.uuid4(),
                "tid": tenant_id,
                "ev": f"github:workflow_run:x:{uuid.uuid4().hex[:8]}:1",
                "idk": f"delivery-{uuid.uuid4().hex[:8]}",
            },
        )


def test_a_signal_with_an_unknown_classification_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A classification nobody declared is one no retention policy covers."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, classification="secret-ish")


def test_a_signal_with_an_unknown_producer_type_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The learning-eligibility rules downstream are written against exactly three kinds."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, producer_type="robot")


@pytest.mark.parametrize(
    "field",
    ["source_system", "producer_id", "source_event_id", "idempotency_key", "content_digest", "authority"],
)
def test_a_signal_with_an_empty_identity_part_is_refused(sync_engine: Engine, tenant_id: uuid.UUID, field: str) -> None:
    """A signal missing an identity part collides with everything else missing it."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, **{field: ""})


def test_a_signal_with_an_unfolded_source_system_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Normalization is enforced so a row written around the service cannot fail to collide."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, source_system="GitHub-Actions")


@pytest.mark.parametrize("field", ["source_event_id", "idempotency_key"])
def test_a_signal_with_an_untrimmed_key_is_refused(sync_engine: Engine, tenant_id: uuid.UUID, field: str) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _signal(conn, tenant_id, **{field: "  padded-key  "})


def test_a_signal_belongs_to_a_tenant_that_exists(sync_engine: Engine) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _signal(conn, uuid.uuid4())


def test_signal_revocation_and_supersession_are_recorded_apart(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A withdrawn event and an overtaken one have different consequences downstream.

    Withdrawal invalidates dependents; supersession leaves both occurrences true
    and only stops the earlier one being the thing to learn from. Collapsing them
    into one flag would make "may this evidence promote a claim?" unanswerable.
    """
    with sync_engine.begin() as conn:
        fresh = _signal(conn, tenant_id)
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT revoked_at, superseded_for_learning FROM external_signals WHERE signal_id = :s"),
            {"s": fresh},
        ).one()
    assert row.revoked_at is None
    assert row.superseded_for_learning is False

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE external_signals SET superseded_for_learning = TRUE WHERE signal_id = :s"), {"s": fresh}
        )
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT revoked_at, superseded_for_learning FROM external_signals WHERE signal_id = :s"),
            {"s": fresh},
        ).one()
    assert row.superseded_for_learning is True
    assert row.revoked_at is None, "supersession must not read as revocation"


def test_the_signal_ledger_holds_no_workspace_body_columns(sync_engine: Engine) -> None:
    """The absence is the design, so it is asserted rather than left to review.

    Workspace text on this table would put unauthorized content on the path that
    later derives claims. A column added for convenience is exactly how that
    happens, and it would look harmless in the diff that added it.
    """
    columns = {c["name"] for c in inspect(sync_engine).get_columns("external_signals")}
    forbidden = {"workspace_id", "workspace_body", "body", "content", "text", "entry_body", "excerpt"}
    assert not (columns & forbidden), f"workspace body columns reached the signal ledger: {sorted(columns & forbidden)}"


def test_the_signal_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Run against a throwaway database on the same server, for the same reason the
    reference suite does: downgrading the shared one would drop tables out from
    under every other integration module in the session."""
    scratch = f"sig_downgrade_{uuid.uuid4().hex[:8]}"
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
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("external_signals")

        down = run("downgrade", "0033_receipt_evidence")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        assert not after.has_table("external_signals"), "external_signals survived the downgrade"
        # The predecessor link is intact: downgrading this revision must not take
        # the one below it with it.
        assert after.has_table("context_receipts"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("external_signals")
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


# ---------------------------------------------------------------------------
# Discriminated feedback
#
# Every test below names "feedback" so the filtered gate selects it explicitly.
# The module name happens to contain the word too, which would select the file
# regardless -- but a suite that depends on its own filename for coverage stops
# being covered the day somebody splits it.
# ---------------------------------------------------------------------------


@pytest.fixture
def receipt(sync_engine: Engine, tenant_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """One receipt with one item on it, returned as (receipt_id, receipt_item_id)."""
    receipt_id = uuid.uuid4()
    item_id = f"item-{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, requested_by)"
                " VALUES (:r, :t, 'complete', TRUE, 'tester')"
            ),
            {"r": receipt_id, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO context_receipt_items (receipt_id, receipt_item_id, block, source, item_key)"
                " VALUES (:r, :i, 'canonical', 'catalog', 'key-1')"
            ),
            {"r": receipt_id, "i": item_id},
        )
    return receipt_id, item_id


def _feedback(
    conn: Any,
    tenant: uuid.UUID,
    *,
    kind: str = "diagnostic_observation",
    receipt_id: uuid.UUID | None = None,
    receipt_item_id: str | None = None,
    rating: str = "irrelevant",
    learning_eligible: bool = False,
    note: str | None = None,
    reporter_id: str = "user:alex",
    reporter_type: str = "human",
    idempotency_key: str | None = None,
    content_digest: str = "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0",
) -> uuid.UUID:
    """Insert one feedback row and return its id, defaulting to a valid diagnostic."""
    feedback_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO context_feedback (feedback_id, tenant_id, kind, receipt_id, receipt_item_id, rating,"
            " learning_eligible, note, reporter_id, reporter_type, idempotency_key, content_digest)"
            " VALUES (:fid, :tid, :kind, :rid, :iid, :rating, :elig, :note, :rep, :rtype, :idk, :dig)"
        ),
        {
            "fid": feedback_id,
            "tid": tenant,
            "kind": kind,
            "rid": receipt_id,
            "iid": receipt_item_id,
            "rating": rating,
            "elig": learning_eligible,
            "note": note,
            "rep": reporter_id,
            "rtype": reporter_type,
            # `is None`, not `or`: an empty key must reach the constraint.
            "idk": f"fb-{uuid.uuid4().hex[:12]}" if idempotency_key is None else idempotency_key,
            "dig": content_digest,
        },
    )
    return feedback_id


def test_the_migration_creates_the_feedback_table(sync_engine: Engine) -> None:
    assert inspect(sync_engine).has_table("context_feedback")


def test_the_feedback_orm_and_the_database_agree_column_for_column(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    live = {column["name"] for column in inspector.get_columns(ContextFeedback.__tablename__)}
    declared = {column.name for column in ContextFeedback.__table__.columns}
    assert (
        declared == live
    ), f"context_feedback drifted: ORM-only {sorted(declared - live)}, database-only {sorted(live - declared)}"


@pytest.mark.parametrize(
    "index",
    ["uq_feedback_idempotency", "ix_feedback_by_receipt", "ix_feedback_learning_candidates"],
)
def test_the_feedback_read_and_uniqueness_paths_have_their_indexes(sync_engine: Engine, index: str) -> None:
    names = {i["name"] for i in inspect(sync_engine).get_indexes("context_feedback")}
    assert index in names, f"missing {index}; present: {sorted(names)}"


def test_item_specific_feedback_cites_a_receipt_and_an_exact_item(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    receipt_id, item_id = receipt
    with sync_engine.begin() as conn:
        feedback_id = _feedback(
            conn,
            tenant_id,
            kind="item_specific",
            receipt_id=receipt_id,
            receipt_item_id=item_id,
            learning_eligible=True,
        )
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT kind, receipt_id, receipt_item_id FROM context_feedback WHERE feedback_id = :f"),
            {"f": feedback_id},
        ).one()
    assert row.kind == "item_specific"
    assert row.receipt_id == receipt_id
    assert row.receipt_item_id == item_id


def test_receipt_level_feedback_cites_a_receipt_and_no_item(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    receipt_id, _ = receipt
    with sync_engine.begin() as conn:
        feedback_id = _feedback(conn, tenant_id, kind="receipt_level", receipt_id=receipt_id, learning_eligible=True)
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT receipt_id, receipt_item_id FROM context_feedback WHERE feedback_id = :f"),
            {"f": feedback_id},
        ).one()
    assert row.receipt_id == receipt_id
    assert row.receipt_item_id is None


def test_diagnostic_feedback_cites_nothing(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        feedback_id = _feedback(conn, tenant_id, kind="diagnostic_observation")
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT receipt_id, receipt_item_id, learning_eligible FROM context_feedback WHERE feedback_id = :f"),
            {"f": feedback_id},
        ).one()
    assert row.receipt_id is None
    assert row.receipt_item_id is None
    assert row.learning_eligible is False


def test_item_specific_feedback_without_an_item_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """Item-specific feedback that names no item is receipt-level feedback wearing the wrong label."""
    receipt_id, _ = receipt
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="item_specific", receipt_id=receipt_id, receipt_item_id=None)


def test_item_specific_feedback_without_a_receipt_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="item_specific", receipt_id=None, receipt_item_id="item-orphan")


def test_receipt_level_feedback_naming_an_item_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """Feedback about a whole answer is not evidence about any one line of it."""
    receipt_id, item_id = receipt
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="receipt_level", receipt_id=receipt_id, receipt_item_id=item_id)


def test_diagnostic_feedback_naming_a_receipt_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    receipt_id, _ = receipt
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="diagnostic_observation", receipt_id=receipt_id)


def test_diagnostic_feedback_can_never_be_learning_eligible(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """It cites nothing, so nothing can check what it refers to.

    Admitting it to the derivation path would let an unattributable complaint
    become evidence about a specific retrieved item.
    """
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="diagnostic_observation", learning_eligible=True)


def test_feedback_cannot_cite_an_item_from_another_receipt(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """The exact-item rule is the pair, not the id.

    Both columns are individually valid here -- the receipt exists and the item
    exists -- and the row is still wrong, because that item is not on that
    receipt. A single-column foreign key would have stored it.
    """
    receipt_id, _ = receipt
    other_receipt = uuid.uuid4()
    foreign_item = f"item-{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, requested_by)"
                " VALUES (:r, :t, 'complete', TRUE, 'tester')"
            ),
            {"r": other_receipt, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO context_receipt_items (receipt_id, receipt_item_id, block, source, item_key)"
                " VALUES (:r, :i, 'canonical', 'catalog', 'key-2')"
            ),
            {"r": other_receipt, "i": foreign_item},
        )
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="item_specific", receipt_id=receipt_id, receipt_item_id=foreign_item)


def test_receipt_level_feedback_needs_a_receipt_that_exists(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The composite key cannot carry this: it is not enforced when the item is NULL."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="receipt_level", receipt_id=uuid.uuid4())


def test_feedback_of_an_unknown_kind_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="vibes")


def test_feedback_with_a_rating_outside_the_vocabulary_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A verdict nobody declared is one no learning or evaluation rule accounts for."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, rating="meh")


def test_feedback_from_an_unknown_reporter_type_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, reporter_type="committee")


def test_replayed_feedback_under_one_key_is_one_row(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    key = "fb-replay-0001"
    with sync_engine.begin() as conn:
        _feedback(conn, tenant_id, idempotency_key=key)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, idempotency_key=key)


def test_two_reporters_may_use_the_same_feedback_key(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Idempotency is per reporter: one reporter's key space is its own."""
    key = "fb-replay-0002"
    with sync_engine.begin() as conn:
        _feedback(conn, tenant_id, idempotency_key=key, reporter_id="user:alex")
        _feedback(conn, tenant_id, idempotency_key=key, reporter_id="user:sam")
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM context_feedback WHERE idempotency_key = :k"), {"k": key}
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize("field", ["reporter_id", "idempotency_key", "content_digest"])
def test_feedback_with_an_empty_identity_part_is_refused(sync_engine: Engine, tenant_id: uuid.UUID, field: str) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, **{field: ""})


def test_feedback_with_an_untrimmed_key_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _feedback(conn, tenant_id, idempotency_key="  padded  ")


def test_deleting_a_receipt_does_not_silently_discard_its_feedback(
    sync_engine: Engine, tenant_id: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """A retention path must decide what happens to feedback, not have it decided by a cascade.

    The receipt's own items cascade; feedback deliberately does not, so removing a
    receipt that someone reported a problem about fails until policy says how to
    redact or tombstone it.
    """
    receipt_id, item_id = receipt
    with sync_engine.begin() as conn:
        _feedback(conn, tenant_id, kind="item_specific", receipt_id=receipt_id, receipt_item_id=item_id)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM context_receipts WHERE receipt_id = :r"), {"r": receipt_id})
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM context_feedback WHERE receipt_id = :r"), {"r": receipt_id}
            ).scalar_one()
            == 1
        )


def test_the_feedback_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Throwaway database, for the same reason the suites above use one."""
    scratch = f"fb_downgrade_{uuid.uuid4().hex[:8]}"
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
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_feedback")

        down = run("downgrade", "0040_external_signals")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        assert not after.has_table("context_feedback"), "context_feedback survived the downgrade"
        # The predecessor link is intact: this downgrade must not take the signal
        # ledger with it.
        assert after.has_table("external_signals"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_feedback")
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
