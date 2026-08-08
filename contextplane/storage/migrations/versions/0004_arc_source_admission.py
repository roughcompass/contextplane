"""Source admission: configured connectors, authorized uploads, evidence.

Revision ID: 0004_arc_source_admission
Revises: 0003_source_provisioning_flag
Create Date: 2026-08-05

Before this migration, ARC's only record of where a revision came from was
`arc_revisions.source_canonical_locator` plus a caller-supplied digest --
metadata, not evidence, since nothing ever fetched bytes and hashed them or
checked that an upstream authority actually approved them. This adds the two
closed admission authorities and the evidence they produce:

- `arc_source_connectors` / `arc_source_upload_policies` -- the registered
  authorities. A caller admitting a source names one of these; it can never
  supply a fetch URL, host, or credential of its own.
- `arc_source_bodies` -- the admitted bytes plus the digest this deployment
  computed over them, never a caller's assertion.
- `arc_source_approval_evidence` -- the closed `source_approval_evidence_v1`
  envelope: the signed or attested claim, how it was verified, and the
  idempotency identity that makes a retry resolvable instead of a duplicate.
- `arc_source_approval_status` -- local, periodically refreshed validity.
  ARC never follows a revocation URL from evidence; it reads this table.

Five tables, inserted in dependency order (bodies and the two authority
tables before evidence, evidence before status) so every foreign key points
at a row that already exists.
"""

from __future__ import annotations

from alembic import op

revision = "0004_arc_source_admission"
down_revision: str | None = "0003_source_provisioning_flag"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_source_connectors -- configured-connector registrations
# ---------------------------------------------------------------------------

_SOURCE_CONNECTORS_DDL = """
CREATE TABLE arc_source_connectors (
    connector_id         TEXT PRIMARY KEY,
    owning_scope         TEXT NOT NULL,
    tenant_id            UUID REFERENCES tenants(tenant_id),
    allowed_schemes      TEXT[] NOT NULL,
    allowed_hosts        TEXT[] NOT NULL,
    allowed_media_types  TEXT[] NOT NULL,
    allowed_verifier_ids TEXT[] NOT NULL,
    max_bytes            INTEGER NOT NULL,
    credential_ref        TEXT,
    registered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_source_connectors_id_len CHECK (char_length(connector_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_source_connectors_scope CHECK (owning_scope IN ('global', 'tenant')),
    CONSTRAINT ck_arc_source_connectors_scope_tenant CHECK (
        (owning_scope = 'tenant') = (tenant_id IS NOT NULL)
    ),
    CONSTRAINT ck_arc_source_connectors_schemes CHECK (array_length(allowed_schemes, 1) >= 1),
    CONSTRAINT ck_arc_source_connectors_hosts CHECK (array_length(allowed_hosts, 1) >= 1),
    CONSTRAINT ck_arc_source_connectors_media_types CHECK (array_length(allowed_media_types, 1) >= 1),
    CONSTRAINT ck_arc_source_connectors_verifiers CHECK (array_length(allowed_verifier_ids, 1) >= 1),
    -- The wire contract caps registration at this ceiling; the streaming
    -- reader enforces the same number independently (see B.2's byte-ceiling
    -- rule) so a connector cannot be registered above the limit admission
    -- will refuse to stream past anyway.
    CONSTRAINT ck_arc_source_connectors_max_bytes CHECK (max_bytes > 0 AND max_bytes <= 10485760)
)
"""

_SOURCE_CONNECTORS_INDEXES = [
    "CREATE INDEX ix_arc_source_connectors_tenant ON arc_source_connectors (tenant_id)",
]

# ---------------------------------------------------------------------------
# arc_source_upload_policies -- authorized-upload registrations
# ---------------------------------------------------------------------------

_SOURCE_UPLOAD_POLICIES_DDL = """
CREATE TABLE arc_source_upload_policies (
    policy_id            TEXT PRIMARY KEY,
    owning_scope         TEXT NOT NULL,
    tenant_id            UUID REFERENCES tenants(tenant_id),
    allowed_media_types  TEXT[] NOT NULL,
    allowed_verifier_ids TEXT[] NOT NULL,
    max_bytes             INTEGER NOT NULL,
    registered_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_source_policies_id_len CHECK (char_length(policy_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_source_policies_scope CHECK (owning_scope IN ('global', 'tenant')),
    CONSTRAINT ck_arc_source_policies_scope_tenant CHECK (
        (owning_scope = 'tenant') = (tenant_id IS NOT NULL)
    ),
    CONSTRAINT ck_arc_source_policies_media_types CHECK (array_length(allowed_media_types, 1) >= 1),
    CONSTRAINT ck_arc_source_policies_verifiers CHECK (array_length(allowed_verifier_ids, 1) >= 1),
    CONSTRAINT ck_arc_source_policies_max_bytes CHECK (max_bytes > 0 AND max_bytes <= 10485760)
)
"""

_SOURCE_UPLOAD_POLICIES_INDEXES = [
    "CREATE INDEX ix_arc_source_upload_policies_tenant ON arc_source_upload_policies (tenant_id)",
]

# ---------------------------------------------------------------------------
# arc_source_bodies -- admitted bytes + the digest this deployment computed.
#
# Inserted before arc_source_approval_evidence so the evidence row's foreign
# key always points at a body that already exists; `source_evidence_id` is
# minted once in application code and used, unchanged, as the primary key of
# both rows -- there is no circular reference to defer.
# ---------------------------------------------------------------------------

_SOURCE_BODIES_DDL = """
CREATE TABLE arc_source_bodies (
    source_evidence_id UUID PRIMARY KEY,
    content_digest      TEXT NOT NULL,
    content_bytes        INTEGER NOT NULL,
    body                  BYTEA NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_source_bodies_digest_len CHECK (char_length(content_digest) = 64),
    CONSTRAINT ck_arc_source_bodies_bytes CHECK (content_bytes >= 0 AND content_bytes <= 10485760)
)
"""

# ---------------------------------------------------------------------------
# arc_source_approval_evidence -- the closed source_approval_evidence_v1
# envelope, plus the admission and idempotency bookkeeping around it.
# ---------------------------------------------------------------------------

_SOURCE_APPROVAL_EVIDENCE_DDL = """
CREATE TABLE arc_source_approval_evidence (
    source_evidence_id                UUID PRIMARY KEY REFERENCES arc_source_bodies(source_evidence_id),
    owning_scope                       TEXT NOT NULL,
    tenant_id                           UUID REFERENCES tenants(tenant_id),
    source_system                       TEXT NOT NULL,
    source_revision_locator            TEXT NOT NULL,
    source_content_type                TEXT NOT NULL,
    source_content_digest              TEXT NOT NULL,
    claim                               JSONB NOT NULL,
    claim_digest                        TEXT NOT NULL,
    verification_method                TEXT NOT NULL,
    verifier_id                         TEXT NOT NULL,
    signature                           TEXT,
    verifier_attestation                JSONB,
    admission_method                    TEXT NOT NULL,
    connector_id                        TEXT REFERENCES arc_source_connectors(connector_id),
    policy_id                           TEXT REFERENCES arc_source_upload_policies(policy_id),
    admitted_at                         TIMESTAMPTZ NOT NULL,
    admitted_by_issuer                  TEXT NOT NULL,
    admitted_by_subject                 TEXT NOT NULL,
    verified_at                         TIMESTAMPTZ NOT NULL,
    expires_at                          TIMESTAMPTZ NOT NULL,
    idempotency_key_digest              TEXT NOT NULL,
    admission_request_payload_digest    TEXT NOT NULL,
    idempotency_scope_digest            TEXT NOT NULL,
    created_at                           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_source_evidence_scope CHECK (owning_scope IN ('global', 'tenant')),
    CONSTRAINT ck_arc_source_evidence_scope_tenant CHECK (
        (owning_scope = 'tenant') = (tenant_id IS NOT NULL)
    ),
    CONSTRAINT ck_arc_source_evidence_verification_method CHECK (
        verification_method IN ('source_signed', 'verifier_attested')
    ),
    CONSTRAINT ck_arc_source_evidence_admission_method CHECK (
        admission_method IN ('configured_connector', 'authorized_upload')
    ),
    -- The unused representation must be NULL, mirroring the same rule on
    -- arc_approval_evidence: evidence cannot be validated against a proof
    -- shape it did not declare.
    CONSTRAINT ck_arc_source_evidence_representation CHECK (
        (verification_method = 'source_signed'
         AND signature IS NOT NULL AND verifier_attestation IS NULL)
        OR (verification_method = 'verifier_attested'
            AND verifier_attestation IS NOT NULL AND signature IS NULL)
    ),
    CONSTRAINT ck_arc_source_evidence_admission_targets CHECK (
        (admission_method = 'configured_connector' AND connector_id IS NOT NULL AND policy_id IS NULL)
        OR (admission_method = 'authorized_upload' AND policy_id IS NOT NULL AND connector_id IS NULL)
    ),
    CONSTRAINT ck_arc_source_evidence_verifier_id_len CHECK (char_length(verifier_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_source_evidence_claim_digest_len CHECK (char_length(claim_digest) = 64),
    CONSTRAINT ck_arc_source_evidence_content_digest_len CHECK (char_length(source_content_digest) = 64),
    CONSTRAINT ck_arc_source_evidence_idem_key_digest_len CHECK (char_length(idempotency_key_digest) = 64),
    CONSTRAINT ck_arc_source_evidence_payload_digest_len CHECK (
        char_length(admission_request_payload_digest) = 64
    ),
    CONSTRAINT ck_arc_source_evidence_scope_digest_len CHECK (char_length(idempotency_scope_digest) = 64)
)
"""

_SOURCE_APPROVAL_EVIDENCE_INDEXES = [
    "CREATE INDEX ix_arc_source_evidence_tenant ON arc_source_approval_evidence (tenant_id)",
    "CREATE INDEX ix_arc_source_evidence_connector ON arc_source_approval_evidence (connector_id)",
    "CREATE INDEX ix_arc_source_evidence_policy ON arc_source_approval_evidence (policy_id)",
    # The final race guard ADR 039 names: a lock-then-recheck in the service
    # closes almost every window, and this closes the rest.
    "CREATE UNIQUE INDEX uq_arc_source_evidence_scope_digest ON arc_source_approval_evidence "
    "(idempotency_scope_digest)",
]

# ---------------------------------------------------------------------------
# arc_source_approval_status -- local, periodically refreshed validity.
# ---------------------------------------------------------------------------

_SOURCE_APPROVAL_STATUS_DDL = """
CREATE TABLE arc_source_approval_status (
    source_evidence_id     UUID PRIMARY KEY REFERENCES arc_source_approval_evidence(source_evidence_id),
    status                  TEXT NOT NULL,
    checked_at               TIMESTAMPTZ NOT NULL,
    next_check_at            TIMESTAMPTZ NOT NULL,
    status_source            TEXT NOT NULL,
    status_evidence_digest   TEXT,
    CONSTRAINT ck_arc_source_status_vocab CHECK (
        status IN ('current', 'expired', 'revoked', 'unknown', 'overdue')
    ),
    CONSTRAINT ck_arc_source_status_source_len CHECK (char_length(status_source) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_source_status_digest_len CHECK (
        status_evidence_digest IS NULL OR char_length(status_evidence_digest) = 64
    ),
    CONSTRAINT ck_arc_source_status_next_check_order CHECK (next_check_at >= checked_at)
)
"""


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    op.execute(_SOURCE_CONNECTORS_DDL)
    for stmt in _SOURCE_CONNECTORS_INDEXES:
        op.execute(stmt)

    op.execute(_SOURCE_UPLOAD_POLICIES_DDL)
    for stmt in _SOURCE_UPLOAD_POLICIES_INDEXES:
        op.execute(stmt)

    op.execute(_SOURCE_BODIES_DDL)

    op.execute(_SOURCE_APPROVAL_EVIDENCE_DDL)
    for stmt in _SOURCE_APPROVAL_EVIDENCE_INDEXES:
        op.execute(stmt)

    op.execute(_SOURCE_APPROVAL_STATUS_DDL)


def downgrade() -> None:
    # Reverse dependency order: status references evidence, evidence
    # references bodies/connectors/policies.
    op.execute("DROP TABLE arc_source_approval_status")
    op.execute("DROP TABLE arc_source_approval_evidence")
    op.execute("DROP TABLE arc_source_bodies")
    op.execute("DROP TABLE arc_source_upload_policies")
    op.execute("DROP TABLE arc_source_connectors")
