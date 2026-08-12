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

from contextplane.entities.models import (
    AssertionProvenance,
    EntityAttributeAssertion,
    EntityHandle,
)
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
            text(
                "UPDATE profile_extensions SET compatibility_result = 'incompatible' "
                "WHERE extension_revision_id = :e"
            ),
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


# --- type-qualified handles and assertion provenance ------------------------------
#
# Three rules carry this slice, and each is tested by trying to break it:
#
# - an attribute assertion cannot exist without a provenance record, because one
#   that does is an assertion nobody can re-check or revoke, and afterwards there
#   is no way to tell it apart from the rest;
# - provenance never changes, because re-stating a source's trust class in place
#   silently re-characterizes every assertion already resting on it;
# - a handle's identity is frozen while its interval stays open to being closed.
#   Those are different rules and a single "immutable" trigger would collapse
#   them, either blocking supersession or permitting a rewrite.


def _entity(conn: object, tenant: uuid.UUID, *, entity_type: str = "service", name: str | None = None) -> uuid.UUID:
    """A real row in the existing entities table.

    Handles reference this id rather than replacing it, so the tests need a
    genuine one — a fabricated uuid would pass every assertion here while
    proving the foreign key was never enforced.
    """
    entity_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active, created_at, visibility) "
            "VALUES (:e, :t, :ty, :n, TRUE, :ts, 'private')"
        ),
        {"e": entity_id, "t": tenant, "ty": entity_type, "n": name or f"entity-{entity_id.hex[:8]}", "ts": _T0},
    )
    return entity_id


def _provenance(
    conn: object,
    tenant: uuid.UUID,
    *,
    authority: str = "canonical_owner",
    confidence: float | None = None,
    freshness_state: str = "fresh",
    revocation_ref: str | None = None,
    revoked_at: datetime.datetime | None = None,
) -> uuid.UUID:
    provenance_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO assertion_provenance ("
            "  provenance_id, tenant_id, source_system, source_namespace, ingested_at,"
            "  authority, freshness_state, confidence, revocation_ref, revoked_at,"
            "  produced_by, created_at"
            ") VALUES (:p, :t, 'crm', 'people', :ts, :a, :f, :c, :rr, :rt, 'ingest', :ts)"
        ),
        {
            "p": provenance_id,
            "t": tenant,
            "ts": _T0,
            "a": authority,
            "f": freshness_state,
            "c": confidence,
            "rr": revocation_ref,
            "rt": revoked_at,
        },
    )
    return provenance_id


def _handle(
    conn: object,
    tenant: uuid.UUID,
    entity: uuid.UUID,
    *,
    entity_type: str = "service",
    namespace: str = "core",
    name: str = "billing",
    kind: str = "primary",
    valid_to: datetime.datetime | None = None,
    lookup_key: str | None = None,
) -> uuid.UUID:
    handle_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO entity_handles ("
            "  handle_id, tenant_id, entity_id, entity_type, namespace, handle_name,"
            "  qualified_handle, lookup_key, kind, valid_from, valid_to, source, recorded_at"
            ") VALUES (:h, :t, :e, :ty, :ns, :n, :q, :lk, :k, :vf, :vt, 'operator', :ts)"
        ),
        {
            "h": handle_id,
            "t": tenant,
            "e": entity,
            "ty": entity_type,
            "ns": namespace,
            "n": name,
            "q": f"{namespace}:{entity_type}/{name}",
            "lk": lookup_key or f"{namespace}:{entity_type}/{name}".lower(),
            "k": kind,
            "vf": _T0,
            "vt": valid_to,
            "ts": _T0,
        },
    )
    return handle_id


def _assertion(
    conn: object,
    tenant: uuid.UUID,
    entity: uuid.UUID,
    provenance: uuid.UUID,
    *,
    property_name: str = "tier",
    valid_to: datetime.datetime | None = None,
) -> uuid.UUID:
    assertion_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO entity_attribute_assertions ("
            "  assertion_id, tenant_id, entity_id, property_name, value, valid_from, valid_to,"
            "  provenance_id, validation_result, recorded_at"
            ") VALUES (:a, :t, :e, :p, '{\"v\": 1}'::jsonb, :vf, :vt, :pr, 'valid', :ts)"
        ),
        {
            "a": assertion_id,
            "t": tenant,
            "e": entity,
            "p": property_name,
            "vf": _T0,
            "vt": valid_to,
            "pr": provenance,
            "ts": _T0,
        },
    )
    return assertion_id


@pytest.mark.parametrize(
    "table",
    ["entity_handles", "assertion_provenance", "entity_attribute_assertions"],
)
def test_the_handle_and_provenance_migration_creates_its_tables(sync_engine: Engine, table: str) -> None:
    assert inspect(sync_engine).has_table(table)


def test_the_handle_and_provenance_orm_agrees_with_the_database_column_for_column(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    for model in (EntityHandle, AssertionProvenance, EntityAttributeAssertion):
        live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        declared = {column.name for column in model.__table__.columns}
        assert declared == live, (
            f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, "
            f"database-only {sorted(live - declared)}"
        )


@pytest.mark.parametrize(
    ("table", "index"),
    [
        ("entity_handles", "uq_entity_handles_active_qualified"),
        ("entity_handles", "uq_entity_handles_active_primary_name"),
        ("entity_handles", "ix_entity_handles_unqualified"),
        ("entity_handles", "ix_entity_handles_entity"),
        ("entity_attribute_assertions", "uq_entity_attribute_assertions_active"),
        ("entity_attribute_assertions", "ix_entity_attribute_assertions_provenance"),
        ("assertion_provenance", "ix_assertion_provenance_external"),
    ],
)
def test_the_handle_and_provenance_lookup_paths_have_their_indexes(sync_engine: Engine, table: str, index: str) -> None:
    """Including the ones only the later backfill and dual-read path will use.

    They land here because that work ships no DDL of its own; a schema change
    arriving together with the machinery that depends on it is the ordering an
    expand exists to avoid.
    """
    with sync_engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})
        }
    assert index in names, f"missing {index}; have {sorted(names)}"


# --- provenance is required, and it is a foreign key ------------------------------


def test_an_attribute_assertion_without_provenance_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The rule this table exists to make unbreakable."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        conn.execute(
            text(
                "INSERT INTO entity_attribute_assertions ("
                "  assertion_id, tenant_id, entity_id, property_name, value, valid_from,"
                "  provenance_id, validation_result, recorded_at"
                ") VALUES (:a, :t, :e, 'tier', '{}'::jsonb, :ts, NULL, 'valid', :ts)"
            ),
            {"a": uuid.uuid4(), "t": tenant_id, "e": entity, "ts": _T0},
        )


def test_an_assertion_naming_provenance_that_does_not_exist_is_refused(
    sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """NOT NULL alone would admit an id pointing at nothing, which is the same
    unverifiable assertion wearing a plausible column value."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        _assertion(conn, tenant_id, entity, uuid.uuid4())


def test_an_assertion_with_real_provenance_is_accepted(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The negative control: without it, a table that refused every insert
    would satisfy both cases above."""
    with sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        assertion = _assertion(conn, tenant_id, entity, _provenance(conn, tenant_id))

    assert assertion is not None


# --- provenance never changes ------------------------------------------------------


def test_a_provenance_record_cannot_be_updated(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Re-stating a trust class in place would silently re-characterize every
    assertion already resting on it, including ones already acted on."""
    with sync_engine.begin() as conn:
        provenance = _provenance(conn, tenant_id)

    with pytest.raises(DBAPIError, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE assertion_provenance SET authority = 'canonical_owner' WHERE provenance_id = :p"),
            {"p": provenance},
        )


def test_a_provenance_record_cannot_be_deleted(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        provenance = _provenance(conn, tenant_id)

    with pytest.raises(DBAPIError, match="immutable"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM assertion_provenance WHERE provenance_id = :p"), {"p": provenance})


def test_confidence_is_refused_on_provenance_that_was_not_derived(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A confidence on something a canonical owner stated invites a reader to
    discount a fact that was never inferred."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _provenance(conn, tenant_id, authority="canonical_owner", confidence=0.8)


def test_confidence_is_allowed_on_derived_provenance(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        assert _provenance(conn, tenant_id, authority="derived", confidence=0.8) is not None


def test_a_revoked_provenance_state_must_carry_its_revocation(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _provenance(conn, tenant_id, freshness_state="revoked")


def test_a_provenance_revocation_needs_both_a_reference_and_a_time(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A revocation with no reference cannot be audited; a reference with no
    time cannot be ordered against the assertions it invalidates."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _provenance(conn, tenant_id, revocation_ref="ticket-1")


def test_an_unknown_provenance_authority_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _provenance(conn, tenant_id, authority="whoever-asked")


# --- handles are temporally append-only -------------------------------------------


def test_a_handle_cannot_be_deleted(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        handle = _handle(conn, tenant_id, _entity(conn, tenant_id))

    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM entity_handles WHERE handle_id = :h"), {"h": handle})


def test_a_handle_name_cannot_be_rewritten_in_place(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The rename this table exists to record is a new row, not an edit to the
    old one — otherwise the previous name becomes unresolvable."""
    with sync_engine.begin() as conn:
        handle = _handle(conn, tenant_id, _entity(conn, tenant_id))

    with pytest.raises(DBAPIError, match="immutable"), sync_engine.begin() as conn:
        conn.execute(text("UPDATE entity_handles SET handle_name = 'renamed' WHERE handle_id = :h"), {"h": handle})


def test_a_handle_interval_can_still_be_closed(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The half of the rule that a blanket immutability trigger would break.

    Retiring a handle *is* an update, so a trigger that could not tell a
    supersession from a rewrite would make the temporal columns unusable.
    """
    with sync_engine.begin() as conn:
        handle = _handle(conn, tenant_id, _entity(conn, tenant_id))

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE entity_handles SET valid_to = :t WHERE handle_id = :h"),
            {"t": _T0 + datetime.timedelta(days=1), "h": handle},
        )
        closed = conn.execute(
            text("SELECT valid_to FROM entity_handles WHERE handle_id = :h"), {"h": handle}
        ).scalar_one()

    assert closed is not None


def test_an_attribute_assertion_cannot_have_its_value_rewritten(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """An attribute's history is the sequence of its revisions; editing one in
    place would restate what a reader already acted on."""
    with sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        assertion = _assertion(conn, tenant_id, entity, _provenance(conn, tenant_id))

    with pytest.raises(DBAPIError, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE entity_attribute_assertions SET value = '{\"v\": 2}'::jsonb WHERE assertion_id = :a"),
            {"a": assertion},
        )


# --- what a qualified handle must be ----------------------------------------------


def test_a_stored_qualified_handle_must_equal_its_own_parts(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Without this the column is free text that merely looks structured, and a
    lookup by qualified handle would be trusting whoever wrote the row."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        conn.execute(
            text(
                "INSERT INTO entity_handles ("
                "  handle_id, tenant_id, entity_id, entity_type, namespace, handle_name,"
                "  qualified_handle, lookup_key, kind, valid_from, source, recorded_at"
                ") VALUES (:h, :t, :e, 'service', 'core', 'billing',"
                "  'core:service/something-else', 'core:service/billing', 'primary', :ts, 'operator', :ts)"
            ),
            {"h": uuid.uuid4(), "t": tenant_id, "e": entity, "ts": _T0},
        )


def test_an_unknown_handle_kind_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _handle(conn, tenant_id, _entity(conn, tenant_id), kind="nickname")


def test_a_handle_interval_cannot_end_before_it_starts(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _handle(conn, tenant_id, _entity(conn, tenant_id), valid_to=_T0 - datetime.timedelta(days=1))


# --- active uniqueness is partial, and type-aware where it should be --------------


def test_one_live_qualified_handle_per_tenant(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _handle(conn, tenant_id, _entity(conn, tenant_id))
        _handle(conn, tenant_id, _entity(conn, tenant_id, name="second"))


def test_a_retired_handle_does_not_block_the_one_that_replaced_it(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The normal state of every rename. A total unique index would forbid it."""
    with sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        _handle(conn, tenant_id, entity, valid_to=_T0 + datetime.timedelta(days=1))
        replacement = _handle(conn, tenant_id, entity)

    assert replacement is not None


def test_two_tenants_may_hold_the_same_qualified_handle(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    other = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": other, "s": f"pp-{other.hex[:8]}", "n": "other"},
        )
        _handle(conn, tenant_id, _entity(conn, tenant_id))
        _handle(conn, other, _entity(conn, other))

    with sync_engine.connect() as conn:
        live = conn.execute(
            text(
                "SELECT count(*) FROM entity_handles "
                "WHERE lookup_key = 'core:service/billing' AND valid_to IS NULL "
                "AND tenant_id IN (:a, :b)"
            ),
            {"a": tenant_id, "b": other},
        ).scalar_one()

    # Both live at once: the uniqueness is per tenant, so one tenant's naming
    # choices cannot constrain another's.
    assert live == 2


def test_primary_handle_names_are_unique_per_type_not_globally(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Two types may legitimately carry the same short name; collapsing them is
    the ambiguity the qualified form exists to remove."""
    with sync_engine.begin() as conn:
        _handle(conn, tenant_id, _entity(conn, tenant_id), entity_type="service", name="billing")
        second = _handle(
            conn, tenant_id, _entity(conn, tenant_id, entity_type="dataset"), entity_type="dataset", name="billing"
        )

    assert second is not None


def test_two_primary_handles_of_one_type_cannot_share_a_name(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _handle(conn, tenant_id, _entity(conn, tenant_id), namespace="core", name="billing")
        # A different namespace, so the qualified-handle index does not fire and
        # this case tests the type-aware primary rule rather than that one.
        _handle(conn, tenant_id, _entity(conn, tenant_id), namespace="ext", name="Billing")


def test_an_alias_may_share_a_name_with_a_primary_handle(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Only primaries are unique by name. An alias pointing at the same short
    name is the ordinary case the kind column exists to separate."""
    with sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        _handle(conn, tenant_id, entity, namespace="core", name="billing", kind="primary")
        alias = _handle(conn, tenant_id, entity, namespace="ext", name="billing", kind="alias")

    assert alias is not None


def test_one_live_assertion_per_property_per_entity(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        provenance = _provenance(conn, tenant_id)
        _assertion(conn, tenant_id, entity, provenance)
        _assertion(conn, tenant_id, entity, provenance)


def test_a_superseded_assertion_does_not_block_its_replacement(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        entity = _entity(conn, tenant_id)
        provenance = _provenance(conn, tenant_id)
        _assertion(conn, tenant_id, entity, provenance, valid_to=_T0 + datetime.timedelta(days=1))
        replacement = _assertion(conn, tenant_id, entity, provenance)

    assert replacement is not None


# --- the expand does not disturb what was already there ---------------------------


def test_the_legacy_entity_name_uniqueness_survives_the_handle_expand(sync_engine: Engine) -> None:
    """No legacy removal in this revision.

    The old rule keeps protecting the old read path until the new one is
    proven; dropping it is a later decision taken against evidence rather than
    a side effect of adding a table.
    """
    with sync_engine.connect() as conn:
        names = {row[0] for row in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'entities'"))}

    assert "uq_entities_tenant_name" in names


def test_a_handle_must_name_an_entity_that_exists(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Handles reference the existing opaque identity rather than replacing it."""
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        _handle(conn, tenant_id, uuid.uuid4())
