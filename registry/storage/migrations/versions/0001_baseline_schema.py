"""Baseline schema — every table, index, constraint, and seed row this service runs on.

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-08-04

This is the one migration the schema has. It replaces a chain of 47 phase-named
revisions that had accumulated the usual cost of that shape: several tables
created in one revision and reshaped by half a dozen later ones, two features
built and then withdrawn, and a rename (``lmm_`` → ``memory_``) that an
incremental chain could only ever express as an ALTER. None of that history was
faithful to begin with — these migrations were hand-edited in place as the
schema design changed, so the chain was already a description of where the
schema ended up, not a record of how it got there. Squashing it loses nothing
real.

**Organization.** Sections below follow the subsystems that own them, not the
order any of the original migrations shipped in. Within a section, tables are
created in dependency order (a table's foreign-key targets exist before it
does); across sections, the same rule applies — a section that references an
earlier one's tables is written after it.

**The rename.** Every table, index, constraint, and sequence that used to
carry an ``lmm_`` prefix now carries ``memory_`` instead — ``memory_claims``,
``memory_promotion_journal``, ``memory_capability_request``, and so on.
``memory_session_events`` was already named right and is unchanged.
Application code, tests, and operational scripts were updated in the same
change; nothing in this repository still writes to an ``lmm_`` name.

**What is deliberately absent.** ``episodes``, ``provenance``, and the
``episodes_new`` shadow shipped in the original baseline as the intended
landing zone for ingested events and their attribution; neither ever gained a
writer, and both were dropped before this squash — they are not recreated
here. ``arc_content_deletion_verifications`` is excluded for the same reason,
confirmed fresh for this change: nothing in the codebase inserts into it. Its
ORM model and the codepaths that would exercise it do not exist either — it
was dropped because nothing read or wrote it, and this file's own git history
carries the paper trail for that decision. ``capability_annotations``,
``workspace_shares``, ``workspace_share_acceptances``, ``roles``,
``actor_roles``, and ``api_tokens`` were features this schema tried and
withdrew; none of the six exist at the revision this baseline replaces, so
none are created here.

**Downgrade.** There is no meaningful "previous schema" to restore — this
*is* the first schema. ``downgrade()`` drops every table this migration
creates, best-effort, and leaves an empty database. Treat a downgrade from
this revision the same way you would treat dropping the database: it is not
a rollback of a change, it is the removal of the schema.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterator

from alembic import op

from registry.arc.vocabularies import CONTENT_CLASSIFICATIONS, RECEIPT_EVENT_TYPES
from registry.embedding.targets import EMBEDDING_TARGETS, sql_set

revision = "0001_baseline_schema"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# The seed tenant every system vocabulary row is seeded against. Every
# migration that ever seeded a row used this same literal rather than
# importing it from application code — a migration has to remain readable and
# runnable at the revision it was written, without depending on code that may
# have moved on. That precedent is kept here.
DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"

# Reserved tenant ARC's deployment-scope audit events attribute to. `disabled_at`
# is what makes the row unusable as a real tenant — see the ARC section for why
# a reserved row exists at all instead of a nullable `audit_log.tenant_id`.
_ARC_DEPLOYMENT_TENANT_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _sql_set(values: frozenset[str]) -> str:
    """Render a closed vocabulary as a sorted SQL `IN` list.

    Sorted so the emitted DDL is stable across runs — a set's iteration order
    is not, and an unstable constraint definition makes every schema diff
    noisy.
    """
    return ", ".join(f"'{value}'" for value in sorted(values))


def _monthly_partition_bounds(start: datetime.date, count: int) -> Iterator[tuple[str, str, str]]:
    """Yield (suffix, from_iso, to_iso) for *count* consecutive months from *start*.

    Pinned callers pass a fixed `start` so the generated DDL is deterministic
    across environments and reruns — a partition set that depended on
    `date.today()` would name and bound its children differently depending on
    the calendar month the migration happened to run in.
    """
    year, month = start.year, start.month
    for _ in range(count):
        from_d = datetime.date(year, month, 1)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        to_d = datetime.date(next_year, next_month, 1)
        suffix = f"{from_d.year:04d}_{from_d.month:02d}"
        yield suffix, from_d.isoformat(), to_d.isoformat()
        year, month = next_year, next_month


def _current_month_partition_bounds(today: datetime.date) -> tuple[str, str, str]:
    """Return (suffix, from_iso, to_iso) for the month containing *today*.

    Used by the tables that only ever need one pre-created partition at
    creation time (a worker or an operator creates the rest as months roll
    over); the fixed-window tables above use `_monthly_partition_bounds`
    instead so their 24 children do not depend on when this migration runs.
    """
    from_d = datetime.date(today.year, today.month, 1)
    if today.month == 12:
        to_d = datetime.date(today.year + 1, 1, 1)
    else:
        to_d = datetime.date(today.year, today.month + 1, 1)
    suffix = f"{from_d.year:04d}_{from_d.month:02d}"
    return suffix, from_d.isoformat(), to_d.isoformat()


# Fixed origin for the three tables partitioned on a 24-month pre-created
# window (audit_log, its cutover shadow, and usage_events). Pinned rather than
# "now" so the generated DDL — and the tests that assert its shape — do not
# drift with the date this migration happens to run on.
_FIXED_PARTITION_START = datetime.date(2025, 1, 1)
_FIXED_PARTITION_COUNT = 24


# ---------------------------------------------------------------------------
# Section 1 — extensions
# ---------------------------------------------------------------------------

# pgcrypto is created when the server offers it, but is not required — the only
# thing the schema uses from it is gen_random_uuid(), which has been part of
# Postgres core since 13. Several perfectly good Postgres builds omit contrib
# entirely (minimal distributions, some managed services), and a hard CREATE
# EXTENSION would lock the schema out of all of them for a function it already
# has.
_EXT_PGCRYPTO_IF_AVAILABLE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
    END IF;
END
$$
"""

_EXT_VECTOR = "CREATE EXTENSION IF NOT EXISTS vector"


# ---------------------------------------------------------------------------
# Section 2 — tenancy, actors, and the shared vocabulary
# ---------------------------------------------------------------------------

_TENANTS_DDL = """
CREATE TABLE tenants (
    tenant_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                       TEXT NOT NULL UNIQUE,
    display_name               TEXT NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active                  BOOLEAN NOT NULL DEFAULT TRUE,

    -- Cross-tenant adoption: whether this tenant is subject to regulated-data
    -- handling, and how often it wants adoption/change notifications digested
    -- rather than delivered singly.
    is_regulated               BOOLEAN NOT NULL DEFAULT FALSE,
    notification_digest_window TEXT NOT NULL DEFAULT 'none',

    -- JIT tenant provisioning. external_tenant_id is the opaque id an upstream
    -- identity system assigned; nullable because manually-provisioned tenants
    -- have no external counterpart. provider discriminates how the tenant was
    -- created — the specific upstream source name belongs in audit-log
    -- payloads, not in this enum.
    external_tenant_id         TEXT,
    provider                   TEXT NOT NULL DEFAULT 'manual',

    -- Operator override that blocks JIT re-materialization of a tenant that
    -- was explicitly disabled.
    disabled_at                TIMESTAMPTZ,

    -- Living memory retention: a tenant setting, not application config, so a
    -- deployment serving several tenants can honour different obligations
    -- without a redeploy. 30 days by default, configurable to 180.
    memory_retention_days       INTEGER NOT NULL DEFAULT 30,

    CONSTRAINT chk_digest_window CHECK (
        notification_digest_window IN ('none', '5m', '15m', '1h', '6h', '24h')
    ),
    CONSTRAINT tenants_provider_check CHECK (provider IN ('manual', 'jit', 'system')),
    CONSTRAINT ck_tenants_memory_retention CHECK (memory_retention_days BETWEEN 1 AND 180)
)
"""

_TENANTS_EXTERNAL_ID_IDX = (
    "CREATE UNIQUE INDEX ix_tenants_external_tenant_id_provider "
    "ON tenants (external_tenant_id, provider) WHERE external_tenant_id IS NOT NULL"
)

# Slimmed to what the OIDC-JWT + external-entitlement-service auth model still
# needs: the actor row survives as the audit-log denormalization key. api_tokens,
# roles, and actor_roles were dropped outright — that authorization state now
# lives in the entitlement service, not this database. email and actor_kind stay:
# registry/ingest/runner.py uses actor_kind to distinguish human actors from
# sync workers, and that path is independent of the auth model.
_ACTORS_DDL = """
CREATE TABLE actors (
    actor_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    display_name TEXT,
    email        TEXT,
    oidc_subject TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_kind   TEXT NOT NULL DEFAULT 'human',

    CONSTRAINT uq_actors_tenant_oidc_subject UNIQUE (tenant_id, oidc_subject)
)
"""

_ACTORS_INDEXES = [
    "CREATE INDEX idx_actors_tenant ON actors (tenant_id)",
    # Sync-worker actors share a (tenant_id, display_name) namespace that must be
    # unique per tenant so each connector type has a stable, non-ambiguous
    # identity. Human actors are excluded, allowing display-name reuse across
    # identity providers.
    "CREATE UNIQUE INDEX uq_actors_tenant_sync_type "
    "ON actors (tenant_id, display_name) WHERE actor_kind = 'sync_worker'",
]

# The controlled-vocabulary table. Most kinds are tenant-scoped; `claim_predicate`
# is the one kind that may also be global (tenant_id IS NULL), because living
# memory needs a predicate to mean the same thing across every tenant writing
# claims against it — two tenants each defining `depends_on` with their own value
# type would make their claims incomparable, defeating the point of a shared
# graph. value_type/claim_category/definition/value_cardinality are the metadata
# a claim predicate must declare before anything can be validated or scored
# against it; every other vocabulary kind leaves them NULL.
_VOCAB_DDL = """
CREATE TABLE vocabulary_values (
    vocab_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID REFERENCES tenants(tenant_id),
    kind               TEXT NOT NULL,
    value              TEXT NOT NULL,
    is_system          BOOLEAN NOT NULL DEFAULT FALSE,
    deprecated_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    value_type         TEXT,
    claim_category     TEXT,
    definition         TEXT,
    value_cardinality  TEXT,

    -- The keyhole: only a claim predicate may be global.
    CONSTRAINT ck_vocab_global_is_claim_predicate CHECK (
        tenant_id IS NOT NULL OR kind = 'claim_predicate'
    ),
    -- A predicate without a declared type cannot validate anything written
    -- against it.
    CONSTRAINT ck_vocab_claim_predicate_metadata CHECK (
        kind <> 'claim_predicate'
        OR (value_type IS NOT NULL AND char_length(value_type) > 0
            AND claim_category IS NOT NULL AND char_length(claim_category) > 0
            AND definition IS NOT NULL AND char_length(definition) > 0)
    ),
    CONSTRAINT ck_vocab_claim_predicate_cardinality CHECK (
        kind <> 'claim_predicate' OR value_cardinality IN ('single', 'multi')
    )
)
"""

# NULL is never equal to NULL in a unique index, so one composite index cannot
# express "one global row per name" — every global predicate could reuse the
# same name. Split by scope instead.
_VOCAB_INDEXES = [
    "CREATE INDEX idx_vocab_tenant_kind ON vocabulary_values (tenant_id, kind)",
    "CREATE UNIQUE INDEX uq_vocab_tenant_kind_value ON vocabulary_values "
    "(tenant_id, kind, value) WHERE tenant_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_vocab_global_kind_value ON vocabulary_values " "(kind, value) WHERE tenant_id IS NULL",
    # Resolution reads global predicates before local ones, so that lookup gets
    # its own index rather than scanning the tenant-oriented one.
    "CREATE INDEX ix_vocab_global_predicates ON vocabulary_values (kind, value) WHERE tenant_id IS NULL",
]

# Per-actor (or per-tenant default) rate-limit overrides. A partial unique index
# per side of the NULL/non-NULL actor_id split, rather than one UNIQUE
# constraint, because a plain UNIQUE would let every tenant-default row collide
# with a NULL actor_id the same way -- exactly one tenant-level default and any
# number of per-actor rows is the shape a partial index expresses and a plain
# UNIQUE cannot.
_RATE_LIMITS_DDL = """
CREATE TABLE rate_limits (
    limit_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id          UUID REFERENCES actors(actor_id),
    reads_per_second  INTEGER NOT NULL DEFAULT 100,
    writes_per_second INTEGER NOT NULL DEFAULT 10,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_RATE_LIMITS_INDEXES = [
    "CREATE UNIQUE INDEX uq_rate_limits_tenant_default ON rate_limits (tenant_id) WHERE actor_id IS NULL",
    "CREATE UNIQUE INDEX uq_rate_limits_actor ON rate_limits (tenant_id, actor_id) WHERE actor_id IS NOT NULL",
    "CREATE INDEX idx_rate_limits_tenant ON rate_limits (tenant_id, actor_id)",
]

# Seed vocabulary for the tables created in this section and the next few —
# entity/fact/edge/lifecycle vocab from the original baseline, plus every value
# added by a later migration. Kept as one seed list (rather than one per
# section) because the seeding mechanism — one idempotent INSERT per row — is
# identical regardless of which section introduced the value, and a reader
# checking "is X a valid entity_type" should be able to find every entity_type
# row in one place.
#
# `visibility` still carries `public-in-fabric` rather than `public`: a later
# migration renamed the *column* CHECK on `entities.visibility` from
# `public-in-fabric` to `public` but never touched this seed row, and the
# baseline reproduces the schema as it stands today rather than fixing that
# pre-existing gap. `entities.visibility` itself only ever accepts `public`.
_VOCAB_SEEDS: list[tuple[str, str]] = [
    # entity_type
    ("entity_type", "capability"),
    ("entity_type", "concept"),
    ("entity_type", "operation"),
    ("entity_type", "person"),
    ("entity_type", "system"),
    ("entity_type", "integration"),
    # fact_category
    ("fact_category", "overview"),
    ("fact_category", "concept_glossary"),
    ("fact_category", "limits"),
    ("fact_category", "security_model"),
    ("fact_category", "pricing"),
    ("fact_category", "release_note"),
    ("fact_category", "faq"),
    ("fact_category", "adr"),
    ("fact_category", "rfc"),
    ("fact_category", "dev_doc"),
    ("fact_category", "api_doc"),
    ("fact_category", "catalog_entry"),
    # edge_rel
    ("edge_rel", "concept_of"),
    ("edge_rel", "operation_of"),
    ("edge_rel", "depends_on"),
    ("edge_rel", "integrates_with"),
    ("edge_rel", "event_source"),
    ("edge_rel", "replaced_by"),
    ("edge_rel", "instance_of"),
    ("edge_rel", "requires"),
    ("edge_rel", "conflicts_with"),
    ("edge_rel", "composes"),
    ("edge_rel", "provides_to"),
    # lifecycle_state
    ("lifecycle_state", "alpha"),
    ("lifecycle_state", "beta"),
    ("lifecycle_state", "ga"),
    ("lifecycle_state", "deprecated"),
    ("lifecycle_state", "retired"),
    # pii_category
    ("pii_category", "email"),
    ("pii_category", "phone"),
    ("pii_category", "ssn"),
    ("pii_category", "aws_access_key"),
    ("pii_category", "aws_secret_key"),
    ("pii_category", "jwt_token"),
    ("pii_category", "credit_card"),
    # visibility — see the docstring note above re: `public-in-fabric`.
    ("visibility", "private"),
    ("visibility", "tenant-shared"),
    ("visibility", "public-in-fabric"),
    # notification_event_kind
    ("notification_event_kind", "version_published"),
    ("notification_event_kind", "deprecation"),
    ("notification_event_kind", "breaking_change"),
    ("notification_event_kind", "conflict_added"),
    ("notification_event_kind", "integration_added"),
]


# ---------------------------------------------------------------------------
# Section 3 — audit log
# ---------------------------------------------------------------------------

# Partitioned by RANGE(ts) from creation, 24 monthly children pre-created
# starting at a fixed origin (see _FIXED_PARTITION_START). The partition key
# has to be part of any PK on a partitioned table, hence PRIMARY KEY (audit_id, ts).
_AUDIT_LOG_DDL = """
CREATE TABLE audit_log (
    audit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id     UUID REFERENCES actors(actor_id),
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    UUID NOT NULL,
    before_jsonb JSONB,
    after_jsonb  JSONB,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT,
    error_code   TEXT,
    PRIMARY KEY (audit_id, ts)
) PARTITION BY RANGE (ts)
"""

_AUDIT_LOG_INDEXES = [
    "CREATE INDEX idx_audit_tenant_ts ON audit_log (tenant_id, ts DESC)",
    "CREATE INDEX idx_audit_target    ON audit_log (tenant_id, target_type, target_id, ts DESC)",
    "CREATE INDEX idx_audit_actor     ON audit_log (tenant_id, actor_id, ts DESC)",
]

# The partition-cutover shadow table `scripts/partition_migrate.py` copies
# `audit_log` into ahead of a rename. Deliberately without FKs on tenant_id /
# actor_id — unlike `audit_log` itself — because the cutover script's
# `INSERT INTO … SELECT *` must not be slowed by per-row FK checks against
# tables that already validated the same rows once.
_AUDIT_LOG_NEW_DDL = """
CREATE TABLE audit_log_new (
    audit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    actor_id     UUID,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    UUID NOT NULL,
    before_jsonb JSONB,
    after_jsonb  JSONB,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT,
    error_code   TEXT,
    PRIMARY KEY (audit_id, ts)
) PARTITION BY RANGE (ts)
"""

_AUDIT_LOG_NEW_INDEXES = [
    "CREATE INDEX idx_audit_new_tenant_ts ON audit_log_new (tenant_id, ts DESC)",
    "CREATE INDEX idx_audit_new_target    ON audit_log_new (tenant_id, target_type, target_id, ts DESC)",
    "CREATE INDEX idx_audit_new_actor     ON audit_log_new (tenant_id, actor_id, ts DESC)",
]


# ---------------------------------------------------------------------------
# Section 4 — entities, attributes, edges, and the closure cache
# ---------------------------------------------------------------------------

_ENTITIES_DDL = """
CREATE TABLE entities (
    entity_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_type TEXT NOT NULL,
    name        TEXT NOT NULL,
    external_id TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  UUID REFERENCES actors(actor_id),
    visibility  TEXT NOT NULL DEFAULT 'private',

    CONSTRAINT chk_entity_visibility CHECK (visibility IN ('private', 'tenant-shared', 'public'))
)
"""

_ENTITIES_INDEXES = [
    "CREATE INDEX idx_entities_tenant_type ON entities (tenant_id, entity_type)",
    # (tenant_id, lower(name)) is unique — enforced going forward, not
    # backfilled against pre-existing rows. Slug validation at the service
    # layer rejects non-slug names at write time.
    "CREATE UNIQUE INDEX uq_entities_tenant_name ON entities (tenant_id, lower(name))",
    "CREATE UNIQUE INDEX idx_entities_external_id ON entities (tenant_id, entity_type, external_id) "
    "WHERE external_id IS NOT NULL",
    "CREATE INDEX idx_entities_visibility ON entities (tenant_id, visibility)",
    # Keyset-pagination support for the capability list endpoint.
    "CREATE INDEX idx_entities_tenant_created ON entities (tenant_id, created_at DESC, entity_id)",
]

_ATTRIBUTES_DDL = """
CREATE TABLE attributes (
    attr_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id        UUID NOT NULL REFERENCES entities(entity_id),
    key              TEXT NOT NULL,
    value            JSONB NOT NULL,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_ATTRIBUTES_INDEXES = [
    "CREATE INDEX idx_attr_entity_current ON attributes (tenant_id, entity_id, key) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_attr_entity_temporal ON attributes (tenant_id, entity_id, t_valid_from, t_valid_to)",
]

_EDGES_DDL = """
CREATE TABLE edges (
    edge_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    src_entity_id    UUID NOT NULL REFERENCES entities(entity_id),
    rel              TEXT NOT NULL,
    dst_entity_id    UUID NOT NULL REFERENCES entities(entity_id),
    properties       JSONB,
    is_authoritative BOOLEAN NOT NULL DEFAULT TRUE,
    sync_run_id      UUID,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_EDGES_INDEXES = [
    "CREATE INDEX idx_edges_src_current ON edges (tenant_id, src_entity_id, rel) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_edges_dst_current ON edges (tenant_id, dst_entity_id, rel) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_edges_temporal ON edges (tenant_id, src_entity_id, t_valid_from, t_valid_to)",
]

_EDGE_PROPERTY_SCHEMAS_DDL = """
CREATE TABLE edge_property_schemas (
    schema_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    edge_rel         TEXT NOT NULL,
    json_schema      JSONB NOT NULL,
    is_advisory      BOOLEAN NOT NULL DEFAULT TRUE,
    advisory_until   TIMESTAMPTZ,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_EDGE_PROPERTY_SCHEMAS_IDX = (
    "CREATE INDEX idx_epschema_tenant_rel ON edge_property_schemas (tenant_id, edge_rel) "
    "WHERE t_invalidated_at IS NULL"
)

# Materialized transitive-closure cache, refreshed by the closure_refresh worker
# from closure_outbox below.
_CLOSURE_CACHE_DDL = """
CREATE TABLE closure_cache (
    cache_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    root_entity_id   UUID NOT NULL REFERENCES entities(entity_id),
    member_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    direction        TEXT NOT NULL,
    depth            INTEGER NOT NULL,
    edge_path        UUID[] NOT NULL,
    edge_rels        TEXT[] NOT NULL,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_direction CHECK (direction IN ('forward', 'reverse'))
)
"""

_CLOSURE_CACHE_INDEXES = [
    "CREATE UNIQUE INDEX idx_closure_unique ON closure_cache (tenant_id, root_entity_id, member_entity_id, direction)",
    "CREATE INDEX idx_closure_root ON closure_cache (tenant_id, root_entity_id, direction)",
    "CREATE INDEX idx_closure_member ON closure_cache (tenant_id, member_entity_id, direction)",
    "CREATE INDEX idx_closure_refreshed ON closure_cache (refreshed_at)",
]

# Edge-oriented transactional outbox for the closure-cache refresh worker.
# embedding_outbox cannot be reused here: its target_id is NOT NULL with an
# implicit fact/claim shape, and edge mutations carry an edge_id.
_CLOSURE_OUTBOX_DDL = """
CREATE TABLE closure_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    edge_id         UUID NOT NULL,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ
)
"""

_CLOSURE_OUTBOX_IDX = "CREATE INDEX idx_closure_outbox_enqueued ON closure_outbox (enqueued_at)"

_EXTERNAL_SYSTEMS_DDL = """
CREATE TABLE external_systems (
    slug         TEXT NOT NULL,
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    display_name TEXT NOT NULL,
    url_template TEXT,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, slug)
)
"""

# Hard-delete only — no t_invalidated_at. An external-id mapping that stops
# being true is removed, not soft-invalidated; there is no bi-temporal history
# to preserve for a pointer to another system's identifier.
_ENTITY_EXTERNAL_IDS_DDL = """
CREATE TABLE entity_external_ids (
    external_id_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id            UUID NOT NULL REFERENCES entities(entity_id),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    external_system_slug TEXT NOT NULL,
    external_id          TEXT NOT NULL,
    url                  TEXT,
    metadata_jsonb        JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_entity_external_id UNIQUE (tenant_id, external_system_slug, external_id)
)
"""

_ENTITY_EXTERNAL_IDS_INDEXES = [
    "CREATE INDEX idx_extid_entity ON entity_external_ids (tenant_id, entity_id)",
    "CREATE INDEX idx_extid_system ON entity_external_ids (tenant_id, external_system_slug, external_id)",
]

# Denormalized pair-discoverability index, populated by a trigger on edges
# rather than computed at read time.
_INTEGRATION_PAIRS_DDL = """
CREATE TABLE integration_pairs (
    pair_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    capability_a_id       UUID NOT NULL REFERENCES entities(entity_id),
    capability_b_id       UUID NOT NULL REFERENCES entities(entity_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_pair_order CHECK (capability_a_id < capability_b_id)
)
"""

_INTEGRATION_PAIRS_INDEXES = [
    "CREATE UNIQUE INDEX uq_pair ON integration_pairs "
    "(tenant_id, integration_entity_id, capability_a_id, capability_b_id)",
    "CREATE INDEX idx_pair_lookup ON integration_pairs (tenant_id, capability_a_id, capability_b_id)",
]

# Fires AFTER INSERT on edges WHERE rel IN ('composes','depends_on') and the
# source entity has type='integration'. Inserts with canonical ordering
# (capability_a_id < capability_b_id) to avoid (A,B)/(B,A) duplicates. No
# visibility filter in the trigger — visibility enforcement belongs at the
# service layer so every consumer of the data shares one chokepoint.
_INTEGRATION_PAIRS_TRIGGER_FUNC = """
CREATE OR REPLACE FUNCTION populate_integration_pairs()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_src_type TEXT;
    v_cap_a    UUID;
    v_cap_b    UUID;
BEGIN
    -- Only process composes and depends_on edges
    IF NEW.rel NOT IN ('composes', 'depends_on') THEN
        RETURN NEW;
    END IF;

    -- Check whether the source entity is of type 'integration'
    -- NOTE: the entities table column is named `entity_type` (not `type`).
    SELECT entity_type INTO v_src_type
      FROM entities
     WHERE entity_id = NEW.src_entity_id;

    IF v_src_type IS DISTINCT FROM 'integration' THEN
        RETURN NEW;
    END IF;

    -- Canonical ordering: smaller UUID goes into capability_a_id
    -- no visibility filter: visibility enforcement belongs at the service layer, not the DB trigger
    IF NEW.src_entity_id < NEW.dst_entity_id THEN
        v_cap_a := NEW.src_entity_id;
        v_cap_b := NEW.dst_entity_id;
    ELSE
        v_cap_a := NEW.dst_entity_id;
        v_cap_b := NEW.src_entity_id;
    END IF;

    INSERT INTO integration_pairs
        (integration_entity_id, tenant_id, capability_a_id, capability_b_id)
    VALUES
        (NEW.src_entity_id, NEW.tenant_id, v_cap_a, v_cap_b)
    ON CONFLICT DO NOTHING;

    RETURN NEW;
END;
$$
"""

_INTEGRATION_PAIRS_TRIGGER = """
CREATE TRIGGER trg_integration_pairs
AFTER INSERT ON edges
FOR EACH ROW EXECUTE FUNCTION populate_integration_pairs()
"""


# ---------------------------------------------------------------------------
# Section 5 — facts, embeddings, and their outboxes
# ---------------------------------------------------------------------------

# sync_run_id's FK to sync_runs is added in Section 9, once that table exists.
_FACTS_DDL = """
CREATE TABLE facts (
    fact_id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id                   UUID NOT NULL REFERENCES entities(entity_id),
    category                    TEXT NOT NULL,
    body                        TEXT NOT NULL,
    is_authoritative            BOOLEAN NOT NULL DEFAULT TRUE,
    is_authoritative_superseded BOOLEAN NOT NULL DEFAULT FALSE,
    sync_run_id                 UUID,
    t_valid_from                TIMESTAMPTZ NOT NULL,
    t_valid_to                  TIMESTAMPTZ,
    t_ingested_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at            TIMESTAMPTZ,
    created_by                  UUID REFERENCES actors(actor_id),

    -- UI consumption: a short title and the body's rendering format.
    title                       TEXT,
    body_format                 TEXT,

    -- Lexical retrieval without a separate pass.
    ts_vector                   TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,

    CONSTRAINT ck_facts_body_format CHECK (body_format IN ('markdown', 'html', 'plain'))
)
"""

_FACTS_INDEXES = [
    "CREATE INDEX idx_facts_entity_current ON facts (tenant_id, entity_id, category) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_facts_entity_temporal ON facts (tenant_id, entity_id, t_valid_from, t_valid_to)",
    "CREATE INDEX idx_facts_sync_run ON facts (sync_run_id) WHERE sync_run_id IS NOT NULL",
    "CREATE INDEX idx_facts_fts ON facts USING GIN (ts_vector)",
]

# The vocabulary lives in code (`registry/embedding/targets.py`) and is
# rendered into the constraint, so the two cannot drift. A conformance test
# reads the constraint back out of the live schema and asserts it enumerates
# exactly this set.
_EMBEDDING_TARGET_TYPE_SET = sql_set(EMBEDDING_TARGETS)


def _embedding_vector_dim() -> int:
    """Width of `embeddings.vector`, from `EMBEDDING_DIM` (default 384).

    Read at creation time, the same way `EMBEDDINGS_PARTITION_COUNT` is read
    below — a documented operator knob the schema must actually honour. Unlike
    the historical migration that resized this column on an already-populated
    table, a baseline never has existing vectors to reconcile: it creates the
    column at the configured width from the start. Changing `EMBEDDING_DIM`
    against an already-migrated database is a forward migration's job, not
    this one's — resizing a populated `vector` column means deleting and
    recomputing every embedding, which only makes sense as an explicit,
    reviewed operation.
    """
    raw = os.environ.get("EMBEDDING_DIM", "384")  # config: intentional
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"EMBEDDING_DIM must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"EMBEDDING_DIM must be positive, got {value}")
    return value


def _embedding_hash_buckets() -> int:
    """How many hash partitions `embeddings` gets, from `EMBEDDINGS_PARTITION_COUNT`.

    Genuinely fixed after creation: hash partitioning cannot redistribute rows
    across a different modulus, so changing it later means rebuilding the
    table.
    """
    raw = os.environ.get("EMBEDDINGS_PARTITION_COUNT", "8")  # config: intentional
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"EMBEDDINGS_PARTITION_COUNT must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"EMBEDDINGS_PARTITION_COUNT must be positive, got {value}")
    return value


# A row is identified by (target_type, target_id); target_type names what kind
# of thing was embedded and target_id points at it. Deliberately no FK on
# target_id — it addresses more than one table, so integrity rests on the
# closed vocabulary here plus there being exactly one enqueuer per kind.
# Partitioned by HASH(tenant_id) from creation rather than as a later cutover,
# so there is one physical shape rather than two.
def _embeddings_ddl(vector_dim: int) -> str:
    return f"""
CREATE TABLE embeddings (
    embedding_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,

    target_type   TEXT NOT NULL,
    target_id     UUID NOT NULL,

    chunk_index   INTEGER NOT NULL DEFAULT 0,
    -- No default: a row inserted without a model id would claim to be
    -- whatever the default named, and vectors from two models are not
    -- comparable.
    model_id      TEXT NOT NULL,
    vector        VECTOR({vector_dim}) NOT NULL,
    text_chunk    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    ts_vector     TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text_chunk)) STORED,

    PRIMARY KEY (embedding_id, tenant_id),

    CONSTRAINT ck_embed_target_type CHECK (target_type IN ({_EMBEDDING_TARGET_TYPE_SET})),

    -- One vector per (source, model, chunk) — what lets the drain upsert
    -- instead of duplicating on retry.
    CONSTRAINT uq_embed_target_chunk
        UNIQUE (tenant_id, target_type, target_id, model_id, chunk_index)
) PARTITION BY HASH (tenant_id)
"""


_EMBEDDINGS_PARTITION_TEMPLATE = (
    "CREATE TABLE embeddings_p{n} PARTITION OF embeddings FOR VALUES WITH (modulus {modulus}, remainder {n})"
)

_EMBEDDINGS_SOURCE_IDX = "CREATE INDEX idx_embed_target ON embeddings (tenant_id, target_type, target_id)"
_EMBEDDINGS_MODEL_IDX = "CREATE INDEX idx_embed_model ON embeddings (model_id)"
_EMBEDDINGS_FTS_IDX = "CREATE INDEX idx_embed_fts ON embeddings USING GIN (ts_vector)"

# HNSW per partition (m=16, ef_construction=64), built while partitions are
# empty — which costs nothing — then filled incrementally as the drain inserts.
_EMBEDDINGS_HNSW_TEMPLATE = (
    "CREATE INDEX idx_embed_hnsw_p{n} ON embeddings_p{n} "
    "USING hnsw (vector vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)

_OUTBOX_DDL = f"""
CREATE TABLE embedding_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    target_type     TEXT NOT NULL,
    target_id       UUID NOT NULL,

    -- The text travels on the row, so the drain never reads the source table.
    text_to_embed   TEXT NOT NULL,
    chunk_plan      JSONB NOT NULL,

    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,

    CONSTRAINT ck_outbox_target_type CHECK (target_type IN ({_EMBEDDING_TARGET_TYPE_SET})),

    -- One queued request per target — the arbiter that makes an enqueue upsert
    -- possible.
    CONSTRAINT uq_outbox_target UNIQUE (tenant_id, target_type, target_id)
)
"""

_OUTBOX_INDEXES = [
    "CREATE INDEX idx_outbox_pending ON embedding_outbox (enqueued_at)",
    "CREATE INDEX idx_outbox_tenant ON embedding_outbox (tenant_id, target_type)",
]

_OUTBOX_FAILED_DDL = f"""
CREATE TABLE embedding_outbox_failed (
    failed_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    target_type   TEXT NOT NULL,
    target_id     UUID NOT NULL,

    text_to_embed TEXT NOT NULL,
    chunk_plan    JSONB NOT NULL,
    failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_text    TEXT NOT NULL,
    attempts      INTEGER NOT NULL,

    CONSTRAINT ck_outbox_failed_target_type CHECK (target_type IN ({_EMBEDDING_TARGET_TYPE_SET}))
)
"""

_OUTBOX_FAILED_IDX = (
    "CREATE INDEX idx_outbox_failed_tenant ON embedding_outbox_failed (tenant_id, target_type, failed_at DESC)"
)


# ---------------------------------------------------------------------------
# Section 6 — schema registry
# ---------------------------------------------------------------------------

_CAPABILITY_TYPE_SCHEMAS_DDL = """
CREATE TABLE capability_type_schemas (
    schema_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    type_name        TEXT NOT NULL,
    json_schema      JSONB NOT NULL,
    is_advisory      BOOLEAN NOT NULL DEFAULT TRUE,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_CAPABILITY_TYPE_SCHEMAS_IDX = (
    "CREATE INDEX idx_captype_tenant_name ON capability_type_schemas (tenant_id, type_name) "
    "WHERE t_invalidated_at IS NULL"
)

# Stable id so a seed row can be found again without relying on the
# auto-generated default.
_INTEGRATION_TYPE_SCHEMA_ID = "b0000007-0000-0000-0000-000000000001"

_INTEGRATION_TYPE_SCHEMA_JSON = (
    '{"type": "object", "properties": {'
    '"config_template": {"type": "string"}, '
    '"runbook_url": {"type": "string", "format": "uri"}, '
    '"known_issues": {"type": "array", "items": {"type": "string"}}}, '
    '"additionalProperties": true}'
)


# ---------------------------------------------------------------------------
# Section 7 — workspaces
# ---------------------------------------------------------------------------
#
# Plaintext-only. No encryption retrofit was ever added — no ciphertext
# columns, no KEK reference — so none appear here.

_WORKSPACES_DDL = """
CREATE TABLE workspaces (
    workspace_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    name             TEXT NOT NULL,
    description      TEXT,
    owner_kind       TEXT NOT NULL,
    owner_actor_id   UUID REFERENCES actors(actor_id),
    encryption_tier  TEXT NOT NULL DEFAULT 'none',
    archived_at      TIMESTAMPTZ,
    t_invalidated_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID REFERENCES actors(actor_id),
    CONSTRAINT chk_owner_kind CHECK (owner_kind IN ('actor', 'tenant')),
    CONSTRAINT chk_encryption_tier CHECK (encryption_tier IN (
        'none', 'paas_tenant_key', 'aws_kms', 'azure_key_vault', 'gcp_kms', 'hashicorp_vault'
    )),
    CONSTRAINT chk_actor_owner CHECK (
        (owner_kind = 'actor' AND owner_actor_id IS NOT NULL) OR owner_kind = 'tenant'
    )
)
"""

_WORKSPACES_INDEXES = [
    "CREATE INDEX idx_ws_tenant ON workspaces (tenant_id) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_ws_owner ON workspaces (owner_actor_id) WHERE owner_actor_id IS NOT NULL",
]

# The workspace-sharing model (workspace_shares / workspace_share_acceptances)
# was replaced by tenant-role-based access control; those tables are not
# recreated here. What remains from that design is the simplest possible
# owner_kind guard: it cannot be changed after creation, full stop, with no
# cross-table dependency on a sharing model that no longer exists.
_WORKSPACE_OWNER_KIND_IMMUTABLE_FUNC = """
CREATE OR REPLACE FUNCTION check_workspace_owner_kind_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.owner_kind IS DISTINCT FROM OLD.owner_kind THEN
        RAISE EXCEPTION 'owner_kind is immutable after creation';
    END IF;
    RETURN NEW;
END;
$$
"""

_WORKSPACE_OWNER_KIND_IMMUTABLE_TRIGGER = """
CREATE TRIGGER trg_ws_owner_kind_immutable
BEFORE UPDATE ON workspaces
FOR EACH ROW EXECUTE FUNCTION check_workspace_owner_kind_immutable()
"""

_WORKSPACE_ENTRIES_DDL = """
CREATE TABLE workspace_entries (
    entry_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     UUID NOT NULL REFERENCES workspaces(workspace_id),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    kind             TEXT NOT NULL,
    body_md          TEXT NOT NULL,
    references_jsonb JSONB,
    reference_ids    UUID[] NOT NULL DEFAULT '{}',
    expires_at       TIMESTAMPTZ,
    t_invalidated_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID REFERENCES actors(actor_id),
    -- 'private_annotation' is not a member: it distinguished private workspace
    -- content from the public capability annotations that were withdrawn, and
    -- every row that used it was behaviourally identical to a 'note' with a
    -- reference — nothing enforced a difference.
    CONSTRAINT chk_entry_kind CHECK (
        kind IN ('note', 'decision', 'open_question', 'saved_query', 'saved_view')
    )
)
"""

_WORKSPACE_ENTRIES_INDEXES = [
    "CREATE INDEX idx_we_workspace ON workspace_entries (workspace_id) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_we_tenant ON workspace_entries (tenant_id) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_we_refs ON workspace_entries USING GIN (reference_ids)",
    "CREATE INDEX idx_we_expires ON workspace_entries (expires_at) "
    "WHERE expires_at IS NOT NULL AND t_invalidated_at IS NULL",
    "CREATE INDEX idx_we_body_fts ON workspace_entries USING GIN (to_tsvector('english', body_md)) "
    "WHERE t_invalidated_at IS NULL",
]


# ---------------------------------------------------------------------------
# Section 8 — provider/consumer adoption and notifications
# ---------------------------------------------------------------------------

_ADOPTION_EVENTS_DDL = """
CREATE TABLE adoption_events (
    adoption_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL REFERENCES tenants(tenant_id),
    provider_capability_id UUID NOT NULL REFERENCES entities(entity_id),
    consumer_tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id               UUID REFERENCES actors(actor_id),
    intent                 TEXT,
    version_pin            TEXT,
    t_valid_from           TIMESTAMPTZ NOT NULL,
    t_valid_to             TIMESTAMPTZ,
    t_ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at       TIMESTAMPTZ,
    CONSTRAINT uq_adoption UNIQUE (tenant_id, provider_capability_id, consumer_tenant_id)
        DEFERRABLE INITIALLY DEFERRED
)
"""

_ADOPTION_EVENTS_INDEXES = [
    "CREATE INDEX idx_adoption_provider ON adoption_events (provider_capability_id) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_adoption_consumer ON adoption_events (consumer_tenant_id) WHERE t_invalidated_at IS NULL",
]

_SUBSCRIPTIONS_DDL = """
CREATE TABLE subscriptions (
    subscription_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id                UUID REFERENCES actors(actor_id),
    capability_id           UUID NOT NULL REFERENCES entities(entity_id),
    event_kinds             TEXT[] NOT NULL,
    webhook_url             TEXT,
    webhook_hmac_secret_ref TEXT,
    is_enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    -- Captures the tenant's notification_digest_window at create/auto-subscribe
    -- time; not retroactively updated.
    digest_window           TEXT NOT NULL DEFAULT 'none',
    t_valid_from            TIMESTAMPTZ NOT NULL,
    t_valid_to              TIMESTAMPTZ,
    t_ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at        TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_SUBSCRIPTIONS_INDEXES = [
    "CREATE INDEX idx_sub_tenant ON subscriptions (tenant_id) WHERE t_invalidated_at IS NULL",
    "CREATE INDEX idx_sub_capability ON subscriptions (capability_id) WHERE t_invalidated_at IS NULL",
]

# Payload-minimal event inbox — no fact/claim body, partitioned monthly with
# one pre-created current-month partition (more are created as months roll
# over; nothing in this baseline pre-creates a fixed window here, matching the
# original migration).
_NOTIFICATIONS_DDL = """
CREATE TABLE notifications (
    notification_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    subscription_id       UUID REFERENCES subscriptions(subscription_id),
    capability_id         UUID NOT NULL REFERENCES entities(entity_id),
    capability_slug       TEXT NOT NULL,
    event_kind            TEXT NOT NULL,
    change_classification TEXT,
    version_before        TEXT,
    version_after         TEXT,
    occurred_at           TIMESTAMPTZ NOT NULL,
    fetch_url             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'unread',
    ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id, ts)
) PARTITION BY RANGE (ts)
"""

_NOTIFICATIONS_INDEXES = [
    "CREATE INDEX idx_notif_tenant_status ON notifications (tenant_id, status, ts DESC)",
    "CREATE INDEX idx_notif_capability ON notifications (tenant_id, capability_id, ts DESC)",
]

_NOTIFICATION_DELIVERIES_DDL = """
CREATE TABLE notification_deliveries (
    delivery_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    notification_id UUID NOT NULL,
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    webhook_url     TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,
    http_status     INTEGER,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at   TIMESTAMPTZ,
    error_text      TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_id, ts)
) PARTITION BY RANGE (ts)
"""

_NOTIFICATION_DELIVERIES_INDEXES = [
    "CREATE INDEX idx_delivery_notification ON notification_deliveries (notification_id)",
    # Carries attempted_at (not just next_retry_at): the webhook worker's claim
    # query orders by next_retry_at NULLS FIRST, attempted_at, and an index
    # without it forces a re-sort of the filtered rows before the LIMIT.
    "CREATE INDEX idx_delivery_pending_sort ON notification_deliveries (tenant_id, next_retry_at, attempted_at) "
    "WHERE status = 'pending'",
]


# ---------------------------------------------------------------------------
# Section 9 — sync infrastructure
# ---------------------------------------------------------------------------

_SYNC_SOURCES_DDL = """
CREATE TABLE sync_sources (
    source_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    source_type     TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    credentials_ref TEXT,
    schedule        TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES actors(actor_id)
)
"""

_SYNC_SOURCES_INDEXES = [
    "CREATE INDEX idx_sync_sources_tenant ON sync_sources (tenant_id)",
    "CREATE INDEX idx_sync_sources_type ON sync_sources (tenant_id, source_type)",
]

_SYNC_RUNS_DDL = """
CREATE TABLE sync_runs (
    sync_run_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(tenant_id),
    source_id      UUID NOT NULL REFERENCES sync_sources(source_id),
    status         TEXT NOT NULL CHECK (status IN ('running', 'done', 'partial', 'failed')),
    trigger        TEXT NOT NULL CHECK (trigger IN ('scheduled', 'webhook', 'manual')),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    duration_s     INTEGER,
    artifact_count INTEGER,
    error_summary  TEXT
)
"""

_SYNC_RUNS_INDEXES = [
    "CREATE INDEX idx_sync_runs_source ON sync_runs (tenant_id, source_id, started_at DESC)",
    "CREATE INDEX idx_sync_runs_status ON sync_runs (tenant_id, status) WHERE status IN ('running', 'partial')",
]

_WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE webhook_deliveries (
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    delivery_id  TEXT NOT NULL,
    source_id    UUID NOT NULL REFERENCES sync_sources(source_id),
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, delivery_id)
)
"""

_WEBHOOK_DELIVERIES_IDX = (
    "CREATE INDEX idx_webhook_deliveries_source ON webhook_deliveries (tenant_id, source_id, received_at DESC)"
)

# facts.sync_run_id has no FK until this point — sync_runs did not exist when
# facts was created in Section 5.
_FACTS_SYNC_RUN_FK = (
    "ALTER TABLE facts ADD CONSTRAINT fk_facts_sync_run FOREIGN KEY (sync_run_id) REFERENCES sync_runs(sync_run_id)"
)


# ---------------------------------------------------------------------------
# Section 10 — progression
# ---------------------------------------------------------------------------

_PROGRESSION_DEFINITIONS_DDL = """
CREATE TABLE progression_definitions (
    progression_id   UUID PRIMARY KEY,
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_type      TEXT NOT NULL,
    definition       JSONB NOT NULL,
    is_advisory      BOOLEAN NOT NULL DEFAULT FALSE,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    UNIQUE (tenant_id, entity_type, t_valid_from)
)
"""

_PROGRESSION_DEFINITIONS_IDX = (
    "CREATE INDEX ix_progression_definitions_active ON progression_definitions (tenant_id, entity_type, t_valid_to)"
)

# audit_event_id is stored but not FK'd: audit_log is a partitioned table and
# Postgres does not support FK references to the root of one. Referential
# integrity is maintained at the service layer — the audit row is inserted
# first, then its id is used here.
_PROGRESSION_OVERRIDES_DDL = """
CREATE TABLE progression_overrides (
    override_id       UUID PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id         UUID NOT NULL REFERENCES entities(entity_id),
    from_state        TEXT NOT NULL,
    to_state          TEXT NOT NULL,
    gate_id           TEXT NOT NULL,
    bypass_skip_rules BOOLEAN NOT NULL DEFAULT FALSE,
    reason            TEXT NOT NULL,
    authorized_by     UUID NOT NULL REFERENCES actors(actor_id),
    t_valid_from      TIMESTAMPTZ NOT NULL,
    t_valid_to        TIMESTAMPTZ NOT NULL,
    consumed_at       TIMESTAMPTZ,
    audit_event_id    UUID NOT NULL
)
"""

_PROGRESSION_OVERRIDES_IDX = (
    "CREATE INDEX ix_progression_overrides_lookup ON progression_overrides "
    "(entity_id, from_state, to_state) WHERE consumed_at IS NULL"
)


# ---------------------------------------------------------------------------
# Section 11 — PII governance
# ---------------------------------------------------------------------------

_PII_PATTERNS_DDL = """
CREATE TABLE pii_patterns (
    pattern_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    regex           TEXT NOT NULL,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,
    detector_module TEXT,
    policy_override TEXT,
    is_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES actors(actor_id),
    CONSTRAINT chk_policy_override
        CHECK (policy_override IS NULL OR policy_override IN ('advisory', 'warn', 'block')),
    -- An entropy-based pattern (no single regex can express it) signals that
    -- through the sentinel regex value plus a detector_module naming the
    -- Python implementation. Any other pattern must not carry a module.
    CONSTRAINT chk_detector_module
        CHECK (detector_module IS NULL OR regex = '__entropy__')
)
"""

_PII_PATTERNS_IDX = "CREATE UNIQUE INDEX uq_pii_pattern_tenant_name ON pii_patterns (tenant_id, name)"

_PII_FIELD_POLICIES_DDL = """
CREATE TABLE pii_field_policies (
    policy_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(tenant_id),
    field_type TEXT NOT NULL,
    pattern_id UUID REFERENCES pii_patterns(pattern_id),
    policy     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_field_policy CHECK (policy IN ('advisory', 'warn', 'block'))
)
"""

_PII_FIELD_POLICIES_INDEXES = [
    # pattern_id may be NULL; COALESCE collapses it to the zero UUID so a
    # functional unique index can still express "one policy per (tenant,
    # field_type, pattern-or-none)".
    "CREATE UNIQUE INDEX uq_field_policy ON pii_field_policies "
    "(tenant_id, field_type, COALESCE(pattern_id, '00000000-0000-0000-0000-000000000000'::uuid))",
    "CREATE INDEX idx_pii_field_policy_tenant ON pii_field_policies (tenant_id, field_type)",
]

_PII_DETECTION_LOG_DDL = """
CREATE TABLE pii_detection_log (
    detection_id UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id     UUID REFERENCES actors(actor_id),
    target_type  TEXT NOT NULL,
    target_id    UUID,
    pattern_id   UUID REFERENCES pii_patterns(pattern_id),
    pattern_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    match_offset INTEGER,
    match_length INTEGER,
    action_taken TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (detection_id, ts)
) PARTITION BY RANGE (ts)
"""

_PII_DETECTION_LOG_INDEXES = [
    "CREATE INDEX idx_pii_log_tenant_ts ON pii_detection_log (tenant_id, ts DESC)",
    "CREATE INDEX idx_pii_log_target ON pii_detection_log "
    "(tenant_id, target_type, target_id, ts DESC) WHERE target_id IS NOT NULL",
]

# Seven built-in patterns, seeded is_system=TRUE against the default tenant.
# Each has a corresponding Python module; the DB row is the registry entry and
# the module is the authoritative implementation. aws_secret_key is
# entropy-based — no single regex expresses it, so it carries the
# '__entropy__' sentinel plus a detector_module instead.
_SYSTEM_PII_PATTERNS: list[tuple[str, str, str, str | None]] = [
    ("email", "email", r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", None),
    ("phone", "phone", r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)?\d{3}[\s.\-]?\d{4}", None),
    ("ssn", "ssn", r"\b(?!000|666|9\d{2})\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b", None),
    ("aws_access_key", "aws_access_key", r"AKIA[0-9A-Z]{16}", None),
    ("aws_secret_key", "aws_secret_key", "__entropy__", "fabric.security.pii_patterns.aws_secret_key"),
    ("jwt_token", "jwt_token", r"eyJ[a-zA-Z0-9_\-]+\.eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+", None),
    (
        "credit_card",
        "credit_card",
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b",
        None,
    ),
]

# Stable UUIDs so a re-run resolves the same rows via ON CONFLICT DO NOTHING.
_SYSTEM_PII_PATTERN_IDS: dict[str, str] = {
    "email": "a0000001-0000-0000-0000-000000000001",
    "phone": "a0000001-0000-0000-0000-000000000002",
    "ssn": "a0000001-0000-0000-0000-000000000003",
    "aws_access_key": "a0000001-0000-0000-0000-000000000004",
    "aws_secret_key": "a0000001-0000-0000-0000-000000000005",
    "jwt_token": "a0000001-0000-0000-0000-000000000006",
    "credit_card": "a0000001-0000-0000-0000-000000000007",
}


# ---------------------------------------------------------------------------
# Section 12 — idempotency
# ---------------------------------------------------------------------------

_IDEMPOTENCY_KEYS_DDL = """
CREATE TABLE idempotency_keys (
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    key              TEXT NOT NULL,
    method           TEXT NOT NULL,
    path             TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    response_status  INTEGER NOT NULL,
    response_body    JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, key, method, path)
)
"""

_IDEMPOTENCY_KEYS_IDX = "CREATE INDEX idx_idempotency_keys_expires ON idempotency_keys (expires_at)"


# ---------------------------------------------------------------------------
# Section 13 — session events: the observation substrate
# ---------------------------------------------------------------------------
#
# One immutable row per thing an agent did, scoped to (tenant_id, actor_id,
# session_id). Scoped by actor, not by entity visibility: a session is
# readable by exactly the actor who had it and nobody else, including
# colleagues in the same tenant — ordinary tenant-scoped visibility is wrong
# here, the same way it would be wrong for another actor's workspace content.
# Ordered by an allocated sequence rather than a timestamp, because a burst of
# events can share a created_at to the microsecond and a replay has to mean
# the order things actually happened in.

_MEMORY_SESSION_EVENTS_DDL = """
CREATE TABLE memory_session_events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id            UUID NOT NULL REFERENCES actors(actor_id),
    session_id          TEXT NOT NULL,
    seq                 BIGINT NOT NULL,
    kind                TEXT NOT NULL,
    body                TEXT NOT NULL,
    tool_name           TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    invalidated_at      TIMESTAMPTZ,
    invalidated_reason  TEXT,

    -- Event-corpus sizing: the denominator the compression ratio needs, in the
    -- same shape memory_claims records for the numerator.
    size_bytes          INTEGER NOT NULL,
    token_count         INTEGER,
    tokenizer_id        TEXT,

    CONSTRAINT ck_mse_kind CHECK (kind IN ('user_message', 'agent_action', 'tool_invocation')),
    CONSTRAINT ck_mse_session_len CHECK (char_length(session_id) BETWEEN 1 AND 200),
    -- Bytes, not characters: a 16 KB cap on characters would admit roughly
    -- four times that in multi-byte text, which is the text most likely to
    -- arrive from a real conversation.
    CONSTRAINT ck_mse_body_bytes CHECK (octet_length(body) <= 16384),
    -- A tool invocation with no tool name is unattributable; any other kind
    -- carrying one is a caller confusing the vocabulary.
    CONSTRAINT ck_mse_tool_name CHECK ((kind = 'tool_invocation') = (tool_name IS NOT NULL)),
    CONSTRAINT ck_mse_invalidation CHECK ((invalidated_at IS NULL) = (invalidated_reason IS NULL)),
    CONSTRAINT ck_mse_reason_len CHECK (
        invalidated_reason IS NULL OR char_length(invalidated_reason) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_mse_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_mse_tokenizer CHECK ((token_count IS NULL) = (tokenizer_id IS NULL)),
    CONSTRAINT uq_mse_session_seq UNIQUE (tenant_id, actor_id, session_id, seq)
)
"""

_MEMORY_SESSION_EVENTS_INDEXES = [
    "CREATE INDEX ix_mse_replay ON memory_session_events "
    "(tenant_id, actor_id, session_id, seq) WHERE invalidated_at IS NULL",
    "CREATE INDEX ix_mse_listing ON memory_session_events (tenant_id, actor_id, created_at DESC)",
    "CREATE INDEX ix_mse_expiry ON memory_session_events (expires_at) WHERE invalidated_at IS NULL",
    "CREATE INDEX ix_mse_metadata ON memory_session_events USING GIN (metadata)",
]


# ---------------------------------------------------------------------------
# Section 14 — memory claims substrate
# ---------------------------------------------------------------------------
#
# A claim is (subject_entity, predicate, value) with a declared type. Staged,
# and unmistakably so: nothing here is served through the capability read
# paths, and `status` is on every row so no reader loses track of which side
# of that line a claim is on. Every column below was added by a separate
# historical migration as the requirement grew from "store a typed triple" to
# "score, contest, confirm, and supersede one" — they are collected into one
# CREATE TABLE here because that growth is finished, not because it happened
# in one step.

_MEMORY_CLAIMS_DDL = """
CREATE TABLE memory_claims (
    claim_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The tenant that owns the *subject*, not the tenant that authored the
    -- claim. NULL exactly when the subject did not resolve (an unlinked
    -- claim has no owner to derive).
    owning_tenant_id     UUID REFERENCES tenants(tenant_id),
    author_tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    author_actor_id      UUID REFERENCES actors(actor_id),

    subject_entity_id    UUID REFERENCES entities(entity_id),
    subject_reference    TEXT NOT NULL,

    predicate            TEXT NOT NULL,
    value_type           TEXT NOT NULL,
    claim_category       TEXT NOT NULL,
    value_jsonb          JSONB NOT NULL,

    -- When the claim *holds* (asserted time), separate from when the store
    -- recorded it.
    asserted_valid_from  TIMESTAMPTZ NOT NULL,
    asserted_valid_to    TIMESTAMPTZ,

    status               TEXT NOT NULL,
    visibility            TEXT NOT NULL,
    source_authority      TEXT NOT NULL,

    -- The compression story's numerator: how much smaller the claim is than
    -- the text it came from.
    size_bytes           INTEGER NOT NULL,
    token_count          INTEGER,
    tokenizer_id         TEXT,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Which extraction namespace/strategy produced this claim. NULL for a
    -- claim from a connector or curator, which has neither.
    namespace            TEXT,
    strategy_id           TEXT,

    -- Declared cardinality (never inferred) of the predicate this claim uses,
    -- copied onto the row at write time so the disagreement sweep never
    -- re-reads the vocabulary. An entity-reference value resolves once, on
    -- the write path, for the same reason.
    value_cardinality    TEXT NOT NULL DEFAULT 'multi',
    value_entity_id       UUID REFERENCES entities(entity_id),

    -- Cached answer to "does an unresolved contest exist for this claim" —
    -- denormalized deliberately so promotion eligibility never depends on a
    -- join that could be forgotten.
    is_contested          BOOLEAN NOT NULL DEFAULT FALSE,

    -- Confidence: the estimated probability the claim is correct, stored with
    -- everything needed to re-derive it. NULL exactly when the claim has no
    -- subject (excluded from scoring).
    confidence            NUMERIC(4, 3),
    confidence_scored_at   TIMESTAMPTZ,
    confidence_inputs      JSONB,
    scorer_version         TEXT,
    calibration_version    TEXT,
    provider_confidence    NUMERIC(5, 4),
    decay_half_life_days   NUMERIC(8, 2),
    confidence_hold_until  TIMESTAMPTZ,

    -- Human confirmation. A person confirming a claim produces a *new* row
    -- (this one) that supersedes the one it confirms; confirms_claim_id is
    -- set on the new row, superseded_by on the old one.
    confirms_claim_id      UUID REFERENCES memory_claims(claim_id),
    confirmed_by           UUID REFERENCES actors(actor_id),
    confirmed_at            TIMESTAMPTZ,
    superseded_by          UUID REFERENCES memory_claims(claim_id),

    -- Bi-temporal closing: transaction time (t_invalidated_at) is distinct
    -- from asserted_valid_to — a claim can be superseded long after the fact
    -- it asserted ceased to be true.
    t_invalidated_at       TIMESTAMPTZ,
    superseded_reason      TEXT,

    -- When this claim was last reconciled against its neighbourhood, so a
    -- repeated consolidation sweep is a no-op rather than a second closure.
    consolidated_at        TIMESTAMPTZ,

    -- Promotion state is a separate axis from staging status: a claim an
    -- owner rejected stays `staged` and keeps serving. NULL means never
    -- through promotion at all — the common case.
    promotion_state         TEXT,

    CONSTRAINT ck_memory_claims_status CHECK (status IN ('staged', 'unlinked', 'superseded', 'rejected')),
    CONSTRAINT ck_memory_claims_unlinked CHECK ((subject_entity_id IS NULL) = (status = 'unlinked')),
    CONSTRAINT ck_memory_claims_owner CHECK ((owning_tenant_id IS NULL) = (subject_entity_id IS NULL)),
    CONSTRAINT ck_memory_claims_visibility CHECK (visibility IN ('private', 'tenant-shared', 'public')),
    CONSTRAINT ck_memory_claims_interval CHECK (asserted_valid_to IS NULL OR asserted_valid_to > asserted_valid_from),
    -- `null` is never a value: an unknown is the absence of a claim, not a
    -- claim of nothing.
    CONSTRAINT ck_memory_claims_value_not_null CHECK (jsonb_typeof(value_jsonb) <> 'null'),
    CONSTRAINT ck_memory_claims_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_memory_claims_tokenizer CHECK ((token_count IS NULL) = (tokenizer_id IS NULL)),
    CONSTRAINT ck_memory_claims_namespace CHECK ((namespace IS NULL) = (strategy_id IS NULL)),
    CONSTRAINT ck_memory_claims_value_cardinality CHECK (value_cardinality IN ('single', 'multi')),
    CONSTRAINT ck_memory_claims_value_entity CHECK (value_entity_id IS NULL OR value_type = 'entity_ref'),
    CONSTRAINT ck_memory_claims_authority CHECK (
        source_authority IN (
            'owner_human', 'owner_extraction', 'owner_inference',
            'observer_human', 'observer_extraction', 'observer_inference',
            'unattributed'
        )
    ),
    -- An authority with no owner to compare against exists exactly when
    -- there is no subject to derive an owner from.
    CONSTRAINT ck_memory_claims_unattributed CHECK (
        (source_authority = 'unattributed') = (subject_entity_id IS NULL)
    ),
    CONSTRAINT ck_memory_claims_confidence_scored CHECK ((confidence IS NULL) = (status = 'unlinked')),
    -- A score without its inputs cannot be re-derived, and a score nobody can
    -- re-derive is not auditable.
    CONSTRAINT ck_memory_claims_confidence_paired CHECK (
        (confidence IS NULL) = (confidence_scored_at IS NULL)
        AND (confidence IS NULL) = (confidence_inputs IS NULL)
        AND (confidence IS NULL) = (scorer_version IS NULL)
        AND (confidence IS NULL) = (calibration_version IS NULL)
        AND (confidence IS NULL) = (decay_half_life_days IS NULL)
    ),
    -- Never below the decay floor, so ageing is monotone non-increasing and a
    -- minimum-confidence query can prefilter on the indexed column.
    CONSTRAINT ck_memory_claims_confidence_range CHECK (
        confidence IS NULL OR (confidence >= 0.100 AND confidence <= 0.980)
    ),
    CONSTRAINT ck_memory_claims_half_life CHECK (decay_half_life_days IS NULL OR decay_half_life_days > 0),
    CONSTRAINT ck_memory_claims_provider_confidence CHECK (
        provider_confidence IS NULL OR (provider_confidence >= 0 AND provider_confidence <= 1)
    ),
    CONSTRAINT ck_memory_claims_confirmation CHECK (
        (confirms_claim_id IS NULL) = (confirmed_by IS NULL)
        AND (confirms_claim_id IS NULL) = (confirmed_at IS NULL)
    ),
    CONSTRAINT ck_memory_claims_confirms_other CHECK (confirms_claim_id IS NULL OR confirms_claim_id <> claim_id),
    CONSTRAINT ck_memory_claims_supersedes_other CHECK (superseded_by IS NULL OR superseded_by <> claim_id),
    CONSTRAINT ck_memory_claims_invalidated CHECK ((t_invalidated_at IS NULL) = (status <> 'superseded')),
    CONSTRAINT ck_memory_claims_superseded_reason CHECK (
        superseded_reason IS NULL
        OR superseded_reason IN ('lost_conflict', 'cluster_collapsed', 'human_confirmed', 'curator_replaced')
    ),
    -- A closed claim names its successor — a chain with a gap cannot be
    -- walked, and walking it is the whole point.
    CONSTRAINT ck_memory_claims_superseded_has_successor CHECK (
        status <> 'superseded' OR superseded_by IS NOT NULL
    ),
    CONSTRAINT ck_memory_claims_promotion_state CHECK (
        promotion_state IS NULL OR promotion_state IN ('proposed', 'promoted', 'rejected', 'reversed')
    )
)
"""

_MEMORY_CLAIMS_INDEXES = [
    "CREATE INDEX ix_memory_claims_subject_predicate ON memory_claims "
    "(subject_entity_id, predicate) WHERE status = 'staged'",
    "CREATE INDEX ix_memory_claims_unlinked ON memory_claims (author_tenant_id, created_at) WHERE status = 'unlinked'",
    "CREATE INDEX ix_memory_claims_owning_tenant ON memory_claims (owning_tenant_id, predicate)",
    "CREATE INDEX ix_memory_claims_author ON memory_claims (author_actor_id)",
    "CREATE INDEX ix_memory_claims_namespace ON memory_claims (namespace) WHERE namespace IS NOT NULL",
    "CREATE INDEX ix_memory_claims_strategy ON memory_claims (strategy_id, created_at) WHERE strategy_id IS NOT NULL",
    "CREATE INDEX ix_memory_claims_contested ON memory_claims "
    "(subject_entity_id, predicate) WHERE is_contested AND status = 'staged'",
    # The disagreement sweep only ever reads single-valued predicates.
    "CREATE INDEX ix_memory_claims_single_valued ON memory_claims "
    "(subject_entity_id, predicate, asserted_valid_from) WHERE status = 'staged' AND value_cardinality = 'single'",
    "CREATE INDEX ix_memory_claims_confidence ON memory_claims "
    "(subject_entity_id, confidence DESC) WHERE status = 'staged'",
    "CREATE INDEX ix_memory_claims_confirms ON memory_claims (confirms_claim_id) WHERE confirms_claim_id IS NOT NULL",
    "CREATE INDEX ix_memory_claims_live ON memory_claims "
    "(subject_entity_id, predicate) WHERE status = 'staged' AND superseded_by IS NULL",
    "CREATE INDEX ix_memory_claims_as_of ON memory_claims "
    "(subject_entity_id, predicate, created_at, t_invalidated_at)",
    "CREATE INDEX ix_memory_claims_superseded_by ON memory_claims (superseded_by) WHERE superseded_by IS NOT NULL",
    # One live claim per successor — what makes a repeated consolidation
    # sweep a no-op rather than a second closure.
    "CREATE UNIQUE INDEX uq_memory_claims_one_closure ON memory_claims (claim_id) WHERE status = 'superseded'",
    "CREATE INDEX ix_memory_claims_unconsolidated ON memory_claims "
    "(subject_entity_id, predicate) WHERE status = 'staged' AND consolidated_at IS NULL",
    "CREATE INDEX ix_memory_claims_subject_authority ON memory_claims "
    "(subject_entity_id, predicate, source_authority) WHERE status = 'staged'",
]

# One mechanism, both directions: a claim resolves to its evidence, and a
# piece of evidence resolves to everything derived from it — the reverse is
# what an erasure request needs and what an array column on the claim could
# not index.
_MEMORY_CLAIM_PROVENANCE_DDL = """
CREATE TABLE memory_claim_provenance (
    claim_id          UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    evidence_kind     TEXT NOT NULL,
    evidence_ref      TEXT NOT NULL,
    evidence_excerpt  TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Corroboration bookkeeping: which independent-source class this evidence
    -- belongs to, stored as a digest so an erasure request can remove the
    -- identifiers it came from without retracting the corroboration itself.
    independence_key    TEXT,
    independence_group   TEXT,

    -- Derivation tier for this specific piece of evidence — authority is a
    -- minimum over a claim's evidence, and this is the per-row input that
    -- minimum is taken over.
    derivation           TEXT NOT NULL DEFAULT 'inference',

    PRIMARY KEY (claim_id, evidence_kind, evidence_ref),

    CONSTRAINT ck_memory_prov_kind CHECK (
        evidence_kind IN (
            'session_event', 'document_revision', 'commit', 'work_item',
            'connector_run', 'curator', 'incident'
        )
    ),
    CONSTRAINT ck_memory_prov_independence CHECK ((independence_key IS NULL) = (independence_group IS NULL)),
    CONSTRAINT ck_memory_prov_derivation CHECK (derivation IN ('human', 'extraction', 'inference'))
)
"""

_MEMORY_CLAIM_PROVENANCE_INDEXES = [
    "CREATE INDEX ix_memory_prov_evidence ON memory_claim_provenance (evidence_kind, evidence_ref)",
    "CREATE INDEX ix_memory_prov_independence ON memory_claim_provenance "
    "(claim_id, independence_key) WHERE independence_key IS NOT NULL",
]

# Two claims about one subject, under one single-valued predicate, with
# values that cannot both hold — recorded so silent coexistence never serves
# two answers to one question with nothing indicating that it is doing so.
_MEMORY_CLAIM_CONTEST_DDL = """
CREATE TABLE memory_claim_contest (
    contest_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Ordered so one disagreement is one row, regardless of how many times
    -- the sweep revisits it.
    lower_claim_id    UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    upper_claim_id    UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    subject_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    predicate         TEXT NOT NULL,
    lower_value       JSONB NOT NULL,
    upper_value       JSONB NOT NULL,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    resolution        TEXT,

    CONSTRAINT uq_memory_contest_pair UNIQUE (lower_claim_id, upper_claim_id),
    CONSTRAINT ck_memory_contest_ordered CHECK (lower_claim_id < upper_claim_id),
    CONSTRAINT ck_memory_contest_resolution CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    CONSTRAINT ck_memory_contest_resolution_value CHECK (
        resolution IS NULL OR resolution IN ('superseded', 'both_retained', 'dismissed', 'claim_withdrawn')
    )
)
"""

_MEMORY_CLAIM_CONTEST_INDEXES = [
    "CREATE INDEX ix_memory_contest_lower ON memory_claim_contest (lower_claim_id) WHERE resolved_at IS NULL",
    "CREATE INDEX ix_memory_contest_upper ON memory_claim_contest (upper_claim_id) WHERE resolved_at IS NULL",
    "CREATE INDEX ix_memory_contest_subject ON memory_claim_contest "
    "(subject_entity_id, predicate) WHERE resolved_at IS NULL",
]

# Which claims were collapsed into which survivor. A separate table rather
# than a column because a collapse is many-to-one, and the merged provenance
# must remain attributable after collapsing many phrasings into one.
_MEMORY_CLAIM_CLUSTER_DDL = """
CREATE TABLE memory_claim_cluster (
    cluster_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    survivor_claim_id    UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    collapsed_claim_id   UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    similarity           NUMERIC(4, 3) NOT NULL,
    matched_by            TEXT NOT NULL,
    collapsed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_memory_cluster_pair UNIQUE (survivor_claim_id, collapsed_claim_id),
    CONSTRAINT ck_memory_cluster_distinct CHECK (survivor_claim_id <> collapsed_claim_id),
    CONSTRAINT ck_memory_cluster_similarity CHECK (similarity >= 0 AND similarity <= 1),
    CONSTRAINT ck_memory_cluster_matched_by CHECK (matched_by IN ('exact_value', 'semantic'))
)
"""

_MEMORY_CLAIM_CLUSTER_INDEXES = [
    "CREATE INDEX ix_memory_cluster_survivor ON memory_claim_cluster (survivor_claim_id)",
    "CREATE INDEX ix_memory_cluster_collapsed ON memory_claim_cluster (collapsed_claim_id)",
]

# Per-tenant confidence-scoring weights. A missing row means the shipped
# defaults, not a disabled feature. The authority ladder (base_owner_human >
# base_owner_extraction > ... ) is deployment-wide: a tenant may move the
# weights apart or together but may not invert them.
_MEMORY_CONFIDENCE_POLICY_DDL = """
CREATE TABLE memory_confidence_policy (
    policy_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(tenant_id),

    base_owner_human          NUMERIC(4, 3) NOT NULL DEFAULT 0.800,
    base_owner_extraction     NUMERIC(4, 3) NOT NULL DEFAULT 0.620,
    base_owner_inference      NUMERIC(4, 3) NOT NULL DEFAULT 0.450,
    base_observer_human       NUMERIC(4, 3) NOT NULL DEFAULT 0.420,
    base_observer_extraction  NUMERIC(4, 3) NOT NULL DEFAULT 0.320,
    base_observer_inference   NUMERIC(4, 3) NOT NULL DEFAULT 0.230,

    corroboration_headroom    NUMERIC(4, 3) NOT NULL DEFAULT 0.600,
    corroboration_scale       NUMERIC(4, 2) NOT NULL DEFAULT 2.00,
    contradiction_penalty     NUMERIC(4, 3) NOT NULL DEFAULT 0.250,
    confirmed_confidence      NUMERIC(4, 3) NOT NULL DEFAULT 0.920,
    confirmation_hold_days    INTEGER       NOT NULL DEFAULT 180,

    -- A multiplier on the shipped half-life, never the half-life itself.
    decay_multiplier          NUMERIC(4, 2) NOT NULL DEFAULT 1.00,

    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by                UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_memory_confidence_policy UNIQUE (tenant_id),
    CONSTRAINT ck_memory_confidence_ladder CHECK (
        base_owner_human > base_owner_extraction
        AND base_owner_extraction > base_owner_inference
        AND base_owner_inference > base_observer_human
        AND base_observer_human > base_observer_extraction
        AND base_observer_extraction > base_observer_inference
        AND base_observer_inference >= 0.100
        AND base_owner_human <= 0.980
    ),
    CONSTRAINT ck_memory_confidence_bounds CHECK (
        corroboration_headroom > 0 AND corroboration_headroom <= 0.800
        AND corroboration_scale >= 0.50 AND corroboration_scale <= 10.00
        AND contradiction_penalty >= 0 AND contradiction_penalty <= 0.800
        AND confirmed_confidence >= 0.850 AND confirmed_confidence <= 0.980
        AND confirmation_hold_days >= 1 AND confirmation_hold_days <= 730
        AND decay_multiplier >= 0.25 AND decay_multiplier <= 4.00
    )
)
"""

_MEMORY_CONFIDENCE_POLICY_IDX = (
    "CREATE INDEX ix_memory_confidence_policy_tenant ON memory_confidence_policy (tenant_id)"
)

# Per-tenant extraction strategy configuration. Enablement, confidence floor,
# prompt, and model are overridable; the output schema, permitted predicate
# set, and namespace template are not — those live in code so a tenant cannot
# redefine the shared vocabulary from a configuration field.
_MEMORY_STRATEGY_CONFIG_DDL = """
CREATE TABLE memory_strategy_config (
    config_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    strategy_id       TEXT NOT NULL,
    is_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    -- Zero disables the floor, the honest default while confidence is
    -- uncalibrated: a floor applied to an uncalibrated number filters by
    -- noise rather than by quality.
    confidence_floor  NUMERIC(4, 3) NOT NULL DEFAULT 0.000,
    prompt_override   TEXT,
    model_override    TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by        UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_memory_strategy_config UNIQUE (tenant_id, strategy_id),
    CONSTRAINT ck_memory_strategy_floor CHECK (confidence_floor >= 0 AND confidence_floor <= 1),
    -- An empty override is not an override — it would silently give the
    -- model no instructions at all.
    CONSTRAINT ck_memory_strategy_prompt CHECK (
        prompt_override IS NULL OR char_length(trim(prompt_override)) > 0
    ),
    CONSTRAINT ck_memory_strategy_model CHECK (
        model_override IS NULL OR char_length(trim(model_override)) > 0
    )
)
"""

_MEMORY_STRATEGY_CONFIG_IDX = "CREATE INDEX ix_memory_strategy_config_tenant ON memory_strategy_config (tenant_id)"

# The extraction outbox: session events queued for a provider, keyed one row
# per (session, strategy) rather than per event — a burst of ten events in
# one session upserts the window into one pending job, not ten.
_MEMORY_EXTRACTION_OUTBOX_DDL = """
CREATE TABLE memory_extraction_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id        UUID NOT NULL REFERENCES actors(actor_id),
    session_id      TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    from_seq        BIGINT NOT NULL,
    through_seq     BIGINT NOT NULL,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- NULL means eligible now; set to a future instant while backing off.
    next_attempt_at TIMESTAMPTZ,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,

    CONSTRAINT uq_memory_outbox_session_strategy UNIQUE (tenant_id, actor_id, session_id, strategy_id),
    CONSTRAINT ck_memory_outbox_window CHECK (through_seq >= from_seq),
    CONSTRAINT ck_memory_outbox_attempts CHECK (attempts >= 0)
)
"""

_MEMORY_EXTRACTION_OUTBOX_INDEXES = [
    "CREATE INDEX ix_memory_outbox_ready ON memory_extraction_outbox (enqueued_at) WHERE next_attempt_at IS NULL",
    "CREATE INDEX ix_memory_outbox_retry ON memory_extraction_outbox (next_attempt_at) "
    "WHERE next_attempt_at IS NOT NULL",
    "CREATE INDEX ix_memory_outbox_actor ON memory_extraction_outbox (tenant_id, actor_id)",
]

_MEMORY_EXTRACTION_OUTBOX_FAILED_DDL = """
CREATE TABLE memory_extraction_outbox_failed (
    failed_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id     UUID NOT NULL REFERENCES actors(actor_id),
    session_id   TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    from_seq     BIGINT NOT NULL,
    through_seq  BIGINT NOT NULL,
    attempts     INTEGER NOT NULL,
    last_error   TEXT NOT NULL,
    enqueued_at  TIMESTAMPTZ NOT NULL,
    failed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_MEMORY_EXTRACTION_OUTBOX_FAILED_INDEXES = [
    "CREATE INDEX ix_memory_outbox_failed_tenant ON memory_extraction_outbox_failed (tenant_id, failed_at)",
    "CREATE INDEX ix_memory_outbox_failed_strategy ON memory_extraction_outbox_failed (strategy_id, failed_at)",
    "CREATE INDEX ix_memory_outbox_failed_actor ON memory_extraction_outbox_failed (tenant_id, actor_id)",
]


# ---------------------------------------------------------------------------
# Section 15 — calibration
# ---------------------------------------------------------------------------
#
# A provider's self-reported confidence is not a probability until something
# has checked it. memory_claim_adjudication is the judged-outcomes table that
# makes leaving the uncalibrated state possible at all; memory_calibration_mapping
# is what a fit from those outcomes produces. Deployment-wide, no tenant
# column: the thing being measured is a shared model, and a tenant cannot
# recalibrate somebody else's.

_MEMORY_CLAIM_ADJUDICATION_DDL = """
CREATE TABLE memory_claim_adjudication (
    adjudication_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_id             UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,
    adjudicated_by       UUID NOT NULL REFERENCES actors(actor_id),

    verdict              TEXT NOT NULL,

    -- What the reviewer was actually looking at, aged to that instant —
    -- recorded rather than recomputed, since a score works out differently at
    -- a different time.
    observed_confidence  NUMERIC(4, 3) NOT NULL,
    observed_bucket      TEXT NOT NULL,
    calibration_version  TEXT NOT NULL,
    provider_confidence  NUMERIC(5, 4),
    source_authority     TEXT NOT NULL,

    note                 TEXT,
    adjudicated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_memory_adjudication UNIQUE (claim_id, adjudicated_by),
    -- Three outcomes: a reviewer who cannot tell has said something, and
    -- folding it into "incorrect" would bias every fit downward.
    CONSTRAINT ck_memory_adjudication_verdict CHECK (verdict IN ('correct', 'incorrect', 'undecidable')),
    CONSTRAINT ck_memory_adjudication_confidence CHECK (observed_confidence >= 0 AND observed_confidence <= 1)
)
"""

_MEMORY_CLAIM_ADJUDICATION_INDEXES = [
    "CREATE INDEX ix_memory_adjudication_claim ON memory_claim_adjudication (claim_id)",
    "CREATE INDEX ix_memory_adjudication_fit ON memory_claim_adjudication (calibration_version, adjudicated_at)",
    "CREATE INDEX ix_memory_adjudication_tenant ON memory_claim_adjudication (tenant_id, adjudicated_at)",
]

# There is no mapping until one is fitted, and the honest form of "no mapping"
# is no row — never an identity mapping, which would assert an unexamined
# correspondence nobody checked. A fit that misses the accuracy target is
# stored as 'failed' rather than discarded, so "why is this still
# uncalibrated" stays answerable.
_MEMORY_CALIBRATION_MAPPING_DDL = """
CREATE TABLE memory_calibration_mapping (
    mapping_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    provider_id     TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    version         TEXT NOT NULL,

    -- Observed correctness per input bin, smoothed toward the pooled rate —
    -- a bin's value is a sentence anybody can check, which is the audit
    -- record a fitted curve would not be.
    bins            JSONB NOT NULL,
    n_adjudicated   INTEGER NOT NULL,
    measured_error  NUMERIC(4, 3) NOT NULL,

    -- 'active' is the one scoring reads.
    status          TEXT NOT NULL,

    fitted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    fitted_by       UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_memory_calibration_version UNIQUE (version),
    CONSTRAINT ck_memory_calibration_status CHECK (status IN ('active', 'superseded', 'failed')),
    -- The token meaning "nothing has been fitted" must never be claimable by
    -- a row.
    CONSTRAINT ck_memory_calibration_not_sentinel CHECK (version <> 'uncalibrated'),
    CONSTRAINT ck_memory_calibration_n CHECK (n_adjudicated >= 200),
    CONSTRAINT ck_memory_calibration_error CHECK (status <> 'active' OR measured_error <= 0.150)
)
"""

_MEMORY_CALIBRATION_MAPPING_INDEXES = [
    # One active fit per (provider, model, strategy) — keyed on the model so
    # changing it matches nothing and scoring reverts to uncalibrated on its
    # own.
    "CREATE UNIQUE INDEX uq_memory_calibration_active ON memory_calibration_mapping "
    "(provider_id, model_id, strategy_id) WHERE status = 'active'",
    "CREATE INDEX ix_memory_calibration_fitted ON memory_calibration_mapping (fitted_at DESC)",
]


# ---------------------------------------------------------------------------
# Section 16 — promotion
# ---------------------------------------------------------------------------
#
# Promotion is what turns a staged claim into a canonical attribute or edge.
# The journal records what a promotion observed, not just what it wrote, so
# a reversal restores the exact prior state rather than "the previous value"
# — a distinction that matters the moment two promotions touch the same row.

_MEMORY_PROMOTION_PROPOSAL_DDL = """
CREATE TABLE memory_promotion_proposal (
    proposal_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id             UUID NOT NULL REFERENCES memory_claims(claim_id),

    -- The tenant that must act — the subject's owner, never the author.
    owner_tenant_id      UUID NOT NULL REFERENCES tenants(tenant_id),
    author_tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    subject_entity_id    UUID NOT NULL REFERENCES entities(entity_id),
    predicate            TEXT NOT NULL,
    target_kind          TEXT NOT NULL,
    target_key           TEXT NOT NULL,
    mapping_version      INTEGER NOT NULL,

    current_value        JSONB,
    proposed_value       JSONB NOT NULL,

    valid_from           TIMESTAMPTZ NOT NULL,
    valid_to             TIMESTAMPTZ,

    high_impact_reasons  JSONB NOT NULL DEFAULT '[]'::JSONB,

    state                TEXT NOT NULL DEFAULT 'open',
    decided_by           UUID REFERENCES actors(actor_id),
    decided_at           TIMESTAMPTZ,
    decision_reason      TEXT,
    -- Set only on accept-with-amendment; both this and proposed_value are
    -- kept, so the record shows what was proposed as well as what was
    -- actually promoted.
    amended_value        JSONB,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_memory_proposal_state CHECK (
        state IN ('open', 'accepted', 'amended', 'rejected', 'withdrawn')
    ),
    CONSTRAINT ck_memory_proposal_target CHECK (target_kind IN ('attribute', 'edge')),
    CONSTRAINT ck_memory_proposal_decision CHECK (
        (state = 'open') = (decided_at IS NULL) AND (decided_at IS NULL) = (decided_by IS NULL)
    ),
    CONSTRAINT ck_memory_proposal_reject_reason CHECK (
        state <> 'rejected' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    CONSTRAINT ck_memory_proposal_amendment CHECK ((amended_value IS NOT NULL) = (state = 'amended')),
    CONSTRAINT ck_memory_proposal_interval CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_memory_proposal_reasons CHECK (jsonb_typeof(high_impact_reasons) = 'array')
)
"""

_MEMORY_PROMOTION_PROPOSAL_INDEXES = [
    "CREATE INDEX ix_memory_proposal_owner_open ON memory_promotion_proposal "
    "(owner_tenant_id, created_at) WHERE state = 'open'",
    "CREATE INDEX ix_memory_proposal_claim ON memory_promotion_proposal (claim_id)",
    # One open proposal per claim — a claim queued twice would let two
    # reviewers decide the same thing differently.
    "CREATE UNIQUE INDEX uq_memory_proposal_open_per_claim ON memory_promotion_proposal (claim_id) "
    "WHERE state = 'open'",
]

_MEMORY_PROMOTION_JOURNAL_DDL = """
CREATE TABLE memory_promotion_journal (
    promotion_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id          UUID NOT NULL REFERENCES memory_promotion_proposal(proposal_id),
    claim_id             UUID NOT NULL REFERENCES memory_claims(claim_id),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),

    target_kind          TEXT NOT NULL,
    -- The canonical row this promotion created, and the one it closed — both
    -- by id, since matching on value could not tell two identical values
    -- apart.
    created_row_id       UUID NOT NULL,
    superseded_row_id     UUID,
    superseded_valid_to   TIMESTAMPTZ,

    promoted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_by          UUID REFERENCES actors(actor_id),

    reversed_at          TIMESTAMPTZ,
    reversed_by          UUID REFERENCES actors(actor_id),
    reversal_reason      TEXT,

    CONSTRAINT ck_memory_journal_target CHECK (target_kind IN ('attribute', 'edge')),
    CONSTRAINT ck_memory_journal_reversal CHECK ((reversed_at IS NULL) = (reversed_by IS NULL)),
    CONSTRAINT ck_memory_journal_superseded CHECK (
        superseded_row_id IS NOT NULL OR superseded_valid_to IS NULL
    )
)
"""

_MEMORY_PROMOTION_JOURNAL_INDEXES = [
    "CREATE INDEX ix_memory_journal_claim ON memory_promotion_journal (claim_id)",
    "CREATE INDEX ix_memory_journal_created_row ON memory_promotion_journal (created_row_id)",
    "CREATE INDEX ix_memory_journal_live ON memory_promotion_journal (tenant_id, promoted_at) "
    "WHERE reversed_at IS NULL",
]

# One row per predicate a tenant has explicitly opted in. No wildcard row on
# purpose — a wildcard is how an allowlist stops being one.
_MEMORY_AUTOPROMOTE_ALLOWLIST_DDL = """
CREATE TABLE memory_autopromote_allowlist (
    entry_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    predicate   TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by    UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_memory_autopromote UNIQUE (tenant_id, predicate),
    CONSTRAINT ck_memory_autopromote_predicate CHECK (char_length(trim(predicate)) > 0 AND predicate <> '*')
)
"""

# Per-tenant review policy. A missing row means the cautious defaults: review
# needed, low blast-radius threshold.
_MEMORY_PROMOTION_POLICY_DDL = """
CREATE TABLE memory_promotion_policy (
    tenant_id                UUID PRIMARY KEY REFERENCES tenants(tenant_id),
    blast_radius_threshold   INTEGER NOT NULL DEFAULT 5,
    always_review            JSONB NOT NULL DEFAULT '[]'::JSONB,
    confidence_floor         NUMERIC(4, 3) NOT NULL DEFAULT 0.000,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by               UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_memory_policy_threshold CHECK (blast_radius_threshold >= 0),
    CONSTRAINT ck_memory_policy_floor CHECK (confidence_floor >= 0 AND confidence_floor <= 1),
    CONSTRAINT ck_memory_policy_always_review CHECK (jsonb_typeof(always_review) = 'array')
)
"""

# Keyed by what was asserted rather than by which row asserted it, so
# restating the same thing lands here instead of queueing a fresh proposal.
_MEMORY_PROMOTION_REJECTION_DDL = """
CREATE TABLE memory_promotion_rejection (
    rejection_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    subject_entity_id   UUID NOT NULL REFERENCES entities(entity_id),
    predicate           TEXT NOT NULL,
    value_digest        TEXT NOT NULL,
    -- A stronger source may revive the assertion; an equal or weaker one may
    -- not.
    rejected_authority   TEXT NOT NULL,

    reason              TEXT NOT NULL,
    proposal_id         UUID NOT NULL REFERENCES memory_promotion_proposal(proposal_id),
    rejected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    rejected_by          UUID REFERENCES actors(actor_id),

    -- Deliberately not keyed on the asserted interval — a restatement
    -- naturally carries a later timestamp.
    CONSTRAINT uq_memory_rejection UNIQUE (tenant_id, subject_entity_id, predicate, value_digest),
    CONSTRAINT ck_memory_rejection_reason CHECK (char_length(trim(reason)) > 0)
)
"""


# ---------------------------------------------------------------------------
# Section 17 — capability requests and source governance
# ---------------------------------------------------------------------------
#
# A request is not a claim: it expresses what somebody needs rather than what
# is true, so it is never scored, decayed, or consolidated. source_governance
# is the companion concern from the same original migration — the ingest
# ceiling a connector may not exceed before its authority tier is trusted.

_MEMORY_CAPABILITY_REQUEST_DDL = """
CREATE TABLE memory_capability_request (
    request_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    owner_tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    requester_tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    requester_actor_id      UUID REFERENCES actors(actor_id),

    subject_entity_id       UUID NOT NULL REFERENCES entities(entity_id),

    request_category        TEXT NOT NULL,
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL,

    status                  TEXT NOT NULL DEFAULT 'raised',
    decided_by              UUID REFERENCES actors(actor_id),
    decided_at               TIMESTAMPTZ,
    decision_reason          TEXT,

    -- Where a request led to a graph change, the promotion that made it —
    -- closes the loop visibly for the requester.
    resulting_promotion_id  UUID REFERENCES memory_promotion_journal(promotion_id),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_memory_request_status CHECK (
        status IN ('raised', 'acknowledged', 'accepted', 'declined', 'duplicate', 'resolved')
    ),
    CONSTRAINT ck_memory_request_title CHECK (char_length(trim(title)) > 0),
    CONSTRAINT ck_memory_request_body CHECK (char_length(trim(body)) > 0),
    -- A decline with no reason reads as neglect from the requester's side.
    CONSTRAINT ck_memory_request_decline_reason CHECK (
        status <> 'declined' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    CONSTRAINT ck_memory_request_duplicate_reason CHECK (
        status <> 'duplicate' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    CONSTRAINT ck_memory_request_decided CHECK ((decided_at IS NULL) = (decided_by IS NULL)),
    CONSTRAINT ck_memory_request_promotion CHECK (
        resulting_promotion_id IS NULL OR status IN ('accepted', 'resolved')
    )
)
"""

_MEMORY_CAPABILITY_REQUEST_INDEXES = [
    "CREATE INDEX ix_memory_request_owner_open ON memory_capability_request "
    "(owner_tenant_id, created_at) WHERE status IN ('raised', 'acknowledged')",
    "CREATE INDEX ix_memory_request_requester ON memory_capability_request (requester_tenant_id, created_at)",
    "CREATE INDEX ix_memory_request_subject ON memory_capability_request (subject_entity_id)",
]

# Append-only lifecycle history, ordered by an insertion sequence rather than
# by occurred_at — two transitions can legitimately share a timestamp.
_MEMORY_REQUEST_TRANSITION_DDL = """
CREATE TABLE memory_request_transition (
    transition_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq            BIGSERIAL NOT NULL,
    request_id     UUID NOT NULL REFERENCES memory_capability_request(request_id) ON DELETE CASCADE,
    from_status    TEXT NOT NULL,
    to_status      TEXT NOT NULL,
    reason         TEXT,
    actor_id       UUID REFERENCES actors(actor_id),
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_memory_transition_moves CHECK (from_status <> to_status)
)
"""

_MEMORY_REQUEST_TRANSITION_IDX = (
    "CREATE INDEX ix_memory_transition_request ON memory_request_transition (request_id, seq)"
)

# One row per source a tenant has registered. authority_tier has no default —
# a source whose tier was implicit would inherit whatever the code happened to
# pass. Breaker state lives on the row so it survives a process restart.
_MEMORY_SOURCE_GOVERNANCE_DDL = """
CREATE TABLE memory_source_governance (
    source_id           UUID PRIMARY KEY REFERENCES sync_sources(source_id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),

    authority_tier       TEXT NOT NULL,

    ingest_ceiling       INTEGER NOT NULL DEFAULT 1000,
    window_seconds       INTEGER NOT NULL DEFAULT 3600,

    breaker_open_until   TIMESTAMPTZ,
    breach_count         INTEGER NOT NULL DEFAULT 0,

    window_started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_count         INTEGER NOT NULL DEFAULT 0,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by           UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_memory_source_authority CHECK (
        authority_tier IN (
            'owner_human', 'owner_extraction', 'owner_inference',
            'observer_human', 'observer_extraction', 'observer_inference',
            'unattributed'
        )
    ),
    CONSTRAINT ck_memory_source_ceiling CHECK (ingest_ceiling > 0),
    CONSTRAINT ck_memory_source_window CHECK (window_seconds > 0),
    CONSTRAINT ck_memory_source_counts CHECK (window_count >= 0 AND breach_count >= 0)
)
"""

_MEMORY_SOURCE_GOVERNANCE_INDEXES = [
    "CREATE INDEX ix_memory_source_governance_tenant ON memory_source_governance (tenant_id)",
    "CREATE INDEX ix_memory_source_governance_open ON memory_source_governance "
    "(breaker_open_until) WHERE breaker_open_until IS NOT NULL",
]


# ---------------------------------------------------------------------------
# Section 18 — usage
# ---------------------------------------------------------------------------
#
# usage_events is structurally incapable of holding content: every column is
# an identifier, a timestamp, a number, or a term from a set fixed in
# `registry/usage/vocabularies.py`. The vocabularies are duplicated here as
# SQL literals on purpose, matching the original migration's own reasoning —
# a migration must be readable and runnable at the revision it was written,
# without importing application code that may have moved on.
# `tests/conformance/test_usage_schema.py` asserts these and the vocabularies
# module describe the same sets, so the duplication is checked rather than
# trusted.

_USAGE_SURFACES = "'rest','mcp'"
_USAGE_OUTCOMES = "'ok','error'"
_USAGE_STATUS_CLASSES = "'2xx','3xx','4xx','5xx','other'"

_USAGE_EVENTS_DDL = f"""
CREATE TABLE usage_events (
    event_id           UUID        NOT NULL,
    occurred_at        TIMESTAMPTZ NOT NULL,
    tenant_id          UUID        NOT NULL,
    -- Nullable by design: a request that fails to authenticate has no actor,
    -- and dropping the row instead would quietly change the denominator of
    -- every rate computed from this table.
    actor_id           UUID,
    surface            TEXT        NOT NULL,
    operation          TEXT        NOT NULL,
    outcome            TEXT        NOT NULL,
    status_class       TEXT        NOT NULL,
    latency_ms         INTEGER     NOT NULL,
    result_count       INTEGER,
    payload_bytes      INTEGER,
    payload_tokens     INTEGER,
    request_id         TEXT,
    subject_entity_ids UUID[]      NOT NULL DEFAULT '{{}}',
    -- Digest, length, and result count only — there is no column for the
    -- search terms themselves.
    query_digest       TEXT,
    query_length       INTEGER,
    CONSTRAINT chk_usage_surface      CHECK (surface IN ({_USAGE_SURFACES})),
    CONSTRAINT chk_usage_outcome      CHECK (outcome IN ({_USAGE_OUTCOMES})),
    CONSTRAINT chk_usage_status_class CHECK (status_class IN ({_USAGE_STATUS_CLASSES})),
    CONSTRAINT chk_usage_query_digest CHECK (query_digest IS NULL OR char_length(query_digest) = 64),
    CONSTRAINT chk_usage_query_length CHECK (query_length IS NULL OR query_length >= 0),
    CONSTRAINT chk_usage_latency      CHECK (latency_ms >= 0),
    PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at)
"""

_USAGE_EVENTS_INDEXES = [
    "CREATE INDEX idx_usage_tenant_time ON usage_events (tenant_id, occurred_at DESC)",
    "CREATE INDEX idx_usage_operation ON usage_events (tenant_id, surface, operation, occurred_at DESC)",
    "CREATE INDEX idx_usage_actor ON usage_events (actor_id, occurred_at DESC) WHERE actor_id IS NOT NULL",
]

# Actor-free rollups the aggregate API reads; kept forever. An aggregate with
# no actor identifier is not personal data, so it carries no erasure
# obligation and an erasure request never rewrites one.
_USAGE_ROLLUP_TENANT_DAY_DDL = """
CREATE TABLE usage_rollup_tenant_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    surface         TEXT        NOT NULL,
    calls           BIGINT      NOT NULL,
    ok_calls        BIGINT      NOT NULL,
    error_calls     BIGINT      NOT NULL,
    -- A count, never a list — the field that keeps the table non-personal.
    distinct_actors INTEGER     NOT NULL,
    p50_ms          INTEGER,
    p95_ms          INTEGER,
    p99_ms          INTEGER,
    payload_bytes   BIGINT,
    payload_tokens  BIGINT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, surface)
)
"""

_USAGE_ROLLUP_CAPABILITY_DAY_DDL = """
CREATE TABLE usage_rollup_capability_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    capability_id   UUID        NOT NULL,
    calls           BIGINT      NOT NULL,
    distinct_actors INTEGER     NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Outcome mix and served cost, added alongside the tenant grain's: "two
    -- thousand calls" and "two thousand calls, four hundred of them errors"
    -- are the same count and different situations.
    ok_calls        BIGINT      NOT NULL DEFAULT 0,
    error_calls     BIGINT      NOT NULL DEFAULT 0,
    payload_bytes   BIGINT,
    PRIMARY KEY (tenant_id, day, capability_id)
)
"""

_USAGE_ROLLUP_TOOL_DAY_DDL = """
CREATE TABLE usage_rollup_tool_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    tool            TEXT        NOT NULL,
    calls           BIGINT      NOT NULL,
    ok_calls        BIGINT      NOT NULL,
    error_calls     BIGINT      NOT NULL,
    distinct_actors INTEGER     NOT NULL,
    p50_ms          INTEGER,
    p95_ms          INTEGER,
    p99_ms          INTEGER,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, tool)
)
"""

_USAGE_ROLLUP_INDEXES = [
    "CREATE INDEX idx_urtd_tenant_day ON usage_rollup_tenant_day (tenant_id, day DESC)",
    "CREATE INDEX idx_urcd_tenant_day ON usage_rollup_capability_day (tenant_id, day DESC)",
    "CREATE INDEX idx_urtld_tenant_day ON usage_rollup_tool_day (tenant_id, day DESC)",
]


# ---------------------------------------------------------------------------
# Section 19 — ARC: governed context artifacts, attested intake, immutable receipts
# ---------------------------------------------------------------------------
#
# Twenty tables for attested context resolution (twenty-one shipped
# originally; `arc_content_deletion_verifications` is excluded here — see the
# module docstring). One section, not several: no intermediate subset of
# these tables is usable on its own.
#
# Global versus tenant scope: `arc_artifacts.tenant_id IS NULL` is the only
# global-artifact marker, and child revisions, directives, and rules carry
# NULL consistently. Request-side tables always carry a concrete requesting
# tenant even when the receipt selected global artifacts.
#
# `audit_log.tenant_id` is NOT NULL and every index on it is tenant-leading,
# so ARC's deployment-global operations (which have no tenant) attribute to a
# reserved tenant row instead of a nullable column that would hide them from
# every existing reader.
_ARC_DEPLOYMENT_TENANT_DDL = f"""
INSERT INTO tenants (
    tenant_id, slug, display_name, created_at, is_active, provider, disabled_at
) VALUES (
    '{_ARC_DEPLOYMENT_TENANT_ID}',
    '_deployment',
    'ARC deployment scope (reserved, not a customer tenant)',
    now(),
    false,
    'system',
    now()
)
"""

_ARC_ARTIFACTS_DDL = """
CREATE TABLE arc_artifacts (
    artifact_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(tenant_id),
    slug                TEXT NOT NULL,
    kind                TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_actor_id UUID REFERENCES actors(actor_id),
    CONSTRAINT ck_arc_artifacts_kind CHECK (
        kind IN ('standard', 'policy', 'adr', 'runbook', 'capability_contract')
    ),
    CONSTRAINT ck_arc_artifacts_slug_len CHECK (char_length(slug) BETWEEN 1 AND 200)
)
"""

_ARC_ARTIFACTS_INDEXES = [
    "CREATE INDEX ix_arc_artifacts_tenant_kind ON arc_artifacts (tenant_id, kind)",
    "CREATE INDEX ix_arc_artifacts_slug ON arc_artifacts (slug)",
    # COALESCE collapses NULL into one comparable sentinel so a plain UNIQUE
    # (which never treats two NULLs as equal) still constrains global rows.
    # The sentinel must be a UUID no real tenant can hold — the deployment
    # tenant, not the all-zero seed `default` tenant.
    f"CREATE UNIQUE INDEX uq_arc_artifacts_scope_slug ON arc_artifacts "
    f"(COALESCE(tenant_id, '{_ARC_DEPLOYMENT_TENANT_ID}'::uuid), slug)",
]

# The content-classification and superseded-by FK are added in the deferred
# section below — content_classification's CHECK because the vocabulary
# import needs to run first in this file's own order, superseded_by because
# it is self-referential.
_ARC_REVISIONS_DDL = f"""
CREATE TABLE arc_revisions (
    revision_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id                 UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    tenant_id                   UUID REFERENCES tenants(tenant_id),
    source_system               TEXT NOT NULL,
    source_canonical_locator     TEXT NOT NULL,
    source_revision_locator      TEXT NOT NULL,
    content_digest               TEXT NOT NULL,
    lifecycle_state              TEXT NOT NULL DEFAULT 'draft',
    effective_from                TIMESTAMPTZ NOT NULL,
    effective_until               TIMESTAMPTZ,
    superseded_by_revision_id     UUID,
    approval_evidence_id          UUID,
    review_expires_at             TIMESTAMPTZ NOT NULL,
    detail_audience               TEXT NOT NULL,
    freshness_basis                TEXT NOT NULL,
    content_classification        TEXT NOT NULL,
    content_retention_until       TIMESTAMPTZ NOT NULL,
    legal_hold                    BOOLEAN NOT NULL DEFAULT FALSE,
    content_storage_mode          TEXT NOT NULL,
    source_body_ciphertext        BYTEA,
    source_body_plaintext         TEXT,
    source_body_nonce             BYTEA,
    source_body_wrapped_dek       BYTEA,
    content_key_id                TEXT,
    content_encryption_profile    TEXT,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at                   TIMESTAMPTZ,
    revoked_at                     TIMESTAMPTZ,
    created_by_actor_id            UUID REFERENCES actors(actor_id),
    CONSTRAINT ck_arc_revisions_lifecycle CHECK (
        lifecycle_state IN ('draft', 'active', 'superseded', 'revoked', 'expired')
    ),
    CONSTRAINT ck_arc_revisions_detail_audience CHECK (
        detail_audience IN ('all_matched_actors', 'tenant_admin_auditor', 'registered_gateway_only')
    ),
    CONSTRAINT ck_arc_revisions_freshness CHECK (
        freshness_basis IN ('connector_verified', 'revision_pinned_only')
    ),
    CONSTRAINT ck_arc_revisions_storage_mode CHECK (content_storage_mode IN ('encrypted', 'none')),
    -- Vocabulary bound so the content-minimization invariant holds even before
    -- the closed-set CHECK below narrows it further.
    CONSTRAINT ck_arc_revisions_classification_len CHECK (char_length(content_classification) BETWEEN 1 AND 64),
    -- The vocabulary comes from registry.arc.vocabularies, and a conformance
    -- test asserts the constants there and this constraint describe the same
    -- set.
    CONSTRAINT ck_arc_revisions_content_classification CHECK (
        content_classification IN ({_sql_set(CONTENT_CLASSIFICATIONS)})
    ),
    -- `regulated` implies encrypted storage, enforced here rather than in the
    -- service layer so no write path can forget it.
    CONSTRAINT ck_arc_revisions_regulated_encrypted CHECK (
        content_classification <> 'regulated' OR content_storage_mode = 'encrypted'
    ),
    CONSTRAINT ck_arc_revisions_superseded_link CHECK (
        lifecycle_state <> 'superseded' OR superseded_by_revision_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_revisions_revoked_at CHECK (lifecycle_state <> 'revoked' OR revoked_at IS NOT NULL),
    CONSTRAINT ck_arc_revisions_body_one_of CHECK (
        source_body_ciphertext IS NULL OR source_body_plaintext IS NULL
    ),
    -- Never plaintext for a global revision: global content always uses the
    -- deployment hierarchy.
    CONSTRAINT ck_arc_revisions_no_global_plaintext CHECK (
        source_body_plaintext IS NULL OR tenant_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_revisions_encrypted_envelope CHECK (
        content_storage_mode <> 'encrypted'
        OR source_body_ciphertext IS NULL
        OR (source_body_nonce IS NOT NULL
            AND source_body_wrapped_dek IS NOT NULL
            AND content_key_id IS NOT NULL
            AND content_encryption_profile IS NOT NULL)
    ),
    -- The composite unique target the child/parent tenant-agreement FKs
    -- below reference. Adds no new uniqueness beyond the primary key — it
    -- exists only to give those FKs a target.
    CONSTRAINT uq_arc_revisions_id_tenant UNIQUE (revision_id, tenant_id)
)
"""

_ARC_REVISIONS_INDEXES = [
    "CREATE INDEX ix_arc_revisions_artifact_lifecycle ON arc_revisions (artifact_id, lifecycle_state)",
    "CREATE INDEX ix_arc_revisions_tenant_lifecycle ON arc_revisions (tenant_id, lifecycle_state)",
    "CREATE INDEX ix_arc_revisions_review_expires_at ON arc_revisions (review_expires_at) "
    "WHERE lifecycle_state = 'active'",
    "CREATE UNIQUE INDEX uq_arc_revisions_source_identity ON arc_revisions "
    "(source_system, source_revision_locator, content_digest)",
    # Database backstop for family-locked activation.
    "CREATE UNIQUE INDEX uq_arc_revisions_one_active_per_artifact ON arc_revisions "
    "(artifact_id) WHERE lifecycle_state = 'active'",
]

_ARC_DIRECTIVE_IDENTITIES_DDL = """
CREATE TABLE arc_directive_identities (
    directive_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id  UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# The digest indexes the canonical subject key; it does not define identity.
# A digest collision with unequal canonical keys is an integrity error, which
# is why the full key is stored alongside it.
_ARC_CONFLICT_DOMAINS_DDL = """
CREATE TABLE arc_conflict_domains (
    conflict_subject_digest TEXT PRIMARY KEY,
    conflict_subject_key    JSONB NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_conflict_domains_digest_len CHECK (char_length(conflict_subject_digest) = 64)
)
"""

_ARC_TASK_KINDS = (
    "'read_only', 'code_change', 'dependency_change', 'configuration_change', "
    "'security_sensitive_change', 'data_access', 'deployment'"
)
_ARC_ACTION_CLASSES = "'merge', 'deploy', 'production_configuration_mutation', 'secret_release', 'data_export'"

# tenant_id here is a copy of the parent revision's tenant, made structural by
# the composite FK to arc_revisions(revision_id, tenant_id) added below — a
# child may only name the tenant its revision names.
_ARC_DIRECTIVES_DDL = """
CREATE TABLE arc_directives (
    directive_id                        UUID NOT NULL REFERENCES arc_directive_identities(directive_id),
    revision_id                         UUID NOT NULL REFERENCES arc_revisions(revision_id),
    tenant_id                           UUID REFERENCES tenants(tenant_id),
    directive_type                      TEXT NOT NULL,
    conflict_key_schema_version          TEXT,
    conflict_subject_digest              TEXT REFERENCES arc_conflict_domains(conflict_subject_digest),
    compact_statement_ciphertext         BYTEA,
    compact_statement_plaintext          TEXT,
    compact_statement_nonce              BYTEA,
    compact_statement_wrapped_dek        BYTEA,
    source_anchor                        TEXT NOT NULL,
    conflict_key_namespace                TEXT,
    conflict_key_subject_selector         TEXT,
    conflict_key_operation                TEXT,
    conflict_key_action_class             TEXT,
    conflict_key_target_selector          TEXT,
    conflict_key_modality                 TEXT,
    conflict_key_constraint_operator      TEXT,
    conflict_key_constraint_value         TEXT,
    satisfaction_mode                     TEXT,
    verification_max_age_seconds          INTEGER,
    accepted_verifier_classes             TEXT[],
    accepted_verifier_ids                 UUID[],
    required_evidence_type                TEXT,
    delegable_exception                   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, directive_id),
    CONSTRAINT ck_arc_directives_type CHECK (
        directive_type IN ('require', 'prohibit', 'verify', 'escalate', 'citation_only')
    ),
    CONSTRAINT ck_arc_directives_schema_version CHECK (
        conflict_key_schema_version IS NULL OR conflict_key_schema_version = 'arc_conflict_v1'
    ),
    CONSTRAINT ck_arc_directives_modality CHECK (
        conflict_key_modality IS NULL OR conflict_key_modality IN ('require', 'prohibit')
    ),
    CONSTRAINT ck_arc_directives_operator CHECK (
        conflict_key_constraint_operator IS NULL
        OR conflict_key_constraint_operator IN ('equals', 'in_set', 'not_in_set', 'present')
    ),
    CONSTRAINT ck_arc_directives_satisfaction_mode CHECK (
        satisfaction_mode IS NULL OR satisfaction_mode IN ('authorized_retrieval', 'signed_result')
    ),
    -- An action-protecting directive must carry the complete arc_conflict_v1
    -- shape; anything less is citation_only.
    CONSTRAINT ck_arc_directives_action_protecting_shape CHECK (
        directive_type = 'citation_only'
        OR (conflict_key_schema_version = 'arc_conflict_v1'
            AND conflict_subject_digest IS NOT NULL
            AND conflict_key_namespace IS NOT NULL
            AND conflict_key_subject_selector IS NOT NULL
            AND conflict_key_operation IS NOT NULL
            AND conflict_key_action_class IS NOT NULL
            AND conflict_key_target_selector IS NOT NULL
            AND conflict_key_modality IS NOT NULL
            AND conflict_key_constraint_operator IS NOT NULL)
    ),
    CONSTRAINT ck_arc_directives_signed_result_policy CHECK (
        satisfaction_mode IS DISTINCT FROM 'signed_result'
        OR (accepted_verifier_classes IS NOT NULL
            AND array_length(accepted_verifier_classes, 1) >= 1
            AND required_evidence_type IS NOT NULL)
    ),
    CONSTRAINT ck_arc_directives_statement_one_of CHECK (
        (compact_statement_ciphertext IS NOT NULL AND compact_statement_plaintext IS NULL)
        OR (compact_statement_plaintext IS NOT NULL AND compact_statement_ciphertext IS NULL)
    ),
    CONSTRAINT ck_arc_directives_no_global_plaintext CHECK (
        compact_statement_plaintext IS NULL OR tenant_id IS NOT NULL
    )
)
"""

_ARC_DIRECTIVES_INDEXES = [
    "CREATE INDEX ix_arc_directives_revision ON arc_directives (revision_id)",
    "CREATE INDEX ix_arc_directives_identity ON arc_directives (directive_id, revision_id)",
    "CREATE INDEX ix_arc_directives_conflict_key ON arc_directives "
    "(conflict_key_namespace, conflict_key_subject_selector, conflict_key_operation, "
    "conflict_key_action_class, conflict_key_target_selector)",
]

_ARC_RULES_DDL = f"""
CREATE TABLE arc_applicability_rules (
    rule_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revision_id             UUID NOT NULL REFERENCES arc_revisions(revision_id),
    tenant_id               UUID REFERENCES tenants(tenant_id),
    scope                   TEXT NOT NULL,
    target_tenant_id        UUID REFERENCES tenants(tenant_id),
    capability_ids          UUID[],
    capability_labels       TEXT[],
    domain_ids              TEXT[],
    task_kinds              TEXT[],
    action_classes          TEXT[],
    environments             TEXT[],
    data_sensitivity_tiers   TEXT[],
    effective_from            TIMESTAMPTZ NOT NULL,
    effective_until           TIMESTAMPTZ,
    is_mandatory              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_rules_scope CHECK (scope IN ('global', 'tenant', 'domain', 'capability', 'task')),
    -- Closed vocabularies, enforced element-wise — a host cannot invent a
    -- lower-risk value to escape an obligation.
    CONSTRAINT ck_arc_rules_task_kinds CHECK (
        task_kinds IS NULL OR task_kinds <@ ARRAY[{_ARC_TASK_KINDS}]::TEXT[]
    ),
    CONSTRAINT ck_arc_rules_action_classes CHECK (
        action_classes IS NULL OR action_classes <@ ARRAY[{_ARC_ACTION_CLASSES}]::TEXT[]
    ),
    CONSTRAINT ck_arc_rules_tenant_scope_target CHECK (scope <> 'tenant' OR target_tenant_id IS NOT NULL),
    CONSTRAINT ck_arc_rules_capability_scope_target CHECK (
        scope <> 'capability'
        OR (capability_ids IS NOT NULL AND array_length(capability_ids, 1) >= 1)
        OR (capability_labels IS NOT NULL AND array_length(capability_labels, 1) >= 1)
    )
)
"""

_ARC_RULES_INDEXES = [
    "CREATE INDEX ix_arc_rules_revision ON arc_applicability_rules (revision_id)",
    "CREATE INDEX ix_arc_rules_tenant_scope ON arc_applicability_rules (tenant_id, scope)",
    "CREATE INDEX ix_arc_rules_capability_ids ON arc_applicability_rules USING GIN (capability_ids)",
    "CREATE INDEX ix_arc_rules_task_kinds ON arc_applicability_rules USING GIN (task_kinds)",
    "CREATE INDEX ix_arc_rules_action_classes ON arc_applicability_rules USING GIN (action_classes)",
]

# Family-level tombstones. Without these, a revoked or review-expired
# mandatory projection would simply stop appearing in selection, and a bundle
# missing an obligation would look identical to one that never had it.
_ARC_OBLIGATIONS_DDL = """
CREATE TABLE arc_mandatory_obligations (
    obligation_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id             UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    directive_id            UUID NOT NULL REFERENCES arc_directive_identities(directive_id),
    current_revision_id     UUID REFERENCES arc_revisions(revision_id),
    applicability_snapshot  JSONB NOT NULL,
    applicability_digest    TEXT NOT NULL,
    obligation_state        TEXT NOT NULL,
    effective_from          TIMESTAMPTZ NOT NULL,
    effective_until         TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_obligations_state CHECK (
        obligation_state IN ('satisfied', 'missing_revoked', 'missing_invalid', 'missing_review_expired')
    ),
    CONSTRAINT ck_arc_obligations_satisfied_revision CHECK (
        obligation_state <> 'satisfied' OR current_revision_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_obligations_digest_len CHECK (char_length(applicability_digest) = 64)
)
"""

_ARC_OBLIGATIONS_INDEXES = [
    "CREATE INDEX ix_arc_obligations_artifact ON arc_mandatory_obligations (artifact_id)",
    "CREATE INDEX ix_arc_obligations_directive ON arc_mandatory_obligations (directive_id)",
    "CREATE INDEX ix_arc_obligations_state ON arc_mandatory_obligations (obligation_state) "
    "WHERE obligation_state <> 'satisfied'",
]

_ARC_HOST_KEYS_DDL = """
CREATE TABLE arc_host_attestation_keys (
    signer_key_id       TEXT PRIMARY KEY,
    host_id             TEXT NOT NULL,
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    attestation_profile TEXT NOT NULL,
    public_key          TEXT NOT NULL,
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_until         TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    replacement_key_id  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_operator TEXT NOT NULL,
    CONSTRAINT ck_arc_host_keys_profile CHECK (attestation_profile = 'arc_host_attestation_v1'),
    CONSTRAINT ck_arc_host_keys_id_len CHECK (char_length(signer_key_id) BETWEEN 1 AND 200)
)
"""

_ARC_HOST_KEYS_INDEXES = [
    "CREATE INDEX ix_arc_host_attestation_keys_valid ON arc_host_attestation_keys (valid_from, valid_until)",
    "CREATE INDEX ix_arc_host_attestation_keys_host ON arc_host_attestation_keys (host_id)",
]

# Public verification history only — private key material stays in the
# configured ReceiptSigningProvider. Retirement never deletes a row: a receipt
# signed years ago must remain verifiable.
_ARC_RECEIPT_KEYS_DDL = """
CREATE TABLE arc_receipt_signing_keys (
    signer_key_id      TEXT PRIMARY KEY,
    algorithm          TEXT NOT NULL,
    public_key         TEXT NOT NULL,
    purpose            TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL,
    valid_until        TIMESTAMPTZ,
    compromised_at     TIMESTAMPTZ,
    replacement_key_id TEXT,
    manifest_digest    TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_receipt_keys_algorithm CHECK (algorithm = 'Ed25519'),
    CONSTRAINT ck_arc_receipt_keys_purpose CHECK (purpose = 'arc_receipt_event_v1')
)
"""

_ARC_APPROVAL_VERIFIERS_DDL = """
CREATE TABLE arc_approval_verifiers (
    approval_verifier_id   TEXT PRIMARY KEY,
    verifier_kind          TEXT NOT NULL,
    allowed_evidence_types TEXT[] NOT NULL,
    scope_kind             TEXT NOT NULL,
    scope_tenant_id        UUID REFERENCES tenants(tenant_id),
    algorithm              TEXT,
    public_key             BYTEA,
    provider_id            TEXT,
    valid_from             TIMESTAMPTZ NOT NULL,
    valid_to               TIMESTAMPTZ,
    revoked_at             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_verifiers_kind CHECK (
        verifier_kind IN ('operator_public_key', 'trusted_attestation_provider')
    ),
    CONSTRAINT ck_arc_verifiers_scope_kind CHECK (scope_kind IN ('global', 'tenant')),
    CONSTRAINT ck_arc_verifiers_tenant_scope CHECK (scope_kind <> 'tenant' OR scope_tenant_id IS NOT NULL),
    CONSTRAINT ck_arc_verifiers_global_scope CHECK (scope_kind <> 'global' OR scope_tenant_id IS NULL),
    -- Exactly one representation, matching the declared kind.
    CONSTRAINT ck_arc_verifiers_representation CHECK (
        (verifier_kind = 'operator_public_key'
         AND algorithm IS NOT NULL AND public_key IS NOT NULL AND provider_id IS NULL)
        OR (verifier_kind = 'trusted_attestation_provider'
            AND provider_id IS NOT NULL AND algorithm IS NULL AND public_key IS NULL)
    ),
    CONSTRAINT ck_arc_verifiers_evidence_types CHECK (array_length(allowed_evidence_types, 1) >= 1)
)
"""

_ARC_APPROVAL_EVIDENCE_DDL = """
CREATE TABLE arc_approval_evidence (
    evidence_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_type                  TEXT NOT NULL,
    scope_kind                     TEXT NOT NULL,
    scope_tenant_id                 UUID REFERENCES tenants(tenant_id),
    approved_artifact_id            UUID REFERENCES arc_artifacts(artifact_id),
    approved_revision_id            UUID,
    approved_exception_id           UUID,
    approved_payload_digest         TEXT NOT NULL,
    approving_principal             TEXT NOT NULL,
    approving_role                  TEXT NOT NULL,
    source_system_approval_locator  TEXT,
    approval_timestamp              TIMESTAMPTZ NOT NULL,
    expires_at                      TIMESTAMPTZ,
    policy_version                  TEXT,
    action_instance_id              TEXT,
    verification_method             TEXT NOT NULL,
    signer_key_id                    TEXT REFERENCES arc_approval_verifiers(approval_verifier_id),
    approval_verifier_id             TEXT REFERENCES arc_approval_verifiers(approval_verifier_id),
    signature                        TEXT,
    verifier_attestation             JSONB,
    verifier_identity                TEXT,
    audit_log_reference               TEXT NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_evidence_type CHECK (
        evidence_type IN (
            'artifact_activation', 'exception_approval', 'global_exception_approval', 'gateway_emergency_bypass'
        )
    ),
    CONSTRAINT ck_arc_evidence_scope_kind CHECK (scope_kind IN ('global', 'tenant', 'domain', 'capability', 'task')),
    CONSTRAINT ck_arc_evidence_method CHECK (verification_method IN ('operator_signed', 'verifier_attested')),
    CONSTRAINT ck_arc_evidence_activation_targets CHECK (
        evidence_type <> 'artifact_activation'
        OR (approved_artifact_id IS NOT NULL AND approved_revision_id IS NOT NULL)
    ),
    CONSTRAINT ck_arc_evidence_exception_targets CHECK (
        evidence_type NOT IN ('exception_approval', 'global_exception_approval')
        OR approved_exception_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_evidence_bypass_targets CHECK (
        evidence_type <> 'gateway_emergency_bypass'
        OR (action_instance_id IS NOT NULL AND policy_version IS NOT NULL)
    ),
    CONSTRAINT ck_arc_evidence_global_scope CHECK (scope_kind <> 'global' OR scope_tenant_id IS NULL),
    CONSTRAINT ck_arc_evidence_tenant_scope CHECK (scope_kind = 'global' OR scope_tenant_id IS NOT NULL),
    -- The unused representation must be NULL, so evidence cannot be
    -- validated against a path it did not declare.
    CONSTRAINT ck_arc_evidence_representation CHECK (
        (verification_method = 'operator_signed'
         AND signer_key_id IS NOT NULL AND signature IS NOT NULL
         AND approval_verifier_id IS NULL AND verifier_attestation IS NULL)
        OR (verification_method = 'verifier_attested'
            AND approval_verifier_id IS NOT NULL AND verifier_attestation IS NOT NULL
            AND signer_key_id IS NULL AND signature IS NULL)
    )
)
"""

_ARC_APPROVAL_EVIDENCE_INDEXES = [
    "CREATE INDEX ix_arc_approval_evidence_artifact ON arc_approval_evidence "
    "(approved_artifact_id, approved_revision_id)",
    "CREATE INDEX ix_arc_approval_evidence_exception ON arc_approval_evidence (approved_exception_id)",
    "CREATE INDEX ix_arc_approval_evidence_expires ON arc_approval_evidence (expires_at) WHERE expires_at IS NOT NULL",
]

_ARC_EXCEPTIONS_DDL = f"""
CREATE TABLE arc_approved_exceptions (
    exception_id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    higher_scope_directive_id         UUID NOT NULL,
    higher_scope_revision_id          UUID NOT NULL,
    lower_scope_kind                  TEXT NOT NULL,
    lower_scope_tenant_id              UUID NOT NULL REFERENCES tenants(tenant_id),
    lower_scope_domain_id               TEXT,
    lower_scope_capability_id           UUID,
    lower_scope_task_kind               TEXT,
    lower_scope_action_class            TEXT,
    lower_scope_environment             TEXT,
    lower_scope_data_sensitivity        TEXT,
    replacement_conflict_descriptor     JSONB NOT NULL,
    exception_statement_ciphertext      BYTEA,
    exception_statement_plaintext       TEXT,
    exception_statement_nonce           BYTEA,
    justification_ciphertext            BYTEA,
    justification_plaintext             TEXT,
    justification_nonce                 BYTEA,
    content_wrapped_dek                 BYTEA,
    content_key_id                      TEXT,
    effective_from                      TIMESTAMPTZ NOT NULL,
    effective_until                     TIMESTAMPTZ,
    revoked_at                          TIMESTAMPTZ,
    approval_evidence_id                UUID NOT NULL,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_actor_id                 UUID REFERENCES actors(actor_id),
    FOREIGN KEY (higher_scope_revision_id, higher_scope_directive_id)
        REFERENCES arc_directives (revision_id, directive_id),
    CONSTRAINT ck_arc_exceptions_lower_scope_kind CHECK (
        lower_scope_kind IN ('tenant', 'domain', 'capability', 'task')
    ),
    CONSTRAINT ck_arc_exceptions_task_kind CHECK (
        lower_scope_task_kind IS NULL OR lower_scope_task_kind IN ({_ARC_TASK_KINDS})
    ),
    CONSTRAINT ck_arc_exceptions_action_class CHECK (
        lower_scope_action_class IS NULL OR lower_scope_action_class IN ({_ARC_ACTION_CLASSES})
    ),
    -- Discriminated scope: only the selectors the declared scope permits.
    CONSTRAINT ck_arc_exceptions_scope_selectors CHECK (
        (lower_scope_kind = 'tenant'
         AND lower_scope_domain_id IS NULL AND lower_scope_capability_id IS NULL
         AND lower_scope_task_kind IS NULL AND lower_scope_action_class IS NULL)
        OR (lower_scope_kind = 'domain'
            AND lower_scope_domain_id IS NOT NULL AND lower_scope_capability_id IS NULL
            AND lower_scope_task_kind IS NULL AND lower_scope_action_class IS NULL)
        OR (lower_scope_kind = 'capability'
            AND lower_scope_capability_id IS NOT NULL AND lower_scope_domain_id IS NULL
            AND lower_scope_task_kind IS NULL AND lower_scope_action_class IS NULL)
        OR (lower_scope_kind = 'task'
            AND lower_scope_task_kind IS NOT NULL AND lower_scope_action_class IS NOT NULL)
    ),
    CONSTRAINT ck_arc_exceptions_statement_one_of CHECK (
        exception_statement_ciphertext IS NULL OR exception_statement_plaintext IS NULL
    ),
    CONSTRAINT ck_arc_exceptions_justification_one_of CHECK (
        justification_ciphertext IS NULL OR justification_plaintext IS NULL
    )
)
"""

_ARC_EXCEPTIONS_INDEXES = [
    "CREATE INDEX ix_arc_exceptions_directive ON arc_approved_exceptions (higher_scope_directive_id)",
    "CREATE INDEX ix_arc_exceptions_scope ON arc_approved_exceptions "
    "(lower_scope_kind, lower_scope_tenant_id, lower_scope_domain_id, lower_scope_capability_id)",
]

# Append-only. The presence of a row makes the evidence unusable immediately;
# there is no un-revoke.
_ARC_EVIDENCE_REVOCATIONS_DDL = """
CREATE TABLE arc_approval_evidence_revocations (
    evidence_id         UUID PRIMARY KEY REFERENCES arc_approval_evidence(evidence_id),
    revoked_at          TIMESTAMPTZ NOT NULL,
    reason_code         TEXT NOT NULL,
    reason_digest       TEXT NOT NULL,
    revoked_by_actor_id UUID REFERENCES actors(actor_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_evidence_revocations_reason_len CHECK (char_length(reason_code) BETWEEN 1 AND 64)
)
"""

# Only the nonce *digest* is stored — the raw nonce is reproducible for an
# exact unexpired retry through the versioned ChallengeNonceDeriver.
_ARC_CHALLENGES_DDL = """
CREATE TABLE arc_context_challenges (
    challenge_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(tenant_id),
    host_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    manifest_claims_digest  TEXT NOT NULL,
    arc_nonce_digest        TEXT NOT NULL UNIQUE,
    nonce_derivation_key_id TEXT NOT NULL,
    issued_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at              TIMESTAMPTZ NOT NULL,
    consumed_at             TIMESTAMPTZ,
    idempotency_key_digest  TEXT NOT NULL,
    CONSTRAINT ck_arc_challenges_nonce_digest_len CHECK (char_length(arc_nonce_digest) = 64),
    CONSTRAINT ck_arc_challenges_claims_digest_len CHECK (char_length(manifest_claims_digest) = 64),
    CONSTRAINT ck_arc_challenges_idem_digest_len CHECK (char_length(idempotency_key_digest) = 64),
    CONSTRAINT ck_arc_challenges_expiry_after_issue CHECK (expires_at > issued_at),
    -- Bounded request-side identifiers (see the module-level note on ARC's
    -- text columns in the receipts/events tables below).
    CONSTRAINT ck_arc_challenges_host_id_len CHECK (char_length(host_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_challenges_session_id_len CHECK (char_length(session_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_challenges_nonce_key_len CHECK (char_length(nonce_derivation_key_id) BETWEEN 1 AND 200)
)
"""

_ARC_CHALLENGES_INDEXES = [
    "CREATE INDEX ix_arc_challenges_nonce_digest ON arc_context_challenges (arc_nonce_digest)",
    "CREATE INDEX ix_arc_challenges_host_session ON arc_context_challenges "
    "(host_id, session_id, idempotency_key_digest)",
    "CREATE INDEX ix_arc_challenges_expires_at ON arc_context_challenges (expires_at)",
    "CREATE UNIQUE INDEX uq_arc_challenges_idempotency ON arc_context_challenges "
    "(tenant_id, host_id, session_id, idempotency_key_digest)",
]

# challenge_id is NOT NULL and UNIQUE: every receipt consumes exactly one
# challenge, and no challenge backs two receipts — half of the single-use
# invariant; the deferred constraint trigger below is the other half.
#
# Receipts, events, selected rows, and challenges are the audit record: they
# may hold bounded identifiers, enumerated codes, counters, and digests — not
# content. Every text column on those tables is therefore bounded, either by
# an enumerating CHECK declared alongside it or by a length CHECK, so the
# invariant `tests/conformance/test_arc_content_minimization.py` checks holds
# by construction.
_ARC_RECEIPTS_DDL = """
CREATE TABLE arc_receipts (
    receipt_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id                UUID NOT NULL UNIQUE REFERENCES arc_context_challenges(challenge_id),
    tenant_id                   UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id                    UUID NOT NULL REFERENCES actors(actor_id),
    host_id                     TEXT NOT NULL,
    session_id                  TEXT NOT NULL,
    manifest_fingerprint         TEXT NOT NULL,
    attestation_id                TEXT NOT NULL,
    resolution_status             TEXT NOT NULL,
    selection_engine_version      TEXT NOT NULL,
    registry_build_revision       TEXT NOT NULL,
    canonical_profile_versions     JSONB NOT NULL,
    selection_config_digest        TEXT NOT NULL,
    evaluated_at                   TIMESTAMPTZ NOT NULL,
    freshness_basis                TEXT NOT NULL,
    freshness_deadline              TIMESTAMPTZ,
    blocked_reasons                 TEXT[],
    degraded_reasons                TEXT[],
    mandatory_directive_count        INTEGER NOT NULL DEFAULT 0,
    rendered_content_bytes           INTEGER NOT NULL DEFAULT 0,
    budget_limit_bytes                INTEGER NOT NULL,
    integrity_state                   TEXT NOT NULL DEFAULT 'valid',
    response_replay_ciphertext        BYTEA NOT NULL,
    response_replay_nonce             BYTEA NOT NULL,
    response_replay_key_id            TEXT NOT NULL,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_receipts_status CHECK (resolution_status IN ('ready', 'degraded', 'blocked')),
    CONSTRAINT ck_arc_receipts_freshness CHECK (freshness_basis IN ('connector_verified', 'revision_pinned_only')),
    CONSTRAINT ck_arc_receipts_integrity CHECK (integrity_state IN ('valid', 'integrity_failed')),
    CONSTRAINT ck_arc_receipts_fingerprint_len CHECK (char_length(manifest_fingerprint) = 64),
    CONSTRAINT ck_arc_receipts_config_digest_len CHECK (char_length(selection_config_digest) = 64),
    CONSTRAINT ck_arc_receipts_attestation_id_len CHECK (char_length(attestation_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_receipts_counts_nonneg CHECK (
        mandatory_directive_count >= 0 AND rendered_content_bytes >= 0 AND budget_limit_bytes > 0
    ),
    CONSTRAINT ck_arc_receipts_blocked_has_reason CHECK (
        resolution_status <> 'blocked' OR (blocked_reasons IS NOT NULL AND array_length(blocked_reasons, 1) >= 1)
    ),
    CONSTRAINT ck_arc_receipts_host_id_len CHECK (char_length(host_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_receipts_session_id_len CHECK (char_length(session_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_receipts_build_revision_len CHECK (char_length(registry_build_revision) BETWEEN 1 AND 64),
    CONSTRAINT ck_arc_receipts_engine_version_len CHECK (char_length(selection_engine_version) BETWEEN 1 AND 64),
    CONSTRAINT ck_arc_receipts_replay_key_len CHECK (char_length(response_replay_key_id) BETWEEN 1 AND 200)
)
"""

_ARC_RECEIPTS_INDEXES = [
    "CREATE INDEX ix_arc_receipts_tenant_actor ON arc_receipts (tenant_id, actor_id, created_at DESC)",
    "CREATE INDEX ix_arc_receipts_host_session ON arc_receipts (host_id, session_id)",
    "CREATE UNIQUE INDEX ix_arc_receipts_host_attestation_id ON arc_receipts (host_id, attestation_id)",
    "CREATE INDEX ix_arc_receipts_manifest_fingerprint ON arc_receipts (tenant_id, manifest_fingerprint)",
]

# The vocabulary comes from registry.arc.vocabularies — exactly the three
# transitions the code performs today. There is deliberately no member for an
# integrity failure: a failed append is rolled back precisely so it leaves no
# row.
_ARC_RECEIPT_EVENTS_DDL = f"""
CREATE TABLE arc_receipt_events (
    event_id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id                          UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    tenant_id                           UUID NOT NULL,
    sequence                            INTEGER NOT NULL,
    event_type                          TEXT NOT NULL,
    event_source                        TEXT NOT NULL,
    actor_id                            UUID REFERENCES actors(actor_id),
    gateway_id                          TEXT,
    signer_key_id                       TEXT REFERENCES arc_receipt_signing_keys(signer_key_id),
    signature_profile                   TEXT NOT NULL,
    idempotency_key_digest               TEXT,
    request_payload_digest               TEXT NOT NULL,
    previous_event_digest                TEXT,
    event_payload                        JSONB NOT NULL,
    consumed_continuation_token_digest    TEXT,
    response_replay_ciphertext            BYTEA,
    response_replay_nonce                 BYTEA,
    response_replay_key_id                TEXT,
    event_digest                          TEXT NOT NULL,
    signature                             TEXT NOT NULL,
    created_at                            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_events_source CHECK (event_source IN ('host', 'gateway', 'system')),
    CONSTRAINT ck_arc_events_type_len CHECK (char_length(event_type) BETWEEN 1 AND 64),
    CONSTRAINT ck_arc_receipt_events_event_type CHECK (event_type IN ({_sql_set(RECEIPT_EVENT_TYPES)})),
    -- 0-indexed: the receipt-creation event is sequence 0. A 1-indexed CHECK
    -- would reject the first event every receipt has.
    CONSTRAINT ck_arc_events_sequence_nonneg CHECK (sequence >= 0),
    CONSTRAINT ck_arc_events_digest_len CHECK (char_length(event_digest) = 64),
    CONSTRAINT ck_arc_events_request_digest_len CHECK (char_length(request_payload_digest) = 64),
    CONSTRAINT ck_arc_events_chain_link CHECK (
        (sequence = 0 AND previous_event_digest IS NULL)
        OR (sequence > 0 AND previous_event_digest IS NOT NULL)
    ),
    CONSTRAINT ck_arc_events_idempotency_required CHECK (
        (event_source = 'system' AND idempotency_key_digest IS NULL)
        OR (event_source <> 'system' AND idempotency_key_digest IS NOT NULL)
    ),
    CONSTRAINT ck_arc_events_gateway_id_len CHECK (gateway_id IS NULL OR char_length(gateway_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_events_signer_key_len CHECK (
        signer_key_id IS NULL OR char_length(signer_key_id) BETWEEN 1 AND 200
    ),
    CONSTRAINT ck_arc_events_sig_profile_len CHECK (char_length(signature_profile) BETWEEN 1 AND 64),
    CONSTRAINT ck_arc_events_signature_len CHECK (char_length(signature) BETWEEN 1 AND 512),
    CONSTRAINT ck_arc_events_replay_key_len CHECK (
        response_replay_key_id IS NULL OR char_length(response_replay_key_id) BETWEEN 1 AND 200
    ),
    CONSTRAINT ck_arc_events_prev_digest_len CHECK (
        previous_event_digest IS NULL OR char_length(previous_event_digest) = 64
    ),
    CONSTRAINT ck_arc_events_idem_digest_len CHECK (
        idempotency_key_digest IS NULL OR char_length(idempotency_key_digest) = 64
    ),
    CONSTRAINT ck_arc_events_page_token_digest_len CHECK (
        consumed_continuation_token_digest IS NULL OR char_length(consumed_continuation_token_digest) = 64
    )
)
"""

_ARC_RECEIPT_EVENTS_INDEXES = [
    "CREATE UNIQUE INDEX ix_arc_receipt_events_receipt_sequence ON arc_receipt_events (receipt_id, sequence)",
    "CREATE UNIQUE INDEX ix_arc_receipt_events_idempotency ON arc_receipt_events "
    "(receipt_id, event_source, idempotency_key_digest)",
    "CREATE INDEX ix_arc_receipt_events_digest ON arc_receipt_events (event_digest)",
    # A continuation token may advance a chain at most once; an exact page
    # retry resolves through the idempotency record above, not by
    # re-consuming.
    "CREATE UNIQUE INDEX uq_arc_receipt_events_page_token ON arc_receipt_events "
    "(receipt_id, consumed_continuation_token_digest) WHERE consumed_continuation_token_digest IS NOT NULL",
]

_ARC_EVENT_HEADS_DDL = """
CREATE TABLE arc_receipt_event_heads (
    receipt_id        UUID PRIMARY KEY REFERENCES arc_receipts(receipt_id),
    next_sequence     INTEGER NOT NULL,
    last_event_digest TEXT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A head exists only once its receipt-creation event (sequence 0) is
    -- written, so the lowest legal value is 1.
    CONSTRAINT ck_arc_event_heads_next_sequence CHECK (next_sequence >= 1),
    CONSTRAINT ck_arc_event_heads_digest_len CHECK (char_length(last_event_digest) = 64)
)
"""

_ARC_SELECTED_REVISIONS_DDL = """
CREATE TABLE arc_receipt_selected_revisions (
    receipt_id      UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    revision_id     UUID NOT NULL REFERENCES arc_revisions(revision_id),
    tenant_id       UUID NOT NULL,
    artifact_id     UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    is_mandatory    BOOLEAN NOT NULL,
    was_omitted     BOOLEAN NOT NULL DEFAULT FALSE,
    omission_reason TEXT,
    PRIMARY KEY (receipt_id, revision_id),
    CONSTRAINT ck_arc_selected_revisions_omission CHECK (was_omitted = FALSE OR omission_reason IS NOT NULL),
    CONSTRAINT ck_arc_selected_revisions_reason_len CHECK (
        omission_reason IS NULL OR char_length(omission_reason) BETWEEN 1 AND 64
    )
)
"""

_ARC_SELECTED_REVISIONS_INDEXES = [
    "CREATE INDEX ix_arc_receipt_revisions_receipt ON arc_receipt_selected_revisions (receipt_id)",
    "CREATE INDEX ix_arc_receipt_revisions_revision ON arc_receipt_selected_revisions (revision_id)",
]

# The per-receipt snapshot JIT authorizes against. Locator and digest columns
# are access-controlled rather than encrypted: they are redacted by artifact
# audience before they reach a caller.
_ARC_SELECTED_DIRECTIVES_DDL = """
CREATE TABLE arc_receipt_selected_directives (
    receipt_id               UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    revision_id               UUID NOT NULL REFERENCES arc_revisions(revision_id),
    directive_id               UUID NOT NULL,
    tenant_id                  UUID NOT NULL,
    artifact_id                 UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    is_mandatory                 BOOLEAN NOT NULL,
    was_omitted                  BOOLEAN NOT NULL DEFAULT FALSE,
    omission_reason               TEXT,
    visibility_decision_id        TEXT NOT NULL,
    source_locator                 TEXT NOT NULL,
    source_revision_locator        TEXT NOT NULL,
    content_digest                  TEXT NOT NULL,
    obligation_fields                JSONB NOT NULL,
    context_handle_digest            TEXT NOT NULL,
    PRIMARY KEY (receipt_id, directive_id),
    FOREIGN KEY (revision_id, directive_id) REFERENCES arc_directives (revision_id, directive_id),
    CONSTRAINT ck_arc_selected_directives_omission CHECK (was_omitted = FALSE OR omission_reason IS NOT NULL),
    CONSTRAINT ck_arc_selected_directives_handle_len CHECK (char_length(context_handle_digest) = 64),
    CONSTRAINT ck_arc_sel_dir_locator_len CHECK (char_length(source_locator) BETWEEN 1 AND 1024),
    CONSTRAINT ck_arc_sel_dir_rev_locator_len CHECK (char_length(source_revision_locator) BETWEEN 1 AND 1024),
    CONSTRAINT ck_arc_sel_dir_visibility_len CHECK (char_length(visibility_decision_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_sel_dir_omission_len CHECK (
        omission_reason IS NULL OR char_length(omission_reason) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_arc_sel_dir_content_digest_len CHECK (char_length(content_digest) = 64)
)
"""

_ARC_SELECTED_DIRECTIVES_INDEXES = [
    "CREATE INDEX ix_arc_receipt_directives_receipt ON arc_receipt_selected_directives (receipt_id)",
    "CREATE INDEX ix_arc_receipt_directives_revision ON arc_receipt_selected_directives (revision_id)",
    # One handle resolves to exactly one selection row, so JIT authorization
    # is never ambiguous.
    "CREATE UNIQUE INDEX uq_arc_receipt_directives_handle ON arc_receipt_selected_directives "
    "(receipt_id, context_handle_digest)",
]

# ARC does not write audit_log inline the way the rest of the codebase does.
# Every ARC write emits an outbox row in the same transaction as its domain
# state, and the drain worker is ARC's only writer to audit_log — receipt
# latency stays independent of audit-sink latency without losing events. A
# row past the attempt ceiling is never deleted and never silently skipped:
# it stays undrained and drops out of the active drain query, so one poison
# row cannot stall the queue behind it.
_ARC_AUDIT_OUTBOX_DDL = """
CREATE TABLE arc_audit_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    event_type      TEXT NOT NULL,
    event_payload   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    drained_at      TIMESTAMPTZ,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_attempt_at TIMESTAMPTZ,
    CONSTRAINT ck_arc_audit_outbox_event_type_len CHECK (char_length(event_type) BETWEEN 1 AND 128),
    CONSTRAINT ck_arc_audit_outbox_attempts_nonneg CHECK (attempts >= 0),
    CONSTRAINT ck_arc_audit_outbox_error_code_len CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_arc_audit_outbox_drained_terminal CHECK (drained_at IS NULL OR last_error_code IS NULL)
)
"""

_ARC_AUDIT_OUTBOX_INDEXES = [
    "CREATE INDEX ix_arc_audit_outbox_drained ON arc_audit_outbox (drained_at) WHERE drained_at IS NULL",
    "CREATE INDEX ix_arc_audit_outbox_created ON arc_audit_outbox (created_at)",
    "CREATE INDEX ix_arc_audit_outbox_stuck ON arc_audit_outbox (attempts) WHERE drained_at IS NULL AND attempts > 0",
]

# Genuinely cyclic references (a revision names the evidence that approved
# it, and the evidence names the revision it approved) plus self-referential
# ones, added after every table exists. DEFERRABLE INITIALLY DEFERRED lets
# one transaction insert both endpoints of a cyclic pair in either order.
# The two composite FKs make the child/parent tenant agreement structural: a
# directive or rule may only name the tenant its revision names.
_ARC_DEFERRED_FKS = [
    "ALTER TABLE arc_revisions ADD CONSTRAINT fk_arc_revisions_approval_evidence "
    "FOREIGN KEY (approval_evidence_id) REFERENCES arc_approval_evidence(evidence_id) DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approval_evidence ADD CONSTRAINT fk_arc_evidence_approved_revision "
    "FOREIGN KEY (approved_revision_id) REFERENCES arc_revisions(revision_id) DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT fk_arc_exceptions_approval_evidence "
    "FOREIGN KEY (approval_evidence_id) REFERENCES arc_approval_evidence(evidence_id) DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approval_evidence ADD CONSTRAINT fk_arc_evidence_approved_exception "
    "FOREIGN KEY (approved_exception_id) REFERENCES arc_approved_exceptions(exception_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_revisions ADD CONSTRAINT fk_arc_revisions_superseded_by "
    "FOREIGN KEY (superseded_by_revision_id) REFERENCES arc_revisions(revision_id)",
    "ALTER TABLE arc_host_attestation_keys ADD CONSTRAINT fk_arc_host_keys_replacement "
    "FOREIGN KEY (replacement_key_id) REFERENCES arc_host_attestation_keys(signer_key_id)",
    "ALTER TABLE arc_receipt_signing_keys ADD CONSTRAINT fk_arc_receipt_keys_replacement "
    "FOREIGN KEY (replacement_key_id) REFERENCES arc_receipt_signing_keys(signer_key_id)",
    "ALTER TABLE arc_directives ADD CONSTRAINT fk_arc_directives_revision_tenant "
    "FOREIGN KEY (revision_id, tenant_id) REFERENCES arc_revisions (revision_id, tenant_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_applicability_rules ADD CONSTRAINT fk_arc_rules_revision_tenant "
    "FOREIGN KEY (revision_id, tenant_id) REFERENCES arc_revisions (revision_id, tenant_id) "
    "DEFERRABLE INITIALLY DEFERRED",
]

# consumed_at IS NOT NULL if and only if exactly one receipt references the
# challenge — checked at COMMIT rather than per statement, so the resolution
# transaction may write the receipt and consume the challenge in either order.
_ARC_CHALLENGE_CONSUMPTION_FN = """
CREATE FUNCTION arc_check_challenge_consumption() RETURNS TRIGGER AS $$
DECLARE
    target_challenge UUID;
    receipt_count    INTEGER;
    is_consumed      BOOLEAN;
BEGIN
    IF TG_TABLE_NAME = 'arc_context_challenges' THEN
        target_challenge := NEW.challenge_id;
    ELSE
        target_challenge := NEW.challenge_id;
    END IF;

    SELECT count(*) INTO receipt_count
      FROM arc_receipts WHERE challenge_id = target_challenge;

    SELECT consumed_at IS NOT NULL INTO is_consumed
      FROM arc_context_challenges WHERE challenge_id = target_challenge;

    IF is_consumed IS NULL THEN
        RETURN NULL;
    END IF;

    IF is_consumed AND receipt_count <> 1 THEN
        RAISE EXCEPTION
            'arc challenge % is consumed but has % receipts (expected exactly 1)',
            target_challenge, receipt_count;
    END IF;

    IF NOT is_consumed AND receipt_count <> 0 THEN
        RAISE EXCEPTION
            'arc challenge % has % receipts but is not marked consumed',
            target_challenge, receipt_count;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_ARC_CHALLENGE_CONSUMPTION_TRIGGERS = [
    "CREATE CONSTRAINT TRIGGER trg_arc_challenge_consumption_on_challenge "
    "AFTER INSERT OR UPDATE ON arc_context_challenges "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION arc_check_challenge_consumption()",
    "CREATE CONSTRAINT TRIGGER trg_arc_challenge_consumption_on_receipt "
    "AFTER INSERT OR UPDATE ON arc_receipts "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION arc_check_challenge_consumption()",
]

# Receipts are the non-repudiation record; the data model requires retaining
# them at least 365 days, and legal hold suspends deletion outright. A plain
# downgrade would drop them, so it refuses when there is anything to lose. The
# escape is deliberate and per-session:
#     SET arc.allow_destructive_downgrade = 'on';
_ARC_DOWNGRADE_GUARD = """
DO $$
DECLARE
    receipt_count INTEGER;
    held_count    INTEGER;
BEGIN
    IF coalesce(current_setting('arc.allow_destructive_downgrade', true), 'off') = 'on' THEN
        RETURN;
    END IF;

    SELECT count(*) INTO receipt_count FROM arc_receipts;
    SELECT count(*) INTO held_count FROM arc_revisions WHERE legal_hold;

    IF receipt_count > 0 OR held_count > 0 THEN
        RAISE EXCEPTION
            'refusing to downgrade: % context receipt(s) and % legal-held revision(s) '
            'would be destroyed. Receipts are retained audit evidence. Archive them '
            'first, then re-run with: SET arc.allow_destructive_downgrade = ''on'';',
            receipt_count, held_count;
    END IF;
END
$$
"""


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Statements are issued one per op.execute — asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.

    # Alembic creates alembic_version itself, before this function runs, with
    # a varchar(32) column regardless of the version_num_col_type passed to
    # context.configure() in env.py. A revision id longer than 32 characters
    # would fail to record on its own upgrade, so this widens the column
    # unconditionally rather than relying on that configuration option.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE TEXT")

    op.execute(_EXT_PGCRYPTO_IF_AVAILABLE)
    op.execute(_EXT_VECTOR)

    # --- Section 2: tenancy, actors, vocabulary ---
    op.execute(_TENANTS_DDL)
    op.execute(_TENANTS_EXTERNAL_ID_IDX)
    op.execute(_ACTORS_DDL)
    for stmt in _ACTORS_INDEXES:
        op.execute(stmt)
    op.execute(_VOCAB_DDL)
    for stmt in _VOCAB_INDEXES:
        op.execute(stmt)
    op.execute(_RATE_LIMITS_DDL)
    for stmt in _RATE_LIMITS_INDEXES:
        op.execute(stmt)

    # The seed tenant every system vocabulary row (below, and every later
    # section's seeds) is inserted against. Must precede any vocabulary_values
    # insert — the FK requires it to already exist.
    op.execute(
        f"INSERT INTO tenants (tenant_id, slug, display_name) "
        f"VALUES ('{DEFAULT_TENANT_UUID}', 'default', 'Default Tenant')"
    )

    for kind, value in _VOCAB_SEEDS:
        op.execute(
            f"INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
            f"VALUES ('{DEFAULT_TENANT_UUID}', '{kind}', '{value}', TRUE) ON CONFLICT DO NOTHING"
        )

    # --- Section 3: audit log ---
    op.execute(_AUDIT_LOG_DDL)
    for stmt in _AUDIT_LOG_INDEXES:
        op.execute(stmt)
    for suffix, from_iso, to_iso in _monthly_partition_bounds(_FIXED_PARTITION_START, _FIXED_PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE audit_log_{suffix} PARTITION OF audit_log FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )

    op.execute(_AUDIT_LOG_NEW_DDL)
    for stmt in _AUDIT_LOG_NEW_INDEXES:
        op.execute(stmt)
    for suffix, from_iso, to_iso in _monthly_partition_bounds(_FIXED_PARTITION_START, _FIXED_PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE audit_log_new_{suffix} PARTITION OF audit_log_new "
            f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )

    # --- Section 4: entities, attributes, edges, closure cache ---
    op.execute(_ENTITIES_DDL)
    for stmt in _ENTITIES_INDEXES:
        op.execute(stmt)
    op.execute(_ATTRIBUTES_DDL)
    for stmt in _ATTRIBUTES_INDEXES:
        op.execute(stmt)
    op.execute(_EDGES_DDL)
    for stmt in _EDGES_INDEXES:
        op.execute(stmt)
    op.execute(_EDGE_PROPERTY_SCHEMAS_DDL)
    op.execute(_EDGE_PROPERTY_SCHEMAS_IDX)
    op.execute(_CLOSURE_CACHE_DDL)
    for stmt in _CLOSURE_CACHE_INDEXES:
        op.execute(stmt)
    op.execute(_CLOSURE_OUTBOX_DDL)
    op.execute(_CLOSURE_OUTBOX_IDX)
    op.execute(_EXTERNAL_SYSTEMS_DDL)
    op.execute(_ENTITY_EXTERNAL_IDS_DDL)
    for stmt in _ENTITY_EXTERNAL_IDS_INDEXES:
        op.execute(stmt)
    op.execute(_INTEGRATION_PAIRS_DDL)
    for stmt in _INTEGRATION_PAIRS_INDEXES:
        op.execute(stmt)
    op.execute(_INTEGRATION_PAIRS_TRIGGER_FUNC)
    op.execute(_INTEGRATION_PAIRS_TRIGGER)

    # --- Section 5: facts, embeddings, outboxes ---
    op.execute(_FACTS_DDL)
    for stmt in _FACTS_INDEXES:
        op.execute(stmt)

    buckets = _embedding_hash_buckets()
    op.execute(_embeddings_ddl(_embedding_vector_dim()))
    for bucket in range(buckets):
        op.execute(_EMBEDDINGS_PARTITION_TEMPLATE.format(n=bucket, modulus=buckets))
    op.execute(_EMBEDDINGS_SOURCE_IDX)
    op.execute(_EMBEDDINGS_MODEL_IDX)
    op.execute(_EMBEDDINGS_FTS_IDX)
    for bucket in range(buckets):
        op.execute(_EMBEDDINGS_HNSW_TEMPLATE.format(n=bucket))

    op.execute(_OUTBOX_DDL)
    for stmt in _OUTBOX_INDEXES:
        op.execute(stmt)
    op.execute(_OUTBOX_FAILED_DDL)
    op.execute(_OUTBOX_FAILED_IDX)

    # --- Section 6: schema registry ---
    op.execute(_CAPABILITY_TYPE_SCHEMAS_DDL)
    op.execute(_CAPABILITY_TYPE_SCHEMAS_IDX)
    op.execute(
        "INSERT INTO capability_type_schemas "
        "(schema_id, tenant_id, type_name, json_schema, is_advisory, t_valid_from, t_ingested_at) "
        f"VALUES ('{_INTEGRATION_TYPE_SCHEMA_ID}', '{DEFAULT_TENANT_UUID}', 'integration', "
        f"CAST('{_INTEGRATION_TYPE_SCHEMA_JSON}' AS jsonb), FALSE, now(), now()) ON CONFLICT DO NOTHING"
    )

    # --- Section 7: workspaces ---
    op.execute(_WORKSPACES_DDL)
    for stmt in _WORKSPACES_INDEXES:
        op.execute(stmt)
    op.execute(_WORKSPACE_OWNER_KIND_IMMUTABLE_FUNC)
    op.execute(_WORKSPACE_OWNER_KIND_IMMUTABLE_TRIGGER)
    op.execute(_WORKSPACE_ENTRIES_DDL)
    for stmt in _WORKSPACE_ENTRIES_INDEXES:
        op.execute(stmt)

    # --- Section 8: provider/consumer adoption and notifications ---
    op.execute(_ADOPTION_EVENTS_DDL)
    for stmt in _ADOPTION_EVENTS_INDEXES:
        op.execute(stmt)
    op.execute(_SUBSCRIPTIONS_DDL)
    for stmt in _SUBSCRIPTIONS_INDEXES:
        op.execute(stmt)

    op.execute(_NOTIFICATIONS_DDL)
    for stmt in _NOTIFICATIONS_INDEXES:
        op.execute(stmt)
    today = datetime.date.today()
    suffix, from_iso, to_iso = _current_month_partition_bounds(today)
    op.execute(
        f"CREATE TABLE notifications_{suffix} PARTITION OF notifications FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    op.execute(_NOTIFICATION_DELIVERIES_DDL)
    for stmt in _NOTIFICATION_DELIVERIES_INDEXES:
        op.execute(stmt)
    op.execute(
        f"CREATE TABLE notification_deliveries_{suffix} "
        f"PARTITION OF notification_deliveries FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    # --- Section 9: sync infrastructure ---
    op.execute(_SYNC_SOURCES_DDL)
    for stmt in _SYNC_SOURCES_INDEXES:
        op.execute(stmt)
    op.execute(_SYNC_RUNS_DDL)
    for stmt in _SYNC_RUNS_INDEXES:
        op.execute(stmt)
    op.execute(_WEBHOOK_DELIVERIES_DDL)
    op.execute(_WEBHOOK_DELIVERIES_IDX)
    op.execute(_FACTS_SYNC_RUN_FK)

    # --- Section 10: progression ---
    op.execute(_PROGRESSION_DEFINITIONS_DDL)
    op.execute(_PROGRESSION_DEFINITIONS_IDX)
    op.execute(_PROGRESSION_OVERRIDES_DDL)
    op.execute(_PROGRESSION_OVERRIDES_IDX)

    # --- Section 11: PII governance ---
    op.execute(_PII_PATTERNS_DDL)
    op.execute(_PII_PATTERNS_IDX)
    op.execute(_PII_FIELD_POLICIES_DDL)
    for stmt in _PII_FIELD_POLICIES_INDEXES:
        op.execute(stmt)
    op.execute(_PII_DETECTION_LOG_DDL)
    for stmt in _PII_DETECTION_LOG_INDEXES:
        op.execute(stmt)
    suffix, from_iso, to_iso = _current_month_partition_bounds(today)
    op.execute(
        f"CREATE TABLE pii_detection_log_{suffix} PARTITION OF pii_detection_log "
        f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    )

    for name, category, regex, detector_module in _SYSTEM_PII_PATTERNS:
        pattern_id = _SYSTEM_PII_PATTERN_IDS[name]
        regex_sq = regex.replace("'", "''").replace(":", r"\:")
        detector_expr = "NULL" if detector_module is None else f"'{detector_module}'"
        op.execute(
            "INSERT INTO pii_patterns "
            "(pattern_id, tenant_id, name, category, regex, is_system, detector_module, is_enabled, created_by) "
            f"VALUES ('{pattern_id}'::uuid, '{DEFAULT_TENANT_UUID}'::uuid, '{name}', '{category}', "
            f"'{regex_sq}', TRUE, {detector_expr}, TRUE, NULL) ON CONFLICT DO NOTHING"
        )

    # --- Section 12: idempotency ---
    op.execute(_IDEMPOTENCY_KEYS_DDL)
    op.execute(_IDEMPOTENCY_KEYS_IDX)

    # --- Section 13: session events ---
    op.execute(_MEMORY_SESSION_EVENTS_DDL)
    for stmt in _MEMORY_SESSION_EVENTS_INDEXES:
        op.execute(stmt)

    # --- Section 14: memory claims substrate ---
    op.execute(_MEMORY_CLAIMS_DDL)
    for stmt in _MEMORY_CLAIMS_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_CLAIM_PROVENANCE_DDL)
    for stmt in _MEMORY_CLAIM_PROVENANCE_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_CLAIM_CONTEST_DDL)
    for stmt in _MEMORY_CLAIM_CONTEST_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_CLAIM_CLUSTER_DDL)
    for stmt in _MEMORY_CLAIM_CLUSTER_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_CONFIDENCE_POLICY_DDL)
    op.execute(_MEMORY_CONFIDENCE_POLICY_IDX)
    op.execute(_MEMORY_STRATEGY_CONFIG_DDL)
    op.execute(_MEMORY_STRATEGY_CONFIG_IDX)
    op.execute(_MEMORY_EXTRACTION_OUTBOX_DDL)
    for stmt in _MEMORY_EXTRACTION_OUTBOX_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_EXTRACTION_OUTBOX_FAILED_DDL)
    for stmt in _MEMORY_EXTRACTION_OUTBOX_FAILED_INDEXES:
        op.execute(stmt)

    # --- Section 15: calibration ---
    op.execute(_MEMORY_CLAIM_ADJUDICATION_DDL)
    for stmt in _MEMORY_CLAIM_ADJUDICATION_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_CALIBRATION_MAPPING_DDL)
    for stmt in _MEMORY_CALIBRATION_MAPPING_INDEXES:
        op.execute(stmt)

    # --- Section 16: promotion ---
    op.execute(_MEMORY_PROMOTION_PROPOSAL_DDL)
    for stmt in _MEMORY_PROMOTION_PROPOSAL_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_PROMOTION_JOURNAL_DDL)
    for stmt in _MEMORY_PROMOTION_JOURNAL_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_AUTOPROMOTE_ALLOWLIST_DDL)
    op.execute(_MEMORY_PROMOTION_POLICY_DDL)
    op.execute(_MEMORY_PROMOTION_REJECTION_DDL)

    # --- Section 17: capability requests and source governance ---
    op.execute(_MEMORY_CAPABILITY_REQUEST_DDL)
    for stmt in _MEMORY_CAPABILITY_REQUEST_INDEXES:
        op.execute(stmt)
    op.execute(_MEMORY_REQUEST_TRANSITION_DDL)
    op.execute(_MEMORY_REQUEST_TRANSITION_IDX)
    op.execute(_MEMORY_SOURCE_GOVERNANCE_DDL)
    for stmt in _MEMORY_SOURCE_GOVERNANCE_INDEXES:
        op.execute(stmt)

    # --- Section 18: usage ---
    op.execute(_USAGE_EVENTS_DDL)
    for suffix, from_iso, to_iso in _monthly_partition_bounds(_FIXED_PARTITION_START, _FIXED_PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE usage_events_{suffix} PARTITION OF usage_events "
            f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )
    for stmt in _USAGE_EVENTS_INDEXES:
        op.execute(stmt)
    op.execute(_USAGE_ROLLUP_TENANT_DAY_DDL)
    op.execute(_USAGE_ROLLUP_CAPABILITY_DAY_DDL)
    op.execute(_USAGE_ROLLUP_TOOL_DAY_DDL)
    for stmt in _USAGE_ROLLUP_INDEXES:
        op.execute(stmt)

    # --- Section 19: ARC ---
    op.execute(_ARC_DEPLOYMENT_TENANT_DDL)
    op.execute(_ARC_ARTIFACTS_DDL)
    for stmt in _ARC_ARTIFACTS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_REVISIONS_DDL)
    for stmt in _ARC_REVISIONS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_DIRECTIVE_IDENTITIES_DDL)
    op.execute(_ARC_CONFLICT_DOMAINS_DDL)
    op.execute(_ARC_DIRECTIVES_DDL)
    for stmt in _ARC_DIRECTIVES_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_RULES_DDL)
    for stmt in _ARC_RULES_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_OBLIGATIONS_DDL)
    for stmt in _ARC_OBLIGATIONS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_HOST_KEYS_DDL)
    for stmt in _ARC_HOST_KEYS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_RECEIPT_KEYS_DDL)
    op.execute(_ARC_APPROVAL_VERIFIERS_DDL)
    op.execute(_ARC_APPROVAL_EVIDENCE_DDL)
    for stmt in _ARC_APPROVAL_EVIDENCE_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_EXCEPTIONS_DDL)
    for stmt in _ARC_EXCEPTIONS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_EVIDENCE_REVOCATIONS_DDL)
    op.execute(_ARC_CHALLENGES_DDL)
    for stmt in _ARC_CHALLENGES_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_RECEIPTS_DDL)
    for stmt in _ARC_RECEIPTS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_RECEIPT_EVENTS_DDL)
    for stmt in _ARC_RECEIPT_EVENTS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_EVENT_HEADS_DDL)
    op.execute(_ARC_SELECTED_REVISIONS_DDL)
    for stmt in _ARC_SELECTED_REVISIONS_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_SELECTED_DIRECTIVES_DDL)
    for stmt in _ARC_SELECTED_DIRECTIVES_INDEXES:
        op.execute(stmt)
    op.execute(_ARC_AUDIT_OUTBOX_DDL)
    for stmt in _ARC_AUDIT_OUTBOX_INDEXES:
        op.execute(stmt)
    for stmt in _ARC_DEFERRED_FKS:
        op.execute(stmt)
    op.execute(_ARC_CHALLENGE_CONSUMPTION_FN)
    for stmt in _ARC_CHALLENGE_CONSUMPTION_TRIGGERS:
        op.execute(stmt)


# Reverse dependency order: last-created-first, so no foreign key outlives its
# target. There is no "previous schema" to restore — see the module
# docstring — so this drops every table the upgrade creates and leaves an
# empty database, rather than attempting to recreate 47 revisions' worth of
# intermediate shapes.
def downgrade() -> None:
    op.execute(_ARC_DOWNGRADE_GUARD)
    op.execute("DROP TRIGGER IF EXISTS trg_arc_challenge_consumption_on_receipt ON arc_receipts")
    op.execute("DROP TRIGGER IF EXISTS trg_arc_challenge_consumption_on_challenge ON arc_context_challenges")
    op.execute("DROP FUNCTION IF EXISTS arc_check_challenge_consumption()")
    op.execute("ALTER TABLE arc_revisions DROP CONSTRAINT IF EXISTS fk_arc_revisions_approval_evidence")
    op.execute("ALTER TABLE arc_approval_evidence DROP CONSTRAINT IF EXISTS fk_arc_evidence_approved_revision")
    op.execute("ALTER TABLE arc_approved_exceptions DROP CONSTRAINT IF EXISTS fk_arc_exceptions_approval_evidence")
    op.execute("ALTER TABLE arc_approval_evidence DROP CONSTRAINT IF EXISTS fk_arc_evidence_approved_exception")
    op.execute("ALTER TABLE arc_revisions DROP CONSTRAINT IF EXISTS fk_arc_revisions_superseded_by")
    op.execute("ALTER TABLE arc_host_attestation_keys DROP CONSTRAINT IF EXISTS fk_arc_host_keys_replacement")
    op.execute("ALTER TABLE arc_receipt_signing_keys DROP CONSTRAINT IF EXISTS fk_arc_receipt_keys_replacement")
    op.execute("ALTER TABLE arc_directives DROP CONSTRAINT IF EXISTS fk_arc_directives_revision_tenant")
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT IF EXISTS fk_arc_rules_revision_tenant")

    for table in (
        "arc_audit_outbox",
        "arc_receipt_selected_directives",
        "arc_receipt_selected_revisions",
        "arc_receipt_event_heads",
        "arc_receipt_events",
        "arc_receipts",
        "arc_context_challenges",
        "arc_approval_evidence_revocations",
        "arc_approved_exceptions",
        "arc_approval_evidence",
        "arc_approval_verifiers",
        "arc_receipt_signing_keys",
        "arc_host_attestation_keys",
        "arc_mandatory_obligations",
        "arc_applicability_rules",
        "arc_directives",
        "arc_conflict_domains",
        "arc_directive_identities",
        "arc_revisions",
        "arc_artifacts",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute(f"DELETE FROM tenants WHERE tenant_id = '{_ARC_DEPLOYMENT_TENANT_ID}' AND slug = '_deployment'")

    for table in (
        "usage_rollup_tool_day",
        "usage_rollup_capability_day",
        "usage_rollup_tenant_day",
        "usage_events",
        "memory_source_governance",
        "memory_request_transition",
        "memory_capability_request",
        "memory_promotion_rejection",
        "memory_promotion_policy",
        "memory_autopromote_allowlist",
        "memory_promotion_journal",
        "memory_promotion_proposal",
        "memory_calibration_mapping",
        "memory_claim_adjudication",
        "memory_extraction_outbox_failed",
        "memory_extraction_outbox",
        "memory_strategy_config",
        "memory_confidence_policy",
        "memory_claim_cluster",
        "memory_claim_contest",
        "memory_claim_provenance",
        "memory_claims",
        "memory_session_events",
        "idempotency_keys",
        "pii_detection_log",
        "pii_field_policies",
        "pii_patterns",
        "progression_overrides",
        "progression_definitions",
        "webhook_deliveries",
        "sync_runs",
        "sync_sources",
        "notification_deliveries",
        "notifications",
        "subscriptions",
        "adoption_events",
        "workspace_entries",
        "workspaces",
        "capability_type_schemas",
        "embedding_outbox_failed",
        "embedding_outbox",
        "embeddings",
        "facts",
        "integration_pairs",
        "entity_external_ids",
        "external_systems",
        "closure_outbox",
        "closure_cache",
        "edge_property_schemas",
        "edges",
        "attributes",
        "entities",
        "audit_log_new",
        "audit_log",
        "rate_limits",
        "vocabulary_values",
        "actors",
        "tenants",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP TRIGGER IF EXISTS trg_integration_pairs ON edges")
    op.execute("DROP FUNCTION IF EXISTS populate_integration_pairs()")
    op.execute("DROP FUNCTION IF EXISTS check_workspace_owner_kind_immutable()")
    # Extensions are left in place — they may be shared with other databases
    # on the same cluster.
