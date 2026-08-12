"""Type-qualified handles, assertion provenance, and per-attribute revisions.

Expand-only: three new tables, nothing altered, nothing dropped. The existing
opaque `entity_id` stays the primary identity and no row in `entities` is
rewritten — handles are a record *about* an entity, not a replacement for its
id, so every foreign key and cached reference elsewhere in the database keeps
pointing at exactly what it pointed at before.

**The legacy uniqueness on `entities` is deliberately left in place.** It says
one name per tenant, case-insensitively, with no notion of type; the handle
table says something narrower and type-aware. Both hold at once during the
expand, which is the point of an expand: the old rule keeps protecting the old
read path until the new one is proven, and removing it is a separate decision
taken later against evidence rather than a side effect of adding a table.

**Provenance is a foreign key, not a convention.** An attribute assertion whose
source cannot be named is an assertion nobody can evaluate, re-check, or revoke,
and once such rows exist there is no way to tell them apart from the rest
afterwards. `provenance_id` is therefore `NOT NULL REFERENCES` — a write with no
provenance fails at the database rather than being reported later by a scan.

**Three kinds of "cannot change", spelled differently because they differ.**

- `assertion_provenance` is fully immutable: a trigger refuses `UPDATE` and
  `DELETE` outright. Re-stating a source's trust class in place would silently
  re-characterize every assertion already resting on it, including ones already
  read and acted on. A correction is a new provenance row and a new assertion
  revision naming it.
- `entity_handles` is *temporally* append-only, which is not the same rule. Its
  identity columns are frozen after insert, while `valid_to` and the supersession
  pointer stay writable, because closing an interval is how a handle is retired
  and freezing those two would make the temporal columns unusable. A trigger
  compares old against new column by column, so "append-only" means the record's
  meaning cannot be rewritten rather than that nothing about the row may move.
- `entity_attribute_assertions` follows the same temporal shape as handles and
  for the same reason: an attribute's history is the sequence of its revisions.

Enforcing all three in the database rather than in Python is deliberate. These
tables are reached by migration tooling and operator sessions as well as by the
application, and a rule that lives only in the service layer is one a `psql`
session does not have.

**Active uniqueness is partial, and type-aware where the profile says so.** A
qualified handle is unique per tenant *among live rows only* — a retired handle
must be able to coexist with the one that replaced it, which a total unique
index would forbid. Primary-name uniqueness additionally keys on entity type,
because two types may legitimately carry the same short name and collapsing
them was the ambiguity the qualified form exists to remove.

**This revision carries the whole identity expand, including the indexes that
only the later backfill and dual-read path will use.** Those are here rather
than in a migration of their own because that later work deliberately ships no
DDL; putting them where they are used would mean a schema change arriving with
the machinery that depends on it, which is the ordering an expand exists to
avoid. They cost writes now and make the cutover a code change alone.

**Why the file is numbered 0051 when the numbering is not the chain.**
`down_revision` below is what orders these, and it was resolved by walking
parents from the root rather than by sorting the directory. That is not a
hypothetical caution here: the file numbers upstream already do not sort into
chain order, and the revision immediately below this one was first written
against the wrong parent for exactly that reason and had to be re-pointed.
"""

from __future__ import annotations

from alembic import op

revision = "0051_handles_and_provenance"
# The chain head, resolved by walking `down_revision` from the root. The highest
# filename is not reliably the head -- the numbering carries deliberate gaps and
# does not sort into chain order upstream of here.
down_revision: str | None = "0050_profile_revisions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_PROVENANCE_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION assertion_provenance_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'assertion provenance is immutable: % on % is refused',
        TG_OP, TG_TABLE_NAME
        USING HINT = 'record a new provenance row and a new assertion revision '
                     'naming it; editing this one would re-characterize every '
                     'assertion already resting on it';
END;
$$ LANGUAGE plpgsql
"""

#: Freezes identity while leaving the temporal columns writable. Spelled as an
#: explicit column-by-column comparison rather than a blanket refusal because
#: retiring a record *is* an update here, and a trigger that could not tell the
#: two apart would either block supersession or permit a rewrite.
_HANDLE_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION entity_handles_are_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'entity handles are append-only: DELETE on % is refused', TG_TABLE_NAME
            USING HINT = 'close the interval by setting valid_to; the record of '
                         'a handle having existed is what makes a historical '
                         'reference resolvable';
    END IF;
    IF NEW.handle_id       IS DISTINCT FROM OLD.handle_id
       OR NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id
       OR NEW.entity_id    IS DISTINCT FROM OLD.entity_id
       OR NEW.entity_type  IS DISTINCT FROM OLD.entity_type
       OR NEW.namespace    IS DISTINCT FROM OLD.namespace
       OR NEW.handle_name  IS DISTINCT FROM OLD.handle_name
       OR NEW.qualified_handle IS DISTINCT FROM OLD.qualified_handle
       OR NEW.lookup_key   IS DISTINCT FROM OLD.lookup_key
       OR NEW.kind         IS DISTINCT FROM OLD.kind
       OR NEW.valid_from   IS DISTINCT FROM OLD.valid_from
       OR NEW.source       IS DISTINCT FROM OLD.source
    THEN
        RAISE EXCEPTION 'entity handle identity is immutable: % may not be rewritten', OLD.handle_id
            USING HINT = 'supersede it -- set valid_to and superseded_by_handle_id, '
                         'then insert the replacement';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_ASSERTION_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION entity_attribute_assertions_are_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'attribute assertions are append-only: DELETE on % is refused', TG_TABLE_NAME
            USING HINT = 'close the interval by setting valid_to; an attribute''s '
                         'history is the sequence of its revisions';
    END IF;
    IF NEW.assertion_id   IS DISTINCT FROM OLD.assertion_id
       OR NEW.tenant_id   IS DISTINCT FROM OLD.tenant_id
       OR NEW.entity_id   IS DISTINCT FROM OLD.entity_id
       OR NEW.property_name IS DISTINCT FROM OLD.property_name
       OR NEW.value       IS DISTINCT FROM OLD.value
       OR NEW.valid_from  IS DISTINCT FROM OLD.valid_from
       OR NEW.provenance_id IS DISTINCT FROM OLD.provenance_id
    THEN
        RAISE EXCEPTION 'attribute assertion content is immutable: % may not be rewritten', OLD.assertion_id
            USING HINT = 'supersede it -- set valid_to and superseded_by_assertion_id, '
                         'then insert the new revision with its own provenance';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_ENTITY_HANDLES = """
CREATE TABLE entity_handles (
    handle_id               UUID PRIMARY KEY,
    tenant_id               UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The existing opaque identity, referenced rather than replaced. Nothing
    -- here rewrites an entity id or any reference to one.
    entity_id               UUID NOT NULL REFERENCES entities(entity_id),

    -- Carried on the handle rather than read through the entity because the
    -- qualified form embeds it, and a handle whose type could drift out from
    -- under it would stop matching its own qualified spelling.
    entity_type             TEXT NOT NULL,

    namespace               TEXT NOT NULL,
    handle_name             TEXT NOT NULL,

    -- The qualified form, stored and constrained to equal its own parts. Stored
    -- so lookups compare one column; constrained so the stored copy cannot
    -- disagree with the parts it was built from.
    qualified_handle        TEXT NOT NULL,

    -- The normalized form every lookup compares against. Separate from the
    -- display spelling so that case and spacing rules can be applied once at
    -- write time rather than by every reader, each with its own idea of them.
    lookup_key              TEXT NOT NULL,

    kind                    TEXT NOT NULL,

    valid_from              TIMESTAMPTZ NOT NULL,
    -- NULL means live. An interval closed by a `valid_to` is what makes a
    -- superseded handle still resolvable for historical references.
    valid_to                TIMESTAMPTZ,

    source                  TEXT NOT NULL,
    superseded_by_handle_id UUID REFERENCES entity_handles(handle_id),

    recorded_at             TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_entity_handles_kind CHECK (
        kind IN ('primary', 'alias', 'legacy', 'external_mapping')
    ),
    CONSTRAINT ck_entity_handles_interval CHECK (
        valid_to IS NULL OR valid_to > valid_from
    ),
    -- The stored qualified form is exactly its parts. Without this the column
    -- is a free-text field that happens to look structured, and a lookup by
    -- qualified handle would be trusting whoever wrote the row.
    CONSTRAINT ck_entity_handles_qualified_form CHECK (
        qualified_handle = namespace || ':' || entity_type || '/' || handle_name
    ),
    CONSTRAINT ck_entity_handles_namespace_present CHECK (length(btrim(namespace)) > 0),
    CONSTRAINT ck_entity_handles_name_present CHECK (length(btrim(handle_name)) > 0),
    CONSTRAINT ck_entity_handles_lookup_key_present CHECK (length(btrim(lookup_key)) > 0),
    CONSTRAINT ck_entity_handles_source_present CHECK (length(btrim(source)) > 0),
    -- A handle that supersedes itself is a chain with no end; a reader walking
    -- supersession forward would not terminate.
    CONSTRAINT ck_entity_handles_supersedes_is_not_self CHECK (
        superseded_by_handle_id IS DISTINCT FROM handle_id
    )
)
"""

_ASSERTION_PROVENANCE = """
CREATE TABLE assertion_provenance (
    provenance_id                  UUID PRIMARY KEY,
    tenant_id                      UUID NOT NULL REFERENCES tenants(tenant_id),

    source_system                  TEXT NOT NULL,
    source_namespace               TEXT NOT NULL,
    -- NULL for a record with no upstream identity of its own, which is a
    -- different statement from an empty external id.
    external_record_id             TEXT,
    external_revision              TEXT,

    -- Three distinct times, kept apart because they answer different questions:
    -- when it happened, when we saw it, when we stored it. Collapsing them
    -- makes staleness unmeasurable.
    event_time                     TIMESTAMPTZ,
    observed_at                    TIMESTAMPTZ,
    ingested_at                    TIMESTAMPTZ NOT NULL,

    derivation_method              TEXT,
    derivation_profile             TEXT,

    authority                      TEXT NOT NULL,

    freshness_state                TEXT NOT NULL,
    expires_at                     TIMESTAMPTZ,
    revocation_ref                 TEXT,
    revoked_at                     TIMESTAMPTZ,

    -- Present only for a derived record. A confidence attached to something a
    -- canonical owner stated is a category error: it invites a reader to
    -- discount a fact that was not inferred in the first place.
    confidence                     DOUBLE PRECISION,

    validating_profile_revision_id UUID REFERENCES profile_revisions(profile_revision_id),
    extension_set_digest           TEXT,

    produced_by                    TEXT NOT NULL,
    approved_by                    TEXT,
    created_at                     TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_assertion_provenance_authority CHECK (
        authority IN ('canonical_owner', 'external_authority', 'observed', 'derived')
    ),
    CONSTRAINT ck_assertion_provenance_freshness CHECK (
        freshness_state IN ('fresh', 'stale', 'expired', 'revoked')
    ),
    CONSTRAINT ck_assertion_provenance_confidence_only_when_derived CHECK (
        confidence IS NULL OR (authority = 'derived' AND confidence >= 0 AND confidence <= 1)
    ),
    -- A revocation with no reference cannot be audited, and a reference with no
    -- time cannot be ordered against the assertions it invalidates.
    CONSTRAINT ck_assertion_provenance_revocation_is_complete CHECK (
        (revoked_at IS NULL) = (revocation_ref IS NULL)
    ),
    CONSTRAINT ck_assertion_provenance_revoked_state_has_a_revocation CHECK (
        freshness_state <> 'revoked' OR revoked_at IS NOT NULL
    ),
    CONSTRAINT ck_assertion_provenance_source_present CHECK (length(btrim(source_system)) > 0),
    CONSTRAINT ck_assertion_provenance_producer_present CHECK (length(btrim(produced_by)) > 0)
)
"""

_ENTITY_ATTRIBUTE_ASSERTIONS = """
CREATE TABLE entity_attribute_assertions (
    assertion_id                   UUID PRIMARY KEY,
    tenant_id                      UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id                      UUID NOT NULL REFERENCES entities(entity_id),

    property_name                  TEXT NOT NULL,
    value                          JSONB NOT NULL,

    valid_from                     TIMESTAMPTZ NOT NULL,
    valid_to                       TIMESTAMPTZ,
    superseded_by_assertion_id     UUID REFERENCES entity_attribute_assertions(assertion_id),

    -- NOT NULL and a real foreign key. An assertion whose source cannot be
    -- named is one nobody can re-check or revoke, and once such rows exist
    -- there is no way to separate them from the rest afterwards.
    provenance_id                  UUID NOT NULL REFERENCES assertion_provenance(provenance_id),

    validation_result              TEXT NOT NULL,
    validating_profile_revision_id UUID REFERENCES profile_revisions(profile_revision_id),

    recorded_at                    TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_entity_attribute_assertions_interval CHECK (
        valid_to IS NULL OR valid_to > valid_from
    ),
    CONSTRAINT ck_entity_attribute_assertions_validation_result CHECK (
        validation_result IN ('valid', 'invalid', 'unchecked')
    ),
    CONSTRAINT ck_entity_attribute_assertions_property_present CHECK (
        length(btrim(property_name)) > 0
    ),
    CONSTRAINT ck_entity_attribute_assertions_supersedes_is_not_self CHECK (
        superseded_by_assertion_id IS DISTINCT FROM assertion_id
    )
)
"""


def upgrade() -> None:
    op.execute(_ENTITY_HANDLES)
    op.execute(_ASSERTION_PROVENANCE)
    op.execute(_ENTITY_ATTRIBUTE_ASSERTIONS)

    # Uniqueness among live rows only. A total unique index would forbid a
    # retired handle coexisting with the one that replaced it, which is the
    # normal state of every rename this table exists to record.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_entity_handles_active_qualified
            ON entity_handles (tenant_id, lookup_key)
            WHERE valid_to IS NULL
        """
    )
    # Primary names are unique per type, not globally: two types may carry the
    # same short name, and collapsing them is the ambiguity the qualified form
    # exists to remove rather than one to re-introduce here.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_entity_handles_active_primary_name
            ON entity_handles (tenant_id, entity_type, lower(handle_name))
            WHERE kind = 'primary' AND valid_to IS NULL
        """
    )
    # An unqualified lookup has to discover that more than one type matches
    # before it can say so; this is the index that makes that check a lookup
    # rather than a scan.
    op.execute(
        """
        CREATE INDEX ix_entity_handles_unqualified
            ON entity_handles (tenant_id, lower(handle_name))
            WHERE valid_to IS NULL
        """
    )
    # Every handle an entity has ever had, which is what the backfill walks and
    # what a dual read consults before falling back to the legacy name.
    op.execute(
        """
        CREATE INDEX ix_entity_handles_entity
            ON entity_handles (tenant_id, entity_id, kind)
        """
    )

    # One live assertion per property per entity. The partial predicate is what
    # lets the superseded revisions stay in the table underneath it.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_entity_attribute_assertions_active
            ON entity_attribute_assertions (tenant_id, entity_id, property_name)
            WHERE valid_to IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_entity_attribute_assertions_entity
            ON entity_attribute_assertions (tenant_id, entity_id, property_name)
        """
    )
    # Reaching every assertion resting on one provenance record, which is what
    # a revocation has to do to mark them.
    op.execute(
        """
        CREATE INDEX ix_entity_attribute_assertions_provenance
            ON entity_attribute_assertions (provenance_id)
        """
    )
    # Re-finding an upstream record on re-ingest, so a second delivery of the
    # same external revision is recognized rather than duplicated.
    op.execute(
        """
        CREATE INDEX ix_assertion_provenance_external
            ON assertion_provenance (tenant_id, source_system, external_record_id)
        """
    )

    op.execute(_PROVENANCE_IMMUTABILITY_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_assertion_provenance_immutable
            BEFORE UPDATE OR DELETE ON assertion_provenance
            FOR EACH ROW EXECUTE FUNCTION assertion_provenance_is_immutable()
        """
    )

    op.execute(_HANDLE_APPEND_ONLY_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_entity_handles_append_only
            BEFORE UPDATE OR DELETE ON entity_handles
            FOR EACH ROW EXECUTE FUNCTION entity_handles_are_append_only()
        """
    )

    op.execute(_ASSERTION_APPEND_ONLY_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_entity_attribute_assertions_append_only
            BEFORE UPDATE OR DELETE ON entity_attribute_assertions
            FOR EACH ROW EXECUTE FUNCTION entity_attribute_assertions_are_append_only()
        """
    )


def downgrade() -> None:
    # Triggers before the functions they call: dropping a function a trigger
    # still references is refused, and the explicit order says which way the
    # dependency runs.
    op.execute("DROP TRIGGER IF EXISTS trg_entity_attribute_assertions_append_only ON entity_attribute_assertions")
    op.execute("DROP TRIGGER IF EXISTS trg_entity_handles_append_only ON entity_handles")
    op.execute("DROP TRIGGER IF EXISTS trg_assertion_provenance_immutable ON assertion_provenance")
    op.execute("DROP FUNCTION IF EXISTS entity_attribute_assertions_are_append_only()")
    op.execute("DROP FUNCTION IF EXISTS entity_handles_are_append_only()")
    op.execute("DROP FUNCTION IF EXISTS assertion_provenance_is_immutable()")

    # Reverse dependency order: assertions reference provenance and entities,
    # handles reference entities, provenance references only published profile
    # revisions, which this revision does not own.
    op.execute("DROP TABLE IF EXISTS entity_attribute_assertions")
    op.execute("DROP TABLE IF EXISTS entity_handles")
    op.execute("DROP TABLE IF EXISTS assertion_provenance")
