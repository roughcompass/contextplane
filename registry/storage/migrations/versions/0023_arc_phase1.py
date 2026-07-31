"""ARC — governed context artifacts, attested intake, immutable receipts.

Twenty-one tables for attested context resolution. One revision, not several:
no intermediate subset of these tables is usable, so splitting would only add a
partial-apply failure mode.

Creation order avoids foreign-key cycles. Four references are genuinely cyclic
and are added at the end as ``DEFERRABLE INITIALLY DEFERRED`` so a single
transaction can insert both endpoints in either order:

    arc_revisions.approval_evidence_id      <-> arc_approval_evidence.approved_revision_id
    arc_approved_exceptions.approval_evidence_id <-> arc_approval_evidence.approved_exception_id

Global versus tenant scope. ``arc_artifacts.tenant_id IS NULL`` is the only
global-artifact marker, and child revisions, directives, and rules carry NULL
consistently. Request-side tables (challenges, receipts, events, selected rows,
audit outbox) always carry a concrete requesting tenant even when the receipt
selected global artifacts.

Deployment-scope audit. ``audit_log.tenant_id`` is NOT NULL, and ARC's
deployment-global operations have no tenant, so this migration inserts a
reserved tenant row for them to attribute to. See ``_DEPLOYMENT_TENANT_DDL`` for
why it is disabled the way it is.

Two CHECK constraints are deliberately absent — ``arc_revisions.content_classification``
and ``arc_receipt_events.event_type``. Both are specified as closed vocabularies
whose members are defined nowhere in the PRD or architecture. They are bounded by
length so the content-minimization invariant still holds; the CHECKs land once
the vocabularies exist rather than being invented here.
"""

from __future__ import annotations

from alembic import op

revision: str = "0023_arc_phase1"
down_revision: str | None = "0022_configurable_embedding_dim"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# Reserved deployment-scope tenant
# ---------------------------------------------------------------------------
#
# `audit_log.tenant_id` is NOT NULL and all three of its indexes are
# tenant-leading, so a nullable column would keep ARC's global operator events
# and hide them from every existing reader. A reserved row is the less-bad
# option: an auditor reading `audit_log` will see a tenant that is not a
# customer, and that is the cost.
#
# `disabled_at` is what protects the row. It is the operator override the JIT
# materialization path checks (`auth/entitlements/actor_store.py`), and it makes
# the row unusable as a real tenant. `is_active = false` is set for consistency
# with the declared column, but nothing in the codebase gates a tenant on it —
# do not mistake it for the guard.
#
# The slug is a second, independent barrier: `service/slugs.py` requires a
# leading alphanumeric, so no entitlement string can name `_deployment`.
#
# NOT the all-zero UUID, which is already the seed `default` tenant from
# migration 0001. Reusing it looked like it worked — the insert was a no-op
# under ON CONFLICT — and the downgrade then tried to delete the real `default`
# tenant. There is deliberately no ON CONFLICT clause now: if this UUID or slug
# is ever taken, the migration must fail loudly rather than silently skip and
# leave ARC attributing global audit events to somebody's tenant.
_DEPLOYMENT_TENANT_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"

_DEPLOYMENT_TENANT_DDL = f"""
INSERT INTO tenants (
    tenant_id, slug, display_name, created_at, is_active, provider, disabled_at
) VALUES (
    '{_DEPLOYMENT_TENANT_ID}',
    '_deployment',
    'ARC deployment scope (reserved, not a customer tenant)',
    now(),
    false,
    'system',
    now()
)
"""

# ---------------------------------------------------------------------------
# 1. arc_artifacts
# ---------------------------------------------------------------------------

_ARTIFACTS_DDL = """
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

_ARTIFACTS_INDEXES = [
    "CREATE INDEX ix_arc_artifacts_tenant_kind ON arc_artifacts (tenant_id, kind)",
    "CREATE INDEX ix_arc_artifacts_slug ON arc_artifacts (slug)",
    # Scoped uniqueness. A plain UNIQUE (tenant_id, slug) would not constrain
    # global rows, because NULL is never equal to NULL in a unique index —
    # every global artifact could reuse the same slug. COALESCE collapses NULL
    # into one comparable value.
    #
    # The sentinel must be a UUID no real tenant can hold, or a global artifact
    # and that tenant's artifact would collide on slug. The all-zero UUID is
    # *not* safe here: it is the seed `default` tenant.
    f"CREATE UNIQUE INDEX uq_arc_artifacts_scope_slug ON arc_artifacts "
    f"(COALESCE(tenant_id, '{_DEPLOYMENT_TENANT_ID}'::uuid), slug)",
]

# ---------------------------------------------------------------------------
# 2. arc_revisions
# ---------------------------------------------------------------------------

_REVISIONS_DDL = """
CREATE TABLE arc_revisions (
    revision_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id                 UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    tenant_id                   UUID REFERENCES tenants(tenant_id),
    source_system               TEXT NOT NULL,
    source_canonical_locator    TEXT NOT NULL,
    source_revision_locator     TEXT NOT NULL,
    content_digest              TEXT NOT NULL,
    lifecycle_state             TEXT NOT NULL DEFAULT 'draft',
    effective_from              TIMESTAMPTZ NOT NULL,
    effective_until             TIMESTAMPTZ,
    superseded_by_revision_id   UUID,
    approval_evidence_id        UUID,
    review_expires_at           TIMESTAMPTZ NOT NULL,
    detail_audience             TEXT NOT NULL,
    freshness_basis             TEXT NOT NULL,
    content_classification      TEXT NOT NULL,
    content_retention_until     TIMESTAMPTZ NOT NULL,
    legal_hold                  BOOLEAN NOT NULL DEFAULT FALSE,
    content_storage_mode        TEXT NOT NULL,
    source_body_ciphertext      BYTEA,
    source_body_plaintext       TEXT,
    source_body_nonce           BYTEA,
    source_body_wrapped_dek     BYTEA,
    content_key_id              TEXT,
    content_encryption_profile  TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at                TIMESTAMPTZ,
    revoked_at                  TIMESTAMPTZ,
    created_by_actor_id         UUID REFERENCES actors(actor_id),
    CONSTRAINT ck_arc_revisions_lifecycle CHECK (
        lifecycle_state IN ('draft', 'active', 'superseded', 'revoked', 'expired')
    ),
    CONSTRAINT ck_arc_revisions_detail_audience CHECK (
        detail_audience IN ('all_matched_actors', 'tenant_admin_auditor', 'registered_gateway_only')
    ),
    CONSTRAINT ck_arc_revisions_freshness CHECK (
        freshness_basis IN ('connector_verified', 'revision_pinned_only')
    ),
    CONSTRAINT ck_arc_revisions_storage_mode CHECK (
        content_storage_mode IN ('encrypted', 'none')
    ),
    -- Vocabulary undefined upstream; bounded so the row stays minimal.
    CONSTRAINT ck_arc_revisions_classification_len CHECK (
        char_length(content_classification) BETWEEN 1 AND 64
    ),
    -- Lifecycle consequences the schema can hold on its own.
    CONSTRAINT ck_arc_revisions_superseded_link CHECK (
        lifecycle_state <> 'superseded' OR superseded_by_revision_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_revisions_revoked_at CHECK (
        lifecycle_state <> 'revoked' OR revoked_at IS NOT NULL
    ),
    -- At most one representation of the source body, and never plaintext for a
    -- global revision: global content always uses the deployment hierarchy.
    CONSTRAINT ck_arc_revisions_body_one_of CHECK (
        source_body_ciphertext IS NULL OR source_body_plaintext IS NULL
    ),
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
    )
)
"""

_REVISIONS_INDEXES = [
    "CREATE INDEX ix_arc_revisions_artifact_lifecycle ON arc_revisions (artifact_id, lifecycle_state)",
    "CREATE INDEX ix_arc_revisions_tenant_lifecycle ON arc_revisions (tenant_id, lifecycle_state)",
    "CREATE INDEX ix_arc_revisions_review_expires_at ON arc_revisions (review_expires_at) "
    "WHERE lifecycle_state = 'active'",
    "CREATE UNIQUE INDEX uq_arc_revisions_source_identity ON arc_revisions "
    "(source_system, source_revision_locator, content_digest)",
    # Database backstop for family-locked activation. Application logic
    # serializes on the artifact family; this makes a second active revision
    # impossible even if that logic is bypassed.
    "CREATE UNIQUE INDEX uq_arc_revisions_one_active_per_artifact ON arc_revisions "
    "(artifact_id) WHERE lifecycle_state = 'active'",
]

# ---------------------------------------------------------------------------
# 3. arc_directive_identities, arc_conflict_domains, arc_directives
# ---------------------------------------------------------------------------

_DIRECTIVE_IDENTITIES_DDL = """
CREATE TABLE arc_directive_identities (
    directive_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id  UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# The digest indexes the canonical subject key; it does not define identity.
# A digest collision with unequal canonical keys is an integrity error, which
# is why the full key is stored alongside it and compared.
_CONFLICT_DOMAINS_DDL = """
CREATE TABLE arc_conflict_domains (
    conflict_subject_digest TEXT PRIMARY KEY,
    conflict_subject_key    JSONB NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_conflict_domains_digest_len CHECK (char_length(conflict_subject_digest) = 64)
)
"""

_DIRECTIVES_DDL = """
CREATE TABLE arc_directives (
    directive_id                        UUID NOT NULL
        REFERENCES arc_directive_identities(directive_id),
    revision_id                         UUID NOT NULL REFERENCES arc_revisions(revision_id),
    tenant_id                           UUID REFERENCES tenants(tenant_id),
    directive_type                      TEXT NOT NULL,
    conflict_key_schema_version         TEXT,
    conflict_subject_digest             TEXT
        REFERENCES arc_conflict_domains(conflict_subject_digest),
    compact_statement_ciphertext        BYTEA,
    compact_statement_plaintext         TEXT,
    compact_statement_nonce             BYTEA,
    compact_statement_wrapped_dek       BYTEA,
    source_anchor                       TEXT NOT NULL,
    conflict_key_namespace              TEXT,
    conflict_key_subject_selector       TEXT,
    conflict_key_operation              TEXT,
    conflict_key_action_class           TEXT,
    conflict_key_target_selector        TEXT,
    conflict_key_modality               TEXT,
    conflict_key_constraint_operator    TEXT,
    conflict_key_constraint_value       TEXT,
    satisfaction_mode                   TEXT,
    verification_max_age_seconds        INTEGER,
    accepted_verifier_classes           TEXT[],
    accepted_verifier_ids               UUID[],
    required_evidence_type              TEXT,
    delegable_exception                 BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
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
        satisfaction_mode IS NULL
        OR satisfaction_mode IN ('authorized_retrieval', 'signed_result')
    ),
    -- An action-protecting directive must carry the complete arc_conflict_v1
    -- shape. Anything less is citation_only: retrievable, but unable to make an
    -- action ready or blocked.
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
    -- signed_result is unusable without something to verify against.
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

_DIRECTIVES_INDEXES = [
    "CREATE INDEX ix_arc_directives_revision ON arc_directives (revision_id)",
    "CREATE INDEX ix_arc_directives_identity ON arc_directives (directive_id, revision_id)",
    "CREATE INDEX ix_arc_directives_conflict_key ON arc_directives "
    "(conflict_key_namespace, conflict_key_subject_selector, conflict_key_operation, "
    "conflict_key_action_class, conflict_key_target_selector)",
]

# ---------------------------------------------------------------------------
# 4. arc_applicability_rules, arc_mandatory_obligations
# ---------------------------------------------------------------------------

_TASK_KINDS = (
    "'read_only', 'code_change', 'dependency_change', 'configuration_change', "
    "'security_sensitive_change', 'data_access', 'deployment'"
)
_ACTION_CLASSES = "'merge', 'deploy', 'production_configuration_mutation', 'secret_release', 'data_export'"

_RULES_DDL = f"""
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
    environments            TEXT[],
    data_sensitivity_tiers  TEXT[],
    effective_from          TIMESTAMPTZ NOT NULL,
    effective_until         TIMESTAMPTZ,
    is_mandatory            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_rules_scope CHECK (
        scope IN ('global', 'tenant', 'domain', 'capability', 'task')
    ),
    -- Closed vocabularies, enforced element-wise. A host cannot invent a
    -- lower-risk value to escape an obligation.
    CONSTRAINT ck_arc_rules_task_kinds CHECK (
        task_kinds IS NULL OR task_kinds <@ ARRAY[{_TASK_KINDS}]::TEXT[]
    ),
    CONSTRAINT ck_arc_rules_action_classes CHECK (
        action_classes IS NULL OR action_classes <@ ARRAY[{_ACTION_CLASSES}]::TEXT[]
    ),
    CONSTRAINT ck_arc_rules_tenant_scope_target CHECK (
        scope <> 'tenant' OR target_tenant_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_rules_capability_scope_target CHECK (
        scope <> 'capability'
        OR (capability_ids IS NOT NULL AND array_length(capability_ids, 1) >= 1)
        OR (capability_labels IS NOT NULL AND array_length(capability_labels, 1) >= 1)
    )
)
"""

_RULES_INDEXES = [
    "CREATE INDEX ix_arc_rules_revision ON arc_applicability_rules (revision_id)",
    "CREATE INDEX ix_arc_rules_tenant_scope ON arc_applicability_rules (tenant_id, scope)",
    "CREATE INDEX ix_arc_rules_capability_ids ON arc_applicability_rules USING GIN (capability_ids)",
    "CREATE INDEX ix_arc_rules_task_kinds ON arc_applicability_rules USING GIN (task_kinds)",
    "CREATE INDEX ix_arc_rules_action_classes ON arc_applicability_rules USING GIN (action_classes)",
]

# Family-level tombstones. Without these, a revoked or review-expired mandatory
# projection would simply stop appearing in selection, and a bundle missing an
# obligation would look identical to one that never had it.
_OBLIGATIONS_DDL = """
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
        obligation_state IN (
            'satisfied', 'missing_revoked', 'missing_invalid', 'missing_review_expired'
        )
    ),
    CONSTRAINT ck_arc_obligations_satisfied_revision CHECK (
        obligation_state <> 'satisfied' OR current_revision_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_obligations_digest_len CHECK (char_length(applicability_digest) = 64)
)
"""

_OBLIGATIONS_INDEXES = [
    "CREATE INDEX ix_arc_obligations_artifact ON arc_mandatory_obligations (artifact_id)",
    "CREATE INDEX ix_arc_obligations_directive ON arc_mandatory_obligations (directive_id)",
    "CREATE INDEX ix_arc_obligations_state ON arc_mandatory_obligations (obligation_state) "
    "WHERE obligation_state <> 'satisfied'",
]

# ---------------------------------------------------------------------------
# 5-6. key registries
# ---------------------------------------------------------------------------

_HOST_KEYS_DDL = """
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

_HOST_KEYS_INDEXES = [
    "CREATE INDEX ix_arc_host_attestation_keys_valid ON arc_host_attestation_keys " "(valid_from, valid_until)",
    "CREATE INDEX ix_arc_host_attestation_keys_host ON arc_host_attestation_keys (host_id)",
]

# Public verification history only. Private key material stays in the configured
# ReceiptSigningProvider and is never stored here. Retirement never deletes a
# row: a receipt signed years ago must remain verifiable.
_RECEIPT_KEYS_DDL = """
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

# ---------------------------------------------------------------------------
# 7. arc_approval_verifiers, arc_approval_evidence
# ---------------------------------------------------------------------------

_APPROVAL_VERIFIERS_DDL = """
CREATE TABLE arc_approval_verifiers (
    approval_verifier_id  TEXT PRIMARY KEY,
    verifier_kind         TEXT NOT NULL,
    allowed_evidence_types TEXT[] NOT NULL,
    scope_kind            TEXT NOT NULL,
    scope_tenant_id       UUID REFERENCES tenants(tenant_id),
    algorithm             TEXT,
    public_key            BYTEA,
    provider_id           TEXT,
    valid_from            TIMESTAMPTZ NOT NULL,
    valid_to              TIMESTAMPTZ,
    revoked_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_verifiers_kind CHECK (
        verifier_kind IN ('operator_public_key', 'trusted_attestation_provider')
    ),
    CONSTRAINT ck_arc_verifiers_scope_kind CHECK (scope_kind IN ('global', 'tenant')),
    CONSTRAINT ck_arc_verifiers_tenant_scope CHECK (
        scope_kind <> 'tenant' OR scope_tenant_id IS NOT NULL
    ),
    CONSTRAINT ck_arc_verifiers_global_scope CHECK (
        scope_kind <> 'global' OR scope_tenant_id IS NULL
    ),
    -- Exactly one representation, matching the declared kind. A verifier that
    -- carried both could be validated down the weaker path.
    CONSTRAINT ck_arc_verifiers_representation CHECK (
        (verifier_kind = 'operator_public_key'
         AND algorithm IS NOT NULL AND public_key IS NOT NULL AND provider_id IS NULL)
        OR (verifier_kind = 'trusted_attestation_provider'
            AND provider_id IS NOT NULL AND algorithm IS NULL AND public_key IS NULL)
    ),
    CONSTRAINT ck_arc_verifiers_evidence_types CHECK (
        array_length(allowed_evidence_types, 1) >= 1
    )
)
"""

_APPROVAL_EVIDENCE_DDL = """
CREATE TABLE arc_approval_evidence (
    evidence_id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_type                 TEXT NOT NULL,
    scope_kind                    TEXT NOT NULL,
    scope_tenant_id               UUID REFERENCES tenants(tenant_id),
    approved_artifact_id          UUID REFERENCES arc_artifacts(artifact_id),
    approved_revision_id          UUID,
    approved_exception_id         UUID,
    approved_payload_digest       TEXT NOT NULL,
    approving_principal           TEXT NOT NULL,
    approving_role                TEXT NOT NULL,
    source_system_approval_locator TEXT,
    approval_timestamp            TIMESTAMPTZ NOT NULL,
    expires_at                    TIMESTAMPTZ,
    policy_version                TEXT,
    action_instance_id            TEXT,
    verification_method           TEXT NOT NULL,
    signer_key_id                 TEXT REFERENCES arc_approval_verifiers(approval_verifier_id),
    approval_verifier_id          TEXT REFERENCES arc_approval_verifiers(approval_verifier_id),
    signature                     TEXT,
    verifier_attestation          JSONB,
    verifier_identity             TEXT,
    audit_log_reference           TEXT NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_evidence_type CHECK (
        evidence_type IN (
            'artifact_activation', 'exception_approval',
            'global_exception_approval', 'gateway_emergency_bypass'
        )
    ),
    CONSTRAINT ck_arc_evidence_scope_kind CHECK (
        scope_kind IN ('global', 'tenant', 'domain', 'capability', 'task')
    ),
    CONSTRAINT ck_arc_evidence_method CHECK (
        verification_method IN ('operator_signed', 'verifier_attested')
    ),
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
    -- The unused representation must be NULL, so evidence cannot be validated
    -- against a path it did not declare.
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

_APPROVAL_EVIDENCE_INDEXES = [
    "CREATE INDEX ix_arc_approval_evidence_artifact ON arc_approval_evidence "
    "(approved_artifact_id, approved_revision_id)",
    "CREATE INDEX ix_arc_approval_evidence_exception ON arc_approval_evidence (approved_exception_id)",
    "CREATE INDEX ix_arc_approval_evidence_expires ON arc_approval_evidence (expires_at) "
    "WHERE expires_at IS NOT NULL",
]

# ---------------------------------------------------------------------------
# 8. arc_approved_exceptions, arc_approval_evidence_revocations
# ---------------------------------------------------------------------------

_EXCEPTIONS_DDL = f"""
CREATE TABLE arc_approved_exceptions (
    exception_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    higher_scope_directive_id       UUID NOT NULL,
    higher_scope_revision_id        UUID NOT NULL,
    lower_scope_kind                TEXT NOT NULL,
    lower_scope_tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    lower_scope_domain_id           TEXT,
    lower_scope_capability_id       UUID,
    lower_scope_task_kind           TEXT,
    lower_scope_action_class        TEXT,
    lower_scope_environment         TEXT,
    lower_scope_data_sensitivity    TEXT,
    replacement_conflict_descriptor JSONB NOT NULL,
    exception_statement_ciphertext  BYTEA,
    exception_statement_plaintext   TEXT,
    exception_statement_nonce       BYTEA,
    justification_ciphertext        BYTEA,
    justification_plaintext         TEXT,
    justification_nonce             BYTEA,
    content_wrapped_dek             BYTEA,
    content_key_id                  TEXT,
    effective_from                  TIMESTAMPTZ NOT NULL,
    effective_until                 TIMESTAMPTZ,
    revoked_at                      TIMESTAMPTZ,
    approval_evidence_id            UUID NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_actor_id             UUID REFERENCES actors(actor_id),
    FOREIGN KEY (higher_scope_revision_id, higher_scope_directive_id)
        REFERENCES arc_directives (revision_id, directive_id),
    CONSTRAINT ck_arc_exceptions_lower_scope_kind CHECK (
        lower_scope_kind IN ('tenant', 'domain', 'capability', 'task')
    ),
    CONSTRAINT ck_arc_exceptions_task_kind CHECK (
        lower_scope_task_kind IS NULL
        OR lower_scope_task_kind IN ({_TASK_KINDS})
    ),
    CONSTRAINT ck_arc_exceptions_action_class CHECK (
        lower_scope_action_class IS NULL
        OR lower_scope_action_class IN ({_ACTION_CLASSES})
    ),
    -- Discriminated scope: only the selectors the declared scope permits. A
    -- lower-scope exception cannot smuggle in a narrowing it did not declare.
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

_EXCEPTIONS_INDEXES = [
    "CREATE INDEX ix_arc_exceptions_directive ON arc_approved_exceptions (higher_scope_directive_id)",
    "CREATE INDEX ix_arc_exceptions_scope ON arc_approved_exceptions "
    "(lower_scope_kind, lower_scope_tenant_id, lower_scope_domain_id, lower_scope_capability_id)",
]

# Append-only. The presence of a row makes the evidence unusable immediately;
# there is no un-revoke.
_EVIDENCE_REVOCATIONS_DDL = """
CREATE TABLE arc_approval_evidence_revocations (
    evidence_id         UUID PRIMARY KEY REFERENCES arc_approval_evidence(evidence_id),
    revoked_at          TIMESTAMPTZ NOT NULL,
    reason_code         TEXT NOT NULL,
    reason_digest       TEXT NOT NULL,
    revoked_by_actor_id UUID REFERENCES actors(actor_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_evidence_revocations_reason_len CHECK (
        char_length(reason_code) BETWEEN 1 AND 64
    )
)
"""

# ---------------------------------------------------------------------------
# 9. arc_context_challenges
# ---------------------------------------------------------------------------

# Only the nonce *digest* is stored. The raw nonce is reproducible for an exact
# unexpired retry through the versioned ChallengeNonceDeriver, so there is no
# reason to keep a recoverable copy.
_CHALLENGES_DDL = """
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
    CONSTRAINT ck_arc_challenges_expiry_after_issue CHECK (expires_at > issued_at)
)
"""

_CHALLENGES_INDEXES = [
    "CREATE INDEX ix_arc_challenges_nonce_digest ON arc_context_challenges (arc_nonce_digest)",
    "CREATE INDEX ix_arc_challenges_host_session ON arc_context_challenges "
    "(host_id, session_id, idempotency_key_digest)",
    "CREATE INDEX ix_arc_challenges_expires_at ON arc_context_challenges (expires_at)",
    "CREATE UNIQUE INDEX uq_arc_challenges_idempotency ON arc_context_challenges "
    "(tenant_id, host_id, session_id, idempotency_key_digest)",
]

# ---------------------------------------------------------------------------
# 10. arc_receipts
# ---------------------------------------------------------------------------

# challenge_id is NOT NULL and UNIQUE: every receipt consumes exactly one
# challenge, and no challenge backs two receipts. That pair of constraints is
# half of the single-use invariant; the deferred trigger below is the other half.
_RECEIPTS_DDL = """
CREATE TABLE arc_receipts (
    receipt_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id              UUID NOT NULL UNIQUE
        REFERENCES arc_context_challenges(challenge_id),
    tenant_id                 UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id                  UUID NOT NULL REFERENCES actors(actor_id),
    host_id                   TEXT NOT NULL,
    session_id                TEXT NOT NULL,
    manifest_fingerprint      TEXT NOT NULL,
    attestation_id            TEXT NOT NULL,
    resolution_status         TEXT NOT NULL,
    selection_engine_version  TEXT NOT NULL,
    registry_build_revision   TEXT NOT NULL,
    canonical_profile_versions JSONB NOT NULL,
    selection_config_digest   TEXT NOT NULL,
    evaluated_at              TIMESTAMPTZ NOT NULL,
    freshness_basis           TEXT NOT NULL,
    freshness_deadline        TIMESTAMPTZ,
    blocked_reasons           TEXT[],
    degraded_reasons          TEXT[],
    mandatory_directive_count INTEGER NOT NULL DEFAULT 0,
    rendered_content_bytes    INTEGER NOT NULL DEFAULT 0,
    budget_limit_bytes        INTEGER NOT NULL,
    integrity_state           TEXT NOT NULL DEFAULT 'valid',
    response_replay_ciphertext BYTEA NOT NULL,
    response_replay_nonce     BYTEA NOT NULL,
    response_replay_key_id    TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_receipts_status CHECK (
        resolution_status IN ('ready', 'degraded', 'blocked')
    ),
    CONSTRAINT ck_arc_receipts_freshness CHECK (
        freshness_basis IN ('connector_verified', 'revision_pinned_only')
    ),
    CONSTRAINT ck_arc_receipts_integrity CHECK (
        integrity_state IN ('valid', 'integrity_failed')
    ),
    CONSTRAINT ck_arc_receipts_fingerprint_len CHECK (char_length(manifest_fingerprint) = 64),
    CONSTRAINT ck_arc_receipts_config_digest_len CHECK (char_length(selection_config_digest) = 64),
    CONSTRAINT ck_arc_receipts_attestation_id_len CHECK (
        char_length(attestation_id) BETWEEN 1 AND 200
    ),
    CONSTRAINT ck_arc_receipts_counts_nonneg CHECK (
        mandatory_directive_count >= 0 AND rendered_content_bytes >= 0 AND budget_limit_bytes > 0
    ),
    -- A blocked receipt without a reason code cannot be explained to the caller
    -- or to an auditor.
    CONSTRAINT ck_arc_receipts_blocked_has_reason CHECK (
        resolution_status <> 'blocked'
        OR (blocked_reasons IS NOT NULL AND array_length(blocked_reasons, 1) >= 1)
    )
)
"""

_RECEIPTS_INDEXES = [
    "CREATE INDEX ix_arc_receipts_tenant_actor ON arc_receipts (tenant_id, actor_id, created_at DESC)",
    "CREATE INDEX ix_arc_receipts_host_session ON arc_receipts (host_id, session_id)",
    "CREATE UNIQUE INDEX ix_arc_receipts_host_attestation_id ON arc_receipts " "(host_id, attestation_id)",
    "CREATE INDEX ix_arc_receipts_manifest_fingerprint ON arc_receipts " "(tenant_id, manifest_fingerprint)",
]

# ---------------------------------------------------------------------------
# 11-14. receipt children
# ---------------------------------------------------------------------------

_RECEIPT_EVENTS_DDL = """
CREATE TABLE arc_receipt_events (
    event_id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id                        UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    tenant_id                         UUID NOT NULL,
    sequence                          INTEGER NOT NULL,
    event_type                        TEXT NOT NULL,
    event_source                      TEXT NOT NULL,
    actor_id                          UUID REFERENCES actors(actor_id),
    gateway_id                        TEXT,
    signer_key_id                     TEXT REFERENCES arc_receipt_signing_keys(signer_key_id),
    signature_profile                 TEXT NOT NULL,
    idempotency_key_digest            TEXT,
    request_payload_digest            TEXT NOT NULL,
    previous_event_digest             TEXT,
    event_payload                     JSONB NOT NULL,
    consumed_continuation_token_digest TEXT,
    response_replay_ciphertext        BYTEA,
    response_replay_nonce             BYTEA,
    response_replay_key_id            TEXT,
    event_digest                      TEXT NOT NULL,
    signature                         TEXT NOT NULL,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_arc_events_source CHECK (event_source IN ('host', 'gateway', 'system')),
    -- Vocabulary undefined upstream; bounded so the row stays minimal.
    CONSTRAINT ck_arc_events_type_len CHECK (char_length(event_type) BETWEEN 1 AND 64),
    -- Sequences are 0-indexed. The receipt-creation event is sequence 0 and the
    -- head starts at next_sequence 1. Getting this backwards is not a cosmetic
    -- off-by-one: a 1-indexed CHECK rejects the first event every receipt has,
    -- so no receipt could ever be created at all.
    CONSTRAINT ck_arc_events_sequence_nonneg CHECK (sequence >= 0),
    CONSTRAINT ck_arc_events_digest_len CHECK (char_length(event_digest) = 64),
    CONSTRAINT ck_arc_events_request_digest_len CHECK (char_length(request_payload_digest) = 64),
    -- The first event has no predecessor; every later one must name it.
    CONSTRAINT ck_arc_events_chain_link CHECK (
        (sequence = 0 AND previous_event_digest IS NULL)
        OR (sequence > 0 AND previous_event_digest IS NOT NULL)
    ),
    -- Host and gateway events must be idempotent; system events rely on their
    -- own operation-specific unique binding instead.
    CONSTRAINT ck_arc_events_idempotency_required CHECK (
        (event_source = 'system' AND idempotency_key_digest IS NULL)
        OR (event_source <> 'system' AND idempotency_key_digest IS NOT NULL)
    )
)
"""

_RECEIPT_EVENTS_INDEXES = [
    "CREATE UNIQUE INDEX ix_arc_receipt_events_receipt_sequence ON arc_receipt_events " "(receipt_id, sequence)",
    "CREATE UNIQUE INDEX ix_arc_receipt_events_idempotency ON arc_receipt_events "
    "(receipt_id, event_source, idempotency_key_digest)",
    "CREATE INDEX ix_arc_receipt_events_digest ON arc_receipt_events (event_digest)",
    # A continuation token may advance a chain at most once. An exact page retry
    # resolves through the idempotency record above, not by re-consuming.
    "CREATE UNIQUE INDEX uq_arc_receipt_events_page_token ON arc_receipt_events "
    "(receipt_id, consumed_continuation_token_digest) "
    "WHERE consumed_continuation_token_digest IS NOT NULL",
]

_EVENT_HEADS_DDL = """
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

_SELECTED_REVISIONS_DDL = """
CREATE TABLE arc_receipt_selected_revisions (
    receipt_id      UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    revision_id     UUID NOT NULL REFERENCES arc_revisions(revision_id),
    tenant_id       UUID NOT NULL,
    artifact_id     UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    is_mandatory    BOOLEAN NOT NULL,
    was_omitted     BOOLEAN NOT NULL DEFAULT FALSE,
    omission_reason TEXT,
    PRIMARY KEY (receipt_id, revision_id),
    CONSTRAINT ck_arc_selected_revisions_omission CHECK (
        was_omitted = FALSE OR omission_reason IS NOT NULL
    ),
    CONSTRAINT ck_arc_selected_revisions_reason_len CHECK (
        omission_reason IS NULL OR char_length(omission_reason) BETWEEN 1 AND 64
    )
)
"""

_SELECTED_REVISIONS_INDEXES = [
    "CREATE INDEX ix_arc_receipt_revisions_receipt ON arc_receipt_selected_revisions (receipt_id)",
    "CREATE INDEX ix_arc_receipt_revisions_revision ON arc_receipt_selected_revisions (revision_id)",
]

# The per-receipt snapshot JIT authorizes against. Locator and digest columns
# are access-controlled rather than encrypted: they are redacted by artifact
# audience before they reach a caller.
_SELECTED_DIRECTIVES_DDL = """
CREATE TABLE arc_receipt_selected_directives (
    receipt_id              UUID NOT NULL REFERENCES arc_receipts(receipt_id),
    revision_id             UUID NOT NULL REFERENCES arc_revisions(revision_id),
    directive_id            UUID NOT NULL,
    tenant_id               UUID NOT NULL,
    artifact_id             UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    is_mandatory            BOOLEAN NOT NULL,
    was_omitted             BOOLEAN NOT NULL DEFAULT FALSE,
    omission_reason         TEXT,
    visibility_decision_id  TEXT NOT NULL,
    source_locator          TEXT NOT NULL,
    source_revision_locator TEXT NOT NULL,
    content_digest          TEXT NOT NULL,
    obligation_fields       JSONB NOT NULL,
    context_handle_digest   TEXT NOT NULL,
    PRIMARY KEY (receipt_id, directive_id),
    FOREIGN KEY (revision_id, directive_id)
        REFERENCES arc_directives (revision_id, directive_id),
    CONSTRAINT ck_arc_selected_directives_omission CHECK (
        was_omitted = FALSE OR omission_reason IS NOT NULL
    ),
    CONSTRAINT ck_arc_selected_directives_handle_len CHECK (
        char_length(context_handle_digest) = 64
    )
)
"""

_SELECTED_DIRECTIVES_INDEXES = [
    "CREATE INDEX ix_arc_receipt_directives_receipt ON arc_receipt_selected_directives (receipt_id)",
    "CREATE INDEX ix_arc_receipt_directives_revision ON arc_receipt_selected_directives (revision_id)",
    # One handle resolves to exactly one selection row, so JIT authorization is
    # never ambiguous.
    "CREATE UNIQUE INDEX uq_arc_receipt_directives_handle ON arc_receipt_selected_directives "
    "(receipt_id, context_handle_digest)",
]

# ---------------------------------------------------------------------------
# 15. arc_content_deletion_verifications, arc_audit_outbox
# ---------------------------------------------------------------------------

_DELETION_VERIFICATIONS_DDL = """
CREATE TABLE arc_content_deletion_verifications (
    verification_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revision_id          UUID NOT NULL REFERENCES arc_revisions(revision_id),
    operation            TEXT NOT NULL,
    removed_body_digest  TEXT,
    destroyed_key_reference TEXT,
    approval_evidence_id UUID NOT NULL REFERENCES arc_approval_evidence(evidence_id),
    verified_at          TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_arc_deletion_operation CHECK (
        operation IN ('body_deleted', 'key_destroyed', 'legal_hold_released')
    )
)
"""

# ARC does not write audit_log inline the way the rest of the codebase does.
# Every ARC write emits an outbox row in the same transaction as its domain
# state, and the drain worker is ARC's only writer to audit_log. That keeps
# receipt latency independent of audit-sink latency without losing events.
#
# The drain is idempotent through the sink's composite identity
# (audit_id = outbox_id, ts = created_at), so at-least-once redelivery cannot
# duplicate an audit row.
_AUDIT_OUTBOX_DDL = """
CREATE TABLE arc_audit_outbox (
    outbox_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),
    event_type    TEXT NOT NULL,
    event_payload JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    drained_at    TIMESTAMPTZ,
    CONSTRAINT ck_arc_audit_outbox_event_type_len CHECK (
        char_length(event_type) BETWEEN 1 AND 128
    )
)
"""

_AUDIT_OUTBOX_INDEXES = [
    "CREATE INDEX ix_arc_audit_outbox_drained ON arc_audit_outbox (drained_at) " "WHERE drained_at IS NULL",
    "CREATE INDEX ix_arc_audit_outbox_created ON arc_audit_outbox (created_at)",
]

# ---------------------------------------------------------------------------
# 16. deferred and self-referential foreign keys
# ---------------------------------------------------------------------------

_DEFERRED_FKS = [
    # Genuinely cyclic: a revision names the evidence that approved it, and the
    # evidence names the revision it approved. DEFERRABLE INITIALLY DEFERRED
    # lets one transaction insert both in either order.
    "ALTER TABLE arc_revisions ADD CONSTRAINT fk_arc_revisions_approval_evidence "
    "FOREIGN KEY (approval_evidence_id) REFERENCES arc_approval_evidence(evidence_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approval_evidence ADD CONSTRAINT fk_arc_evidence_approved_revision "
    "FOREIGN KEY (approved_revision_id) REFERENCES arc_revisions(revision_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT fk_arc_exceptions_approval_evidence "
    "FOREIGN KEY (approval_evidence_id) REFERENCES arc_approval_evidence(evidence_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_approval_evidence ADD CONSTRAINT fk_arc_evidence_approved_exception "
    "FOREIGN KEY (approved_exception_id) REFERENCES arc_approved_exceptions(exception_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    # Self-referential; added after the tables exist.
    "ALTER TABLE arc_revisions ADD CONSTRAINT fk_arc_revisions_superseded_by "
    "FOREIGN KEY (superseded_by_revision_id) REFERENCES arc_revisions(revision_id)",
    "ALTER TABLE arc_host_attestation_keys ADD CONSTRAINT fk_arc_host_keys_replacement "
    "FOREIGN KEY (replacement_key_id) REFERENCES arc_host_attestation_keys(signer_key_id)",
    "ALTER TABLE arc_receipt_signing_keys ADD CONSTRAINT fk_arc_receipt_keys_replacement "
    "FOREIGN KEY (replacement_key_id) REFERENCES arc_receipt_signing_keys(signer_key_id)",
]

# ---------------------------------------------------------------------------
# 17. deferred challenge-consumption constraint trigger
# ---------------------------------------------------------------------------

# `consumed_at IS NOT NULL` if and only if exactly one receipt references the
# challenge. Checked at COMMIT rather than per statement, so the resolution
# transaction may write the receipt and consume the challenge in either order,
# while orphan receipts and consumed-without-receipt rows are still rejected.
_CHALLENGE_CONSUMPTION_FN = """
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

_CHALLENGE_CONSUMPTION_TRIGGERS = [
    "CREATE CONSTRAINT TRIGGER trg_arc_challenge_consumption_on_challenge "
    "AFTER INSERT OR UPDATE ON arc_context_challenges "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION arc_check_challenge_consumption()",
    "CREATE CONSTRAINT TRIGGER trg_arc_challenge_consumption_on_receipt "
    "AFTER INSERT OR UPDATE ON arc_receipts "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION arc_check_challenge_consumption()",
]


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------

# Order matters: each table's foreign keys must already have their targets.
_CREATE_SEQUENCE: list[str] = [
    _ARTIFACTS_DDL,
    *_ARTIFACTS_INDEXES,
    _REVISIONS_DDL,
    *_REVISIONS_INDEXES,
    _DIRECTIVE_IDENTITIES_DDL,
    _CONFLICT_DOMAINS_DDL,
    _DIRECTIVES_DDL,
    *_DIRECTIVES_INDEXES,
    _RULES_DDL,
    *_RULES_INDEXES,
    _OBLIGATIONS_DDL,
    *_OBLIGATIONS_INDEXES,
    _HOST_KEYS_DDL,
    *_HOST_KEYS_INDEXES,
    _RECEIPT_KEYS_DDL,
    _APPROVAL_VERIFIERS_DDL,
    _APPROVAL_EVIDENCE_DDL,
    *_APPROVAL_EVIDENCE_INDEXES,
    _EXCEPTIONS_DDL,
    *_EXCEPTIONS_INDEXES,
    _EVIDENCE_REVOCATIONS_DDL,
    _CHALLENGES_DDL,
    *_CHALLENGES_INDEXES,
    _RECEIPTS_DDL,
    *_RECEIPTS_INDEXES,
    _RECEIPT_EVENTS_DDL,
    *_RECEIPT_EVENTS_INDEXES,
    _EVENT_HEADS_DDL,
    _SELECTED_REVISIONS_DDL,
    *_SELECTED_REVISIONS_INDEXES,
    _SELECTED_DIRECTIVES_DDL,
    *_SELECTED_DIRECTIVES_INDEXES,
    _DELETION_VERIFICATIONS_DDL,
    _AUDIT_OUTBOX_DDL,
    *_AUDIT_OUTBOX_INDEXES,
    *_DEFERRED_FKS,
    _CHALLENGE_CONSUMPTION_FN,
    *_CHALLENGE_CONSUMPTION_TRIGGERS,
]

# Receipts are the whole point of ARC: they are the non-repudiation record, the
# data model requires retaining them at least 365 days, and legal hold suspends
# deletion outright. A plain `alembic downgrade` would drop them, so it refuses
# when there is anything to lose.
#
# The escape is deliberate and per-session, not a flag someone sets once and
# forgets:
#
#     SET arc.allow_destructive_downgrade = 'on';
#
# A dev database with no receipts downgrades freely, which is the case that
# actually happens during development.
_DOWNGRADE_GUARD = """
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

# Reverse dependency order. Tables go last-created-first so no foreign key
# outlives its target.
_DROP_SEQUENCE: list[str] = [
    _DOWNGRADE_GUARD,
    "DROP TRIGGER IF EXISTS trg_arc_challenge_consumption_on_receipt ON arc_receipts",
    "DROP TRIGGER IF EXISTS trg_arc_challenge_consumption_on_challenge ON arc_context_challenges",
    "DROP FUNCTION IF EXISTS arc_check_challenge_consumption()",
    # Deferred and self-referential FKs first: dropping them decouples the
    # cycles so the table drops below need no particular order among them.
    "ALTER TABLE arc_revisions DROP CONSTRAINT IF EXISTS fk_arc_revisions_approval_evidence",
    "ALTER TABLE arc_approval_evidence DROP CONSTRAINT IF EXISTS fk_arc_evidence_approved_revision",
    "ALTER TABLE arc_approved_exceptions DROP CONSTRAINT IF EXISTS fk_arc_exceptions_approval_evidence",
    "ALTER TABLE arc_approval_evidence DROP CONSTRAINT IF EXISTS fk_arc_evidence_approved_exception",
    "ALTER TABLE arc_revisions DROP CONSTRAINT IF EXISTS fk_arc_revisions_superseded_by",
    "ALTER TABLE arc_host_attestation_keys DROP CONSTRAINT IF EXISTS fk_arc_host_keys_replacement",
    "ALTER TABLE arc_receipt_signing_keys DROP CONSTRAINT IF EXISTS fk_arc_receipt_keys_replacement",
    "DROP TABLE IF EXISTS arc_audit_outbox",
    "DROP TABLE IF EXISTS arc_content_deletion_verifications",
    "DROP TABLE IF EXISTS arc_receipt_selected_directives",
    "DROP TABLE IF EXISTS arc_receipt_selected_revisions",
    "DROP TABLE IF EXISTS arc_receipt_event_heads",
    "DROP TABLE IF EXISTS arc_receipt_events",
    "DROP TABLE IF EXISTS arc_receipts",
    "DROP TABLE IF EXISTS arc_context_challenges",
    "DROP TABLE IF EXISTS arc_approval_evidence_revocations",
    "DROP TABLE IF EXISTS arc_approved_exceptions",
    "DROP TABLE IF EXISTS arc_approval_evidence",
    "DROP TABLE IF EXISTS arc_approval_verifiers",
    "DROP TABLE IF EXISTS arc_receipt_signing_keys",
    "DROP TABLE IF EXISTS arc_host_attestation_keys",
    "DROP TABLE IF EXISTS arc_mandatory_obligations",
    "DROP TABLE IF EXISTS arc_applicability_rules",
    "DROP TABLE IF EXISTS arc_directives",
    "DROP TABLE IF EXISTS arc_conflict_domains",
    "DROP TABLE IF EXISTS arc_directive_identities",
    "DROP TABLE IF EXISTS arc_revisions",
    "DROP TABLE IF EXISTS arc_artifacts",
    # The reserved tenant row goes too, so downgrade leaves no ARC trace.
    # Matched on slug as well as ID: a DELETE keyed on an ID alone is one typo
    # away from removing a real tenant, which is exactly what happened when this
    # migration first used the all-zero UUID. Already-drained audit_log rows are
    # deliberately left alone — audit history is not ARC's to delete — so this
    # DELETE fails loudly if any remain, rather than orphaning them.
    f"DELETE FROM tenants WHERE tenant_id = '{_DEPLOYMENT_TENANT_ID}' AND slug = '_deployment'",
]


def upgrade() -> None:
    op.execute(_DEPLOYMENT_TENANT_DDL)
    for statement in _CREATE_SEQUENCE:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DROP_SEQUENCE:
        op.execute(statement)
