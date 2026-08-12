"""Typed relationship metadata, beside the existing edge rather than inside it.

Governed relationships need a type definition, validated properties, temporal
state, readiness, provenance and the binding revision they were asserted under.
None of that exists on `edges`, which predates profiles.

**A sidecar table keyed by the edge's own id, not new columns on `edges`.** The
requirement is to preserve current edge ids and interfaces, and this is the
reading that preserves them literally: nothing about `edges` changes, so no
existing reader, index or query plan moves. The governed row points at the edge it
describes and shares its identity, which is what "on the existing edge identity"
means in storage terms.

It is also the only shape whose ORM stays honest. `edges` is mapped by a
declarative class in the shared storage models, and adding columns in a migration
without adding them to that class would leave the database and the ORM disagreeing
— the exact drift the parity test exists to catch.

**The endpoints are stored again here, deliberately.** They already live on
`edges`, and duplicating a column normally invites the two copies to diverge.
They are duplicated because every constraint below is *about* the endpoints: a
uniqueness rule and a temporal exclusion cannot be expressed over columns in
another table, and a trigger reaching across to read them would enforce at a
moment of its own choosing rather than at write time. A foreign key to the edge
plus a check that the pair matches would be a second copy of the rule; instead the
governed row owns its endpoints and the edge keeps its own, and the two are joined
by identity rather than kept in step by hand.

**Temporal exclusion rather than a plain unique constraint.** One relationship of
one type between one pair of endpoints may be asserted many times over history —
that is what makes the state temporal. What may not happen is two assertions of
the same type over the same pair whose validity intervals overlap, because then
"is this relationship in force?" has two answers. A unique constraint would
forbid the legitimate sequence; `EXCLUDE USING gist` forbids only the overlap.
`btree_gist` is already installed by the profile-publication revision.

**The lock key is a function, not a convention.** Aggregate cardinality checks —
"at most three of this type per source" — cannot be enforced row-locally: a
count-then-write races, and two concurrent writers each see two rows and each
write a third. Serializing them needs every writer to derive the *same* advisory
lock key from the same three facts, and a key computed in application code is one
each caller can compute slightly differently. Stated here as SQL, so the database
is the single definition and a psql session gets the same answer as the service.
"""

from __future__ import annotations

from alembic import op

revision = "0052_relationship_metadata"
# Resolved by walking `down_revision` from the root rather than by reading
# filenames, which do not sort into chain order in this repository.
down_revision: str | None = "0051_handles_and_provenance"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_RELATIONSHIP_METADATA = """
CREATE TABLE relationship_metadata (
    -- The edge's own id. Shared rather than reassigned: a governed row describes
    -- an existing relationship and must be reachable by the id every current
    -- reader already holds.
    relationship_id                  UUID PRIMARY KEY REFERENCES edges(edge_id) ON DELETE CASCADE,
    tenant_id                        UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Which compiled definition governs this row. NOT NULL: an edge with no
    -- definition is exactly an ungoverned edge, and those stay on `edges` alone
    -- rather than appearing here with a NULL that readers must interpret.
    relationship_type_definition_id  UUID NOT NULL
        REFERENCES relationship_type_definitions(definition_id),

    -- Denormalized from `edges` because every constraint below is about them.
    source_entity_id                 UUID NOT NULL REFERENCES entities(entity_id),
    destination_entity_id            UUID NOT NULL REFERENCES entities(entity_id),

    -- The type name and cardinality scope as compiled, carried so the lock key
    -- and the aggregate checks can be computed without joining to the definition
    -- on every write. The definition is the authority; these are its values at
    -- assertion time, which is also what makes a later definition change visible
    -- as a difference rather than silently rewriting history.
    relationship_type                TEXT NOT NULL,
    cardinality_scope                TEXT NOT NULL,

    -- Properties validated against the definition's schema. `{}` rather than
    -- NULL for a relationship carrying none, so "no properties" and "not yet
    -- validated" are not the same value.
    properties                       JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Temporal state of the governed assertion, independent of the edge's own
    -- ingest clock. `effective_to` NULL means still in force.
    effective_from                   TIMESTAMPTZ NOT NULL,
    effective_to                     TIMESTAMPTZ,

    -- Whether this row satisfies its definition's readiness rules. Stored rather
    -- than derived because readiness gates activation, and a value recomputed on
    -- read would answer with today's rules about a row asserted under older ones.
    readiness_state                  TEXT NOT NULL,

    -- Who asserted it and under which binding. Both NOT NULL: a governed
    -- assertion with no provenance is not governed, and one with no binding
    -- cannot say which profile revision validated it.
    provenance_id                    UUID NOT NULL REFERENCES assertion_provenance(provenance_id),
    profile_binding_id               UUID NOT NULL REFERENCES profile_bindings(binding_id),

    recorded_at                      TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_relationship_metadata_readiness CHECK (
        readiness_state IN ('draft', 'ready', 'blocked')
    ),
    CONSTRAINT ck_relationship_metadata_scope CHECK (
        cardinality_scope IN ('per_source', 'per_destination', 'per_pair')
    ),
    CONSTRAINT ck_relationship_metadata_interval CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    -- A relationship from a thing to itself is the shape that makes a closure
    -- walk non-terminating. The profile decides whether a type admits it, so this
    -- is not forbidden outright -- but source and destination being equal while
    -- the type is asserted between two different things is not representable, and
    -- the endpoints must at least both resolve.
    CONSTRAINT ck_relationship_metadata_endpoints_present CHECK (
        source_entity_id IS NOT NULL AND destination_entity_id IS NOT NULL
    )
)
"""

#: Row-local uniqueness: one governed assertion per type per ordered pair per
#: instant. Written as a temporal exclusion rather than a unique constraint
#: because the same relationship may legitimately be asserted, ended and
#: re-asserted -- what may not exist is two in force at once, which is when "is
#: this in force?" acquires two answers.
_TEMPORAL_EXCLUSION = """
ALTER TABLE relationship_metadata
    ADD CONSTRAINT ex_relationship_metadata_no_overlap
    EXCLUDE USING gist (
        tenant_id WITH =,
        relationship_type WITH =,
        source_entity_id WITH =,
        destination_entity_id WITH =,
        tstzrange(effective_from, effective_to) WITH &&
    )
"""

#: The advisory-lock key every writer of an aggregate cardinality check must take.
#:
#: `hashtextextended` with a fixed seed rather than `hashtext`: the two-argument
#: form returns bigint, which is what `pg_advisory_xact_lock` accepts, and the
#: seed is pinned so the key is stable across releases rather than following a
#: default somebody may change.
#:
#: IMMUTABLE and STRICT are both load-bearing. IMMUTABLE lets the planner treat it
#: as a constant within a statement; STRICT means a NULL input yields NULL rather
#: than a key -- so a caller who forgot the binding gets a failed lock acquisition
#: instead of quietly sharing key zero with every other such caller.
_LOCK_KEY_FUNCTION = """
CREATE OR REPLACE FUNCTION relationship_aggregate_lock_key(
    binding UUID,
    relationship_type TEXT,
    cardinality_scope TEXT
) RETURNS BIGINT AS $$
    SELECT hashtextextended(binding::text || '|' || relationship_type || '|' || cardinality_scope, 0)
$$ LANGUAGE sql IMMUTABLE STRICT
"""


def upgrade() -> None:
    op.execute(_RELATIONSHIP_METADATA)
    op.execute(_TEMPORAL_EXCLUSION)
    op.execute(_LOCK_KEY_FUNCTION)

    # The question the validator asks once per governed write: what else of this
    # type is in force for this source right now. Leading with the columns it
    # filters on, in the order it filters them.
    op.execute(
        """
        CREATE INDEX ix_relationship_metadata_source
            ON relationship_metadata (tenant_id, relationship_type, source_entity_id, effective_from DESC)
        """
    )
    # The same question from the other end, which `per_destination` scopes ask.
    op.execute(
        """
        CREATE INDEX ix_relationship_metadata_destination
            ON relationship_metadata (tenant_id, relationship_type, destination_entity_id, effective_from DESC)
        """
    )
    # Reading every governed row under one binding, which is what a migration
    # run and a rollback both walk.
    op.execute(
        """
        CREATE INDEX ix_relationship_metadata_binding
            ON relationship_metadata (profile_binding_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_relationship_metadata_binding")
    op.execute("DROP INDEX IF EXISTS ix_relationship_metadata_destination")
    op.execute("DROP INDEX IF EXISTS ix_relationship_metadata_source")
    op.execute("DROP FUNCTION IF EXISTS relationship_aggregate_lock_key(UUID, TEXT, TEXT)")
    # The table takes its own constraints with it; the exclusion is not dropped
    # separately for that reason.
    op.execute("DROP TABLE IF EXISTS relationship_metadata")

    # `btree_gist` is deliberately left installed. It arrived with the
    # profile-publication revision and that revision's own downgrade leaves it
    # too: dropping an extension another revision relies on is a wider blast
    # radius than this downgrade is entitled to.
