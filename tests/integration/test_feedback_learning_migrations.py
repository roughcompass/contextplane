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

from contextplane.retention.models import (
    DerivativeRegistration,
    DerivativeSourceLink,
    DerivativeWorkItem,
    PrivacyAggregate,
    RetentionPolicy,
    SourceTombstone,
)
from contextplane.service.memory.models import ClaimDerivation, CurationCase, DerivationEvidenceLink
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


# ---------------------------------------------------------------------------
# Derivation attempts, evidence links and curation cases
#
# The filter for this group is `-k 'derivation or curation'`, and neither word
# appears in the module name -- unlike "feedback", which selects the whole file
# by filename alone. So every name below has to carry one of the two keywords or
# the gate silently skips it. Checked with --collect-only before handoff.
# ---------------------------------------------------------------------------


@pytest.fixture
def derivation(sync_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    """One staged derivation attempt with no evidence attached yet."""
    with sync_engine.begin() as conn:
        return _derivation(conn, tenant_id)


def _derivation(
    conn: Any,
    tenant: uuid.UUID,
    *,
    profile: str = "outcome-extractor",
    profile_version: str = "1.4.0",
    status: str = "staged",
    applicability: str = "repo:roughcompass/contextplane",
    assertion_digest: str | None = None,
    source_authority: str = "github-actions:workflow-conclusion",
    classification: str = "internal",
    created_claim_id: uuid.UUID | None = None,
) -> uuid.UUID:
    derivation_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO claim_derivations (derivation_id, tenant_id, profile, profile_version, status,"
            " applicability, assertion_digest, source_authority, classification, created_claim_id)"
            " VALUES (:d, :t, :p, :pv, :s, :a, :dig, :auth, :cls, :claim)"
        ),
        {
            "d": derivation_id,
            "t": tenant,
            "p": profile,
            "pv": profile_version,
            "s": status,
            "a": applicability,
            "dig": f"sha256:{uuid.uuid4().hex}" if assertion_digest is None else assertion_digest,
            "auth": source_authority,
            "cls": classification,
            "claim": created_claim_id,
        },
    )
    return derivation_id


def _evidence(
    conn: Any,
    derivation_id: uuid.UUID,
    *,
    evidence_kind: str = "checkpoint",
    signal_id: uuid.UUID | None = None,
    receipt_id: uuid.UUID | None = None,
    receipt_item_id: str | None = None,
    reference_id: uuid.UUID | None = None,
    checkpoint_id: uuid.UUID | None = None,
    checkpoint_digest: str | None = None,
    source_authority: str = "github-actions:workflow-conclusion",
    classification: str = "internal",
    excerpt: str | None = None,
) -> uuid.UUID:
    if evidence_kind == "checkpoint" and checkpoint_id is None and checkpoint_digest is None:
        checkpoint_id = uuid.uuid4()
        checkpoint_digest = f"sha256:{uuid.uuid4().hex}"
    link_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO derivation_evidence_links (link_id, derivation_id, evidence_kind, signal_id, receipt_id,"
            " receipt_item_id, reference_id, checkpoint_id, checkpoint_digest, source_authority, classification,"
            " excerpt)"
            " VALUES (:l, :d, :k, :sig, :r, :i, :ref, :cid, :cdig, :auth, :cls, :ex)"
        ),
        {
            "l": link_id,
            "d": derivation_id,
            "k": evidence_kind,
            "sig": signal_id,
            "r": receipt_id,
            "i": receipt_item_id,
            "ref": reference_id,
            "cid": checkpoint_id,
            "cdig": checkpoint_digest,
            "auth": source_authority,
            "cls": classification,
            "ex": excerpt,
        },
    )
    return link_id


def _case(
    conn: Any,
    tenant: uuid.UUID,
    *,
    subject_reference: str = "capability:billing",
    predicate: str = "owner",
    raised_by_derivation_id: uuid.UUID | None = None,
    status: str = "open",
    owner_id: str | None = None,
    routed_at: datetime.datetime | None = None,
    disposition: str | None = None,
    approval_authority: str | None = None,
    evidence_threshold: str | None = None,
) -> uuid.UUID:
    case_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO curation_cases (case_id, tenant_id, subject_reference, predicate,"
            " raised_by_derivation_id, status, owner_id, routed_at, disposition, approval_authority,"
            " evidence_threshold)"
            " VALUES (:c, :t, :sub, :pred, :raised, :st, :own, :routed, :disp, :auth, :thr)"
        ),
        {
            "c": case_id,
            "t": tenant,
            "sub": subject_reference,
            "pred": predicate,
            "raised": raised_by_derivation_id,
            "st": status,
            "own": owner_id,
            "routed": routed_at,
            "disp": disposition,
            "auth": approval_authority,
            "thr": evidence_threshold,
        },
    )
    return case_id


@pytest.mark.parametrize("table", ["claim_derivations", "derivation_evidence_links", "curation_cases"])
def test_the_derivation_and_curation_migration_creates_every_table(sync_engine: Engine, table: str) -> None:
    assert inspect(sync_engine).has_table(table)


@pytest.mark.parametrize("model", [ClaimDerivation, DerivationEvidenceLink, CurationCase])
def test_the_derivation_and_curation_orm_agrees_with_the_database(sync_engine: Engine, model: Any) -> None:
    inspector = inspect(sync_engine)
    live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
    declared = {column.name for column in model.__table__.columns}
    assert (
        declared == live
    ), f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, database-only {sorted(live - declared)}"


@pytest.mark.parametrize(
    "index",
    [
        "uq_derivation_assertion",
        "ix_derivation_pending",
        "ix_evidence_by_derivation",
        "ix_evidence_by_signal",
        "ix_curation_open_cases",
        "ix_curation_by_axis",
    ],
)
def test_the_derivation_and_curation_read_paths_have_their_indexes(sync_engine: Engine, index: str) -> None:
    names: set[str] = set()
    for table in ("claim_derivations", "derivation_evidence_links", "curation_cases"):
        names |= {i["name"] for i in inspect(sync_engine).get_indexes(table)}
    assert index in names, f"missing {index}; present: {sorted(names)}"


def test_a_derivation_attempt_is_kept_whether_or_not_it_concluded(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """ "We looked and concluded nothing" and "we never looked" are different states."""
    with sync_engine.begin() as conn:
        pending = _derivation(conn, tenant_id, status="pending")
        rejected = _derivation(conn, tenant_id, status="rejected")
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM claim_derivations WHERE derivation_id IN (:a, :b) ORDER BY status"),
            {"a": pending, "b": rejected},
        ).all()
    assert [r.status for r in rows] == ["pending", "rejected"]


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_an_unconcluded_derivation_cannot_name_a_created_claim(
    sync_engine: Engine, tenant_id: uuid.UUID, status: str
) -> None:
    """Storing a claim id on a rejected attempt is how a refused assertion acquires a citation."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _derivation(conn, tenant_id, status=status, created_claim_id=uuid.uuid4())


def test_a_derivation_of_an_unknown_status_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _derivation(conn, tenant_id, status="maybe")


def test_one_derivation_per_assertion_per_extractor_version(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    with sync_engine.begin() as conn:
        _derivation(conn, tenant_id, assertion_digest=digest)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _derivation(conn, tenant_id, assertion_digest=digest)


def test_a_new_extractor_version_may_reach_the_same_derivation(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The version is part of the key: a later extractor concluding the same thing is its own attempt."""
    digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    with sync_engine.begin() as conn:
        _derivation(conn, tenant_id, assertion_digest=digest, profile_version="1.4.0")
        _derivation(conn, tenant_id, assertion_digest=digest, profile_version="1.5.0")
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM claim_derivations WHERE assertion_digest = :d"), {"d": digest}
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "field", ["profile", "profile_version", "applicability", "assertion_digest", "source_authority"]
)
def test_a_derivation_with_an_empty_identity_part_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, field: str
) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _derivation(conn, tenant_id, **{field: ""})


def test_derivation_evidence_records_the_authority_each_source_carried(
    sync_engine: Engine, tenant_id: uuid.UUID, derivation: uuid.UUID
) -> None:
    """The ceiling is auditable only if every input's own authority is stored beside the result's.

    The database cannot enforce "no more than the weakest source" -- authority is
    a source-issued string with no ordering -- so what it must guarantee is that
    the comparison is possible at all.
    """
    with sync_engine.begin() as conn:
        _evidence(conn, derivation, source_authority="github-actions:workflow-conclusion")
        _evidence(conn, derivation, source_authority="human:reviewer-attestation")
    with sync_engine.connect() as conn:
        authorities = {
            r.source_authority
            for r in conn.execute(
                text("SELECT source_authority FROM derivation_evidence_links WHERE derivation_id = :d"),
                {"d": derivation},
            ).all()
        }
        claimed = conn.execute(
            text("SELECT source_authority FROM claim_derivations WHERE derivation_id = :d"), {"d": derivation}
        ).scalar_one()
    assert authorities == {"github-actions:workflow-conclusion", "human:reviewer-attestation"}
    assert claimed in authorities


def test_derivation_evidence_of_an_unknown_kind_is_refused(sync_engine: Engine, derivation: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _evidence(conn, derivation, evidence_kind="vibes", checkpoint_id=uuid.uuid4(), checkpoint_digest="d")


def test_derivation_evidence_pointing_nowhere_is_refused(sync_engine: Engine, derivation: uuid.UUID) -> None:
    """A link with no referent is not evidence of anything."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _evidence(conn, derivation, evidence_kind="signal", signal_id=None)


def test_derivation_evidence_pointing_at_two_things_is_refused(
    sync_engine: Engine, derivation: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """The discriminant names one pointer; a second makes the kind a lie."""
    receipt_id, _ = receipt
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _evidence(
            conn,
            derivation,
            evidence_kind="checkpoint",
            checkpoint_id=uuid.uuid4(),
            checkpoint_digest="sha256:abc",
            receipt_id=receipt_id,
        )


def test_a_checkpoint_cited_as_derivation_evidence_carries_its_digest(
    sync_engine: Engine, derivation: uuid.UUID
) -> None:
    """The id says which checkpoint; the digest says it had not changed when it was read."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _evidence(conn, derivation, evidence_kind="checkpoint", checkpoint_id=uuid.uuid4(), checkpoint_digest=None)


def test_derivation_evidence_cannot_cite_an_item_from_another_receipt(
    sync_engine: Engine, tenant_id: uuid.UUID, derivation: uuid.UUID, receipt: tuple[uuid.UUID, str]
) -> None:
    """Both columns individually valid, the pair still wrong -- the same trap feedback closes."""
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
                " VALUES (:r, :i, 'canonical', 'catalog', 'key-3')"
            ),
            {"r": other_receipt, "i": foreign_item},
        )
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _evidence(conn, derivation, evidence_kind="receipt_item", receipt_id=receipt_id, receipt_item_id=foreign_item)


def test_derivation_evidence_may_cite_a_signal(
    sync_engine: Engine, tenant_id: uuid.UUID, derivation: uuid.UUID
) -> None:
    """The path the revocation sweep walks: signal -> the attempts that read it."""
    with sync_engine.begin() as conn:
        signal_id = _signal(conn, tenant_id)
        _evidence(conn, derivation, evidence_kind="signal", signal_id=signal_id)
    with sync_engine.connect() as conn:
        found = conn.execute(
            text("SELECT derivation_id FROM derivation_evidence_links WHERE signal_id = :s"), {"s": signal_id}
        ).scalar_one()
    assert found == derivation


def test_deleting_a_derivation_takes_its_evidence_links(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A link to an attempt that no longer exists is not evidence; unlike feedback, it has no independent standing."""
    with sync_engine.begin() as conn:
        derivation_id = _derivation(conn, tenant_id)
        _evidence(conn, derivation_id)
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM claim_derivations WHERE derivation_id = :d"), {"d": derivation_id})
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM derivation_evidence_links WHERE derivation_id = :d"),
                {"d": derivation_id},
            ).scalar_one()
            == 0
        )


def test_a_curation_case_disposition_names_its_approving_authority(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A proposal without a named authority is a decision nobody is accountable for."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _case(conn, tenant_id, status="resolved", disposition="propose_canonical", approval_authority=None)


def test_a_curation_case_disposition_names_its_evidence_threshold(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _case(
            conn,
            tenant_id,
            status="resolved",
            disposition="propose_canonical",
            approval_authority="catalog-owner",
            evidence_threshold=None,
        )


def test_a_resolved_curation_case_says_what_was_decided(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _case(conn, tenant_id, status="resolved", disposition=None)


def test_a_routed_curation_case_names_an_owner(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Contradiction that reaches nobody is contradiction that stays."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _case(conn, tenant_id, status="routed", owner_id=None)


def test_a_curation_case_of_an_unknown_disposition_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _case(
            conn,
            tenant_id,
            status="resolved",
            disposition="overrule",
            approval_authority="catalog-owner",
            evidence_threshold="two-independent-sources",
        )


def test_a_curation_case_routes_and_resolves(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The whole legal path, so the constraints above are not passing by refusing everything."""
    routed = datetime.datetime(2026, 8, 9, 12, 0, 0, tzinfo=datetime.UTC)
    with sync_engine.begin() as conn:
        derivation_id = _derivation(conn, tenant_id)
        case_id = _case(
            conn,
            tenant_id,
            raised_by_derivation_id=derivation_id,
            status="routed",
            owner_id="team:catalog",
            routed_at=routed,
        )
        conn.execute(
            text(
                "UPDATE curation_cases SET status = 'resolved', disposition = 'supersede',"
                " approval_authority = 'catalog-owner', evidence_threshold = 'two-independent-sources',"
                " resolved_at = now() WHERE case_id = :c"
            ),
            {"c": case_id},
        )
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, disposition, approval_authority, evidence_threshold, raised_by_derivation_id"
                " FROM curation_cases WHERE case_id = :c"
            ),
            {"c": case_id},
        ).one()
    assert row.status == "resolved"
    assert row.disposition == "supersede"
    assert row.approval_authority == "catalog-owner"
    assert row.evidence_threshold == "two-independent-sources"
    assert row.raised_by_derivation_id == derivation_id


def test_a_curation_case_has_no_column_that_writes_its_target(sync_engine: Engine) -> None:
    """Dispositions are proposals. A write-target column would make "decided" and "written" one event.

    Asserted rather than left to review, because the column that collapses them
    would look like a convenience in the diff that added it.
    """
    columns = {c["name"] for c in inspect(sync_engine).get_columns("curation_cases")}
    forbidden = {"target_entity_id", "target_claim_id", "canonical_entity_id", "written_target_id", "applied_to"}
    assert not (columns & forbidden), f"a curation case can write its own target: {sorted(columns & forbidden)}"


def test_derivation_evidence_holds_no_workspace_body_columns(sync_engine: Engine) -> None:
    """The extractor keeps bounded excerpts, never a workspace copy."""
    columns = {c["name"] for c in inspect(sync_engine).get_columns("derivation_evidence_links")}
    forbidden = {"workspace_id", "workspace_body", "body", "content", "entry_body"}
    assert not (
        columns & forbidden
    ), f"workspace body columns reached derivation evidence: {sorted(columns & forbidden)}"


def test_the_derivation_and_curation_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Throwaway database, for the same reason the suites above use one."""
    scratch = f"dc_downgrade_{uuid.uuid4().hex[:8]}"
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
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("claim_derivations")

        down = run("downgrade", "0041_discriminated_feedback")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        for table in ("curation_cases", "derivation_evidence_links", "claim_derivations"):
            assert not after.has_table(table), f"{table} survived the downgrade"
        # The predecessor link is intact: this downgrade must not take feedback
        # or the signal ledger with it.
        assert after.has_table("context_feedback"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("curation_cases")
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
# Retention policy, tombstones, derivatives and privacy-safe aggregates
#
# Filter for this group is `-k 'retention or derivative or aggregate'`. None of
# those words is in the module name, so every test below carries one or the gate
# skips it silently. Confirmed with --collect-only before handoff.
# ---------------------------------------------------------------------------

_POLICY_VERSION = "CP-POLICY-2026-08-A"


@pytest.fixture
def retention_policy(sync_engine: Engine) -> str:
    """The policy version the tombstone tests below decide under."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO retention_policies (policy_version, record_class, legal_basis, retention_days,"
                " erasure_mode, minimization_action, tombstone_behaviour, verifier_disclosure)"
                " VALUES (:v, 'signal', 'legitimate interest', 180, 'minimize_and_tombstone',"
                " 'payload replaced by hmac prefix', 'retained for audit',"
                " 'structural integrity only; never content') ON CONFLICT DO NOTHING"
            ),
            {"v": _POLICY_VERSION},
        )
    return _POLICY_VERSION


def _tombstone(
    conn: Any,
    tenant: uuid.UUID,
    *,
    record_class: str = "signal",
    subject_id: uuid.UUID | None = None,
    policy_version: str = _POLICY_VERSION,
    request_authority: str = "tenant-owner",
    reason: str = "subject erasure request",
    proof_hmac: str = "hmac:9f2c4a1b8e7d",
    propagation_state: str = "pending",
) -> uuid.UUID:
    tombstone_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source_tombstones (tombstone_id, tenant_id, record_class, subject_id, policy_version,"
            " request_authority, reason, proof_hmac, propagation_state)"
            " VALUES (:tb, :t, :rc, :sub, :v, :auth, :reason, :hmac, :state)"
        ),
        {
            "tb": tombstone_id,
            "t": tenant,
            "rc": record_class,
            "sub": uuid.uuid4() if subject_id is None else subject_id,
            "v": policy_version,
            "auth": request_authority,
            "reason": reason,
            "hmac": proof_hmac,
            "state": propagation_state,
        },
    )
    return tombstone_id


def _derivative(
    conn: Any,
    tenant: uuid.UUID,
    *,
    derivative_kind: str = "vector",
    storage_locator: str | None = None,
    audience_partition: str = "tenant-internal",
    classification: str = "internal",
    policy_version: str = _POLICY_VERSION,
    expires_at: datetime.datetime | None = None,
    blocking: bool = False,
) -> uuid.UUID:
    derivative_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO derivative_registrations (derivative_id, tenant_id, derivative_kind, storage_locator,"
            " audience_partition, classification, rebuild_handler_version, delete_handler_version,"
            " redact_handler_version, policy_version, expires_at, blocking)"
            " VALUES (:d, :t, :k, :loc, :aud, :cls, 'rebuild@1', 'delete@1', 'redact@1', :v, :exp, :blk)"
        ),
        {
            "d": derivative_id,
            "t": tenant,
            "k": derivative_kind,
            "loc": f"pgvector://chunks/{uuid.uuid4().hex[:10]}" if storage_locator is None else storage_locator,
            "aud": audience_partition,
            "cls": classification,
            "v": policy_version,
            "exp": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC) if expires_at is None else expires_at,
            "blk": blocking,
        },
    )
    return derivative_id


def _source_link(
    conn: Any,
    derivative_id: uuid.UUID,
    *,
    source_record_class: str = "signal",
    source_id: uuid.UUID | None = None,
    source_revision: str | None = None,
    source_expires_at: datetime.datetime | None = None,
) -> uuid.UUID:
    link_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO derivative_source_links (link_id, derivative_id, source_record_class, source_id,"
            " source_revision, source_expires_at) VALUES (:l, :d, :rc, :sid, :rev, :exp)"
        ),
        {
            "l": link_id,
            "d": derivative_id,
            "rc": source_record_class,
            "sid": uuid.uuid4() if source_id is None else source_id,
            "rev": source_revision,
            "exp": source_expires_at,
        },
    )
    return link_id


def _work_item(
    conn: Any,
    tenant: uuid.UUID,
    derivative_id: uuid.UUID,
    *,
    operation: str = "rebuild",
    trigger: str = "expiry",
    tombstone_id: uuid.UUID | None = None,
    state: str = "pending",
    last_error: str | None = None,
) -> uuid.UUID:
    work_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO derivative_work_outbox (work_id, tenant_id, derivative_id, operation, trigger,"
            " tombstone_id, state, last_error) VALUES (:w, :t, :d, :op, :trg, :tb, :st, :err)"
        ),
        {
            "w": work_id,
            "t": tenant,
            "d": derivative_id,
            "op": operation,
            "trg": trigger,
            "tb": tombstone_id,
            "st": state,
            "err": last_error,
        },
    )
    return work_id


def _aggregate(
    conn: Any,
    tenant: uuid.UUID,
    *,
    cohort_key: str = "team:catalog",
    metric: str = "feedback.rating.share",
    window_start: datetime.datetime | None = None,
    window_end: datetime.datetime | None = None,
    actor_count: int = 12,
    value: str | None = '{"relevant": 0.8}',
    suppressed: bool = False,
    partial: bool = False,
) -> uuid.UUID:
    aggregate_id = uuid.uuid4()
    statement = text(
        "INSERT INTO privacy_aggregates (aggregate_id, tenant_id, cohort_key, metric, window_start, window_end,"
        " actor_count, value, suppressed, partial, policy_version, expires_at)"
        " VALUES (:a, :t, :ck, :m, :ws, :we, :n, CAST(:val AS JSONB), :sup, :part, :v, :exp)"
    )
    conn.execute(
        statement,
        {
            "a": aggregate_id,
            "t": tenant,
            "ck": cohort_key,
            "m": metric,
            "ws": datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC) if window_start is None else window_start,
            "we": datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC) if window_end is None else window_end,
            "n": actor_count,
            "val": value,
            "sup": suppressed,
            "part": partial,
            "v": _POLICY_VERSION,
            "exp": datetime.datetime(2027, 9, 1, tzinfo=datetime.UTC),
        },
    )
    return aggregate_id


@pytest.mark.parametrize(
    "table",
    [
        "retention_policies",
        "source_tombstones",
        "derivative_registrations",
        "derivative_source_links",
        "derivative_work_outbox",
        "privacy_aggregates",
    ],
)
def test_the_retention_and_derivative_migration_creates_every_table(sync_engine: Engine, table: str) -> None:
    assert inspect(sync_engine).has_table(table)


@pytest.mark.parametrize(
    "model",
    [
        RetentionPolicy,
        SourceTombstone,
        DerivativeRegistration,
        DerivativeSourceLink,
        DerivativeWorkItem,
        PrivacyAggregate,
    ],
)
def test_the_retention_and_derivative_orm_agrees_with_the_database(sync_engine: Engine, model: Any) -> None:
    inspector = inspect(sync_engine)
    live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
    declared = {column.name for column in model.__table__.columns}
    assert (
        declared == live
    ), f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, database-only {sorted(live - declared)}"


@pytest.mark.parametrize(
    "index",
    [
        "uq_tombstone_subject",
        "ix_tombstone_unpropagated",
        "uq_derivative_locator",
        "ix_derivative_expiry",
        "ix_derivative_blocking_overdue",
        "uq_derivative_source",
        "ix_derivative_source_lookup",
        "uq_work_per_cause",
        "ix_work_claimable",
        "uq_aggregate_cell",
        "ix_aggregate_expiry",
    ],
)
def test_the_retention_and_derivative_read_paths_have_their_indexes(sync_engine: Engine, index: str) -> None:
    names: set[str] = set()
    for table in (
        "source_tombstones",
        "derivative_registrations",
        "derivative_source_links",
        "derivative_work_outbox",
        "privacy_aggregates",
    ):
        names |= {i["name"] for i in inspect(sync_engine).get_indexes(table)}
    assert index in names, f"missing {index}; present: {sorted(names)}"


def test_a_retention_period_may_be_event_bounded_rather_than_a_duration(sync_engine: Engine) -> None:
    """ "Life of tenant" is NULL, not a very large number of days.

    Storing a sentinel duration would make an event-bounded period and a very
    long one indistinguishable to every sweep that reads the column.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO retention_policies (policy_version, record_class, legal_basis, retention_days,"
                " erasure_mode, minimization_action, verifier_disclosure)"
                " VALUES ('CP-TEST-EVENT', 'task_checkpoint', 'contract performance', NULL,"
                " 'minimize_and_tombstone', 'body minimized', 'structural integrity only')"
            )
        )
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT retention_days FROM retention_policies"
                    " WHERE policy_version = 'CP-TEST-EVENT' AND record_class = 'task_checkpoint'"
                )
            ).scalar_one()
            is None
        )


def test_a_retention_period_of_zero_days_is_refused(sync_engine: Engine) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO retention_policies (policy_version, record_class, legal_basis, retention_days,"
                " erasure_mode, verifier_disclosure)"
                " VALUES ('CP-TEST-ZERO', 'export', 'contract performance', 0, 'delete', 'nothing')"
            )
        )


def test_a_minimizing_retention_class_says_what_minimization_means(sync_engine: Engine) -> None:
    """Otherwise "minimized" is a status nobody can verify was reached."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO retention_policies (policy_version, record_class, legal_basis, retention_days,"
                " erasure_mode, minimization_action, verifier_disclosure)"
                " VALUES ('CP-TEST-MIN', 'feedback', 'contract performance', 365, 'minimize', NULL, 'structural')"
            )
        )


def test_a_retention_tombstone_names_the_policy_version_it_was_decided_under(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    """A correction to a period is a new policy version, so an old tombstone stays readable."""
    with sync_engine.begin() as conn:
        tombstone_id = _tombstone(conn, tenant_id, policy_version=retention_policy)
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT policy_version FROM source_tombstones WHERE tombstone_id = :t"), {"t": tombstone_id}
            ).scalar_one()
            == retention_policy
        )


def test_a_retention_tombstone_under_an_unknown_policy_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _tombstone(conn, tenant_id, policy_version="CP-POLICY-NEVER-APPROVED")


def test_erasing_one_record_twice_is_one_retention_tombstone(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    subject = uuid.uuid4()
    with sync_engine.begin() as conn:
        _tombstone(conn, tenant_id, subject_id=subject)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _tombstone(conn, tenant_id, subject_id=subject)


@pytest.mark.parametrize("field", ["request_authority", "reason", "proof_hmac"])
def test_a_retention_tombstone_without_accountability_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str, field: str
) -> None:
    """An erasure nobody can account for is indistinguishable from data loss."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _tombstone(conn, tenant_id, **{field: ""})


def test_a_retention_tombstone_holds_no_column_for_what_was_erased(sync_engine: Engine) -> None:
    """The proof is a tenant-keyed HMAC and nothing else.

    A bare content digest here would be a confirmation oracle: erased content is
    often low-entropy and guessable, so anyone who can guess it could verify the
    guess, and equal digests would reveal equality across erased records.
    """
    columns = {c["name"] for c in inspect(sync_engine).get_columns("source_tombstones")}
    forbidden = {"content", "body", "payload", "erased_value", "content_digest", "excerpt", "item_key"}
    assert not (columns & forbidden), f"erased content reached the tombstone: {sorted(columns & forbidden)}"
    assert "proof_hmac" in columns


def test_a_derivative_registers_every_source_it_was_built_from(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Retention is the minimum across sources, which one source column cannot express.

    This is the shape that lets a sweep compute the true expiry; a registration
    naming only its triggering source is how a derivative outlives something
    nobody remembered it read.
    """
    earliest = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    latest = datetime.datetime(2027, 6, 1, tzinfo=datetime.UTC)
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id, expires_at=earliest)
        _source_link(conn, derivative_id, source_record_class="signal", source_expires_at=earliest)
        _source_link(conn, derivative_id, source_record_class="context_receipt", source_expires_at=latest)
    with sync_engine.connect() as conn:
        computed = conn.execute(
            text("SELECT min(source_expires_at) FROM derivative_source_links WHERE derivative_id = :d"),
            {"d": derivative_id},
        ).scalar_one()
        registered = conn.execute(
            text("SELECT expires_at FROM derivative_registrations WHERE derivative_id = :d"), {"d": derivative_id}
        ).scalar_one()
    assert computed == earliest
    assert registered == computed, "a derivative must not outlive its earliest-expiring source"


def test_a_derivative_cannot_be_registered_without_an_expiry(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """An unbounded derivative is precisely the one that outlives its sources silently."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO derivative_registrations (derivative_id, tenant_id, derivative_kind, storage_locator,"
                " audience_partition, classification, rebuild_handler_version, delete_handler_version,"
                " redact_handler_version, policy_version, expires_at)"
                " VALUES (:d, :t, 'cache', 'redis://x', 'tenant-internal', 'internal', 'r@1', 'd@1', 'x@1', :v, NULL)"
            ),
            {"d": uuid.uuid4(), "t": tenant_id, "v": _POLICY_VERSION},
        )


def test_a_derivative_of_an_unregistered_kind_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """An unregistered derivative is release-gating; a kind nobody declared has no handler."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _derivative(conn, tenant_id, derivative_kind="mystery_blob")


def test_one_derivative_registration_per_locator_and_audience(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The same store serving two audiences is two derivatives with two expiries."""
    locator = "pgvector://chunks/shared-1"
    with sync_engine.begin() as conn:
        _derivative(conn, tenant_id, storage_locator=locator, audience_partition="tenant-internal")
        _derivative(conn, tenant_id, storage_locator=locator, audience_partition="tenant-public")
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _derivative(conn, tenant_id, storage_locator=locator, audience_partition="tenant-internal")


def test_deleting_a_derivative_takes_its_source_links(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id)
        _source_link(conn, derivative_id)
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM derivative_registrations WHERE derivative_id = :d"), {"d": derivative_id})
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM derivative_source_links WHERE derivative_id = :d"), {"d": derivative_id}
            ).scalar_one()
            == 0
        )


def test_derivative_work_from_an_erasure_names_the_tombstone_that_ordered_it(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    """Work that cannot name its cause cannot be audited."""
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id)
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _work_item(conn, tenant_id, derivative_id, trigger="erasure", tombstone_id=None)


def test_derivative_work_is_enqueued_once_per_cause(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    """A repeated sweep must enqueue nothing new, including for triggers with no tombstone.

    The NULL tombstone is the case that breaks under ordinary unique-index
    semantics, where every NULL counts as distinct and the same work is
    re-enqueued on every pass.
    """
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id)
        _work_item(conn, tenant_id, derivative_id, operation="rebuild", trigger="expiry", tombstone_id=None)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _work_item(conn, tenant_id, derivative_id, operation="rebuild", trigger="expiry", tombstone_id=None)


def test_two_causes_produce_two_derivative_work_items(
    sync_engine: Engine, tenant_id: uuid.UUID, retention_policy: str
) -> None:
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id)
        tombstone_id = _tombstone(conn, tenant_id)
        _work_item(conn, tenant_id, derivative_id, operation="rebuild", trigger="expiry")
        _work_item(conn, tenant_id, derivative_id, operation="redact", trigger="erasure", tombstone_id=tombstone_id)
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM derivative_work_outbox WHERE derivative_id = :d"), {"d": derivative_id}
            ).scalar_one()
            == 2
        )


def test_failed_derivative_work_says_why_it_failed(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        derivative_id = _derivative(conn, tenant_id)
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _work_item(conn, tenant_id, derivative_id, state="failed", last_error=None)


def test_an_aggregate_below_the_actor_floor_cannot_carry_a_value(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The floor is enforced by the schema, not only by the job that computes it.

    A floor living solely in the aggregation code holds until the second consumer
    writes its own query, and the offending row looks like every other row.
    """
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, actor_count=4, suppressed=False)


def test_an_aggregate_below_the_floor_may_exist_only_as_a_suppressed_cell(
    sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    with sync_engine.begin() as conn:
        aggregate_id = _aggregate(conn, tenant_id, actor_count=2, suppressed=True, value=None, cohort_key="team:tiny")
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT actor_count, value, suppressed FROM privacy_aggregates WHERE aggregate_id = :a"),
            {"a": aggregate_id},
        ).one()
    assert row.suppressed is True
    assert row.value is None
    assert row.actor_count == 2


def test_a_suppressed_aggregate_carrying_a_value_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Suppression that leaves the value in place is suppression at the display layer only."""
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, actor_count=9, suppressed=True, value='{"relevant": 0.5}')


def test_a_reported_aggregate_without_a_value_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, actor_count=9, suppressed=False, value=None)


def test_only_one_version_of_an_aggregate_cell_can_exist(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """This is the differencing defence, and it is why there is no version column.

    A reader who saw a cell before an erasure and again after would recover the
    erased subject's exact contribution by subtraction. The policy answer is that
    a recompute destroys its predecessor; making a second version unstorable means
    the recompute cannot forget to.
    """
    with sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, cohort_key="team:billing", value='{"relevant": 0.8}')
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, cohort_key="team:billing", value='{"relevant": 0.6}')


def test_recomputing_an_aggregate_replaces_the_cell_in_place(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The legal path, so the constraint above is not passing by forbidding recompute itself."""
    with sync_engine.begin() as conn:
        _aggregate(conn, tenant_id, cohort_key="team:payments", actor_count=11, value='{"relevant": 0.8}')
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE privacy_aggregates SET actor_count = 10, value = CAST('{\"relevant\": 0.7}' AS JSONB),"
                " computed_at = now() WHERE tenant_id = :t AND cohort_key = 'team:payments'"
            ),
            {"t": tenant_id},
        )
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_count FROM privacy_aggregates WHERE tenant_id = :t AND cohort_key = 'team:payments'"),
            {"t": tenant_id},
        ).all()
    assert len(rows) == 1, "a recompute must leave exactly one version of the cell"
    assert rows[0].actor_count == 10


def test_an_aggregate_window_must_be_ordered(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises((IntegrityError, DBAPIError)), sync_engine.begin() as conn:
        _aggregate(
            conn,
            tenant_id,
            cohort_key="team:backwards",
            window_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
            window_end=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
        )


def test_the_retention_and_derivative_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Throwaway database, for the same reason every suite above uses one."""
    scratch = f"rt_downgrade_{uuid.uuid4().hex[:8]}"
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
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("derivative_registrations")

        down = run("downgrade", "0042_derivation_and_curation")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        for table in (
            "privacy_aggregates",
            "derivative_work_outbox",
            "derivative_source_links",
            "derivative_registrations",
            "source_tombstones",
            "retention_policies",
        ):
            assert not after.has_table(table), f"{table} survived the downgrade"
        assert after.has_table("claim_derivations"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("privacy_aggregates")
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
# Signals as a reference-binding subject
#
# Every test below names "reference" or "binding" so the filtered gate selects
# it explicitly, for the same reason the feedback suite above names its own.
# ---------------------------------------------------------------------------


def _external_reference(conn: Any, tenant: uuid.UUID, *, external_id: str = "412") -> uuid.UUID:
    """One reference row, keyed the way the service keys it."""
    rid = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO context_external_references
                (reference_id, tenant_id, source_system, source_namespace, kind, external_id,
                 classification, external_authority, collision_key)
            VALUES (:rid, :tid, 'github', 'roughcompass/contextplane', 'pull_request', :eid,
                    'internal', 'platform-team', :key)
            """
        ),
        {"rid": rid, "tid": tenant, "eid": external_id, "key": f"key-{rid.hex}"},
    )
    return rid


def _binding(conn: Any, tenant: uuid.UUID, reference: uuid.UUID, subject: uuid.UUID, subject_type: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO context_reference_bindings (binding_id, tenant_id, reference_id, subject_type, subject_id)
            VALUES (:bid, :tid, :rid, :st, :sid)
            """
        ),
        {"bid": uuid.uuid4(), "tid": tenant, "rid": reference, "st": subject_type, "sid": subject},
    )


def test_a_signal_may_now_be_the_subject_of_a_reference_binding(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The whole point of the widening: before it, this insert was refused, so
    "which references did this signal carry" had no rows to answer from."""
    with sync_engine.begin() as conn:
        signal = _signal(conn, tenant_id)
        reference = _external_reference(conn, tenant_id, external_id="bind-signal-1")
        _binding(conn, tenant_id, reference, signal, "external_signal")

    with sync_engine.connect() as conn:
        bound = conn.execute(
            text(
                "SELECT reference_id FROM context_reference_bindings "
                "WHERE subject_type = 'external_signal' AND subject_id = :s"
            ),
            {"s": signal},
        ).scalar_one()
    assert bound == reference


@pytest.mark.parametrize("subject_type", ["task_checkpoint", "context_item"])
def test_the_subjects_that_could_bind_a_reference_before_still_can(
    sync_engine: Engine, tenant_id: uuid.UUID, subject_type: str
) -> None:
    """A widened set is only widened if nothing fell out of it. Checkpoints and
    context items were the two values in production use when this changed."""
    with sync_engine.begin() as conn:
        reference = _external_reference(conn, tenant_id, external_id=f"bind-{subject_type}")
        _binding(conn, tenant_id, reference, uuid.uuid4(), subject_type)

    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM context_reference_bindings WHERE reference_id = :r"), {"r": reference}
        ).scalar_one()
    assert count == 1


def test_a_reference_binding_of_an_unknown_subject_type_is_still_refused(
    sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Widened, not opened. The set stays closed so a typo cannot create a
    binding nobody queries for, and the refusal keeps the constraint's own name
    -- a refusal identified by a different name is one no existing reader
    recognises."""
    with pytest.raises(IntegrityError, match="ck_reference_binding_subject_type"), sync_engine.begin() as conn:
        reference = _external_reference(conn, tenant_id, external_id="bind-typo")
        _binding(conn, tenant_id, reference, uuid.uuid4(), "external_signl")


def test_the_reference_binding_check_kept_its_name(sync_engine: Engine) -> None:
    """Re-created rather than replaced: the name is what an existing refusal test
    matches on, and what an operator reading a failed insert sees."""
    with sync_engine.connect() as conn:
        admitted = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_reference_binding_subject_type' "
                "AND conrelid = 'context_reference_bindings'::regclass"
            )
        ).scalar_one()
    for subject_type in ("task_checkpoint", "context_item", "external_signal"):
        assert subject_type in admitted, f"{subject_type} is not in the check the database is enforcing"


def test_the_signal_reference_binding_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Throwaway database, for the same reason every suite above uses one.

    The downgrade is the interesting direction here. Re-narrowing the CHECK
    against a table still holding `external_signal` rows would fail and leave the
    database on neither revision, so the migration deletes them first -- and this
    proves it does, rather than trusting that no such row exists.
    """
    scratch = f"srb_downgrade_{uuid.uuid4().hex[:8]}"
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

        scratch_engine = create_engine(_sync_url(scratch_url))
        tenant = uuid.uuid4()
        with scratch_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'downgrade')"),
                {"t": tenant, "s": f"srb-{tenant.hex[:8]}"},
            )
            reference = _external_reference(conn, tenant, external_id="survives-the-downgrade")
            _binding(conn, tenant, reference, uuid.uuid4(), "external_signal")
            _binding(conn, tenant, reference, uuid.uuid4(), "task_checkpoint")

        down = run("downgrade", "0043_retention_and_derivatives")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        with scratch_engine.connect() as conn:
            surviving = (
                conn.execute(
                    text("SELECT subject_type FROM context_reference_bindings WHERE reference_id = :r"),
                    {"r": reference},
                )
                .scalars()
                .all()
            )
            # The reference outlives the downgrade: it is a shared row other
            # subjects still cite, and only the bindings the narrow schema has
            # nowhere to keep are dropped.
            assert (
                conn.execute(
                    text("SELECT count(*) FROM context_external_references WHERE reference_id = :r"), {"r": reference}
                ).scalar_one()
                == 1
            )
        assert sorted(surviving) == ["task_checkpoint"], "the downgrade kept a binding the narrow CHECK forbids"

        with pytest.raises(IntegrityError, match="ck_reference_binding_subject_type"), scratch_engine.begin() as conn:
            _binding(conn, tenant, reference, uuid.uuid4(), "external_signal")

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        with scratch_engine.begin() as conn:
            _binding(conn, tenant, reference, uuid.uuid4(), "external_signal")
        scratch_engine.dispose()
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
