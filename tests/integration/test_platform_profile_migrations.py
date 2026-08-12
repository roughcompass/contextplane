"""Profile publication tables: what the migration builds, and what it refuses.

Two rules here are the reason these tables exist at all, so both are tested by
trying to break them rather than by asserting they were declared:

- a published revision, extension or compiled definition cannot be updated or
  deleted. Everything downstream names published rows by id, so an edit after
  publication would silently restate what a bound tenant already validated
  against;
- one tenant has at most one active binding at any instant. Two overlapping
  active bindings make "which profile governs this write?" answerable two ways,
  and the answer would depend on which row the query happened to find first.

The parity test follows the precedent set by the receipt and task-memory suites:
a column in the migration but not the ORM is invisible to service code, and one
in the ORM but not the database fails at query time rather than at import.
"""

from __future__ import annotations

import datetime
import os
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from contextplane.profile.models import (
    EntityTypeDefinition,
    ProfileBinding,
    ProfileCompileResult,
    ProfileExtension,
    ProfileRevision,
    RelationshipTypeDefinition,
)

_MODELS = (
    ProfileRevision,
    ProfileExtension,
    ProfileBinding,
    EntityTypeDefinition,
    RelationshipTypeDefinition,
    ProfileCompileResult,
)

#: The five append-only tables. `profile_bindings` is deliberately absent: it is
#: the one table here whose rows are meant to move.
_IMMUTABLE_TABLES = (
    "profile_revisions",
    "profile_extensions",
    "entity_type_definitions",
    "relationship_type_definitions",
    "profile_compile_results",
)

_T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


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
            {"t": tid, "s": f"pp-{tid.hex[:8]}", "n": "profile test"},
        )
    return tid


def _revision(
    conn: object,
    *,
    compatibility: str = "backward_compatible",
    predecessor: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Publish a core revision. Family and version are randomized so the
    uniqueness constraints do not couple unrelated tests to each other."""
    rid = revision_id or uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO profile_revisions (
                profile_revision_id, profile_family, profile_name, semantic_version,
                canonical_document, document_digest, compatibility,
                predecessor_revision_id, published_by, published_at
            )
            VALUES (
                :rid, :family, 'core', '1.0.0',
                '{"types": []}'::jsonb, :digest, :compat,
                :pred, 'actor:publisher', :at
            )
            """
        ),
        {
            "rid": rid,
            "family": f"fam-{rid.hex[:8]}",
            "digest": f"sha256:{rid.hex}",
            "compat": compatibility,
            "pred": predecessor,
            "at": _T0,
        },
    )
    return rid


def _extension(conn: object, tenant: uuid.UUID, core: uuid.UUID, *, namespace: str | None = None) -> uuid.UUID:
    eid = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO profile_extensions (
                extension_revision_id, tenant_id, namespace, target_core_revision_id,
                canonical_document, document_digest, extension_points,
                compatibility_result, published_by, published_at
            )
            VALUES (
                :eid, :tid, :ns, :core,
                '{"types": []}'::jsonb, :digest, '[]'::jsonb,
                'compatible', 'actor:publisher', :at
            )
            """
        ),
        {
            "eid": eid,
            "tid": tenant,
            "ns": namespace or f"ns-{eid.hex[:8]}",
            "core": core,
            "digest": f"sha256:{eid.hex}",
            "at": _T0,
        },
    )
    return eid


def _binding(
    conn: object,
    tenant: uuid.UUID,
    core: uuid.UUID,
    *,
    state: str = "active",
    effective_from: datetime.datetime = _T0,
    effective_to: datetime.datetime | None = None,
) -> uuid.UUID:
    bid = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO profile_bindings (
                binding_id, tenant_id, profile_revision_id, extension_set_digest,
                state, effective_from, effective_to, rollback_ready,
                actor, reason, recorded_at
            )
            VALUES (
                :bid, :tid, :core, 'sha256:empty',
                :state, :efrom, :eto, FALSE,
                'actor:operator', 'rollout', :at
            )
            """
        ),
        {
            "bid": bid,
            "tid": tenant,
            "core": core,
            "state": state,
            "efrom": effective_from,
            "eto": effective_to,
            "at": _T0,
        },
    )
    return bid


def _entity_definition(
    conn: object,
    core: uuid.UUID,
    *,
    extension: uuid.UUID | None = None,
    type_name: str = "Service",
) -> uuid.UUID:
    did = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO entity_type_definitions (
                definition_id, profile_revision_id, extension_revision_id, type_name,
                required_properties, optional_properties, value_schemas,
                authority, default_provenance, readiness_rules, compiled_at
            )
            VALUES (
                :did, :core, :ext, :name,
                '["name"]'::jsonb, '[]'::jsonb, '{}'::jsonb,
                'platform', '{}'::jsonb, '{}'::jsonb, :at
            )
            """
        ),
        {"did": did, "core": core, "ext": extension, "name": type_name, "at": _T0},
    )
    return did


def _relationship_definition(
    conn: object,
    core: uuid.UUID,
    *,
    cross_org_policy: str = "deny",
    min_cardinality: int = 0,
    max_cardinality: int | None = None,
    relationship_type: str = "depends_on",
) -> uuid.UUID:
    did = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO relationship_type_definitions (
                definition_id, profile_revision_id, relationship_type,
                source_type, destination_type, direction, property_schema,
                duplicate_policy, symmetry, inverse_view_policy,
                min_cardinality, max_cardinality, cardinality_scope,
                authority, cross_org_policy, compiled_at
            )
            VALUES (
                :did, :core, :rtype,
                'Service', 'Service', 'directed', '{}'::jsonb,
                'reject', 'asymmetric', 'read_only',
                :minc, :maxc, 'per_source',
                'platform', :policy, :at
            )
            """
        ),
        {
            "did": did,
            "core": core,
            "rtype": relationship_type,
            "minc": min_cardinality,
            "maxc": max_cardinality,
            "policy": cross_org_policy,
            "at": _T0,
        },
    )
    return did


def _compile_result(conn: object, core: uuid.UUID, *, compiler_version: str = "1.0.0") -> uuid.UUID:
    cid = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO profile_compile_results (
                compile_result_id, profile_revision_id, input_digests,
                compiler_version, output_digest, conflicts, warnings, compiled_at
            )
            VALUES (
                :cid, :core, '{"core": "sha256:x"}'::jsonb,
                :ver, :out, '[]'::jsonb, '[]'::jsonb, :at
            )
            """
        ),
        {"cid": cid, "core": core, "ver": compiler_version, "out": f"sha256:{cid.hex}", "at": _T0},
    )
    return cid


# --- fresh install --------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "profile_revisions",
        "profile_extensions",
        "profile_bindings",
        "entity_type_definitions",
        "relationship_type_definitions",
        "profile_compile_results",
    ],
)
def test_the_migration_creates_every_profile_table(sync_engine: Engine, table: str) -> None:
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
        ("profile_bindings", "ix_profile_bindings_tenant_effective"),
        ("profile_revisions", "ix_profile_revisions_predecessor"),
        ("entity_type_definitions", "ix_entity_type_definitions_lookup"),
        ("relationship_type_definitions", "ix_relationship_type_definitions_endpoints"),
    ],
)
def test_the_lookup_paths_have_their_indexes(sync_engine: Engine, table: str, index: str) -> None:
    with sync_engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})
        }
    assert index in names, f"missing {index}; have {sorted(names)}"


def test_the_exclusion_constraint_needs_its_extension(sync_engine: Engine) -> None:
    """The gist operator class for `=` on a uuid is what makes the one-active-
    binding rule expressible; without the extension the constraint could not
    have been created and the rule would be unenforced."""
    with sync_engine.connect() as conn:
        installed = {row[0] for row in conn.execute(text("SELECT extname FROM pg_extension"))}
    assert "btree_gist" in installed


# --- publication is immutable -----------------------------------------------------


def test_a_published_revision_cannot_be_updated(sync_engine: Engine) -> None:
    """The document every binding and compiled definition names by id. Editing it
    would restate what a bound tenant already validated against, with nothing in
    the row recording that it moved."""
    with sync_engine.begin() as conn:
        rid = _revision(conn)
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE profile_revisions SET published_by = 'actor:other' WHERE profile_revision_id = :r"),
            {"r": rid},
        )


def test_a_published_revision_cannot_be_deleted(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        rid = _revision(conn)
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM profile_revisions WHERE profile_revision_id = :r"), {"r": rid})


def test_a_published_extension_cannot_be_updated(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        eid = _extension(conn, tenant_id, _revision(conn))
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE profile_extensions SET compatibility_result = 'incompatible' WHERE extension_revision_id = :e"),
            {"e": eid},
        )


def test_a_compiled_entity_definition_cannot_be_updated(sync_engine: Engine) -> None:
    """A compiled definition is a projection of a frozen document. If it can be
    edited then the projection and the document it came from disagree, and the
    validator reads the projection."""
    with sync_engine.begin() as conn:
        did = _entity_definition(conn, _revision(conn))
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE entity_type_definitions SET authority = 'tenant' WHERE definition_id = :d"),
            {"d": did},
        )


def test_a_compiled_relationship_definition_cannot_be_deleted(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        did = _relationship_definition(conn, _revision(conn))
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM relationship_type_definitions WHERE definition_id = :d"), {"d": did})


def test_a_compile_result_cannot_be_updated(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        cid = _compile_result(conn, _revision(conn))
    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE profile_compile_results SET output_digest = 'sha256:tampered' WHERE compile_result_id = :c"),
            {"c": cid},
        )


def test_every_immutable_table_carries_its_trigger(sync_engine: Engine) -> None:
    """Named directly, because a table added to this group later without a
    trigger would be append-only in intent and editable in fact."""
    with sync_engine.connect() as conn:
        triggered = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname LIKE '%_immutable'"
                )
            )
        }
    assert set(_IMMUTABLE_TABLES) <= triggered, f"unprotected: {sorted(set(_IMMUTABLE_TABLES) - triggered)}"


def test_a_binding_is_meant_to_move(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The counterpart to the rules above: freezing a binding would make its
    state column a lie, because a rollout walks it through states by design."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        bid = _binding(conn, tenant_id, core, state="planned")
        conn.execute(text("UPDATE profile_bindings SET state = 'validating' WHERE binding_id = :b"), {"b": bid})
        state = conn.execute(text("SELECT state FROM profile_bindings WHERE binding_id = :b"), {"b": bid}).scalar_one()
    assert state == "validating"


# --- one active binding per tenant per instant ------------------------------------


def test_two_active_bindings_cannot_overlap_for_one_tenant(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The case that makes validation context ambiguous: a governed write would
    be checked against whichever of the two the query found."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _binding(conn, tenant_id, core, effective_from=_T0, effective_to=None)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _binding(conn, tenant_id, core, effective_from=_T0 + datetime.timedelta(days=1), effective_to=None)


def test_consecutive_active_bindings_are_allowed(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A rollout followed by a replacement: both active, neither overlapping. A
    plain unique index on the tenant would have refused this legitimate pair."""
    closed = _T0 + datetime.timedelta(days=30)
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _binding(conn, tenant_id, core, effective_from=_T0, effective_to=closed)
        second = _binding(conn, tenant_id, core, effective_from=closed, effective_to=None)
    assert second is not None


def test_two_tenants_bind_independently(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    other = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": other, "s": f"pp-{other.hex[:8]}", "n": "other tenant"},
        )
        core = _revision(conn)
        _binding(conn, tenant_id, core)
        _binding(conn, other, core)
        live = conn.execute(
            text("SELECT count(*) FROM profile_bindings WHERE tenant_id IN (:a, :b) AND state = 'active'"),
            {"a": tenant_id, "b": other},
        ).scalar_one()
    assert live == 2


def test_overlapping_planned_bindings_are_allowed(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Drafting several candidate rollouts over the same future window is
    legitimate; only promotion to active has to be exclusive."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _binding(conn, tenant_id, core, state="planned")
        second = _binding(conn, tenant_id, core, state="planned")
    assert second is not None


def test_a_binding_interval_cannot_end_before_it_starts(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        core = _revision(conn)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _binding(conn, tenant_id, core, effective_from=_T0, effective_to=_T0 - datetime.timedelta(days=1))


def test_an_unknown_binding_state_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        core = _revision(conn)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _binding(conn, tenant_id, core, state="live")


# --- what a published document must say -------------------------------------------


def test_an_unknown_compatibility_classification_is_refused(sync_engine: Engine) -> None:
    """The three answers exist because a migration plan acts on them; a fourth
    would be a classification nothing knows how to handle."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _revision(conn, compatibility="mostly_fine")


def test_a_revision_cannot_be_its_own_predecessor(sync_engine: Engine) -> None:
    """A chain with no beginning: a reader walking back from this revision would
    not terminate."""
    rid = uuid.uuid4()
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _revision(conn, revision_id=rid, predecessor=rid)


def test_the_same_document_cannot_be_published_twice(sync_engine: Engine) -> None:
    """Two ids for one document makes "which revision is this?" answerable two
    ways."""
    with sync_engine.begin() as conn:
        first = _revision(conn)
        family, digest = conn.execute(
            text("SELECT profile_family, document_digest FROM profile_revisions WHERE profile_revision_id = :r"),
            {"r": first},
        ).one()
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO profile_revisions (
                    profile_revision_id, profile_family, profile_name, semantic_version,
                    canonical_document, document_digest, compatibility, published_by, published_at
                )
                VALUES (
                    :rid, :family, 'core', '2.0.0',
                    '{"types": []}'::jsonb, :digest, 'backward_compatible', 'actor:publisher', :at
                )
                """
            ),
            {"rid": uuid.uuid4(), "family": family, "digest": digest, "at": _T0},
        )


def test_an_extension_must_name_a_core_revision_that_exists(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """An extension with no resolvable target cannot be checked for collisions
    against anything."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _extension(conn, tenant_id, uuid.uuid4())


# --- compiled definitions ----------------------------------------------------------


def test_a_core_type_is_compiled_once_per_revision(sync_engine: Engine) -> None:
    """The `NULLS NOT DISTINCT` case. Core definitions leave the extension id
    NULL, and under ordinary uniqueness every NULL compares distinct — so a
    repeated compile would insert a second copy of every core type and the
    validator would find two definitions for one name."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _entity_definition(conn, core, type_name="Service")
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _entity_definition(conn, core, type_name="Service")


def test_an_extension_may_define_a_type_the_core_already_names(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Same name, different origin: the uniqueness is per (revision, extension),
    so a tenant's namespaced definition does not collide with core's."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        extension = _extension(conn, tenant_id, core)
        _entity_definition(conn, core, type_name="Service")
        namespaced = _entity_definition(conn, core, extension=extension, type_name="Service")
    assert namespaced is not None


def test_an_unknown_cross_organization_policy_is_refused(sync_engine: Engine) -> None:
    """Omitted policy is deny, so the column is NOT NULL and its vocabulary is
    closed; an unrecognised value would be read as neither."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _relationship_definition(conn, core, cross_org_policy="ask_someone")


def test_a_cross_organization_policy_cannot_be_omitted(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        core = _revision(conn)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO relationship_type_definitions (
                    definition_id, profile_revision_id, relationship_type,
                    source_type, destination_type, direction, property_schema,
                    duplicate_policy, symmetry, inverse_view_policy,
                    min_cardinality, cardinality_scope, authority, compiled_at
                )
                VALUES (
                    :did, :core, 'depends_on', 'Service', 'Service', 'directed', '{}'::jsonb,
                    'reject', 'asymmetric', 'read_only', 0, 'per_source', 'platform', :at
                )
                """
            ),
            {"did": uuid.uuid4(), "core": core, "at": _T0},
        )


def test_a_maximum_below_the_minimum_is_refused(sync_engine: Engine) -> None:
    """A window nothing can satisfy: every instance would be invalid and no
    write could ever succeed against this type."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _relationship_definition(conn, core, min_cardinality=2, max_cardinality=1)


def test_an_unbounded_maximum_is_allowed(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        core = _revision(conn)
        did = _relationship_definition(conn, core, min_cardinality=1, max_cardinality=None)
    assert did is not None


def test_recompiling_the_same_inputs_is_not_a_second_result(sync_engine: Engine) -> None:
    """A repeated publication attempt must be a no-op rather than an
    accumulating log of identical results."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _compile_result(conn, core, compiler_version="1.0.0")
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _compile_result(conn, core, compiler_version="1.0.0")


def test_a_new_compiler_version_records_its_own_result(sync_engine: Engine) -> None:
    """The same inputs through a different compiler may legitimately differ, so
    the version is part of the identity rather than a field on one row."""
    with sync_engine.begin() as conn:
        core = _revision(conn)
        _compile_result(conn, core, compiler_version="1.0.0")
        second = _compile_result(conn, core, compiler_version="1.1.0")
    assert second is not None


# --- upgrade, rollback, upgrade again ---------------------------------------------


def test_the_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """On a throwaway database, for the same reason the sibling suites use one:
    downgrading the shared one would drop tables out from under every other
    integration module in the session."""
    scratch = f"pp_downgrade_{uuid.uuid4().hex[:8]}"
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
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("profile_revisions")

        down = run("downgrade", "0048_intent_memory_nomenclature")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        for table in _IMMUTABLE_TABLES + ("profile_bindings",):
            assert not after.has_table(table), f"{table} survived the downgrade"
        # The predecessor is intact. Named against a table the *immediately*
        # preceding revision introduces, so this fails if the rollback overshoots
        # by even one step: `intent_checkpoints` is that revision's own rename and
        # its downgrade puts the name back. A table from further down the chain
        # would survive an overshoot too, and would prove nothing about where the
        # downgrade actually stopped.
        assert after.has_table("intent_checkpoints"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("profile_compile_results")
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


def test_the_chain_has_exactly_one_head(pg_container: str) -> None:
    """A second head is what a revision added in parallel produces, and
    `alembic upgrade head` fails outright on it rather than picking one."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=os.getcwd(),
        env={**os.environ, "DATABASE_URL": pg_container},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected one head, got: {heads}"
