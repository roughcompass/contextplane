"""Ownership as an auditable assertion, cross-organization grants, and migration dispositions.

Three tables that share one property: each records a *decision somebody made*,
and each is worthless if it cannot be read back as evidence years later. So every
one of them stores who decided, when it takes effect, and why — and the
constraints below exist to stop a row that answers none of those from being
written.

**Ownership is an assertion, never an authorization input.** The contract's rule is
that no authorization or entitlement path consults ownership state, and the
storage side of that is a table with **no foreign key into any principal,
actor, role or entitlement table**. `owner_principal` and the owned target are
recorded as opaque references precisely so that a future reader cannot join
ownership to an identity table and quietly turn "who is accountable for this" into
"who may change it". Those are different questions and answering the second with
the first is how an audit field becomes an access-control bug.

An absence cannot be expressed as a constraint, so it is asserted by a test that
reads the live foreign keys of this table and refuses any that reach an
identity-shaped table. That is the only form of enforcement available for a rule
whose content is "this edge does not exist".

**The ownership lifecycle is already frozen in the profile schema, and this
migration restates its transition table in SQL rather than trusting callers.** The
five states and the moves between them are compiled vocabulary; a transition row
carrying an illegal move would be an audit trail of something that cannot have
happened. `superseded` and `revoked` lead nowhere, which is what makes them
terminal — a row leaving either is refused by the same check.

**A grant's revocation is all-or-nothing, and its policy version is mandatory.**
An omitted grant or policy is a denial, so a grant with no policy version cannot
be evaluated and must not be storable. Revocation is three facts — when, why, by
whom — and a row carrying one without the others describes a revocation nobody can
audit, so the check admits all three or none.

**A grandfather disposition carries its whole justification or it is not one.**
Owner, reason, warning, expiry and enforced action are what separate a deliberate
temporary exemption from an indefinite one nobody revisits. The expiry ceiling is a
CHECK rather than a convention because the failure mode is an exemption quietly
outliving the migration that needed it, and the database is the only place that
notices without somebody looking.
"""

from __future__ import annotations

from alembic import op

revision = "0053_ownership_and_grants"
# Resolved by walking `down_revision` from the root; filenames do not sort into
# chain order in this repository.
down_revision: str | None = "0052_relationship_metadata"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The frozen ownership vocabulary, restated as SQL. Kept as literals rather than
#: imported from the profile schema for the reason every migration keeps its own
#: values: a migration is history, and re-running this revision years from now
#: must reproduce the states approved today, not whatever the module has become.
_OWNERSHIP_STATES = "'draft', 'proposed', 'validated', 'superseded', 'revoked'"

#: Every legal move, as the compiled lifecycle defines them. Absence is a refusal
#: rather than an unknown; `superseded` and `revoked` appear only as destinations.
_LEGAL_TRANSITIONS = (
    "(from_state = 'draft'     AND to_state IN ('proposed', 'revoked')) OR "
    "(from_state = 'proposed'  AND to_state IN ('validated', 'revoked')) OR "
    "(from_state = 'validated' AND to_state IN ('superseded', 'revoked'))"
)

#: The ceiling on a grandfather exemption, in days. A year is the outer bound the
#: approved policy admits; the default is 90 and lives with the code that creates
#: these rows, because a default is a choice and a ceiling is a rule.
_GRANDFATHER_MAX_DAYS = 365


_OWNERSHIP_ASSIGNMENTS = f"""
CREATE TABLE ownership_assignments (
    ownership_assignment_id  UUID PRIMARY KEY,
    tenant_id                UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The owner and the owned thing, as opaque references. Deliberately NOT
    -- foreign keys into any identity table: see this module's docstring for why
    -- an ownership row must not be joinable to a principal.
    owner_principal          TEXT NOT NULL,
    owned_target_kind        TEXT NOT NULL,
    owned_target_id          UUID NOT NULL,

    role                     TEXT NOT NULL,
    scope                    TEXT NOT NULL,

    -- How this assignment came to exist. `derivation_method` and `confidence` are
    -- NULL together for a hand-asserted one: a human assertion has no inference
    -- confidence, and storing 1.0 would make it indistinguishable from a machine
    -- that was certain.
    source                   TEXT NOT NULL,
    derivation_method        TEXT,
    confidence               DOUBLE PRECISION,

    validation_state         TEXT NOT NULL,

    effective_from           TIMESTAMPTZ NOT NULL,
    effective_to             TIMESTAMPTZ,

    provenance_id            UUID NOT NULL REFERENCES assertion_provenance(provenance_id),

    -- Why it stopped being current. A superseded assignment names its replacement;
    -- a revoked one names a reason. Both stay readable forever, which is the point
    -- of ending here rather than deleting the row.
    replaced_by_assignment_id UUID REFERENCES ownership_assignments(ownership_assignment_id),
    revocation_reason        TEXT,

    recorded_by              TEXT NOT NULL,
    recorded_at              TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_ownership_assignments_state CHECK (validation_state IN ({_OWNERSHIP_STATES})),
    CONSTRAINT ck_ownership_assignments_interval CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    -- Inference is two facts or neither. A confidence with no method cannot be
    -- reproduced; a method with no confidence cannot be weighed.
    CONSTRAINT ck_ownership_assignments_inference_complete CHECK (
        (derivation_method IS NULL) = (confidence IS NULL)
    ),
    CONSTRAINT ck_ownership_assignments_confidence_range CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    -- A revoked assignment says why; a non-revoked one has nothing to explain.
    CONSTRAINT ck_ownership_assignments_revocation CHECK (
        (validation_state = 'revoked') = (revocation_reason IS NOT NULL)
    ),
    -- Only a superseded assignment names a replacement, and never itself.
    CONSTRAINT ck_ownership_assignments_replacement CHECK (
        (replaced_by_assignment_id IS NULL OR validation_state = 'superseded')
        AND replaced_by_assignment_id IS DISTINCT FROM ownership_assignment_id
    )
)
"""

_OWNERSHIP_TRANSITIONS = f"""
CREATE TABLE ownership_assignment_transitions (
    transition_id            UUID PRIMARY KEY,
    ownership_assignment_id  UUID NOT NULL
        REFERENCES ownership_assignments(ownership_assignment_id) ON DELETE CASCADE,

    -- Which move this is, counted from one. Unique per assignment so a retry that
    -- lost its response cannot record the same move twice at the same position.
    sequence                 INTEGER NOT NULL,

    from_state               TEXT NOT NULL,
    to_state                 TEXT NOT NULL,

    reason                   TEXT NOT NULL,
    recorded_by              TEXT NOT NULL,
    recorded_at              TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_ownership_transitions_sequence UNIQUE (ownership_assignment_id, sequence),
    CONSTRAINT ck_ownership_transitions_sequence_positive CHECK (sequence >= 1),
    CONSTRAINT ck_ownership_transitions_states CHECK (
        from_state IN ({_OWNERSHIP_STATES}) AND to_state IN ({_OWNERSHIP_STATES})
    ),
    -- The compiled lifecycle, restated. A row outside it is an audit trail of a
    -- move that cannot have happened.
    CONSTRAINT ck_ownership_transitions_legal_move CHECK ({_LEGAL_TRANSITIONS}),
    CONSTRAINT ck_ownership_transitions_reason_present CHECK (length(btrim(reason)) > 0)
)
"""

_CROSS_ORG_GRANTS = """
CREATE TABLE cross_org_grants (
    grant_id                 UUID PRIMARY KEY,

    source_tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    destination_tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),

    grant_kind               TEXT NOT NULL,
    grant_state              TEXT NOT NULL,

    -- What the grant reaches. Selectors and type lists are documents rather than
    -- columns because their shape is the profile's to define, and a column per
    -- selector kind would need a migration every time the profile grew one.
    profile_types            JSONB NOT NULL DEFAULT '[]'::jsonb,
    relationship_types       JSONB NOT NULL DEFAULT '[]'::jsonb,
    instance_selectors       JSONB NOT NULL DEFAULT '[]'::jsonb,
    audience                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    applicability            JSONB NOT NULL DEFAULT '{}'::jsonb,
    allowed_operations       JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- The most sensitive classification this grant may carry across. A ceiling
    -- rather than a filter: content above it is not shared, rather than shared in
    -- redacted form.
    classification_ceiling   TEXT NOT NULL,

    effective_from           TIMESTAMPTZ NOT NULL,
    effective_to             TIMESTAMPTZ,

    approving_authorities    JSONB NOT NULL DEFAULT '[]'::jsonb,
    approval_evidence        TEXT,

    revoked_at               TIMESTAMPTZ,
    revocation_reason        TEXT,
    revoked_by               TEXT,

    -- Omitted policy is deny, so a grant that cannot name the policy version it
    -- was evaluated under must not be storable.
    policy_version           TEXT NOT NULL,

    recorded_by              TEXT NOT NULL,
    recorded_at              TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_cross_org_grants_kind CHECK (
        grant_kind IN ('relationship', 'adoption', 'learning', 'projection', 'context')
    ),
    CONSTRAINT ck_cross_org_grants_state CHECK (
        grant_state IN ('proposed', 'active', 'revoked', 'expired')
    ),
    -- A grant from a tenant to itself is not a cross-organization grant, and
    -- admitting one would create a row every isolation check has to special-case.
    CONSTRAINT ck_cross_org_grants_distinct_tenants CHECK (
        source_tenant_id <> destination_tenant_id
    ),
    CONSTRAINT ck_cross_org_grants_interval CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    -- Revocation is when, why and by whom: all three or none. One without the
    -- others describes a revocation nobody can audit.
    CONSTRAINT ck_cross_org_grants_revocation_complete CHECK (
        (revoked_at IS NULL AND revocation_reason IS NULL AND revoked_by IS NULL)
        OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL AND revoked_by IS NOT NULL)
    ),
    -- The state and the evidence must agree. A row in `revoked` with no
    -- revocation, or a revocation with the state left elsewhere, is a grant whose
    -- two answers to "is this live?" disagree.
    CONSTRAINT ck_cross_org_grants_state_matches_revocation CHECK (
        (grant_state = 'revoked') = (revoked_at IS NOT NULL)
    ),
    -- An active grant has been approved by somebody. `proposed` has not yet.
    CONSTRAINT ck_cross_org_grants_active_is_approved CHECK (
        grant_state <> 'active' OR jsonb_array_length(approving_authorities) > 0
    ),
    CONSTRAINT ck_cross_org_grants_policy_version_present CHECK (
        length(btrim(policy_version)) > 0
    )
)
"""

_MIGRATION_DISPOSITIONS = f"""
CREATE TABLE profile_migration_dispositions (
    disposition_id           UUID PRIMARY KEY,
    tenant_id                UUID NOT NULL REFERENCES tenants(tenant_id),

    -- What is being inventoried. `record_class` plus `subject_id` because a
    -- subject id is only unique inside its class.
    record_class             TEXT NOT NULL,
    subject_id               UUID NOT NULL,

    disposition              TEXT NOT NULL,

    -- The grandfather justification. All five or none: they are what separate a
    -- deliberate temporary exemption from an indefinite one nobody revisits.
    grandfather_owner        TEXT,
    grandfather_reason       TEXT,
    grandfather_warning      TEXT,
    grandfather_expires_at   TIMESTAMPTZ,
    enforced_action          TEXT,

    recorded_by              TEXT NOT NULL,
    recorded_at              TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_migration_dispositions_subject UNIQUE (tenant_id, record_class, subject_id),
    CONSTRAINT ck_migration_dispositions_kind CHECK (
        disposition IN ('migrate', 'grandfather', 'quarantine', 'remove')
    ),
    -- A grandfather carries its whole justification, and nothing else may carry
    -- any of it: a `migrate` row with an expiry would be an exemption in disguise.
    CONSTRAINT ck_migration_dispositions_grandfather_complete CHECK (
        (
            disposition = 'grandfather'
            AND grandfather_owner IS NOT NULL
            AND grandfather_reason IS NOT NULL
            AND grandfather_warning IS NOT NULL
            AND grandfather_expires_at IS NOT NULL
            AND enforced_action IS NOT NULL
        )
        OR (
            disposition <> 'grandfather'
            AND grandfather_owner IS NULL
            AND grandfather_reason IS NULL
            AND grandfather_warning IS NULL
            AND grandfather_expires_at IS NULL
            AND enforced_action IS NULL
        )
    ),
    -- The ceiling, as a rule rather than a convention: the failure mode is an
    -- exemption outliving the migration that needed it, and this is the only
    -- place that notices without somebody looking.
    CONSTRAINT ck_migration_dispositions_expiry_ceiling CHECK (
        grandfather_expires_at IS NULL
        OR (
            grandfather_expires_at > recorded_at
            AND grandfather_expires_at <= recorded_at + INTERVAL '{_GRANDFATHER_MAX_DAYS} days'
        )
    )
)
"""


def upgrade() -> None:
    op.execute(_OWNERSHIP_ASSIGNMENTS)
    op.execute(_OWNERSHIP_TRANSITIONS)
    op.execute(_CROSS_ORG_GRANTS)
    op.execute(_MIGRATION_DISPOSITIONS)

    # The question ownership resolution asks: who is accountable for this target
    # right now. Leading with the columns it filters on.
    op.execute(
        """
        CREATE INDEX ix_ownership_assignments_target
            ON ownership_assignments (tenant_id, owned_target_kind, owned_target_id, effective_from DESC)
        """
    )
    # The revocation sweep's question, from both ends: a grant is purged by
    # source or found by destination, and both directions are walked.
    op.execute(
        """
        CREATE INDEX ix_cross_org_grants_source
            ON cross_org_grants (source_tenant_id, grant_state, grant_kind)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_cross_org_grants_destination
            ON cross_org_grants (destination_tenant_id, grant_state, grant_kind)
        """
    )
    # Finding exemptions that are about to lapse, which is the sweep that keeps a
    # grandfather from becoming permanent.
    op.execute(
        """
        CREATE INDEX ix_migration_dispositions_expiry
            ON profile_migration_dispositions (grandfather_expires_at)
            WHERE grandfather_expires_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_migration_dispositions_expiry")
    op.execute("DROP INDEX IF EXISTS ix_cross_org_grants_destination")
    op.execute("DROP INDEX IF EXISTS ix_cross_org_grants_source")
    op.execute("DROP INDEX IF EXISTS ix_ownership_assignments_target")
    op.execute("DROP TABLE IF EXISTS profile_migration_dispositions")
    op.execute("DROP TABLE IF EXISTS cross_org_grants")
    # Transitions before assignments: each points at the one after it.
    op.execute("DROP TABLE IF EXISTS ownership_assignment_transitions")
    op.execute("DROP TABLE IF EXISTS ownership_assignments")
