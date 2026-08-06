"""Artifact families gain a writer; the proposal aggregate lands.

Revision ID: 0005_arc_authoring_proposals
Revises: 0004_arc_source_admission
Create Date: 2026-08-06

Before this migration, `arc_artifacts` existed with no writer at all -- the
authoring surface's `create_family` is the first one, and it needs three
columns the baseline table never had: a human-facing `title`, an
`active_revision_id` for activation's baseline-drift compare-and-swap to
target, and a `created_by_issuer`/`created_by_subject` pair matching the
issuer/subject shape every other authoring-surface table already uses
(`created_by_actor_id` predates that convention and is left as-is, unused by
the new writer). The table has no reader or writer anywhere in this tree
today, so widening it here is additive in practice, not just in principle.

Five new tables complete the proposal aggregate ADR 040 defines:

- `arc_authoring_proposals` -- a proposal thread: stable identity and
  sequence coordination only, one row per artifact family (`artifact_id` is
  `UNIQUE`). State never lives here.
- `arc_authoring_proposal_versions` -- where all proposal state lives.
  Composite PK `(proposal_id, proposal_version)`; `UNIQUE (revision_id)` is
  the bijection; the partial `UNIQUE` on `proposal_id` for nonterminal
  states is the one-candidate-per-thread rule; the CHECK on `state` closes
  the eight-literal vocabulary.
- `arc_authoring_field_provenance`, `arc_authoring_semantic_tests`,
  `arc_authoring_reach_confirmations` -- the rest of the aggregate later
  tasks populate. Created here so the migration chain does not need a
  second entry for tables that belong to the same aggregate; their
  conditional-requiredness and overlap rules are service-enforced (see
  each table's own comment below), not DDL, because those rules span
  columns in ways a CHECK cannot express.
"""

from __future__ import annotations

from alembic import op

revision = "0005_arc_authoring_proposals"
down_revision: str | None = "0004_arc_source_admission"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_artifacts -- widen the pre-existing, previously-unwritten table.
# ---------------------------------------------------------------------------

_ARTIFACTS_ALTER = [
    # DEFAULT then DROP DEFAULT: safe as a metadata-only op on an empty
    # table, and it means a future insert must supply the value explicitly
    # rather than silently inherit a placeholder.
    "ALTER TABLE arc_artifacts ADD COLUMN title TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE arc_artifacts ALTER COLUMN title DROP DEFAULT",
    "ALTER TABLE arc_artifacts ADD CONSTRAINT ck_arc_artifacts_title_len CHECK (char_length(title) <= 200)",
    "ALTER TABLE arc_artifacts ADD COLUMN active_revision_id UUID REFERENCES arc_revisions(revision_id)",
    "ALTER TABLE arc_artifacts ADD COLUMN created_by_issuer TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE arc_artifacts ALTER COLUMN created_by_issuer DROP DEFAULT",
    "ALTER TABLE arc_artifacts ADD COLUMN created_by_subject TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE arc_artifacts ALTER COLUMN created_by_subject DROP DEFAULT",
]

# ---------------------------------------------------------------------------
# arc_authoring_proposals -- the thread. One row per artifact family.
# ---------------------------------------------------------------------------

_PROPOSALS_DDL = """
CREATE TABLE arc_authoring_proposals (
    proposal_id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL UNIQUE REFERENCES arc_artifacts(artifact_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# ---------------------------------------------------------------------------
# arc_authoring_proposal_versions -- all proposal state.
# ---------------------------------------------------------------------------

_PROPOSAL_VERSIONS_DDL = """
CREATE TABLE arc_authoring_proposal_versions (
    proposal_id                    UUID NOT NULL REFERENCES arc_authoring_proposals(proposal_id),
    proposal_version                INTEGER NOT NULL,
    artifact_id                     UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    tenant_id                       UUID REFERENCES tenants(tenant_id),
    state                            TEXT NOT NULL,
    source_evidence_id              UUID NOT NULL REFERENCES arc_source_approval_evidence(source_evidence_id),
    reviewed_baseline_revision_id   UUID REFERENCES arc_revisions(revision_id),
    revision_id                     UUID REFERENCES arc_revisions(revision_id),
    risk_classification             TEXT,
    risk_algorithm_version           TEXT,
    opened_by_issuer                 TEXT NOT NULL,
    opened_by_subject                TEXT NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at                        TIMESTAMPTZ,
    terminal_reason_code             TEXT,
    terminal_note                    TEXT,
    terminal_by_issuer               TEXT,
    terminal_by_subject              TEXT,
    terminalized_at                  TIMESTAMPTZ,
    PRIMARY KEY (proposal_id, proposal_version),
    CONSTRAINT uq_arc_authoring_proposal_versions_revision UNIQUE (revision_id),
    CONSTRAINT ck_arc_authoring_proposal_versions_version_positive CHECK (proposal_version >= 1),
    CONSTRAINT ck_arc_authoring_proposal_versions_state CHECK (
        state IN ('open', 'submitted', 'approved', 'activated', 'rejected', 'stale', 'superseded', 'withdrawn')
    ),
    CONSTRAINT ck_arc_authoring_proposal_versions_note_len CHECK (
        terminal_note IS NULL OR char_length(terminal_note) <= 2000
    )
)
"""

_PROPOSAL_VERSIONS_INDEXES = [
    # The one-nonterminal-candidate-per-thread rule: a thread may have any
    # number of terminal versions, but at most one row in a live state.
    # This is what "second open rejected" and "N+1 opens only after N is
    # terminal" both resolve to at the database layer -- an application
    # check alone would leave a window between check and insert.
    "CREATE UNIQUE INDEX uq_arc_authoring_proposal_versions_one_live ON arc_authoring_proposal_versions "
    "(proposal_id) WHERE state IN ('open', 'submitted', 'approved')",
    "CREATE INDEX ix_arc_authoring_proposal_versions_artifact ON arc_authoring_proposal_versions (artifact_id)",
    "CREATE INDEX ix_arc_authoring_proposal_versions_tenant ON arc_authoring_proposal_versions (tenant_id)",
]

# ---------------------------------------------------------------------------
# arc_authoring_field_provenance -- one field_provenance_v1 record per
# semantic field instance. The three-way conditional requiredness
# (source_backed / human_judgment / server_derived each require and forbid
# a different column group) is enforced in provenance.py, not here: it is a
# same-row, cross-column rule a CHECK can express, but ADR 040 assigns it a
# specific refusal code (arc_provenance_invalid) owned by that service, and
# a CHECK failure would surface as an opaque database IntegrityError instead
# -- the same reasoning arc_authoring.py's own docstring gives for not
# re-validating it a second time at the wire layer.
# ---------------------------------------------------------------------------

_FIELD_PROVENANCE_DDL = """
CREATE TABLE arc_authoring_field_provenance (
    proposal_id       UUID NOT NULL,
    proposal_version   INTEGER NOT NULL,
    field_path          TEXT NOT NULL,
    provenance_class    TEXT NOT NULL,
    source_evidence_id  UUID REFERENCES arc_source_approval_evidence(source_evidence_id),
    source_anchor        TEXT,
    excerpt_digest        TEXT,
    author_issuer          TEXT,
    author_subject         TEXT,
    author_role            TEXT,
    derivation_profile     TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (proposal_id, proposal_version, field_path),
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT ck_arc_authoring_field_provenance_class CHECK (
        provenance_class IN ('source_backed', 'human_judgment', 'server_derived')
    ),
    CONSTRAINT ck_arc_authoring_field_provenance_digest_len CHECK (
        excerpt_digest IS NULL OR char_length(excerpt_digest) = 64
    )
)
"""

# ---------------------------------------------------------------------------
# arc_authoring_semantic_tests -- frozen test inputs/results.
# ---------------------------------------------------------------------------

_SEMANTIC_TESTS_DDL = """
CREATE TABLE arc_authoring_semantic_tests (
    proposal_id       UUID NOT NULL,
    proposal_version   INTEGER NOT NULL,
    test_id             TEXT NOT NULL,
    manifest             JSONB NOT NULL,
    passed                BOOLEAN NOT NULL,
    expected               JSONB NOT NULL,
    actual                  JSONB NOT NULL,
    executed_at              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (proposal_id, proposal_version, test_id),
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version)
)
"""

# ---------------------------------------------------------------------------
# arc_authoring_reach_confirmations -- per-field confirmation state.
# ---------------------------------------------------------------------------

_REACH_CONFIRMATIONS_DDL = """
CREATE TABLE arc_authoring_reach_confirmations (
    proposal_id        UUID NOT NULL,
    proposal_version    INTEGER NOT NULL,
    field_path            TEXT NOT NULL,
    confirmed              BOOLEAN NOT NULL DEFAULT false,
    confirmed_at            TIMESTAMPTZ,
    confirmed_by_issuer      TEXT,
    confirmed_by_subject     TEXT,
    PRIMARY KEY (proposal_id, proposal_version, field_path),
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version)
)
"""


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    for stmt in _ARTIFACTS_ALTER:
        op.execute(stmt)

    op.execute(_PROPOSALS_DDL)

    op.execute(_PROPOSAL_VERSIONS_DDL)
    for stmt in _PROPOSAL_VERSIONS_INDEXES:
        op.execute(stmt)

    op.execute(_FIELD_PROVENANCE_DDL)
    op.execute(_SEMANTIC_TESTS_DDL)
    op.execute(_REACH_CONFIRMATIONS_DDL)


def downgrade() -> None:
    # Reverse dependency order: the three child tables reference
    # proposal_versions, which references proposals, which references
    # arc_artifacts -- and the added arc_artifacts columns come off last.
    op.execute("DROP TABLE arc_authoring_reach_confirmations")
    op.execute("DROP TABLE arc_authoring_semantic_tests")
    op.execute("DROP TABLE arc_authoring_field_provenance")
    op.execute("DROP TABLE arc_authoring_proposal_versions")
    op.execute("DROP TABLE arc_authoring_proposals")
    op.execute("ALTER TABLE arc_artifacts DROP COLUMN created_by_subject")
    op.execute("ALTER TABLE arc_artifacts DROP COLUMN created_by_issuer")
    op.execute("ALTER TABLE arc_artifacts DROP COLUMN active_revision_id")
    op.execute("ALTER TABLE arc_artifacts DROP COLUMN title")
