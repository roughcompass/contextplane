"""Profile revisions, tenant bindings, extensions, and the compiled definitions they produce.

Expand-only: six new tables and nothing altered. Nothing reads them yet — the
compiler and the publication path land above this revision — so this is the
storage half of governed profiles arriving before the behaviour that needs it.

**Publication is immutable, enforced by trigger rather than by convention.** A
published revision is the document every binding, every compiled definition and
every governed assertion later names. If it can be edited after publication then
a tenant that validated against it is validating against something that no longer
exists, and nothing in the row records that it moved. Five of the six tables are
therefore append-only in the database: the two published documents and the three
compiled projections derived from them. The application is not the only writer
these tables will ever see -- migration tooling and operator sessions reach them
too -- and a rule that lives only in Python is one a psql session does not have.

`profile_bindings` is deliberately *not* in that set. A binding is the one row
here that is meant to move: `planned` becomes `validating` becomes `active`, and
a rollback walks it back again. Freezing it would make the state column a lie.

**One active binding per tenant per instant, as an exclusion constraint.** The
rule is temporal, not row-local: two bindings may both be `active` for one tenant
as long as their effective intervals do not overlap, which is exactly what a
rollout followed by a rollback looks like. A unique index cannot express that --
it would either forbid the legitimate sequence or admit two bindings live at the
same moment, and the second is the case where validation context becomes
ambiguous and a write is checked against whichever profile the query happened to
find. `EXCLUDE USING gist` states the actual rule, which is why this revision
takes a dependency on `btree_gist` to get `=` on a uuid inside a gist operator
class.

**Why the file is numbered 0050 when it follows 0048.** The numbering is a label,
not the chain; `down_revision` below is what orders these. The gap is deliberate
headroom so that concurrent work adding revisions in this range does not collide
with this chain. The file numbers already do not sort into chain order upstream
of here -- 0044 is followed by 0047, then 0045, then 0046 -- so reading the
highest filename as the head is unreliable, and this revision's parent was
resolved by walking `down_revision` from the root rather than by sorting the
directory.

That is not a hypothetical caution. This revision was first written against
`0046_legal_holds`, correctly at the time, and `0048` was authored against the
same parent in another branch. Each was a valid single-parent revision on its
own; together they were two heads, which `alembic upgrade head` refuses outright.
Nothing either branch could run would have shown it, because the conflict exists
only in the relationship between them. The unit-tier head check added alongside
this revision is what makes the next one visible on the branch that introduces
it.
"""

from __future__ import annotations

from alembic import op

revision = "0050_profile_revisions"
# The chain head, not the highest filename. Resolved by walking `down_revision`
# from the root, which is the only thing that orders these -- `0047` sits *below*
# `0045`/`0046` despite its number, so the highest number is not the head and
# reading the directory would name the wrong parent.
down_revision: str | None = "0048_intent_memory_nomenclature"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION profile_publication_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'published profile data is append-only: % on % is refused',
        TG_OP, TG_TABLE_NAME
        USING HINT = 'publish a new revision and bind to it; a published '
                     'document is what existing bindings and compiled '
                     'definitions already name';
END;
$$ LANGUAGE plpgsql
"""

_PROFILE_REVISIONS = """
CREATE TABLE profile_revisions (
    profile_revision_id     UUID PRIMARY KEY,

    -- Family and name together identify the profile; the semantic version
    -- identifies this publication of it. All three are needed because a
    -- deployment may carry more than one family and a family more than one
    -- profile.
    profile_family          TEXT NOT NULL,
    profile_name            TEXT NOT NULL,
    semantic_version        TEXT NOT NULL,

    -- The document as published, and the digest of exactly those bytes. The
    -- digest is stored rather than recomputed on read because the canonical
    -- form is the compiler's output, and a reader that re-derives it is
    -- trusting today's canonicalizer to agree with the one that published.
    canonical_document      JSONB NOT NULL,
    document_digest         TEXT NOT NULL,

    -- What this publication does to consumers of its predecessor. Constrained
    -- to the three answers a migration plan can act on.
    compatibility           TEXT NOT NULL,

    -- NULL exactly for the first revision of a profile, not "unknown".
    predecessor_revision_id UUID REFERENCES profile_revisions(profile_revision_id),
    migration_plan_ref      TEXT,

    published_by            TEXT NOT NULL,
    published_at            TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_profile_revisions_version
        UNIQUE (profile_family, profile_name, semantic_version),
    -- The same bytes cannot be published twice under one profile: a second row
    -- would give two ids to one document and make "which revision is this?"
    -- answerable two ways.
    CONSTRAINT uq_profile_revisions_digest
        UNIQUE (profile_family, profile_name, document_digest),
    CONSTRAINT ck_profile_revisions_compatibility CHECK (
        compatibility IN ('backward_compatible', 'breaking', 'deprecating')
    ),
    -- A revision that is its own predecessor is a chain with no beginning; the
    -- readers that walk backwards from a revision would not terminate.
    CONSTRAINT ck_profile_revisions_predecessor_is_not_self CHECK (
        predecessor_revision_id IS DISTINCT FROM profile_revision_id
    ),
    CONSTRAINT ck_profile_revisions_digest_present CHECK (length(btrim(document_digest)) > 0)
)
"""

_PROFILE_EXTENSIONS = """
CREATE TABLE profile_extensions (
    extension_revision_id   UUID PRIMARY KEY,
    tenant_id               UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The tenant's own namespace. Every definition an extension adds is spelled
    -- inside it, which is what keeps one tenant's additions from colliding with
    -- another's or with core.
    namespace               TEXT NOT NULL,

    -- Which core revision this extension was written against. An extension is
    -- only meaningful relative to the core it extends, so this is NOT NULL:
    -- an extension with no declared target cannot be checked for collisions
    -- against anything.
    target_core_revision_id UUID NOT NULL REFERENCES profile_revisions(profile_revision_id),

    canonical_document      JSONB NOT NULL,
    document_digest         TEXT NOT NULL,

    -- The points the extension declares it extends, kept separately from the
    -- document so publication can be checked against them without re-parsing.
    extension_points        JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- The verdict publication recorded. Stored because a later reader asking
    -- "was this ever checked?" must not have to re-run the compiler to find out.
    compatibility_result    TEXT NOT NULL,

    published_by            TEXT NOT NULL,
    published_at            TIMESTAMPTZ NOT NULL,

    -- One row per distinct document per tenant namespace. Re-publishing the
    -- same bytes is the same extension revision, not a new one.
    CONSTRAINT uq_profile_extensions_digest
        UNIQUE (tenant_id, namespace, document_digest),
    CONSTRAINT ck_profile_extensions_compatibility_result CHECK (
        compatibility_result IN ('compatible', 'incompatible')
    ),
    CONSTRAINT ck_profile_extensions_namespace_present CHECK (length(btrim(namespace)) > 0),
    CONSTRAINT ck_profile_extensions_digest_present CHECK (length(btrim(document_digest)) > 0)
)
"""

_PROFILE_BINDINGS = """
CREATE TABLE profile_bindings (
    binding_id                 UUID PRIMARY KEY,
    tenant_id                  UUID NOT NULL REFERENCES tenants(tenant_id),
    profile_revision_id        UUID NOT NULL REFERENCES profile_revisions(profile_revision_id),

    -- The digest over the set of extension revisions in force under this
    -- binding. The set itself is a separate relation; this is what makes
    -- "the same extensions as before" a single comparison.
    extension_set_digest       TEXT NOT NULL,

    state                      TEXT NOT NULL,

    -- The interval this binding is in force. `effective_to` NULL means open,
    -- which is what an active binding looks like until something replaces it.
    effective_from             TIMESTAMPTZ NOT NULL,
    effective_to               TIMESTAMPTZ,

    migration_run_id           UUID,

    -- Where a rollback would land, and whether it could be taken right now.
    -- Both, because a target with no readiness is a plan nobody has checked.
    rollback_target_binding_id UUID REFERENCES profile_bindings(binding_id),
    rollback_ready             BOOLEAN NOT NULL DEFAULT FALSE,

    actor                      TEXT NOT NULL,
    reason                     TEXT NOT NULL,
    audit_reference            TEXT,
    recorded_at                TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_profile_bindings_state CHECK (
        state IN ('planned', 'validating', 'active', 'rollback_pending', 'rolled_back', 'retired')
    ),
    CONSTRAINT ck_profile_bindings_interval CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    CONSTRAINT ck_profile_bindings_rollback_is_not_self CHECK (
        rollback_target_binding_id IS DISTINCT FROM binding_id
    ),
    -- The temporal rule this table exists to enforce: for one tenant, no two
    -- active bindings whose effective intervals overlap. Restricted to `active`
    -- on purpose -- several `planned` bindings may legitimately be drafted over
    -- the same future window, and only promotion to active has to be exclusive.
    CONSTRAINT ex_profile_bindings_one_active_per_tenant EXCLUDE USING gist (
        tenant_id WITH =,
        tstzrange(effective_from, effective_to) WITH &&
    ) WHERE (state = 'active')
)
"""

_ENTITY_TYPE_DEFINITIONS = """
CREATE TABLE entity_type_definitions (
    definition_id         UUID PRIMARY KEY,

    -- Every definition names the core revision it belongs to. When it came from
    -- a tenant extension it names that too; NULL means core. The pair is the
    -- compile identity, which is why uniqueness below treats NULLs as equal.
    profile_revision_id   UUID NOT NULL REFERENCES profile_revisions(profile_revision_id),
    extension_revision_id UUID REFERENCES profile_extensions(extension_revision_id),

    type_name             TEXT NOT NULL,

    required_properties   JSONB NOT NULL DEFAULT '[]'::jsonb,
    optional_properties   JSONB NOT NULL DEFAULT '[]'::jsonb,
    value_schemas         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Who may assert this type, and what provenance a write inherits when it
    -- does not state its own.
    authority             TEXT NOT NULL,
    default_provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- What makes an instance ready, as opposed to merely valid. Kept apart from
    -- the property lists because a draft is allowed to fail readiness while
    -- still being a well-formed entity.
    readiness_rules       JSONB NOT NULL DEFAULT '{}'::jsonb,

    compiled_at           TIMESTAMPTZ NOT NULL,

    -- `NULLS NOT DISTINCT` is load-bearing: core definitions leave
    -- `extension_revision_id` NULL, and under an ordinary UNIQUE every one of
    -- them would compare distinct from every other, so a repeated compile of
    -- the same core revision would insert a second copy of every type.
    CONSTRAINT uq_entity_type_definitions_compiled UNIQUE NULLS NOT DISTINCT (
        profile_revision_id, extension_revision_id, type_name
    ),
    CONSTRAINT ck_entity_type_definitions_name_present CHECK (length(btrim(type_name)) > 0)
)
"""

_RELATIONSHIP_TYPE_DEFINITIONS = """
CREATE TABLE relationship_type_definitions (
    definition_id         UUID PRIMARY KEY,

    profile_revision_id   UUID NOT NULL REFERENCES profile_revisions(profile_revision_id),
    extension_revision_id UUID REFERENCES profile_extensions(extension_revision_id),

    relationship_type     TEXT NOT NULL,

    -- Endpoints as type names rather than definition ids: an extension may add
    -- a relationship whose endpoint is a core type compiled under a different
    -- definition row, and a foreign key here would make that unexpressible.
    source_type           TEXT NOT NULL,
    destination_type      TEXT NOT NULL,
    direction             TEXT NOT NULL,

    property_schema       JSONB NOT NULL DEFAULT '{}'::jsonb,

    duplicate_policy      TEXT NOT NULL,
    symmetry              TEXT NOT NULL,
    inverse_view_policy   TEXT NOT NULL,

    -- The cardinality window, and the scope the count is taken over. A minimum
    -- with no scope is not checkable: "at least one owner" per what?
    min_cardinality       INTEGER NOT NULL DEFAULT 0,
    max_cardinality       INTEGER,
    cardinality_scope     TEXT NOT NULL,

    authority             TEXT NOT NULL,

    -- Default deny: an omitted cross-organization policy is a denial, so the
    -- column is NOT NULL and the compiler must write the decision down rather
    -- than leaving it to whatever a reader assumes.
    cross_org_policy      TEXT NOT NULL,

    compiled_at           TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_relationship_type_definitions_compiled UNIQUE NULLS NOT DISTINCT (
        profile_revision_id, extension_revision_id, relationship_type
    ),
    CONSTRAINT ck_relationship_type_definitions_direction CHECK (
        direction IN ('directed', 'undirected')
    ),
    CONSTRAINT ck_relationship_type_definitions_cardinality CHECK (
        min_cardinality >= 0
        AND (max_cardinality IS NULL OR max_cardinality >= min_cardinality)
    ),
    CONSTRAINT ck_relationship_type_definitions_cross_org_policy CHECK (
        cross_org_policy IN ('deny', 'allow_with_grant')
    ),
    CONSTRAINT ck_relationship_type_definitions_name_present CHECK (
        length(btrim(relationship_type)) > 0
    )
)
"""

_PROFILE_COMPILE_RESULTS = """
CREATE TABLE profile_compile_results (
    compile_result_id     UUID PRIMARY KEY,

    profile_revision_id   UUID NOT NULL REFERENCES profile_revisions(profile_revision_id),
    extension_revision_id UUID REFERENCES profile_extensions(extension_revision_id),

    -- What went in, what compiled it, and what came out. All three are needed to
    -- reproduce a compile: the same inputs through a different compiler version
    -- may legitimately produce a different output digest, and without the
    -- version recorded that difference is indistinguishable from corruption.
    input_digests         JSONB NOT NULL,
    compiler_version      TEXT NOT NULL,
    output_digest         TEXT NOT NULL,

    -- Ordered, and stored as arrays for that reason: the compiler's report is a
    -- sequence, and a set would lose the order a reader works through them in.
    conflicts             JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings              JSONB NOT NULL DEFAULT '[]'::jsonb,

    compiled_at           TIMESTAMPTZ NOT NULL,

    -- One result per input set per compiler version. Recompiling identical
    -- inputs with the same compiler must be a no-op rather than an accumulating
    -- log, which is what makes a repeated publication attempt idempotent.
    CONSTRAINT uq_profile_compile_results_inputs UNIQUE NULLS NOT DISTINCT (
        profile_revision_id, extension_revision_id, compiler_version
    ),
    CONSTRAINT ck_profile_compile_results_output_digest_present CHECK (
        length(btrim(output_digest)) > 0
    )
)
"""


def upgrade() -> None:
    # `=` on a uuid inside a gist operator class, which core gist does not carry.
    # Without it the exclusion constraint on profile_bindings cannot be created.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute(_PROFILE_REVISIONS)
    op.execute(_PROFILE_EXTENSIONS)
    op.execute(_PROFILE_BINDINGS)
    op.execute(_ENTITY_TYPE_DEFINITIONS)
    op.execute(_RELATIONSHIP_TYPE_DEFINITIONS)
    op.execute(_PROFILE_COMPILE_RESULTS)

    # The tenant's view of its own bindings, newest first: the question the
    # validation path asks on every governed write.
    op.execute(
        """
        CREATE INDEX ix_profile_bindings_tenant_effective
            ON profile_bindings (tenant_id, state, effective_from DESC)
        """
    )
    # Walking a profile's publication chain backwards from any revision.
    op.execute(
        """
        CREATE INDEX ix_profile_revisions_predecessor
            ON profile_revisions (predecessor_revision_id)
        """
    )
    # Resolving a type by name within a compiled revision, which is what the
    # entity validator does once per governed write.
    op.execute(
        """
        CREATE INDEX ix_entity_type_definitions_lookup
            ON entity_type_definitions (profile_revision_id, type_name)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_relationship_type_definitions_endpoints
            ON relationship_type_definitions (profile_revision_id, source_type, destination_type)
        """
    )

    # One function, five triggers, spelled out rather than looped over a list of
    # table names. The five are exactly the published documents and the
    # projections compiled from them; `profile_bindings` is absent by design and
    # a literal list is where that omission is visible to a reader.
    op.execute(_IMMUTABILITY_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_profile_revisions_immutable
            BEFORE UPDATE OR DELETE ON profile_revisions
            FOR EACH ROW EXECUTE FUNCTION profile_publication_is_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_profile_extensions_immutable
            BEFORE UPDATE OR DELETE ON profile_extensions
            FOR EACH ROW EXECUTE FUNCTION profile_publication_is_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_entity_type_definitions_immutable
            BEFORE UPDATE OR DELETE ON entity_type_definitions
            FOR EACH ROW EXECUTE FUNCTION profile_publication_is_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_relationship_type_definitions_immutable
            BEFORE UPDATE OR DELETE ON relationship_type_definitions
            FOR EACH ROW EXECUTE FUNCTION profile_publication_is_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_profile_compile_results_immutable
            BEFORE UPDATE OR DELETE ON profile_compile_results
            FOR EACH ROW EXECUTE FUNCTION profile_publication_is_immutable()
        """
    )


def downgrade() -> None:
    # Triggers first, then the function they call: dropping the function while a
    # trigger still references it is refused, and keeping the order explicit says
    # which way that dependency runs.
    op.execute("DROP TRIGGER IF EXISTS trg_profile_revisions_immutable ON profile_revisions")
    op.execute("DROP TRIGGER IF EXISTS trg_profile_extensions_immutable ON profile_extensions")
    op.execute("DROP TRIGGER IF EXISTS trg_entity_type_definitions_immutable ON entity_type_definitions")
    op.execute("DROP TRIGGER IF EXISTS trg_relationship_type_definitions_immutable ON relationship_type_definitions")
    op.execute("DROP TRIGGER IF EXISTS trg_profile_compile_results_immutable ON profile_compile_results")
    op.execute("DROP FUNCTION IF EXISTS profile_publication_is_immutable()")

    # Reverse dependency order: the definitions and results reference both
    # published documents, bindings reference revisions, extensions reference
    # revisions, and revisions reference only themselves.
    op.execute("DROP TABLE IF EXISTS profile_compile_results")
    op.execute("DROP TABLE IF EXISTS relationship_type_definitions")
    op.execute("DROP TABLE IF EXISTS entity_type_definitions")
    op.execute("DROP TABLE IF EXISTS profile_bindings")
    op.execute("DROP TABLE IF EXISTS profile_extensions")
    op.execute("DROP TABLE IF EXISTS profile_revisions")

    # btree_gist is deliberately left installed. Dropping an extension another
    # revision may have come to rely on is a wider blast radius than this
    # downgrade is entitled to, and an unused extension costs nothing.
