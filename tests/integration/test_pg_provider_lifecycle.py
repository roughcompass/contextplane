"""The broker's provider lifecycle, against a real PostgreSQL server.

The unit tests prove the broker's *decisions*. This file proves the parts
only a real server can: that a migrated template can be published
unconnectable and still cloned, that clones are genuinely independent
databases, that concurrent clones do not collide, and that a run leaves the
server exactly as it found it.

**Why this file owns its own server.** The suite's session-scoped
`pg_container` fixture provisions one migrated database for every test that
asks for it. This file is testing the thing that provisions databases, so
depending on that fixture would mean measuring the harness with itself.
Nothing here requests `pg_container`; the server below is started, used,
and torn down by this module alone.

**Why some of this skips rather than fails.** `CONTEXTPLANE_TEST_PG` names
a provider a caller can *ask for* on a host that cannot supply it — asking
for devstack where no PostgreSQL binaries resolve, or for testcontainers
with no container runtime. The capability probe answers that honestly and
the tests skip with the reason, because a host that cannot supply a server
is a fact about the host, not a defect in the broker.

Note that "no `initdb` on `PATH`" is *not* that condition: the devstack
resolver also finds Postgres.app and the `pgserver` wheel, so a host with
an empty `PATH` for Postgres can still be fully capable. Availability is
what the probe says, never what `PATH` suggests.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.helpers.pg_provider import (
    build_broker,
    devstack_available,
    probe_capabilities,
    selected_mode,
)
from tests.helpers.pg_run_broker import (
    AdmissionError,
    BrokerManifest,
    ControlError,
    ControlPayload,
    SequenceLease,
    control_ttl_expiry,
    serialize_control,
)
from tests.helpers.pg_template import (
    SchemaEnvironment,
    ServerVersions,
    canonical_schema_digest,
    catalog_queries,
    compute_fingerprint,
    revision_chain,
    run_migrations,
    template_name,
    utc_date,
)

CONTROLLER = "lifecycle-controller"
SEQUENCE = "lifecycle-sequence"


# -- the server this module owns ------------------------------------------


@pytest.fixture(scope="module")
def admin_url() -> Iterator[str]:
    """An admin connection URL for the selected provider, or a skip.

    Yields a URL against the `postgres` maintenance database: every
    statement the broker issues (`CREATE DATABASE`, `DROP DATABASE`) has to
    run from outside the database it targets.
    """
    mode = selected_mode()
    capabilities = probe_capabilities(mode)
    if not capabilities.complete:
        _, reason = devstack_available() if mode == "devstack" else (False, capabilities.detail)
        pytest.skip(f"provider {mode} cannot supply a server here: {reason or capabilities.detail}")

    if mode == "testcontainers":
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer(image="pgvector/pgvector:pg16", username="postgres", password="password")
        container.with_command("postgres -c max_connections=50 -c shared_buffers=128MB")
        container.start()
        try:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(5432)
            yield f"postgresql://postgres:password@{host}:{port}/postgres"
        finally:
            container.stop()
        return

    if mode == "devstack":
        import uuid

        from scripts.devstack.cluster import Cluster
        from scripts.devstack.pg_provider import resolve_local

        run_id = uuid.uuid4().hex[:8]
        # tempfile.gettempdir() rather than a literal /tmp: the cluster's
        # socket path has to live somewhere writable, and on macOS that is a
        # per-user directory, not /tmp.
        cluster, lease = Cluster.for_run(resolve_local(), run_id, base=Path(tempfile.gettempdir()) / "cp-itp-clusters")
        lease.acquire()
        try:
            cluster.start(server_flags=("-c", "max_connections=50", "-c", "shared_buffers=128MB"))
            yield cluster.url("postgres").replace("postgresql+asyncpg://", "postgresql://")
        finally:
            cluster.destroy()
            lease.release()
        return

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("external provider requires DATABASE_URL")
    yield url


@pytest.fixture()
def broker(admin_url: str):
    """A broker whose databases are dropped even if a test fails midway."""
    instance = build_broker(admin_url, provider=selected_mode())
    try:
        yield instance
    finally:
        instance.cleanup()


@pytest.fixture(scope="module")
def published_template(admin_url: str) -> Iterator[str]:
    """One migrated, connection-disabled template for the whole module.

    Migrating is the expensive step this phase exists to do once, so the
    module does it once. Publication follows the same order the broker
    requires: migrate, verify the date has not rolled, terminate
    connections, then disable new ones.
    """
    from tests.helpers.pg_provider import admin_executor

    execute = admin_executor(admin_url)
    started = utc_date()
    fingerprint = compute_fingerprint(
        heads=["module-scope"],
        revision_chain=revision_chain(),
        environment=SchemaEnvironment.from_environ({}, date=started),
        versions=ServerVersions(postgres="16", pgvector="unknown"),
    )
    name = template_name(fingerprint)

    execute(f'DROP DATABASE IF EXISTS "{name}"')
    execute(f'CREATE DATABASE "{name}"')
    run_migrations(_migration_url(admin_url, name))

    assert utc_date() == started, "UTC date rolled during template creation; this run is not measurable"

    execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
    )
    execute(f"UPDATE pg_database SET datallowconn = false WHERE datname = '{name}'")
    try:
        yield name
    finally:
        execute(f"UPDATE pg_database SET datallowconn = true WHERE datname = '{name}'")
        execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )
        execute(f'DROP DATABASE IF EXISTS "{name}"')


def _swap_database(admin_url: str, database: str) -> str:
    """Point an admin URL at a different database on the same server."""
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{database}"


def _migration_url(admin_url: str, database: str) -> str:
    """The `+asyncpg` form Alembic needs.

    The project ships no synchronous driver, so a bare `postgresql://` URL
    resolves to psycopg2 and SQLAlchemy's async engine rejects it. The
    broker's own executor wants the bare form for `asyncpg.connect`, so the
    two spellings are converted at the boundary rather than one being
    carried everywhere.
    """
    return _swap_database(admin_url, database).replace("postgresql://", "postgresql+asyncpg://")


def _query(admin_url: str, database: str, sql: str) -> list[tuple[object, ...]]:
    from tests.helpers.pg_provider import admin_executor

    return admin_executor(_swap_database(admin_url, database))(sql)


# -- capability reporting -------------------------------------------------


def test_the_probe_agrees_with_the_server_it_produced(admin_url: str) -> None:
    """Reaching this test means a server exists, so the probe must say so."""
    capabilities = probe_capabilities()
    assert capabilities.complete
    assert capabilities.missing == ()
    assert capabilities.provider == selected_mode()


def test_probe_evidence_survives_a_json_round_trip() -> None:
    """Evidence is written to a manifest, so it has to survive json.dumps.

    Asserted on the round-trip rather than just calling `dumps`: a payload
    can serialize and still lose the fields a reader needs.
    """
    evidence = probe_capabilities().as_evidence()
    restored = json.loads(json.dumps(evidence))
    assert restored == evidence
    assert restored["provider"] == selected_mode()
    assert restored["complete"] is True
    assert set(restored["capabilities"]) == {"create", "clone", "terminate", "drop"}


# -- template publication -------------------------------------------------


def test_a_published_template_refuses_connections(admin_url: str, published_template: str) -> None:
    """Tests must never connect to a template, and the server enforces it."""
    rows = _query(admin_url, "postgres", f"SELECT datallowconn FROM pg_database WHERE datname = '{published_template}'")
    assert rows and rows[0][0] is False


def test_connecting_to_a_published_template_fails(admin_url: str, published_template: str) -> None:
    with pytest.raises(Exception, match="not currently accepting|datallowconn|is not currently"):
        _query(admin_url, published_template, "SELECT 1")


def test_the_template_carries_the_migrated_schema(admin_url: str, published_template: str, broker) -> None:
    """Proven through a clone, since the template itself is unconnectable."""
    clone = broker.clone_database(broker.database_name("worker", "schema_check"), template=published_template)
    rows = _query(admin_url, clone, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    assert int(rows[0][0]) > 0, "clone of the migrated template has no public tables"


def test_the_clone_has_pgvector_installed(admin_url: str, published_template: str, broker) -> None:
    clone = broker.clone_database(broker.database_name("worker", "vector_check"), template=published_template)
    rows = _query(admin_url, clone, "SELECT extname FROM pg_extension WHERE extname = 'vector'")
    assert rows, "migrated template is missing the vector extension"


def test_the_canonical_schema_digest_is_reproducible(admin_url: str, published_template: str, broker) -> None:
    """Two clones of one template must digest identically.

    This is the property template reuse depends on: if the digest moved
    between two copies of the same bytes, every reuse check would fail and
    every run would migrate from scratch.
    """
    first = broker.clone_database(broker.database_name("worker", "digest_a"), template=published_template)
    second = broker.clone_database(broker.database_name("worker", "digest_b"), template=published_template)

    def digest(database: str) -> str:
        return canonical_schema_digest(
            {dimension: _query(admin_url, database, sql) for dimension, sql in catalog_queries()}
        )

    assert digest(first) == digest(second)


def test_a_dropped_table_changes_the_clone_digest(admin_url: str, published_template: str, broker) -> None:
    """Proves the digest is actually reading the catalog, not a constant."""

    def digest(database: str) -> str:
        return canonical_schema_digest(
            {dimension: _query(admin_url, database, sql) for dimension, sql in catalog_queries()}
        )

    clone = broker.clone_database(broker.database_name("worker", "digest_mutate"), template=published_template)
    before = digest(clone)
    _query(admin_url, clone, "CREATE TABLE a_new_relation (id int primary key)")
    assert digest(clone) != before


# -- clone independence and concurrency ----------------------------------


def test_clones_are_independent_databases(admin_url: str, published_template: str, broker) -> None:
    """The property that makes sharing a server sound.

    A row written in one clone must be invisible in the other; the per-test
    session commits rather than rolling back, so anything less than full
    database isolation lets two workers see each other's writes.
    """
    first = broker.clone_database(broker.database_name("worker", "iso_a"), template=published_template)
    second = broker.clone_database(broker.database_name("worker", "iso_b"), template=published_template)

    _query(admin_url, first, "CREATE TABLE isolation_probe (id int primary key)")
    _query(admin_url, first, "INSERT INTO isolation_probe VALUES (1)")

    present = _query(admin_url, second, "SELECT to_regclass('public.isolation_probe') IS NOT NULL")
    assert present[0][0] is False, "a table created in one clone is visible in another"


def test_concurrent_clones_all_succeed(admin_url: str, published_template: str, broker) -> None:
    """Four workers cloning at once is the shape a parallel run actually uses."""
    names = [broker.database_name("worker", f"concurrent_{index}") for index in range(4)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda name: broker.clone_database(name, template=published_template), names))

    existing = {
        str(row[0])
        for row in _query(
            admin_url,
            "postgres",
            "SELECT datname FROM pg_database WHERE datname LIKE 'cp_worker_%_concurrent_%'",
        )
    }
    assert set(names) <= existing
    assert len(set(names)) == 4


def test_every_consumer_kind_gets_its_own_database(admin_url: str, published_template: str, broker) -> None:
    """Workers, migration scratch, and embedding scenarios never share one."""
    created = [
        broker.clone_database(broker.database_name(kind, "one"), template=published_template)
        for kind in ("worker", "scratch", "scenario")
    ]
    assert len(set(created)) == 3
    for database in created:
        rows = _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{database}'")
        assert rows, f"{database} was not created"


# -- distinct embedding templates ----------------------------------------


@pytest.mark.slow
def test_default_and_configured_embedding_templates_are_distinct(admin_url: str) -> None:
    """A configured embedding width must produce a different template.

    Runs a second real migration under `EMBEDDING_DIM=1536` rather than
    asserting on the fingerprint alone: the fingerprint differing is
    already unit-proven, and what matters here is that the *schema* the
    migration produces differs, since that is what a clone hands a worker.
    """
    from tests.helpers.pg_provider import admin_executor

    execute = admin_executor(admin_url)
    started = utc_date()
    configured = SchemaEnvironment.from_environ({"EMBEDDING_DIM": "1536"}, date=started)
    fingerprint = compute_fingerprint(
        heads=["embedding-scenario"],
        revision_chain=revision_chain(),
        environment=configured,
        versions=ServerVersions(postgres="16", pgvector="unknown"),
    )
    name = template_name(fingerprint, prefix="cp_tmpl_dim")

    execute(f'DROP DATABASE IF EXISTS "{name}"')
    execute(f'CREATE DATABASE "{name}"')
    try:
        run_migrations(_migration_url(admin_url, name), env={**os.environ, "EMBEDDING_DIM": "1536"})
        rows = _query(
            admin_url,
            name,
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid WHERE c.relname = 'embeddings' "
            "AND a.attname = 'vector' AND a.attnum > 0",
        )
        assert rows, "no embeddings.vector column found in the configured-dimension template"
        assert "1536" in str(rows[0][0]), f"expected a 1536-wide vector column, got {rows[0][0]!r}"
    finally:
        execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )
        execute(f'DROP DATABASE IF EXISTS "{name}"')


# -- cleanup and inventory ------------------------------------------------


def test_terminate_then_drop_removes_a_database(admin_url: str, published_template: str, broker) -> None:
    name = broker.clone_database(broker.database_name("worker", "dropme"), template=published_template)
    broker.drop_database(name)
    rows = _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
    assert not rows


def test_a_drop_succeeds_with_a_connection_open(admin_url: str, published_template: str, broker) -> None:
    """The case a bare DROP fails on.

    The broker terminates backends first, so a clone a worker is still
    connected to can be reclaimed instead of blocking cleanup.
    """
    name = broker.clone_database(broker.database_name("worker", "busy"), template=published_template)
    _query(admin_url, name, "SELECT 1")
    broker.drop_database(name)
    assert not _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")


def test_dropping_twice_is_idempotent(admin_url: str, published_template: str, broker) -> None:
    """Cleanup runs on paths that may already have cleaned up.

    The second drop must be a no-op rather than an error, and it must also
    stop tracking the database — a name left in `owned_databases` would be
    retried by every later cleanup.
    """
    name = broker.clone_database(broker.database_name("worker", "twice"), template=published_template)
    broker.drop_database(name)
    assert name not in broker.owned_databases

    broker.drop_database(name)
    assert name not in broker.owned_databases
    assert not _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")


def test_cleanup_leaves_the_server_as_it_was(admin_url: str, published_template: str) -> None:
    """Before/after inventory over a full provision-and-clean cycle.

    The template is expected to survive — it is the allowed warm state. A
    mutable database surviving is not.
    """
    instance = build_broker(admin_url, provider=selected_mode())
    before = instance.inventory()

    for index in range(3):
        instance.clone_database(instance.database_name("worker", f"inv_{index}"), template=published_template)
    assert instance.inventory().unexpected_against(before)["new_databases"], "clones did not appear in the inventory"

    assert instance.cleanup() == []
    after = instance.inventory()
    assert after.unexpected_against(before)["new_databases"] == []
    assert published_template in after.templates, "cleanup removed the live template"


def test_cleanup_cannot_remove_a_live_template(admin_url: str, published_template: str, broker) -> None:
    """Templates are not broker-owned, so broker cleanup must not touch them."""
    broker.clone_database(broker.database_name("worker", "keeps_template"), template=published_template)
    broker.cleanup()
    rows = _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{published_template}'")
    assert rows, "broker cleanup dropped a template it does not own"


def test_provisioning_boundaries_are_recorded(admin_url: str, published_template: str, broker) -> None:
    name = broker.clone_database(broker.database_name("worker", "timed"), template=published_template)
    broker.drop_database(name)
    recorded = {boundary.name for boundary in broker.boundaries}
    assert {"clone_database", "drop_database", "terminate_connections"} <= recorded


# -- lease and control against a real server -----------------------------


def _control(lease: SequenceLease, **overrides: object) -> str:
    payload: dict[str, object] = {
        "controller_id": lease.controller_id,
        "lease_id": lease.lease_id,
        "sequence_id": lease.sequence_id,
        "child_sequence_number": 1,
        "mode": "hard-gate",
        "role": "measured",
        "committed_worker_count": 2,
        "provider": lease.provider,
        "expected_product_commit": "b" * 40,
        "host_digest": "host",
        "template_fingerprint": "fingerprint",
        "collection_digest": "collection",
        "command_digest": "command",
        "nonce": "integration-nonce",
        "expires_at": control_ttl_expiry(600),
    }
    payload.update(overrides)
    return serialize_control(ControlPayload(**payload), lease.secret)  # type: ignore[arg-type]


def test_a_valid_control_admits_a_real_clone(admin_url: str, published_template: str, broker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    name = broker.database_name("worker", "controlled")
    broker.clone_database(name, template=published_template, control=_control(lease))
    assert _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
    broker.close_sequence(CONTROLLER)


def test_an_uncontrolled_clone_creates_no_database(admin_url: str, published_template: str, broker) -> None:
    """The refusal has to land before the server is touched."""
    broker.open_sequence(CONTROLLER, SEQUENCE)
    name = broker.database_name("worker", "refused")
    with pytest.raises(AdmissionError):
        broker.clone_database(name, template=published_template)
    assert not _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
    broker.close_sequence(CONTROLLER)


def test_a_replayed_control_creates_no_second_database(admin_url: str, published_template: str, broker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = _control(lease)
    broker.clone_database(broker.database_name("worker", "replay_first"), template=published_template, control=document)

    second = broker.database_name("worker", "replay_second")
    with pytest.raises(ControlError, match="already consumed"):
        broker.clone_database(second, template=published_template, control=document)
    assert not _query(admin_url, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{second}'")
    broker.close_sequence(CONTROLLER)


def test_the_manifest_hands_each_worker_its_own_clone(
    admin_url: str, published_template: str, broker, tmp_path: Path
) -> None:
    """End-to-end handoff: clone per worker, private manifest, redacted evidence."""
    manifest = BrokerManifest(run_id=broker.run_id)
    for worker in ("gw0", "gw1"):
        name = broker.clone_database(broker.database_name("worker", worker), template=published_template)
        manifest.assign(worker, _swap_database(admin_url, name), name)

    path = manifest.write(tmp_path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert (
            manifest.worker_environment("gw0")["CONTEXTPLANE_TEST_DATABASE_URL"]
            != manifest.worker_environment("gw1")["CONTEXTPLANE_TEST_DATABASE_URL"]
        )
        evidence = json.dumps(manifest.as_evidence())
        assert "postgresql://" not in evidence
    finally:
        manifest.delete()
    assert not path.exists()
