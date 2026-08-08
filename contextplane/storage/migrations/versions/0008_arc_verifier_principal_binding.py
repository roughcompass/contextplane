"""Principal-bound verifier enrollment: a challenge/proof table, and the
binding columns `arc_approval_verifiers` gains to carry the answer.

Revision ID: 0008_arc_verifier_principal_binding
Revises: 0007_arc_operational_chain
Create Date: 2026-08-06

Before this migration, `arc_approval_verifiers` recorded a public key or a
provider id and nothing about *whose* key it was -- enrollment was allowlist
membership only, with no cryptographic binding to a registered principal.
`ApprovalEvidenceVerifier.verify()` (a later task's caller) can check a
signed principal against this row only once the row can name one.

`arc_approval_verifier_enrollment_challenges` is the proof-of-possession
half of that binding. The server pre-allocates the verifier id that will be
created, generates a random nonce, and commits every immutable registration
field into `canonical_enrollment_bytes` -- the exact bytes a caller signs (or
a configured provider attests over) to prove it holds the credential before
the row is ever written. `UNIQUE (nonce)` backs the single-use rule the
service enforces with a `consumed_at IS NULL` compare-and-swap; `UNIQUE
(verifier_id)` stops two challenges from racing to pre-allocate the same
future row.

`arc_approval_verifiers` gains eight columns and two CHECK constraints. The
new columns are all nullable: every row inserted by the pre-existing
`VerifierRegistry.register()` writer (used for `exception_approval`
verifiers, which this migration does not touch) has no principal binding at
all, and the shape CHECK's `principal_binding_kind IS NULL` escape clause is
what keeps those legitimate. A row that *does* declare a binding kind must
take exactly one of the two shapes ADR 039 defines -- `exact_principal`
(a named issuer/subject, no provider) or `provider_delegated` (a trusted
provider's allowed issuer, no principal fields at enrollment time; the
provider names the exact subject dynamically, at approval time, per D1) --
never a hybrid of both and never neither.
"""

from __future__ import annotations

from alembic import op

revision = "0008_arc_verifier_principal_binding"
down_revision: str | None = "0007_arc_operational_chain"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_approval_verifier_enrollment_challenges -- the proof-of-possession
# round trip's own state. One row per challenge; consumed at most once.
# ---------------------------------------------------------------------------

_ENROLLMENT_CHALLENGES_DDL = """
CREATE TABLE arc_approval_verifier_enrollment_challenges (
    enrollment_challenge_id            UUID PRIMARY KEY,
    verifier_id                        TEXT NOT NULL,
    nonce                              TEXT NOT NULL,
    binding_kind                       TEXT NOT NULL,
    principal_issuer                   TEXT,
    principal_subject                  TEXT,
    provider_id                        TEXT,
    provider_allowed_principal_issuer  TEXT,
    owning_scope                       TEXT NOT NULL,
    target_tenant_id                   UUID REFERENCES tenants(tenant_id),
    allowed_evidence_types             TEXT[] NOT NULL,
    signature_algorithm                TEXT NOT NULL,
    credential_material                BYTEA NOT NULL,
    canonical_enrollment_bytes         BYTEA NOT NULL,
    valid_from                         TIMESTAMPTZ NOT NULL,
    valid_to                           TIMESTAMPTZ NOT NULL,
    issued_at                          TIMESTAMPTZ NOT NULL,
    expires_at                         TIMESTAMPTZ NOT NULL,
    consumed_at                        TIMESTAMPTZ,
    created_by_issuer                  TEXT NOT NULL,
    created_by_subject                 TEXT NOT NULL,
    created_at                         TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_arc_verifier_enrollment_challenges_verifier_id UNIQUE (verifier_id),
    CONSTRAINT uq_arc_verifier_enrollment_challenges_nonce UNIQUE (nonce),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_binding_kind CHECK (
        binding_kind IN ('exact_principal', 'provider_delegated')
    ),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_scope_kind CHECK (owning_scope IN ('global', 'tenant')),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_scope_tenant CHECK (
        (owning_scope = 'tenant') = (target_tenant_id IS NOT NULL)
    ),
    -- Exactly one binding shape, never both and never neither. Mirrors the
    -- CHECK `arc_approval_verifiers` gains below -- see this migration's own
    -- docstring for why a hybrid (both principal and provider fields set)
    -- must fail here rather than only being caught by request validation.
    CONSTRAINT ck_arc_verifier_enrollment_challenges_binding_shape CHECK (
        (binding_kind = 'exact_principal'
         AND principal_issuer IS NOT NULL AND principal_subject IS NOT NULL
         AND provider_id IS NULL AND provider_allowed_principal_issuer IS NULL)
        OR (binding_kind = 'provider_delegated'
            AND provider_id IS NOT NULL AND provider_allowed_principal_issuer IS NOT NULL
            AND principal_issuer IS NULL AND principal_subject IS NULL)
    ),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_algorithm CHECK (signature_algorithm = 'Ed25519'),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_evidence_types CHECK (
        array_length(allowed_evidence_types, 1) >= 1
    ),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_validity CHECK (valid_from < valid_to),
    CONSTRAINT ck_arc_verifier_enrollment_challenges_window CHECK (issued_at < expires_at)
)
"""

# ---------------------------------------------------------------------------
# arc_approval_verifiers -- widen the pre-existing trust-root table with the
# principal-binding columns ADR 039's D1 design adds.
# ---------------------------------------------------------------------------

_APPROVAL_VERIFIERS_ALTER = [
    "ALTER TABLE arc_approval_verifiers ADD COLUMN principal_binding_kind TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN principal_issuer TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN principal_subject TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN provider_allowed_principal_issuer TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN credential_fingerprint TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN provider_configuration_digest TEXT",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN enrollment_challenge_id UUID "
    "REFERENCES arc_approval_verifier_enrollment_challenges(enrollment_challenge_id)",
    "ALTER TABLE arc_approval_verifiers ADD COLUMN enrollment_verified_at TIMESTAMPTZ",
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT uq_arc_approval_verifiers_enrollment_challenge "
    "UNIQUE (enrollment_challenge_id)",
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT ck_arc_approval_verifiers_binding_kind CHECK ("
    "  principal_binding_kind IS NULL OR principal_binding_kind IN ('exact_principal', 'provider_delegated')"
    ")",
    # Appendix B.3's constraint, verbatim: exact_principal requires the
    # principal fields and forbids the provider fields; provider_delegated
    # requires both provider fields (and, symmetrically, forbids the
    # principal fields -- see the module docstring for why `provider_
    # delegated` legitimately has no principal at enrollment time).
    # `principal_binding_kind IS NULL` is the escape hatch for every
    # pre-existing and future non-principal-bound row (the `exception_
    # approval` verifiers `VerifierRegistry.register()` still writes).
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT ck_arc_approval_verifiers_binding_shape CHECK ("
    "  principal_binding_kind IS NULL"
    "  OR (principal_binding_kind = 'exact_principal'"
    "      AND principal_issuer IS NOT NULL AND principal_subject IS NOT NULL"
    "      AND provider_allowed_principal_issuer IS NULL)"
    "  OR (principal_binding_kind = 'provider_delegated'"
    "      AND provider_id IS NOT NULL AND provider_allowed_principal_issuer IS NOT NULL"
    "      AND principal_issuer IS NULL AND principal_subject IS NULL)"
    ")",
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT ck_arc_approval_verifiers_credential_fp_len CHECK ("
    "  credential_fingerprint IS NULL OR char_length(credential_fingerprint) = 64"
    ")",
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT ck_arc_approval_verifiers_provider_digest_len CHECK ("
    "  provider_configuration_digest IS NULL OR char_length(provider_configuration_digest) = 64"
    ")",
    # One verifier id binds one credential and one exact principal (D1);
    # rotation creates a new id rather than rebinding this one. NULL is
    # distinct from NULL in a plain UNIQUE index, so this is silent for
    # every provider_delegated or pre-existing non-principal-bound row --
    # it only fires for two exact_principal rows sharing the same
    # (issuer, subject, credential) triple.
    "ALTER TABLE arc_approval_verifiers ADD CONSTRAINT uq_arc_approval_verifiers_principal "
    "UNIQUE (principal_issuer, principal_subject, credential_fingerprint)",
]

_APPROVAL_VERIFIERS_ALTER_DOWNGRADE = [
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT uq_arc_approval_verifiers_principal",
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT ck_arc_approval_verifiers_provider_digest_len",
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT ck_arc_approval_verifiers_credential_fp_len",
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT ck_arc_approval_verifiers_binding_shape",
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT ck_arc_approval_verifiers_binding_kind",
    "ALTER TABLE arc_approval_verifiers DROP CONSTRAINT uq_arc_approval_verifiers_enrollment_challenge",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN enrollment_verified_at",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN enrollment_challenge_id",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN provider_configuration_digest",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN credential_fingerprint",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN provider_allowed_principal_issuer",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN principal_subject",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN principal_issuer",
    "ALTER TABLE arc_approval_verifiers DROP COLUMN principal_binding_kind",
]


def upgrade() -> None:
    # The challenge table first: `arc_approval_verifiers.enrollment_challenge_id`
    # below references it.
    op.execute(_ENROLLMENT_CHALLENGES_DDL)
    for stmt in _APPROVAL_VERIFIERS_ALTER:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _APPROVAL_VERIFIERS_ALTER_DOWNGRADE:
        op.execute(stmt)
    op.execute("DROP TABLE arc_approval_verifier_enrollment_challenges")
