"""The template fingerprint must move when anything shaping the schema moves.

These are mutation tests in the literal sense: each one changes one input,
recomputes, and asserts the fingerprint changed. A fingerprint that failed
to move would let a run clone a template built from different migration
bytes than the code under test, and the resulting failure would surface as
an unrelated assertion somewhere else entirely.

The three properties the schema-reuse safety argument rests on:

- byte changes in a *recursively imported* helper change the fingerprint —
  the case Alembic's head revision cannot see;
- a UTC-date rollover prevents both publication and reuse;
- a canonical schema digest mismatch rejects the template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.pg_template import (
    DateRolloverError,
    SchemaDigestMismatch,
    SchemaEnvironment,
    ServerVersions,
    advisory_lock_key,
    assert_no_rollover,
    canonical_schema_digest,
    catalog_queries,
    compute_fingerprint,
    fingerprint_inputs,
    migration_environment,
    migration_transitive_sources,
    revision_chain,
    template_name,
    utc_date,
)

VERSIONS = ServerVersions(postgres="16.4", pgvector="0.7.4")


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def fake_tree(tmp_path: Path) -> Path:
    """A minimal repo shaped like the real one's migration layout.

    Deliberately built rather than pointed at the real tree: a mutation test
    has to edit bytes, and editing the shipped migrations to prove a point
    would be a test that damages the thing it measures.
    """
    _write(tmp_path / "alembic.ini", "[alembic]\nscript_location = contextplane/storage/migrations\n")
    migrations = tmp_path / "contextplane" / "storage" / "migrations"
    _write(migrations / "env.py", "from contextplane.storage.schema_vocabulary import COLUMNS\n")
    _write(
        tmp_path / "contextplane" / "storage" / "schema_vocabulary.py",
        "COLUMNS = ('id', 'created_at')\n",
    )
    _write(
        migrations / "versions" / "0001_baseline.py",
        "revision = '0001'\ndown_revision = None\n" "from contextplane.storage.schema_helpers import build_table\n",
    )
    _write(
        tmp_path / "contextplane" / "storage" / "schema_helpers.py",
        "def build_table(name):\n    return name\n",
    )
    _write(
        migrations / "versions" / "0002_followup.py",
        "revision = '0002'\ndown_revision = '0001'\n",
    )
    return tmp_path


def _fingerprint(root: Path, *, environment: SchemaEnvironment | None = None) -> str:
    return compute_fingerprint(
        root=root,
        heads=["0002"],
        revision_chain=["0001", "0002"],
        environment=environment or SchemaEnvironment.from_environ({}, date="2026-08-11"),
        versions=VERSIONS,
    )


# -- recursive import discovery -------------------------------------------


def test_transitive_sources_follow_imports_two_levels_deep(fake_tree: Path) -> None:
    found = {path.relative_to(fake_tree).as_posix() for path in migration_transitive_sources(fake_tree)}
    assert "contextplane/storage/migrations/env.py" in found
    assert "contextplane/storage/migrations/versions/0001_baseline.py" in found
    # Reached only through the revision's own import, which is the case a
    # head-revision comparison is blind to.
    assert "contextplane/storage/schema_helpers.py" in found
    assert "contextplane/storage/schema_vocabulary.py" in found


def test_third_party_imports_are_not_collected(fake_tree: Path) -> None:
    _write(
        fake_tree / "contextplane" / "storage" / "migrations" / "versions" / "0003_third_party.py",
        "revision = '0003'\ndown_revision = '0002'\nimport sqlalchemy\nimport os\n",
    )
    found = {path.name for path in migration_transitive_sources(fake_tree)}
    # Their versions are pinned by the lockfile, not by bytes in this tree.
    assert "sqlalchemy" not in found
    assert not any(name.startswith("os.py") for name in found)


# -- the mutation proofs the contract names -------------------------------


def test_editing_an_imported_schema_vocabulary_changes_the_fingerprint(fake_tree: Path) -> None:
    before = _fingerprint(fake_tree)
    vocabulary = fake_tree / "contextplane" / "storage" / "schema_vocabulary.py"
    vocabulary.write_text("COLUMNS = ('id', 'created_at', 'tenant_id')\n", encoding="utf-8")
    assert _fingerprint(fake_tree) != before


def test_editing_an_imported_helper_changes_the_fingerprint(fake_tree: Path) -> None:
    before = _fingerprint(fake_tree)
    helper = fake_tree / "contextplane" / "storage" / "schema_helpers.py"
    helper.write_text("def build_table(name):\n    return name.upper()\n", encoding="utf-8")
    assert _fingerprint(fake_tree) != before


def test_editing_a_revision_in_place_changes_the_fingerprint(fake_tree: Path) -> None:
    """The case that makes head-only comparison unsafe.

    The revision identifier is untouched, so Alembic still reports the same
    head; only the bytes moved.
    """
    before = _fingerprint(fake_tree)
    revision = fake_tree / "contextplane" / "storage" / "migrations" / "versions" / "0002_followup.py"
    revision.write_text(
        "revision = '0002'\ndown_revision = '0001'\n# adds a column\n",
        encoding="utf-8",
    )
    assert _fingerprint(fake_tree) != before


def test_editing_alembic_ini_changes_the_fingerprint(fake_tree: Path) -> None:
    before = _fingerprint(fake_tree)
    (fake_tree / "alembic.ini").write_text(
        "[alembic]\nscript_location = contextplane/storage/migrations\ncompare_type = true\n",
        encoding="utf-8",
    )
    assert _fingerprint(fake_tree) != before


def test_default_and_configured_embedding_environments_differ(fake_tree: Path) -> None:
    default = _fingerprint(fake_tree, environment=SchemaEnvironment.from_environ({}, date="2026-08-11"))
    configured = _fingerprint(
        fake_tree,
        environment=SchemaEnvironment.from_environ({"EMBEDDING_DIM": "1536"}, date="2026-08-11"),
    )
    assert default != configured


def test_partition_count_changes_the_fingerprint(fake_tree: Path) -> None:
    eight = _fingerprint(
        fake_tree, environment=SchemaEnvironment.from_environ({"EMBEDDINGS_PARTITION_COUNT": "8"}, date="2026-08-11")
    )
    sixteen = _fingerprint(
        fake_tree, environment=SchemaEnvironment.from_environ({"EMBEDDINGS_PARTITION_COUNT": "16"}, date="2026-08-11")
    )
    assert eight != sixteen


def test_utc_date_changes_the_fingerprint(fake_tree: Path) -> None:
    """Date-dependent partition DDL makes the calendar day part of the schema."""
    before = _fingerprint(fake_tree, environment=SchemaEnvironment.from_environ({}, date="2026-08-11"))
    after = _fingerprint(fake_tree, environment=SchemaEnvironment.from_environ({}, date="2026-08-12"))
    assert before != after


def test_server_versions_change_the_fingerprint(fake_tree: Path) -> None:
    other = compute_fingerprint(
        root=fake_tree,
        heads=["0002"],
        revision_chain=["0001", "0002"],
        environment=SchemaEnvironment.from_environ({}, date="2026-08-11"),
        versions=ServerVersions(postgres="16.4", pgvector="0.8.0"),
    )
    assert other != _fingerprint(fake_tree)


def test_revision_chain_order_changes_the_fingerprint(fake_tree: Path) -> None:
    reordered = compute_fingerprint(
        root=fake_tree,
        heads=["0002"],
        revision_chain=["0002", "0001"],
        environment=SchemaEnvironment.from_environ({}, date="2026-08-11"),
        versions=VERSIONS,
    )
    assert reordered != _fingerprint(fake_tree)


def test_fingerprint_is_stable_across_recomputation(fake_tree: Path) -> None:
    """Without this, every reuse check would fail and every run would migrate."""
    assert _fingerprint(fake_tree) == _fingerprint(fake_tree)


def test_fingerprint_inputs_name_the_moved_input(fake_tree: Path) -> None:
    """A bare digest cannot say which input moved; the payload can."""
    before = fingerprint_inputs(
        root=fake_tree,
        heads=["0002"],
        revision_chain=["0001", "0002"],
        environment=SchemaEnvironment.from_environ({}, date="2026-08-11"),
        versions=VERSIONS,
    )
    (fake_tree / "contextplane" / "storage" / "schema_helpers.py").write_text("def build_table(n):\n    return n\n")
    after = fingerprint_inputs(
        root=fake_tree,
        heads=["0002"],
        revision_chain=["0001", "0002"],
        environment=SchemaEnvironment.from_environ({}, date="2026-08-11"),
        versions=VERSIONS,
    )
    moved = [
        entry["path"]
        for entry in after["sources"]  # type: ignore[index]
        if entry not in before["sources"]  # type: ignore[operator]
    ]
    assert moved == ["contextplane/storage/schema_helpers.py"]


# -- UTC-date rollover ----------------------------------------------------


def test_rollover_during_publication_is_refused() -> None:
    """A date that cannot be today, so the assertion does not depend on when it runs.

    Pinning a real calendar date here would pass on every day except that
    one, and the one day it failed would look like a defect in the rollover
    guard rather than in the test.
    """
    with pytest.raises(DateRolloverError, match="during template publication"):
        assert_no_rollover("1999-12-31", stage="template publication")


def test_rollover_during_reuse_is_refused() -> None:
    with pytest.raises(DateRolloverError, match="no longer matches its fingerprint"):
        assert_no_rollover("1999-12-31", stage="template reuse")


def test_no_rollover_passes_silently() -> None:
    assert_no_rollover(utc_date(), stage="template publication")


# -- canonical schema digest ---------------------------------------------


def _catalog(**overrides: object) -> dict[str, list[list[object]]]:
    base: dict[str, list[list[object]]] = {
        "schemas": [["public"]],
        "relations": [["public", "entities", "r"]],
        "columns": [["public", "entities", "id", "uuid", "NO", "", "", ""]],
        "constraints": [["public", "entities_pkey", "p", "PRIMARY KEY (id)"]],
        "indexes": [["public", "entities_pkey", "CREATE UNIQUE INDEX ..."]],
        "types": [["public", "entity_kind", "e"]],
        "functions": [["public", "touch_updated_at", ""]],
        "triggers": [["public", "entities", "entities_touch", "EXECUTE FUNCTION touch_updated_at()"]],
        "extensions": [["vector", "0.7.4"]],
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def test_schema_digest_is_stable_for_the_same_catalog() -> None:
    assert canonical_schema_digest(_catalog()) == canonical_schema_digest(_catalog())


def test_added_column_changes_the_schema_digest() -> None:
    with_extra = _catalog(
        columns=[
            ["public", "entities", "id", "uuid", "NO", "", "", ""],
            ["public", "entities", "tenant_id", "uuid", "NO", "", "", ""],
        ]
    )
    assert canonical_schema_digest(with_extra) != canonical_schema_digest(_catalog())


def test_changed_index_definition_changes_the_schema_digest() -> None:
    changed = _catalog(indexes=[["public", "entities_pkey", "CREATE UNIQUE INDEX ... DESC"]])
    assert canonical_schema_digest(changed) != canonical_schema_digest(_catalog())


def test_missing_extension_changes_the_schema_digest() -> None:
    assert canonical_schema_digest(_catalog(extensions=[])) != canonical_schema_digest(_catalog())


def test_none_and_empty_string_normalize_together() -> None:
    """Two drivers report a null default differently; the digest must not."""
    with_none = _catalog(columns=[["public", "entities", "id", "uuid", "NO", None, None, None]])
    with_empty = _catalog(columns=[["public", "entities", "id", "uuid", "NO", "", "", ""]])
    assert canonical_schema_digest(with_none) == canonical_schema_digest(with_empty)


def test_digest_covers_every_declared_catalog_dimension() -> None:
    """A dimension added to the query set must reach the digest.

    Dropping each dimension in turn and asserting the digest moves is what
    stops a query being added to `catalog_queries()` while the digest keeps
    ignoring it.
    """
    full = canonical_schema_digest(_catalog())
    for dimension, _sql in catalog_queries():
        reduced = _catalog()
        reduced[dimension] = []
        assert canonical_schema_digest(reduced) != full, f"{dimension} does not reach the digest"


def test_schema_digest_mismatch_is_its_own_error_type() -> None:
    """Callers distinguish "wrong schema" from "date moved"."""
    assert issubclass(SchemaDigestMismatch, RuntimeError)
    assert not issubclass(SchemaDigestMismatch, DateRolloverError)


# -- migration subprocess environment -------------------------------------


def test_migration_environment_forces_utc_over_an_inherited_timezone() -> None:
    env = migration_environment("postgresql://x/y", env={"TZ": "America/Los_Angeles", "PATH": "/usr/bin"})
    assert env["TZ"] == "UTC"


def test_migration_environment_sets_utc_when_absent() -> None:
    assert migration_environment("postgresql://x/y", env={"PATH": "/usr/bin"})["TZ"] == "UTC"


def test_migration_environment_carries_the_database_url() -> None:
    env = migration_environment("postgresql+asyncpg://u:p@h/db", env={})
    assert env["DATABASE_URL"] == "postgresql+asyncpg://u:p@h/db"


# -- naming and locking ---------------------------------------------------


def test_template_name_fits_the_identifier_limit() -> None:
    name = template_name("a" * 64)
    assert len(name) <= 63
    assert name.startswith("cp_tmpl_")


def test_distinct_fingerprints_get_distinct_template_names() -> None:
    assert template_name("a" * 64) != template_name("b" * 64)


def test_advisory_lock_key_is_stable_and_fits_a_signed_bigint() -> None:
    key = advisory_lock_key("deadbeef")
    assert key == advisory_lock_key("deadbeef")
    assert 0 <= key <= 0x7FFF_FFFF_FFFF_FFFF


def test_different_templates_do_not_serialize_on_one_lock() -> None:
    assert advisory_lock_key("aaaa") != advisory_lock_key("bbbb")


# -- revision chain reading ----------------------------------------------


def test_revision_chain_orders_oldest_first(fake_tree: Path) -> None:
    assert revision_chain(root=fake_tree) == ["0001", "0002"]


def test_revision_chain_reads_the_real_migrations() -> None:
    """The shipped tree must be readable by the same code path."""
    chain = revision_chain()
    assert chain, "no revisions found in the shipped migration tree"
    assert len(set(chain)) == len(chain), "duplicate revision identifiers"
