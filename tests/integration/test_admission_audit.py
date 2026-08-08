"""A refusal record fits the audit table, proved against a real Postgres.

Admission decides; it holds no session and writes nothing. So what this suite
proves is not that a row gets written -- that is the enforcement task's job --
but that the record admission produces *can* be written, in the shape the
rejection-audit contract asks for, into the table that already exists.

That is worth proving before anything depends on it. `audit_log` was chosen over
a new table on the strength of a column-by-column reading, and a reading is not
a test: the columns have constraints, some are `NOT NULL`, two carry foreign
keys, and a shape that fits on paper can still fail on insert. Finding that out
here costs one test; finding it out while wiring five write surfaces costs the
wiring.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.admission import RefusalRecord, admit

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)

#: A fabricated specimen. Syntactically an SSN, deliberately not a real one --
#: a genuine prohibited value in a test file is what this whole module exists to
#: keep out of storage.
_SPECIMEN = "patient ssn 123-45-6789 on file"

#: The action a refusal is recorded under. Named here rather than imported
#: because the shared constant belongs to the enforcement task that owns
#: `audit/actions.py`; this suite only needs a value the column accepts.
_REFUSAL_ACTION = "context.admission_refused"


@pytest_asyncio.fixture
async def principal(pg_container: str) -> AsyncIterator[dict[str, object]]:
    """A real tenant and actor, because two of the columns are foreign keys.

    Inserting against invented ids would prove the shape fits a table with the
    constraints switched off, which is the one variant nobody deploys.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
                {"t": tenant_id, "s": f"adm-{tenant_id.hex[:10]}", "n": "admission audit"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'admission-test-actor', :sub, now())"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}"},
            )
        yield {"factory": factory, "tenant_id": tenant_id, "actor_id": actor_id}
    finally:
        await engine.dispose()


async def _write(principal: dict[str, object], refusal: RefusalRecord) -> uuid.UUID:
    """Persist one refusal the way an enforcing caller would have to."""
    audit_id = uuid.uuid4()
    factory = principal["factory"]
    async with factory() as session, session.begin():  # type: ignore[operator]
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, :ttype, :target, NULL, "
                "        CAST(:after AS JSONB), :ts, NULL, :error_code)"
            ),
            {
                "audit_id": audit_id,
                "tid": refusal.tenant_id,
                "aid": refusal.actor_id,
                "action": _REFUSAL_ACTION,
                "ttype": refusal.target_type,
                "target": refusal.target_id,
                "after": json.dumps(refusal.as_audit_payload(), sort_keys=True, default=str),
                "ts": refusal.occurred_at,
                "error_code": refusal.trigger,
            },
        )
    return audit_id


def _refusal(principal: dict[str, object], *, target_id: uuid.UUID | None) -> RefusalRecord:
    decision = admit(
        _SPECIMEN,
        field_type="memory_session_event.body",
        tenant_id=principal["tenant_id"],  # type: ignore[arg-type]
        now=_NOW,
        actor_id=principal["actor_id"],  # type: ignore[arg-type]
        target_id=target_id,
    )
    assert not decision.admitted, "the specimen must be refused, or this suite proves nothing"
    return decision.refusals[0]


# --- The record fits the table ------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_persists_into_the_existing_audit_table(principal: dict[str, object]) -> None:
    """The claim that decided against a new table, checked against the table."""
    refusal = _refusal(principal, target_id=uuid.uuid4())

    audit_id = await _write(principal, refusal)

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        row = (
            (
                await session.execute(
                    text("SELECT action, target_type, error_code, after_jsonb, ts FROM audit_log WHERE audit_id = :a"),
                    {"a": audit_id},
                )
            )
            .mappings()
            .first()
        )

    assert row is not None
    assert row["action"] == _REFUSAL_ACTION
    assert row["target_type"] == "memory_session_event.body"
    assert row["error_code"] == "pii_blocked"
    assert row["after_jsonb"]["pii_class"] == "ssn"
    assert row["ts"] == _NOW


@pytest.mark.asyncio
async def test_the_stored_payload_carries_no_prohibited_value(principal: dict[str, object]) -> None:
    """The point of refusing is that the content is prohibited. An audit row is
    the one place guaranteed to be retained and read, so a refusal that copied
    the value there would put it somewhere worse than where it was headed."""
    refusal = _refusal(principal, target_id=uuid.uuid4())

    audit_id = await _write(principal, refusal)

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        stored = (
            (
                await session.execute(
                    text("SELECT after_jsonb::text AS body FROM audit_log WHERE audit_id = :a"), {"a": audit_id}
                )
            )
            .mappings()
            .first()
        )

    assert stored is not None
    assert "123-45-6789" not in stored["body"]
    assert "123456789" not in stored["body"]
    for field in ("offset", "length", "match_offset", "match_length", "excerpt"):
        assert field not in stored["body"], f"{field} locates the value inside stored text"


@pytest.mark.asyncio
async def test_the_class_and_the_coarse_category_are_both_kept(principal: dict[str, object]) -> None:
    """The reference inventory names `pii_category` both as the class and as a
    map onto the coarse detector category; those are different columns. Both are
    stored, so neither reading loses information."""
    refusal = _refusal(principal, target_id=uuid.uuid4())

    audit_id = await _write(principal, refusal)

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        payload = (
            (await session.execute(text("SELECT after_jsonb FROM audit_log WHERE audit_id = :a"), {"a": audit_id}))
            .mappings()
            .first()
        )

    assert payload is not None
    assert payload["after_jsonb"]["pii_class"] == "ssn"
    assert payload["after_jsonb"]["pii_category"] not in (None, "", "ssn")


@pytest.mark.asyncio
async def test_a_null_attribution_persists_rather_than_failing(principal: dict[str, object]) -> None:
    """`strategy_id` is set only where a namespace is present, so a refusal
    without one is ordinary. A column that refused it would push callers into
    inventing an attribution."""
    refusal = _refusal(principal, target_id=uuid.uuid4())

    audit_id = await _write(principal, refusal)

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        payload = (
            (await session.execute(text("SELECT after_jsonb FROM audit_log WHERE audit_id = :a"), {"a": audit_id}))
            .mappings()
            .first()
        )

    assert payload is not None
    assert payload["after_jsonb"]["strategy_id"] is None


# --- What the enforcing caller must supply ------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_with_no_target_cannot_be_persisted_as_is(principal: dict[str, object]) -> None:
    """The constraint the enforcement task inherits, surfaced here rather than
    there.

    `admit` accepts `target_id=None`, because a caller may ask about content
    before the row it belongs to exists. `audit_log.target_id` is `NOT NULL`. So
    a persisting caller has to supply the subject it was writing to -- the
    session, the task, the entry -- and cannot simply forward what admission
    returned. Better found by one test than by five wired surfaces.
    """
    refusal = _refusal(principal, target_id=None)

    with pytest.raises(IntegrityError):
        await _write(principal, refusal)


@pytest.mark.asyncio
async def test_the_trigger_survives_as_a_queryable_column(principal: dict[str, object]) -> None:
    """An auditor asking "what was refused today" filters rather than scanning
    JSON, so the trigger goes in a column and not only in the payload."""
    refusal = _refusal(principal, target_id=uuid.uuid4())
    await _write(principal, refusal)

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        count = (
            (
                await session.execute(
                    text(
                        "SELECT count(*) AS n FROM audit_log "
                        "WHERE tenant_id = :t AND action = :action AND error_code = 'pii_blocked'"
                    ),
                    {"t": principal["tenant_id"], "action": _REFUSAL_ACTION},
                )
            )
            .mappings()
            .first()
        )

    assert count is not None
    assert count["n"] == 1


# --- The wired path writes it, not just the shape fitting ---------------------
#
# Everything above proves a refusal record *can* be stored. These prove the
# helper every write surface now calls actually stores one, against a live
# database -- the difference between a shape that fits and a row that lands.


@pytest.mark.asyncio
async def test_the_shared_helper_refuses_and_records(principal: dict[str, object]) -> None:
    from contextplane.security.pii_guard import AdmissionRefused, admission_target_id, admit_or_refuse
    from contextplane.types import TenantContext

    ctx = TenantContext(
        tenant_id=principal["tenant_id"],  # type: ignore[arg-type]
        actor_id=principal["actor_id"],  # type: ignore[arg-type]
        roles=["producer"],
    )

    with pytest.raises(AdmissionRefused) as refused:
        await admit_or_refuse(
            principal["factory"],  # type: ignore[arg-type]
            ctx,
            _SPECIMEN,
            "memory_session_event.body",
            subject="session-42",
        )

    assert "ssn" in refused.value.decision.classes

    expected_target = admission_target_id(
        tenant_id=ctx.tenant_id, field_type="memory_session_event.body", subject="session-42"
    )
    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        row = (
            (
                await session.execute(
                    text(
                        "SELECT action, error_code, after_jsonb FROM audit_log "
                        "WHERE tenant_id = :t AND target_id = :target"
                    ),
                    {"t": ctx.tenant_id, "target": expected_target},
                )
            )
            .mappings()
            .first()
        )

    assert row is not None, "the refusal was raised but never recorded"
    assert row["action"] == "context.admission_refused"
    assert row["error_code"] == "pii_blocked"
    assert row["after_jsonb"]["subject"] == "session-42"


@pytest.mark.asyncio
async def test_the_audit_target_is_stable_for_the_same_subject(principal: dict[str, object]) -> None:
    """An auditor recomputes the id to find every refusal against one session.
    A random id per refusal would satisfy the column and answer no question."""
    from contextplane.security.pii_guard import admission_target_id

    tenant = principal["tenant_id"]
    first = admission_target_id(tenant_id=tenant, field_type="claim_value", subject="s-1")  # type: ignore[arg-type]
    again = admission_target_id(tenant_id=tenant, field_type="claim_value", subject="s-1")  # type: ignore[arg-type]
    other = admission_target_id(tenant_id=tenant, field_type="claim_value", subject="s-2")  # type: ignore[arg-type]

    assert first == again
    assert first != other


@pytest.mark.asyncio
async def test_admitted_content_records_nothing(principal: dict[str, object]) -> None:
    """The audit table is for refusals. A row per admitted write would bury them."""
    from contextplane.security.pii_guard import admit_or_refuse
    from contextplane.types import TenantContext

    ctx = TenantContext(
        tenant_id=principal["tenant_id"],  # type: ignore[arg-type]
        actor_id=principal["actor_id"],  # type: ignore[arg-type]
        roles=["producer"],
    )

    await admit_or_refuse(
        principal["factory"],  # type: ignore[arg-type]
        ctx,
        "the deploy finished and the queue drained",
        "claim_value",
        subject="s-clean",
    )

    factory = principal["factory"]
    async with factory() as session:  # type: ignore[operator]
        count = (
            (
                await session.execute(
                    text("SELECT count(*) AS n FROM audit_log WHERE tenant_id = :t AND action = :a"),
                    {"t": ctx.tenant_id, "a": "context.admission_refused"},
                )
            )
            .mappings()
            .first()
        )

    assert count is not None
    assert count["n"] == 0
