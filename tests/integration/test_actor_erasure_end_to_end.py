"""A real right-to-be-forgotten request, against a real database, start to finish.

Every part of this path was written, reviewed and unit-tested, and none of it had
ever run. The participant's five source queries named columns four of those tables
do not have; the tombstone bound a `UUID` into a `TEXT` column; and
`retention_policies` — which `source_tombstones` holds a foreign key into — was
created empty and never filled, so the very first statement of the very first
erasure would have failed even if the queries had been right.

Each of those is invisible to a unit test with a faked session, which is the point
of this file: the fake answers whatever it is asked, so a question no table can
answer still looks answered. So these tests seed the actual tables, run the actual
participant, and read the actual rows back.

Two levels, deliberately:

- **The participant alone**, with all five classes present, proving each query
  finds what its class stores and schedules the derivatives built from it.
- **The wired registry**, proving an erasure request survives every participant in
  the order the application registers them, and leaves behind a tombstone a
  verifier can be shown.

The per-class counts are asserted at the first level and not the second, and that
is not an oversight: participants ahead of this one delete the rows it reads, so
what a class schedules through the live registry depends on registration order
rather than on this query. Order is somebody else's contract to fix; whether the
query works is this one's.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.derivatives import ACTOR_RECORD_CLASSES, ContextDerivativeErasure
from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_LATER = _NOW + datetime.timedelta(days=365)

#: A key id and its material, so tombstone proofs are keyed rather than refused.
#: The shipped default configures none — an erasure that cannot mint a keyed
#: tombstone must fail loudly — so a test that wants one has to say so.
_KEY_ID = "test-key"
_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


async def _seed_actor_records(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """One row per record class, all authored by `actor_id`.

    Written as raw inserts rather than through each owning service because what is
    under test is how the erasure *reads* these tables. Going through five services
    would make a column-name regression show up as a service failure somewhere else.
    """
    # Exactly the classes the erasure walks — not every class the policy covers.
    # The other seven are logs, exports and derivatives, which nobody authors.
    ids = {record_class: uuid.uuid4() for record_class in ACTOR_RECORD_CLASSES}

    await session.execute(
        text(
            "INSERT INTO task_checkpoints (checkpoint_id, tenant_id, task_id, sequence, goal, "
            "                              author, recorded_at, retention_policy, digest) "
            "VALUES (:id, :t, :task, 1, 'ship it', :author, :now, 'standard', :digest)"
        ),
        {
            "id": ids[policies.RECORD_TASK_CHECKPOINT],
            "t": tenant_id,
            "task": uuid.uuid4(),
            # Text, not a uuid column: a checkpoint's author need not be an actor row.
            "author": str(actor_id),
            "now": _NOW,
            "digest": f"sha256:{uuid.uuid4().hex}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type, "
            "                              source_event_id, idempotency_key, content_digest, authority, "
            "                              classification, ingested_at, schema_version, payload) "
            "VALUES (:id, :t, 'github', :producer, 'human', :event, :idem, :digest, 'observer_extraction', "
            "        'internal', :now, 'external_signal.v1', '{\"conclusion\": \"success\"}'::jsonb)"
        ),
        {
            "id": ids[policies.RECORD_EXTERNAL_SIGNAL],
            "t": tenant_id,
            "producer": str(actor_id),
            "event": f"github:run:{uuid.uuid4().hex[:12]}",
            "idem": f"delivery-{uuid.uuid4().hex[:12]}",
            "digest": f"sha256:{uuid.uuid4().hex}",
            "now": _NOW,
        },
    )
    await session.execute(
        text(
            "INSERT INTO context_feedback (feedback_id, tenant_id, kind, rating, learning_eligible, note, "
            "                              reporter_id, reporter_type, idempotency_key, content_digest, created_at) "
            "VALUES (:id, :t, 'diagnostic_observation', 'irrelevant', FALSE, 'a note', "
            "        :reporter, 'human', :idem, :digest, :now)"
        ),
        {
            "id": ids[policies.RECORD_CONTEXT_FEEDBACK],
            "t": tenant_id,
            "reporter": str(actor_id),
            "idem": f"fb-{uuid.uuid4().hex[:12]}",
            "digest": f"sha256:{uuid.uuid4().hex}",
            "now": _NOW,
        },
    )
    await session.execute(
        text(
            "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
            "VALUES (:id, :t, 'complete', FALSE, :now, :requester)"
        ),
        {
            "id": ids[policies.RECORD_CONTEXT_RECEIPT],
            "t": tenant_id,
            "now": _NOW,
            "requester": str(actor_id),
        },
    )
    await session.execute(
        text(
            "INSERT INTO memory_claims (claim_id, author_tenant_id, author_actor_id, subject_reference, "
            "                           predicate, value_type, claim_category, value_jsonb, asserted_valid_from, "
            "                           status, visibility, source_authority, size_bytes, is_contested, created_at) "
            "VALUES (:id, :t, :author, 'svc:checkout', 'owns', 'string', 'ownership', "
            "        '\"team-a\"'::jsonb, :now, 'unlinked', 'private', 'unattributed', 12, FALSE, :now)"
        ),
        {
            "id": ids[policies.RECORD_MEMORY_CLAIM],
            "t": tenant_id,
            # The one table keyed on a real actor row, and the one scoped by the
            # authoring tenant rather than by `tenant_id`.
            "author": actor_id,
            "now": _NOW,
        },
    )
    return ids


async def _register_derivatives(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_ids: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """One registered derivative per class, so there is something to schedule.

    Without these the erasure would report five honest zeros and prove nothing: a
    query that finds no sources and a query that finds sources with no derivatives
    both enqueue nothing.
    """
    registered: dict[str, uuid.UUID] = {}
    for record_class, source_id in source_ids.items():
        registered[record_class] = await derivatives.register_derivative(
            session,
            tenant_id=tenant_id,
            kind=derivatives.KIND_VECTOR,
            storage_locator=f"vectors/{record_class}/{source_id}",
            audience_partition="private",
            classification="internal",
            handler_version="v1",
            sources=[derivatives.SourceRef(record_class=record_class, source_id=source_id, expires_at=_LATER)],
        )
    return registered


@pytest_asyncio.fixture
async def erasure_world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """A tenant, an erased actor, a colleague, one record each, and their derivatives."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, colleague_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'actor erasure')"),
                {"t": tenant_id, "s": f"aee-{tenant_id.hex[:10]}"},
            )
            for aid in (actor_id, colleague_id):
                await session.execute(
                    text(
                        "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                        "VALUES (:a, :t, 'actor', :sub, :now)"
                    ),
                    {"a": aid, "t": tenant_id, "sub": f"sub-{aid.hex[:10]}", "now": _NOW},
                )
            targets = await _seed_actor_records(session, tenant_id=tenant_id, actor_id=actor_id)
            colleague = await _seed_actor_records(session, tenant_id=tenant_id, actor_id=colleague_id)
            derivative_ids = await _register_derivatives(session, tenant_id=tenant_id, source_ids=targets)
            colleague_derivatives = await _register_derivatives(session, tenant_id=tenant_id, source_ids=colleague)

        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "colleague_id": colleague_id,
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"]),
            "sources": targets,
            "derivatives": derivative_ids,
            "colleague_derivatives": colleague_derivatives,
        }
    finally:
        await engine.dispose()


def _participant(world: dict[str, Any]) -> ContextDerivativeErasure:
    return ContextDerivativeErasure(world["factory"], _salts())


async def _rows(world: dict[str, Any], sql: str, params: dict[str, Any]) -> list[Any]:
    async with world["factory"]() as session:
        return list((await session.execute(text(sql), params)).mappings().all())


# --- the participant, against the tables it actually reads ---------------------


@pytest.mark.asyncio
async def test_the_erasure_finds_what_the_actor_authored_in_every_class(erasure_world: dict[str, Any]) -> None:
    """Five classes, five different column names, five different scopes.

    This is the test that would have caught the shipped participant: it named
    `producer_actor_id`, `actor_id`, `requested_by_actor_id` and
    `asserted_by_actor_id`, none of which exist, and `author_actor_id` on a table
    that spells it `author`.
    """
    scheduled = await _participant(erasure_world).erase_actor(erasure_world["ctx"], erasure_world["actor_id"])

    assert scheduled == {record_class: 1 for record_class in erasure_world["sources"]}


@pytest.mark.asyncio
async def test_the_scheduled_work_names_its_derivative_and_its_cause(erasure_world: dict[str, Any]) -> None:
    """An outbox item that cannot say what caused it is one nobody can audit."""
    await _participant(erasure_world).erase_actor(erasure_world["ctx"], erasure_world["actor_id"])

    work = await _rows(
        erasure_world,
        "SELECT derivative_id, operation, trigger, tombstone_id, state "
        "FROM derivative_work_outbox WHERE tenant_id = :t",
        {"t": erasure_world["tenant_id"]},
    )

    assert {row["derivative_id"] for row in work} == set(erasure_world["derivatives"].values())
    assert {row["operation"] for row in work} == {derivatives.OPERATION_DELETE}
    assert {row["trigger"] for row in work} == {derivatives.TRIGGER_ERASURE}
    assert all(row["tombstone_id"] is not None for row in work)


@pytest.mark.asyncio
async def test_a_colleagues_records_are_not_scheduled(erasure_world: dict[str, Any]) -> None:
    """Same tenant, same five tables, different author. An erasure scoped only by
    tenant would take their work too and report it as the erased person's."""
    await _participant(erasure_world).erase_actor(erasure_world["ctx"], erasure_world["actor_id"])

    work = await _rows(
        erasure_world,
        "SELECT derivative_id FROM derivative_work_outbox WHERE tenant_id = :t",
        {"t": erasure_world["tenant_id"]},
    )

    assert set(erasure_world["colleague_derivatives"].values()).isdisjoint({row["derivative_id"] for row in work})


@pytest.mark.asyncio
async def test_running_the_same_erasure_twice_schedules_nothing_new(erasure_world: dict[str, Any]) -> None:
    """Retrying a partly-failed erasure is the normal recovery path, so a repeat
    must be free rather than an amplifier."""
    participant = _participant(erasure_world)
    first = await participant.erase_actor(erasure_world["ctx"], erasure_world["actor_id"])
    second = await participant.erase_actor(erasure_world["ctx"], erasure_world["actor_id"])

    assert set(first.values()) == {1}
    assert set(second.values()) == {0}
    work = await _rows(
        erasure_world,
        "SELECT work_id FROM derivative_work_outbox WHERE tenant_id = :t",
        {"t": erasure_world["tenant_id"]},
    )
    assert len(work) == len(erasure_world["derivatives"])


# --- the tombstone, and what a verifier may be shown ---------------------------


@pytest.mark.asyncio
async def test_the_tombstone_is_written_and_is_disclosable(erasure_world: dict[str, Any]) -> None:
    """ "Present" is half of it. A tombstone whose policy row does not exist cannot
    be written at all, and one whose class has no verifier sentence cannot be shown
    to the person asking whether their erasure happened."""
    await _participant(erasure_world).erase_actor(erasure_world["ctx"], erasure_world["actor_id"])

    stored = await _rows(
        erasure_world,
        "SELECT t.record_class, t.subject_id, t.effective_at, t.policy_version, t.proof_hmac, "
        "       t.request_authority, p.verifier_disclosure "
        "  FROM source_tombstones t "
        "  JOIN retention_policies p "
        "    ON p.policy_version = t.policy_version AND p.record_class = t.record_class "
        " WHERE t.tenant_id = :t AND t.subject_id = :s",
        {"t": erasure_world["tenant_id"], "s": erasure_world["actor_id"]},
    )

    assert len(stored) == 1
    row = stored[0]
    assert row["request_authority"] == str(erasure_world["actor_id"])
    disclosure = tombstones.disclose(
        record_class=row["record_class"],
        subject_id=row["subject_id"],
        erased_at=row["effective_at"],
        policy_version=row["policy_version"],
        proof_hmac=row["proof_hmac"],
        salt_available=True,
    )
    assert disclosure.proof_hmac == row["proof_hmac"]
    # The sentence the database holds and the sentence the code would disclose are
    # the same sentence. Two answers to "what may a verifier be told" is the state
    # this join exists to make impossible.
    assert disclosure.verifier_disclosure == row["verifier_disclosure"]


@pytest.mark.asyncio
async def test_a_tombstone_write_succeeds_against_a_freshly_migrated_database(
    erasure_world: dict[str, Any],
) -> None:
    """The property the seeding revision exists for, stated on its own.

    `source_tombstones` references `retention_policies` on (version, class). With
    that table empty every write violated the key, so no erasure could record that
    it happened — and nothing said so, because no test wrote a tombstone for real.
    """
    subject_id = uuid.uuid4()
    async with erasure_world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO source_tombstones (tenant_id, record_class, subject_id, policy_version, "
                "                               request_authority, reason, effective_at, proof_hmac, "
                "                               propagation_state) "
                "VALUES (:t, :cls, :s, :v, 'operator', 'erasure', :now, :proof, 'pending')"
            ),
            {
                "t": erasure_world["tenant_id"],
                "cls": policies.RECORD_TASK_CHECKPOINT,
                "s": subject_id,
                "v": policies.POLICY_VERSION,
                "now": _NOW,
                "proof": "sha256:whatever",
            },
        )

    # Read back rather than trusting the absence of an exception: the row has to
    # survive the commit, and the foreign key is checked there.
    stored = await _rows(
        erasure_world,
        "SELECT policy_version FROM source_tombstones WHERE tenant_id = :t AND subject_id = :s",
        {"t": erasure_world["tenant_id"], "s": subject_id},
    )
    assert [row["policy_version"] for row in stored] == [policies.POLICY_VERSION]


# --- the seeded policy rows ----------------------------------------------------


@pytest.mark.asyncio
async def test_every_approved_disposition_reached_the_database(erasure_world: dict[str, Any]) -> None:
    """The migration carries these values as literals so history stays reproducible.
    That leaves it free to drift from the module, so this is where drift dies."""
    stored = await _rows(
        erasure_world,
        "SELECT record_class, legal_basis, retention_days, erasure_mode, minimization_action, "
        "       tombstone_behaviour, verifier_disclosure "
        "  FROM retention_policies WHERE policy_version = :v",
        {"v": policies.POLICY_VERSION},
    )
    by_class = {row["record_class"]: row for row in stored}

    assert set(by_class) == set(policies.RECORD_CLASSES)
    for record_class in policies.RECORD_CLASSES:
        approved = policies.disposition(record_class)
        row = by_class[record_class]
        assert row["legal_basis"] == approved.legal_basis, record_class
        assert row["retention_days"] == approved.retention_days, record_class
        assert row["erasure_mode"] == approved.erasure_mode, record_class
        assert row["minimization_action"] == approved.minimization_action, record_class
        assert row["tombstone_behaviour"] == approved.tombstone_behaviour, record_class
        assert row["verifier_disclosure"] == approved.verifier_disclosure, record_class


# --- the whole registry, as the application wires it ---------------------------


@pytest.mark.asyncio
async def test_a_real_erasure_request_survives_every_registered_participant(pg_container: str) -> None:
    """End to end on the live app: each participant runs, and the last one records it.

    Per-class counts are not asserted here on purpose — see the module docstring.
    What this pins is that an erasure request reaches the end of the registry at
    all, which it could not do while the derivative participant raised on its first
    query.
    """
    from tests.helpers.auth_harness import EntitlementAuthHarness, default_settings

    settings = default_settings(pg_container).model_copy(
        update={
            "retention_keys": SecretStr(f"{_KEY_ID}:{_KEY_HEX}"),
            "retention_active_key_id": _KEY_ID,
        }
    )
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'wired erasure')"),
                {"t": tenant_id, "s": f"wire-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
            sources = await _seed_actor_records(session, tenant_id=tenant_id, actor_id=actor_id)
            await _register_derivatives(session, tenant_id=tenant_id, source_ids=sources)

        async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
            registry = harness.app.state.erasure
            results = await registry.erase_actor(
                TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"]),
                actor_id,
            )

        # Every registered subsystem reported. A participant that raised would have
        # stopped the fan-out, and the list would be short rather than wrong.
        assert [result.subsystem for result in results] == list(registry.subsystems)
        assert "context_derivatives" in registry.subsystems

        async with factory() as session:
            tombstone = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t AND subject_id = :s "
                        "  AND record_class = :cls"
                    ),
                    {"t": tenant_id, "s": actor_id, "cls": policies.RECORD_DERIVATIVE},
                )
            ).scalar_one()
        assert int(tombstone) == 1
    finally:
        await engine.dispose()
