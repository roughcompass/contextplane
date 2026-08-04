"""SQLAlchemy 2.0 declarative ORM mapped classes for the catalog schema.

Performance-critical indexes are declared both in the Alembic migration that
creates them (authoritative DDL source) and in ``__table_args__`` on the
relevant model class (documentation for service-code readers). PARTITION
declarations live in the migrations only.

The ORM exists to give service code a typed Python surface; SQL constraints
(NOT NULL, CHECK, foreign keys) are the authoritative isolation guard.

`TenantMixin` adds an `INSERT`-time assertion that `tenant_id is not None` —
defense-in-depth on top of the SQL `NOT NULL` constraint.

`Fact.sync_run_id` is a nullable UUID column with no FK until the sync_runs
migration runs (the FK is activated in the migration that creates sync_runs).

`RateLimit` mapped class enforces per-tenant rate budgets.
Default role seeding (4 roles per tenant: consumer, producer, admin, auditor) is
performed by `CatalogService.seed_default_roles(session, tenant_id)` at tenant
creation time — not in the migration.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Mapper,
    mapped_column,
)


class Base(DeclarativeBase):
    pass


class TenantMixin:
    """Defense-in-depth: every tenant-scoped row must carry a non-NULL tenant_id."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


def _assert_tenant_id(_mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    if target.tenant_id is None:
        msg = f"{type(target).__name__} insert without tenant_id (TenantMixin invariant)"
        raise ValueError(msg)


@event.listens_for(TenantMixin, "before_insert", propagate=True)
def _tenant_mixin_before_insert(mapper: Mapper[Any], connection: Any, target: Any) -> None:
    _assert_tenant_id(mapper, connection, target)


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Opaque ID assigned by an upstream identity system. NULL for manually-provisioned
    # tenants. Uniqueness among non-NULL rows is enforced by a partial DB index
    # (ix_tenants_external_tenant_id_provider) — see the 0015 migration.
    external_tenant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How this tenant was created. CHECK constraint in DB enforces 'manual' | 'jit' | 'system'.
    # The specific upstream source name belongs in audit-log payloads, not here.
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    # Operator override for entitlement-resolved tenant materialization: when
    # set, the JIT path refuses to use this tenant and the middleware drops
    # the tuple. Audit-log FKs are preserved (rows are NOT deleted).
    disabled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Actor(Base, TenantMixin):
    __tablename__ = "actors"

    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # `email` and `actor_kind` are retained for the sync-worker subsystem
    # (registry/ingest/runner.py uses actor_kind to distinguish humans from workers).
    # The auth ADR called for slimming these out, but the sync path predates
    # and is independent of the auth rewrite — a deeper refactor of the
    # sync-actor representation is out of scope for the auth consolidation.
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False, default="human")

    __table_args__ = (UniqueConstraint("tenant_id", "oidc_subject", name="uq_actors_tenant_oidc_subject"),)


#: The one vocabulary kind that may exist at organization scope. Living memory
#: needs a predicate to mean the same thing in every tenant, or two tenants'
#: claims about the same subject cannot be compared at all.
CLAIM_PREDICATE_KIND = "claim_predicate"


class VocabularyValue(Base):
    """A vocabulary term, tenant-scoped except for global claim predicates.

    Deliberately **not** `TenantMixin`. That mixin asserts a non-NULL tenant on
    every insert, which is right for every other model and would reject the
    global predicates this table now has to hold. Rather than weaken the mixin
    -- which would quietly relax the invariant for every model using it -- this
    model carries its own narrower rule, enforced below: a row needs a tenant
    unless it is a global claim predicate.

    The database holds the same rule as a CHECK. Both exist because they fail
    at different moments: the CHECK catches anything reaching the table by any
    path, and the listener catches it at the point of the mistake with a
    message naming the model.
    """

    __tablename__ = "vocabulary_values"

    vocab_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Nullable only for global claim predicates -- see the class docstring and
    # `ck_vocab_global_is_claim_predicate`.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deprecated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Required for every claim predicate, global or local. A predicate with no
    # declared type cannot validate anything written against it, which is the
    # failure that makes claims incomparable in the first place.
    value_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many values of this predicate may hold at one instant. Nullable in the
    # ORM because every other vocabulary kind has no cardinality; a CHECK
    # requires it for claim predicates specifically.
    value_cardinality: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def scope(self) -> str:
        """`global` or `tenant`. Callers need to tell them apart on read."""
        return "tenant" if self.tenant_id is not None else "global"


@event.listens_for(VocabularyValue, "before_insert")
def _vocabulary_tenancy_rule(_mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    """A vocabulary row needs a tenant unless it is a global claim predicate.

    Narrower than `TenantMixin`'s rule and deliberately its own listener: a
    blanket bypass would let any vocabulary kind go global, and the point of
    this requirement is that exactly one kind may.
    """
    if target.tenant_id is not None:
        return
    if target.kind != CLAIM_PREDICATE_KIND:
        msg = (
            f"VocabularyValue insert without tenant_id for kind {target.kind!r}; "
            f"only {CLAIM_PREDICATE_KIND!r} may be global"
        )
        raise ValueError(msg)


class Entity(Base, TenantMixin):
    __tablename__ = "entities"
    __table_args__ = (
        # Supports keyset pagination: WHERE tenant_id = :t AND (created_at, entity_id) < (:ts, :id)
        # ORDER BY created_at DESC, entity_id.  Without this index Postgres scans all
        # tenant rows and sorts them before applying LIMIT — degrades linearly with table size.
        # Authoritative DDL: migration 0013_missing_indexes.
        Index("idx_entities_tenant_created", "tenant_id", "created_at", "entity_id"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    # visibility column added by the provider/consumer Alembic migration.
    # CHECK (visibility IN ('private', 'tenant-shared', 'public'))
    # ORM column declared here so service code compiles before the migration runs.
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private")


# notification_deliveries is queried via raw SQL (see workers/webhook_delivery.py).
# The partial index on that table is declared here for discoverability:
#
#   idx_delivery_pending_sort  ON notification_deliveries
#       (tenant_id, next_retry_at, attempted_at) WHERE status = 'pending'
#
# The webhook worker's claim query sorts by next_retry_at NULLS FIRST, attempted_at.
# Including attempted_at in the index avoids a re-sort pass on the filtered rows.
# Authoritative DDL: migration 0013_missing_indexes.


class Attribute(Base, TenantMixin):
    __tablename__ = "attributes"

    attr_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class Fact(Base, TenantMixin):
    __tablename__ = "facts"

    fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_authoritative_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # FK to sync_runs(sync_run_id) activated by the sync-infra migration once
    # the sync_runs table exists.
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_runs.sync_run_id"), nullable=True
    )
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class Edge(Base, TenantMixin):
    __tablename__ = "edges"

    edge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    src_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    rel: Mapped[str] = mapped_column(Text, nullable=False)
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class AuditLog(Base, TenantMixin):
    __tablename__ = "audit_log"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    before_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)


# --- Schema registry additions ---


class CapabilityTypeSchema(Base, TenantMixin):
    __tablename__ = "capability_type_schemas"

    schema_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    type_name: Mapped[str] = mapped_column(Text, nullable=False)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


# --- Embedding additions ---


class Embedding(Base, TenantMixin):
    """One row per embedded text chunk, for any kind of thing worth embedding.

    Identified by `(target_type, target_id)`: `target_type` names the kind of row and
    `target_id` points at it. There is no foreign key on `target_id` because it addresses
    more than one table — the closed CHECK on `target_type` plus a single enqueuer per
    kind is what keeps it honest. The pair is this schema's existing vocabulary for a
    polymorphic reference; `audit_log` uses the same two names to mean the same thing.

    `chunk_index` is 0 for a whole-body embed and greater for sliding-window chunks.
    Claims always use 0 — a claim is one assertion, and splitting it would make it
    compete against itself in a ranking.

    `ts_vector` is GENERATED ALWAYS and managed by Postgres, so it is deliberately not
    mapped here: the ORM must never try to write it.

    The physical table is `PARTITION BY HASH (tenant_id)` with child partitions
    `embeddings_p{n}`, each carrying its own HNSW index. SQLAlchemy does not declare
    native partitioning, so this mapping targets the parent and the planner prunes to one
    bucket whenever a query filters `tenant_id` — which every read path does. The primary
    key is composite because a partitioned table requires the partition key in every
    unique constraint.
    """

    __tablename__ = "embeddings"

    embedding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Width is deliberately left unconstrained here. Migrations own the DDL, and
    # the column's real dimension follows EMBEDDING_DIM; restating a literal in
    # the ORM would just be a second copy to keep in sync, silently wrong for any
    # deployment running a different width. Startup verifies the live column
    # against the configured dimension.
    vector: Mapped[Any] = mapped_column(Vector(), nullable=False)
    text_chunk: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmbeddingOutbox(Base, TenantMixin):
    """Transactional outbox for the async embedding drain job.

    Written in the same transaction as the row it describes, so a rollback removes both
    atomically. The drain deletes a row after inserting its vectors.

    The row carries `text_to_embed` and `chunk_plan`, so the drain never reads the source
    table. That is what makes the consumer type-blind: adding a new kind of target needs
    a new producer and no change to the drain at all.

    One pending row per target, enforced by a unique key, so repeated edits collapse into
    one request carrying the newest text rather than queueing several embeddings of
    successively staler text.
    """

    __tablename__ = "embedding_outbox"

    outbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    text_to_embed: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_plan: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enqueued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmbeddingOutboxFailed(Base, TenantMixin):
    """Dead-letter table for outbox rows that exceeded `outbox_max_attempts`.

    The drain job moves rows here after `attempts >= settings.outbox_max_attempts`
    (default 5).  A Prometheus alert fires when this table grows.
    """

    __tablename__ = "embedding_outbox_failed"

    failed_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    text_to_embed: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_plan: Mapped[Any] = mapped_column(JSONB, nullable=False)
    failed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_text: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


# --- Sync infrastructure additions ---


class SyncSource(Base, TenantMixin):
    """One row per configured connector source.

    `source_type` is vocab-validated at the service layer against
    `vocabulary_values` (kind='source_type'); no DB CHECK constraint is
    used here, matching the pattern for `entity_type` on `entities`.

    `config` is an opaque JSONB blob; the connector implementation is
    responsible for interpreting it.  `credentials_ref` is an environment
    variable name resolved at runtime — never stored as a credential value.
    """

    __tablename__ = "sync_sources"

    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[Any] = mapped_column(JSONB, nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class SyncRun(Base, TenantMixin):
    """One row per execution of a sync source ingestion.

    `status` and `trigger` are CHECK-constrained at the DB level
    (allowed status values: 'running', 'done', 'partial', 'failed';
    allowed trigger values: 'scheduled', 'webhook', 'manual').  The ORM
    does not re-declare these constraints — they live in the migration DDL.

    `duration_s` and `artifact_count` are NULL until the run finishes.
    `error_summary` is set when status is 'partial' or 'failed'.
    """

    __tablename__ = "sync_runs"

    sync_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_sources.source_id"), nullable=False
    )
    # Allowed values: 'running' | 'done' | 'partial' | 'failed' (CHECK in DB)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Allowed values: 'scheduled' | 'webhook' | 'manual' (CHECK in DB)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class WebhookDelivery(Base, TenantMixin):
    """Idempotency log for inbound webhook payloads.

    Composite PK `(tenant_id, delivery_id)` ensures a given provider-assigned
    delivery ID cannot be processed twice within a tenant.  `processed_at`
    being NULL indicates the payload arrived but has not yet been drained.
    """

    __tablename__ = "webhook_deliveries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_sources.source_id"), nullable=False
    )
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Role grants are not modelled in this catalog DB — they live in the
# entitlement service and are resolved per-request via the claim resolver.


# --- External-ID registry ---


class ExternalSystem(Base, TenantMixin):
    """Registry of upstream external systems whose IDs are mapped onto entities.

    ``(tenant_id, slug)`` is the composite primary key — slugs are
    tenant-scoped so different tenants may independently use the same slug.
    ``url_template`` is optional; when present the service substitutes
    ``{external_id}`` at mapping-insert time.
    """

    __tablename__ = "external_systems"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    url_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntityExternalId(Base, TenantMixin):
    """Passive external-system ID mapping.  Hard-delete only; no soft-history.

    External IDs are immutable once written; use hard-delete and re-insert
    to replace them.  Unique constraint ``uq_entity_external_id`` on
    ``(tenant_id, external_system_slug, external_id)`` is enforced at the DB
    level; the service converts ``IntegrityError`` to ``ConflictError``.
    """

    __tablename__ = "entity_external_ids"

    external_id_pk: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    external_system_slug: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- PII pattern admin ---


class PiiPatternRow(Base, TenantMixin):
    """Tenant PII pattern registry row (both built-in system rows and custom tenant rows).

    ``is_system=True`` rows are seeded by the graph-primitives migration and must
    not be modified or deleted by tenant admins (403).  ``regex='__entropy__'`` is
    the sentinel for the entropy-based aws_secret_key pattern.

    ``policy_override`` overrides the tenant-default policy for this pattern;
    NULL means "fall back to tenant default" (level 2 in the three-level
    resolution hierarchy).

    ``uq_pii_pattern_tenant_name`` index enforces name uniqueness per tenant.
    """

    __tablename__ = "pii_patterns"

    pattern_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    regex: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detector_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class PiiFieldPolicyRow(Base, TenantMixin):
    """Per-field (and optionally per-pattern) PII policy override.

    ``pattern_id`` may be NULL, meaning the policy applies to ALL patterns for
    this field.  The DB unique index ``uq_field_policy`` uses
    ``COALESCE(pattern_id, zero_uuid)`` so that at most one NULL-pattern row
    exists per ``(tenant_id, field_type)`` pair.
    """

    __tablename__ = "pii_field_policies"

    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    field_type: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pii_patterns.pattern_id"), nullable=True
    )
    policy: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RateLimit(Base, TenantMixin):
    """Per-actor (or per-tenant default) rate-limit override row.

    When ``actor_id IS NULL`` the row is the tenant-level default.  The DB
    enforces at most one default row per tenant and at most one per
    (tenant_id, actor_id) pair via partial unique indexes in the migration.

    Reactive activation: per-actor rows are inserted only when a runaway
    actor is detected (OQ3); the tenant default row is inserted at tenant
    creation time by ``CatalogService.seed_default_roles``.
    """

    __tablename__ = "rate_limits"

    limit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    reads_per_second: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    writes_per_second: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Progression definitions ---


class ProgressionDefinition(Base, TenantMixin):
    """Bi-temporal definition of stage-transition rules for an entity type.

    Each row describes how entities of a given ``entity_type`` within a tenant
    may move between stages. ``definition`` is an opaque JSONB blob interpreted
    by the progression service; the schema is validated at write time, not here.

    ``is_advisory`` controls enforcement: FALSE means the service rejects
    invalid transitions; TRUE means it records a warning and allows them.

    Bi-temporal columns follow the registry standard:
      - ``t_valid_from`` / ``t_valid_to``   — real-world validity window
      - ``t_ingested_at`` / ``t_invalidated_at`` — registry observation window

    The unique constraint on ``(tenant_id, entity_type, t_valid_from)`` prevents
    two definitions from starting at the same instant, removing ambiguity when
    the service resolves the active definition for a given (tenant, entity_type).
    """

    __tablename__ = "progression_definitions"

    progression_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[Any] = mapped_column(JSONB, nullable=False)
    is_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProgressionOverride(Base, TenantMixin):
    """Single-use grant authorizing an entity to bypass a gate for a specific transition.

    Each row represents an explicit override issued by an authorized actor. The
    validity window (``t_valid_from`` / ``t_valid_to``) bounds when the override
    may be consumed. ``gate_id`` identifies the specific gate to bypass, or "*"
    meaning any gate on that transition.

    ``bypass_skip_rules`` defaults to False. Set it to True only when the override
    is intended to allow skipping intermediate states as well as the gate check —
    this must be an explicit opt-in per the override schema.

    Single-use invariant: ``consumed_at IS NULL`` means the override is available.
    The progression service writes ``consumed_at`` in the same transaction as the
    transition it authorizes. No DB constraint enforces single-use — the service
    owns this invariant and must check ``consumed_at IS NULL`` before consuming.
    """

    __tablename__ = "progression_overrides"

    override_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    gate_id: Mapped[str] = mapped_column(Text, nullable=False)
    bypass_skip_rules: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False)
    t_valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    t_valid_to: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # References audit_log(audit_id) — the audit record written at the time the
    # override was issued. Column is named audit_event_id to match the domain term
    # used in override-creation requests; the DB FK resolves to audit_log.audit_id.
    audit_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_log.audit_id"), nullable=False
    )


# --- Workspace additions ---


class WorkspaceRecord(Base, TenantMixin):
    """One row per workspace.

    ``owner_kind`` is CHECK-constrained to 'actor' | 'tenant' in the DB.
    When ``owner_kind = 'actor'``, ``owner_actor_id`` must be non-NULL
    (enforced by ``chk_actor_owner`` in the DB).

    ``encryption_tier`` is NOT NULL with a server default of 'none' — it is a
    forward-compatibility column so the regulated-tenant block and future ENC
    detection can read it without a schema change. WS-phase service code only
    reads it to enforce the regulated-tenant gate; it never writes a value other
    than 'none'.

    Soft-delete is implemented via ``t_invalidated_at``: active workspaces always
    have ``t_invalidated_at IS NULL``. Hard-delete is not performed in this phase.

    ``archived_at`` marks a workspace as archived (read-only) without
    soft-deleting it. A non-NULL ``archived_at`` means entry writes are rejected
    by the service layer.
    """

    __tablename__ = "workspaces"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CHECK (owner_kind IN ('actor','tenant')) enforced in DB
    owner_kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    # Forward-compatibility column for future ENC-phase detection. WS-phase code
    # only reads this to enforce the regulated-tenant block; it never writes a
    # value other than 'none'. NOT NULL with DB DEFAULT 'none'.
    encryption_tier: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    archived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class WorkspaceEntryRecord(Base, TenantMixin):
    """One row per entry within a workspace.

    ``body_md`` is NOT NULL in this phase — every entry must carry a plaintext
    body. The ENC-phase ALTER TABLE will drop the NOT NULL constraint and add
    ``body_ciphertext`` / ``body_nonce`` columns at that point. No ciphertext
    columns exist on this ORM class; their presence is a contract violation.

    ``references_jsonb`` is an optional JSONB blob for structured cross-reference
    metadata (e.g. linked entity schemas).

    ``reference_ids`` is a UUID[] column holding the IDs of entities or facts
    this entry directly references. The GIN index ``idx_we_refs`` enables
    efficient ``ANY(reference_ids)`` lookups in the service layer without
    loading every entry row.

    ``kind`` is CHECK-constrained in the DB to the set of known entry kinds
    ('note', 'decision', 'open_question', 'saved_query', 'saved_view').

    Soft-delete via ``t_invalidated_at``. Hard-delete is performed only by the
    RTBF purge path (physical purge, not soft-delete).
    """

    __tablename__ = "workspace_entries"

    entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.workspace_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    # CHECK (kind IN ('note','decision','open_question','saved_query','saved_view'))
    # enforced in DB
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL in this phase: plaintext body required. ENC-phase ALTER drops this
    # constraint and adds body_ciphertext/body_nonce — no ORM change here until then.
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional JSONB blob for structured cross-reference metadata.
    references_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # UUID[] — GIN-indexed (idx_we_refs) for fast ANY(reference_ids) filtering.
    reference_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    t_invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
