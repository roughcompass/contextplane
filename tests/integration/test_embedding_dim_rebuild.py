"""EMBEDDING_DIM on a fresh database: the width is set at creation, not rebuilt.

The migration chain this file used to test squashed a rebuild branch that
only ever fired once, while migrating *through* the one revision that added
it — and only for a database that already held 384-dimensional vectors. That
branch does not exist anymore because the baseline this repository now ships
does not need it: there is nothing to rebuild on a fresh database. The
baseline reads `EMBEDDING_DIM` once, at `CREATE TABLE embeddings` time, and
creates the `vector` column at the configured width directly.

What is still true, and still worth an integration test rather than a unit
one: a real `alembic upgrade head` against a real Postgres, with
`EMBEDDING_DIM` set in the environment, produces a column at that width and a
working HNSW index on every hash partition — not just a Python function that
returns the right integer.

Changing `EMBEDDING_DIM` against a database already at head — the case the
old rebuild branch handled, badly, for exactly one migration's lifetime — has
no mechanism here either. That is deliberate: it is a destructive, one-time
operator action (delete and recompute every embedding), and it belongs in an
explicit, reviewed script when the need actually arises, not in a migration
that runs unattended as part of every deploy.

**Distinct databases, one shared server.** These scenarios need databases
that disagree about `EMBEDDING_DIM`; they do not need servers that disagree.
This module used to run `initdb` and start a cluster on a fixed port 5488
four times over, and skipped entirely when no local `bindir` was set — so the
width contract went unverified on exactly the machines least likely to have
Postgres binaries on `PATH`. It now takes the shared provider-owned server
and asks the broker for four distinct databases on it: a clone of a template
migrated at the default width, a clone of one migrated at a configured width,
a second clone of that same configured template for the index check, and — for
the invalid-value scenario — an *empty* database, because a clone of an
already-migrated template would have no `upgrade head` left to fail.

Two templates rather than four migrations is the whole saving: the width is
schema-affecting, so it belongs in the template fingerprint, and each width
is migrated once and then cloned.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers.pg_provider import (
    admin_executor,
    build_broker,
    devstack_available,
    probe_capabilities,
    selected_mode,
)
from tests.helpers.pg_run_broker import RunBroker, redacted_digest
from tests.helpers.pg_template import (
    SchemaEnvironment,
    ServerVersions,
    TemplateError,
    assert_no_rollover,
    canonical_schema_digest,
    catalog_queries,
    compute_fingerprint,
    revision_chain,
    run_migrations,
    template_name,
    utc_date,
)

_TARGET_DIM = 128
_DEFAULT_DIM = 384

# The `kind` every database this module provisions is tagged with, so a leaked
# database is attributable to these scenarios rather than to "the run".
_SCENARIO_KIND = "embedscenario"


# -- the server these scenarios share -------------------------------------


@pytest.fixture(scope="module")
def admin_url() -> Iterator[str]:
    """An admin connection URL for the selected provider, or a skip.

    Yields a URL against the `postgres` maintenance database: `CREATE
    DATABASE` and `DROP DATABASE` have to run from outside the database they
    target.

    A skip here is a statement about the host, never about `EMBEDDING_DIM`.
    Only the capability probe can produce one — in particular an unset
    `CONTEXTPLANE_PG_BINDIR` cannot, because the devstack resolver also finds
    Postgres.app and the `pgserver` wheel, so a host with no Postgres on
    `PATH` may still be fully capable.
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
        cluster, lease = Cluster.for_run(
            resolve_local(), run_id, base=Path(tempfile.gettempdir()) / "cp-embeddim-clusters"
        )
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


@pytest.fixture(scope="module")
def broker(admin_url: str) -> Iterator[RunBroker]:
    """Owns every database these scenarios create, dropped even on failure."""
    instance = build_broker(admin_url, provider=selected_mode())
    try:
        yield instance
    finally:
        instance.cleanup()


def _swap_database(admin_url: str, database: str) -> str:
    """Point an admin URL at a different database on the same server."""
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{database}"


def _migration_url(admin_url: str, database: str) -> str:
    """The `+asyncpg` form Alembic needs.

    The project ships no synchronous driver, so a bare `postgresql://` URL
    resolves to psycopg2 and SQLAlchemy's async engine rejects it.
    """
    return _swap_database(admin_url, database).replace("postgresql://", "postgresql+asyncpg://")


def _query(admin_url: str, database: str, sql: str) -> list[tuple[object, ...]]:
    return admin_executor(_swap_database(admin_url, database))(sql)


def _migration_env(**overrides: str) -> dict[str, str]:
    """A migration environment that keeps the parent's PATH and HOME.

    `migration_environment` replaces the whole environment when handed one
    rather than merging, so the parent's has to be carried in explicitly —
    the Alembic subprocess still needs to find its interpreter's dependencies.
    """
    return {**os.environ, **overrides}


def _publish_template(admin_url: str, broker: RunBroker, *, label: str, dim: str | None) -> str:
    """Migrate one template at *dim* and publish it unconnectable.

    Follows the order publication requires: create, migrate under a UTC-pinned
    subprocess, verify the calendar date did not roll, terminate connections,
    then refuse new ones. A template built across a UTC-date boundary would
    carry partition DDL that disagrees with the fingerprint naming it.
    """
    schema_env = {} if dim is None else {"EMBEDDING_DIM": dim}
    started = utc_date()
    fingerprint = compute_fingerprint(
        heads=[f"embedding-dim-{label}"],
        revision_chain=revision_chain(),
        environment=SchemaEnvironment.from_environ(schema_env, date=started),
        versions=ServerVersions(postgres="16", pgvector="unknown"),
    )
    name = template_name(fingerprint)

    execute = admin_executor(admin_url)
    execute(f'DROP DATABASE IF EXISTS "{name}"')
    broker.create_database(name)
    run_migrations(_migration_url(admin_url, name), env=_migration_env(**schema_env))
    assert_no_rollover(started, stage=f"{label} template creation")

    broker.terminate_connections(name)
    broker.disable_connections(name)
    return name


@pytest.fixture(scope="module")
def default_width_template(admin_url: str, broker: RunBroker) -> Iterator[str]:
    """A template migrated with `EMBEDDING_DIM` unset."""
    name = _publish_template(admin_url, broker, label="default", dim=None)
    try:
        yield name
    finally:
        admin_executor(admin_url)(f"UPDATE pg_database SET datallowconn = true WHERE datname = '{name}'")
        broker.drop_database(name)


@pytest.fixture(scope="module")
def configured_width_template(admin_url: str, broker: RunBroker) -> Iterator[str]:
    """A template migrated at the configured width."""
    name = _publish_template(admin_url, broker, label="configured", dim=str(_TARGET_DIM))
    try:
        yield name
    finally:
        admin_executor(admin_url)(f"UPDATE pg_database SET datallowconn = true WHERE datname = '{name}'")
        broker.drop_database(name)


@contextmanager
def _scenario_database(broker: RunBroker, *, label: str, template: str | None = None) -> Iterator[str]:
    """One scenario's own database, terminated and dropped independently.

    `template=None` means a genuinely empty database: the scenario that has
    to watch `alembic upgrade head` fail needs migrations still to run.
    """
    name = broker.database_name(_SCENARIO_KIND, label)
    if template is None:
        broker.create_database(name)
    else:
        broker.clone_database(name, template=template)
    try:
        yield name
    finally:
        # Terminates before dropping, and drops this scenario alone: a
        # scenario must not be able to take another one's database with it.
        broker.drop_database(name)


def _vector_width(admin_url: str, database: str) -> int:
    rows = _query(
        admin_url,
        database,
        "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        " WHERE c.relname = 'embeddings' AND a.attname = 'vector'",
    )
    return int(rows[0][0])


def _catalog_digest(admin_url: str, database: str) -> str:
    """The canonical schema digest of one scenario database."""
    return canonical_schema_digest(
        {dimension: _query(admin_url, database, sql) for dimension, sql in catalog_queries()}
    )


# -- the four width scenarios ---------------------------------------------


def test_a_default_width_creates_the_documented_384_dim_column(
    admin_url: str, broker: RunBroker, default_width_template: str
) -> None:
    with _scenario_database(broker, label="default_width", template=default_width_template) as database:
        assert _vector_width(admin_url, database) == _DEFAULT_DIM


def test_a_configured_width_creates_the_column_at_that_width(
    admin_url: str, broker: RunBroker, configured_width_template: str
) -> None:
    """No opt-in required — a fresh database has no existing vectors to lose,
    so there is nothing destructive about honouring EMBEDDING_DIM at creation."""
    with _scenario_database(broker, label="configured_width", template=configured_width_template) as database:
        assert _vector_width(admin_url, database) == _TARGET_DIM


def test_hnsw_indexes_exist_on_every_partition_at_a_configured_width(
    admin_url: str, broker: RunBroker, configured_width_template: str
) -> None:
    with _scenario_database(broker, label="hnsw_every_partition", template=configured_width_template) as database:
        hnsw = _query(
            admin_url,
            database,
            "SELECT count(*) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            " JOIN pg_am am ON am.oid = i.relam WHERE am.amname = 'hnsw'",
        )
        assert int(hnsw[0][0]) >= 1, "no HNSW index was built at the configured width"

        # "every partition" is what the name promises, so count the partitions
        # rather than trusting that at least one index implies all of them.
        partitions = _query(
            admin_url,
            database,
            "SELECT count(*) FROM pg_inherits i JOIN pg_class parent ON parent.oid = i.inhparent "
            " WHERE parent.relname = 'embeddings'",
        )
        assert int(partitions[0][0]) >= 1, "embeddings is not partitioned in this database"
        assert int(hnsw[0][0]) == int(
            partitions[0][0]
        ), f"{hnsw[0][0]} HNSW indexes across {partitions[0][0]} partitions; every partition needs one"


def test_an_invalid_embedding_dim_fails_the_migration_rather_than_the_first_write(
    admin_url: str, broker: RunBroker
) -> None:
    """A mistyped value fails the deploy, not silently falls back to a default
    that then disagrees with the embedder's actual output width.

    Runs against an empty database on purpose. Cloning a migrated template
    would leave `upgrade head` with nothing to do, and the scenario would pass
    without ever reaching the validation it exists to check.

    Two different layers can catch the bad value, and the assertion accepts
    either: settings validation refuses it while Alembic's `env.py` is still
    importing, before the migration's own `EMBEDDING_DIM must be an integer`
    check is ever reached. What matters to a deploy is that `upgrade head`
    exits non-zero and says which variable was wrong, not which layer said so.
    """
    with _scenario_database(broker, label="invalid_dimension") as database:
        with pytest.raises(TemplateError, match=r"(?is)embedding_dim.*(valid integer|must be an integer)"):
            run_migrations(
                _migration_url(admin_url, database),
                env=_migration_env(EMBEDDING_DIM="not-a-number"),
            )


# -- what sharing one server has to keep true -----------------------------


def test_all_four_scenarios_run_on_one_shared_server(
    admin_url: str, broker: RunBroker, default_width_template: str, configured_width_template: str
) -> None:
    """One server, four databases — proven by the server's own identifier.

    `pg_control_system()` reports a value fixed at `initdb` time, so four
    databases agreeing on it cannot be four clusters.
    """
    identifiers = set()
    with (
        _scenario_database(broker, label="one_server_default", template=default_width_template) as first,
        _scenario_database(broker, label="one_server_configured", template=configured_width_template) as second,
        _scenario_database(broker, label="one_server_hnsw", template=configured_width_template) as third,
        _scenario_database(broker, label="one_server_invalid") as fourth,
    ):
        for database in (first, second, third, fourth):
            rows = _query(admin_url, database, "SELECT system_identifier FROM pg_control_system()")
            identifiers.add(str(rows[0][0]))
    assert len(identifiers) == 1, f"scenarios landed on {len(identifiers)} servers, not one"


def test_the_four_scenarios_hold_four_distinct_redacted_identities(
    broker: RunBroker, default_width_template: str, configured_width_template: str
) -> None:
    """Distinct databases, and evidence that says so without naming them."""
    with (
        _scenario_database(broker, label="identity_default", template=default_width_template) as first,
        _scenario_database(broker, label="identity_configured", template=configured_width_template) as second,
        _scenario_database(broker, label="identity_hnsw", template=configured_width_template) as third,
        _scenario_database(broker, label="identity_invalid") as fourth,
    ):
        names = (first, second, third, fourth)
        assert len(set(names)) == 4, f"scenario databases collided: {names}"
        digests = {redacted_digest(name) for name in names}
        assert len(digests) == 4, "four scenarios did not produce four distinct redacted identities"
        assert not any(
            name in digest for name in names for digest in digests
        ), "a redacted identity leaked the database name it stands for"


def test_no_scenario_observes_another_scenarios_state(
    admin_url: str, broker: RunBroker, configured_width_template: str
) -> None:
    """Two clones of one template share a schema and no rows."""
    with (
        _scenario_database(broker, label="visibility_writer", template=configured_width_template) as writer,
        _scenario_database(broker, label="visibility_reader", template=configured_width_template) as reader,
    ):
        _query(admin_url, writer, "CREATE TABLE scenario_local_marker (id integer PRIMARY KEY)")
        _query(admin_url, writer, "INSERT INTO scenario_local_marker (id) VALUES (1)")

        rows = _query(
            admin_url,
            reader,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'scenario_local_marker'",
        )
        assert int(rows[0][0]) == 0, "one scenario can see another scenario's table"


def test_both_widths_produce_valid_digests_under_two_distinct_fingerprints(
    admin_url: str, broker: RunBroker, default_width_template: str, configured_width_template: str
) -> None:
    """Two templates, two fingerprints, two well-formed catalog digests.

    The fingerprint is what separates the two templates: the width is
    schema-affecting, so it is part of the fingerprint, and the two templates
    therefore have different names and cannot be confused for one another.
    """
    assert (
        default_width_template != configured_width_template
    ), "both widths resolved to one template name; the width is not reaching the fingerprint"

    with (
        _scenario_database(broker, label="digest_default", template=default_width_template) as default_clone,
        _scenario_database(broker, label="digest_configured", template=configured_width_template) as configured_clone,
    ):
        default_digest = _catalog_digest(admin_url, default_clone)
        configured_digest = _catalog_digest(admin_url, configured_clone)

    for digest in (default_digest, configured_digest):
        assert len(digest) == 64, f"digest is not a sha256 hex string: {digest!r}"
        assert set(digest) <= set("0123456789abcdef"), f"digest is not lowercase hex: {digest!r}"


def test_the_catalog_digest_distinguishes_embedding_width(
    admin_url: str, broker: RunBroker, default_width_template: str, configured_width_template: str
) -> None:
    """Two databases differing only in embedding width must digest apart.

    This assertion used to run the other way. The digest reads `data_type`,
    `character_maximum_length` and `numeric_precision` for every column, and a
    pgvector column reports `USER-DEFINED` with both lengths null while
    carrying its dimension in the type modifier — so before the `columns`
    dimension read that modifier, two databases at 384 and 128 produced
    byte-identical digests. A template accidentally built at the wrong width
    passed a digest comparison, which made the digest silent on the one
    property these scenarios exist to vary.

    Kept as an inequality rather than deleted, because deleting it would leave
    no way for a later reader to tell whether the gap was ever closed. The
    width precondition is asserted first: without it, a run where both clones
    happened to share a width would pass this test for the wrong reason.
    """
    with (
        _scenario_database(broker, label="widthdigest_default", template=default_width_template) as default_clone,
        _scenario_database(
            broker, label="widthdigest_configured", template=configured_width_template
        ) as configured_clone,
    ):
        assert _vector_width(admin_url, default_clone) != _vector_width(
            admin_url, configured_clone
        ), "the two clones do not actually differ in width; this test proves nothing"
        assert _catalog_digest(admin_url, default_clone) != _catalog_digest(
            admin_url, configured_clone
        ), "two databases at different embedding widths still digest identically"


def test_the_invalid_dimension_scenario_starts_from_an_empty_database(admin_url: str, broker: RunBroker) -> None:
    """The invalid-value scenario must not take a clone shortcut.

    Guards the setup rather than the outcome: if this database ever arrived
    pre-migrated, `upgrade head` would succeed with nothing to do and the
    failure assertion would pass for the wrong reason.
    """
    with _scenario_database(broker, label="empty_precondition") as database:
        rows = _query(
            admin_url,
            database,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
        )
        assert int(rows[0][0]) == 0, "the invalid-dimension scenario was handed a migrated database"


def test_a_rolled_utc_date_is_rejected_rather_than_recorded(admin_url: str) -> None:
    """A template built across midnight UTC is refused, not published.

    Its partition DDL would be keyed to a different calendar day than the
    fingerprint that names it, and the mismatch would surface only near
    midnight, only on some machines.
    """
    from tests.helpers.pg_template import DateRolloverError

    with pytest.raises(DateRolloverError, match="UTC date rolled"):
        assert_no_rollover("1999-12-31", stage="a deliberately stale start date")
