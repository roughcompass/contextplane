"""Shadow observation, qualification, and replay -- the last table group in
this phase's migration chain.

Revision ID: 0011_arc_observation
Revises: 0010_arc_risk_and_envelopes
Create Date: 2026-08-07

Five tables, in dependency order:

`arc_observation_cohorts` freezes one `arc_observation_cohort_v1` record per
candidate proposal version (`UNIQUE (proposal_id, proposal_version)` -- one
cohort per candidate, ever; a new attempt is a new proposal version, not a
re-frozen cohort). Two columns beyond the wire profile are bookkeeping, not
part of the closed record: `closed_at` is set once, the instant the window
stops accepting further observations (either because the required window
elapsed with sufficient coverage, or because the seven-day maximum elapsed
without it); `window_ended_at` is the actual observed-window end the
qualification decision is computed over, which is `window_deadline` in the
happy path but can extend to the seven-day cap when the required window
alone was insufficient. Both are `NULL` until `qualification.py` or
`observation_window_evaluator.py` closes the cohort -- see either module's
own docstring for why the closing instant matters (a write accepted after
`closed_at` would let a late-arriving result change an already-fixed
denominator).

`arc_observation_cohort_members` is the one place a global cohort's tenant
membership is durable. Nothing outside this table ever holds it: qualifying
a global candidate reads only the aggregate counters in
`arc_observation_results`, grouped by `cohort_id` and never projecting
`tenant_id` -- see `queries/observation.py::load_aggregate_counters`. `PK
(cohort_id, tenant_id)` is therefore not just a key choice, it is the
control: there is no column anywhere else that could leak a member tenant
into a cross-tenant read, because the identity only ever lives in a table
whose own query surface refuses to select it back out in aggregate.

`arc_observation_results` carries only bounded counters and digests, per
ADR 041 Sec.7's "fingerprints, never manifests" rule: no manifest, no
repository identity, no session id, no task summary, anywhere in this
table. One row per `(cohort_id, tenant_id)` -- even a global cohort's
results are tenant-attributed at the storage layer, because a global
qualification's aggregate view is a `SUM()` over these rows, not a second,
untenanted row that would otherwise be the one place membership-shaped
information could hide unaggregated. `fingerprint_digests` is the bounded,
per-observation-class-digest granularity that `observation_fingerprint_
reaper.py` clears 30 days after the owning cohort closes; `legal_hold_at`
suspends that clearing for a row under active appeal.

`arc_observation_qualifications` is the durable, signed decision record.
`qualification_id` is both the primary key and named again as its own
`UNIQUE` constraint -- the same restatement `0009_arc_approval_challenges.py`
uses for `approval_challenge_id`, because the TDD names the rule as a
discrete property to prove rather than an incidental consequence of the key
choice. The eight-column binding tuple is `UNIQUE NULLS NOT DISTINCT`, not a
plain `UNIQUE`: two of the eight columns (`baseline_revision_id`,
`replay_corpus_digest`) are legitimately `NULL` on the common path (a new
artifact family has no baseline; no candidate has an approved replay corpus
before the seven-day mark), and Postgres's ordinary `UNIQUE` treats two
`NULL`s as distinct from each other -- so a plain `UNIQUE` here would let an
"exact retry" of the same unaccepted qualification insert a second row
every single time, which is precisely the idempotency rule ADR 041 Sec.6
requires this constraint to hold. `NULLS NOT DISTINCT` (Postgres 15+) closes
that gap.

`arc_observation_replay_corpora` is the operator/tenant-admin-approved
fallback corpus record -- digests and counts only, per the same "never
manifests" rule.
"""

from __future__ import annotations

from alembic import op

revision = "0011_arc_observation"
down_revision: str | None = "0010_arc_risk_and_envelopes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_observation_cohorts
# ---------------------------------------------------------------------------

_COHORTS_DDL = """
CREATE TABLE arc_observation_cohorts (
    cohort_id                      UUID PRIMARY KEY,
    proposal_id                    UUID NOT NULL,
    proposal_version               INTEGER NOT NULL,
    candidate_revision_id           UUID NOT NULL REFERENCES arc_revisions(revision_id),
    risk_classification              TEXT NOT NULL,
    scope_predicate_digest            TEXT NOT NULL,
    tenant_membership_digest           TEXT NOT NULL,
    eligibility_predicate_digest         TEXT NOT NULL,
    frozen_at                              TIMESTAMPTZ NOT NULL,
    window_started_at                        TIMESTAMPTZ NOT NULL,
    window_deadline                            TIMESTAMPTZ NOT NULL,
    window_ended_at                              TIMESTAMPTZ,
    closed_at                                      TIMESTAMPTZ,
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT uq_arc_observation_cohorts_version UNIQUE (proposal_id, proposal_version),
    CONSTRAINT ck_arc_observation_cohorts_classification CHECK (
        risk_classification IN (
            'global_mandatory', 'global_non_mandatory',
            'tenant_mandatory', 'tenant_non_mandatory',
            'domain_mandatory', 'domain_non_mandatory',
            'capability_mandatory', 'capability_non_mandatory',
            'task_mandatory', 'task_non_mandatory'
        )
    ),
    CONSTRAINT ck_arc_observation_cohorts_window CHECK (window_started_at < window_deadline),
    -- A cohort is either fully open (neither bookkeeping column set) or
    -- fully closed (both set together) -- there is no state where the
    -- window has an end but is still accepting writes, or vice versa.
    CONSTRAINT ck_arc_observation_cohorts_close_pair CHECK (
        (closed_at IS NULL) = (window_ended_at IS NULL)
    )
)
"""

_COHORTS_INDEXES = [
    "CREATE INDEX ix_arc_observation_cohorts_candidate ON arc_observation_cohorts (candidate_revision_id)",
]

# ---------------------------------------------------------------------------
# arc_observation_cohort_members -- the one durable home for a global
# cohort's tenant membership. See this module's own docstring for why the
# PK choice is the leak-prevention control, not incidental.
# ---------------------------------------------------------------------------

_COHORT_MEMBERS_DDL = """
CREATE TABLE arc_observation_cohort_members (
    cohort_id      UUID NOT NULL REFERENCES arc_observation_cohorts(cohort_id),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    added_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cohort_id, tenant_id)
)
"""

# ---------------------------------------------------------------------------
# arc_observation_results -- bounded counters and digests only. No manifest,
# repository identity, session id, or task summary column exists here, per
# ADR 041 Sec.7. `fingerprint_digests` is the one column that carries
# per-observation granularity (a bounded JSON array of individual
# observation-class digests, never a manifest) -- it is what `observation_
# fingerprint_reaper.py` clears thirty days after the owning cohort closes,
# per Sec.7's "delete per-manifest fingerprints ... keep only counters and
# signed digests" rule. The aggregate counters below never get reaped: they
# are what "keep only counters" means. `legal_hold_at` is the one column
# that keeps that clearing from happening while an appeal against this
# cohort's decision is open.
# ---------------------------------------------------------------------------

_RESULTS_DDL = """
CREATE TABLE arc_observation_results (
    cohort_id                UUID NOT NULL REFERENCES arc_observation_cohorts(cohort_id),
    tenant_id                  UUID NOT NULL REFERENCES tenants(tenant_id),
    eligible_count               INTEGER NOT NULL DEFAULT 0,
    observed_count                  INTEGER NOT NULL DEFAULT 0,
    unexplained_count                 INTEGER NOT NULL DEFAULT 0,
    out_of_envelope_count               INTEGER NOT NULL DEFAULT 0,
    counters_by_delta_code                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint_digests                       JSONB NOT NULL DEFAULT '[]'::jsonb,
    legal_hold_at                                TIMESTAMPTZ,
    fingerprints_reaped_at                         TIMESTAMPTZ,
    updated_at                                        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cohort_id, tenant_id),
    CONSTRAINT ck_arc_observation_results_eligible_nonneg CHECK (eligible_count >= 0),
    CONSTRAINT ck_arc_observation_results_observed_nonneg CHECK (observed_count >= 0),
    CONSTRAINT ck_arc_observation_results_observed_bounded CHECK (observed_count <= eligible_count)
)
"""

# ---------------------------------------------------------------------------
# arc_observation_qualifications
# ---------------------------------------------------------------------------

_QUALIFICATIONS_DDL = """
CREATE TABLE arc_observation_qualifications (
    qualification_id                 UUID PRIMARY KEY,
    idempotency_key_digest             TEXT NOT NULL,
    candidate_review_package_digest    TEXT NOT NULL,
    candidate_revision_id              UUID NOT NULL REFERENCES arc_revisions(revision_id),
    proposal_id                        UUID NOT NULL,
    proposal_version                   INTEGER NOT NULL,
    risk_classification                TEXT NOT NULL,
    risk_algorithm_version             TEXT NOT NULL,
    baseline_revision_id               UUID REFERENCES arc_revisions(revision_id),
    selection_engine_version           TEXT NOT NULL,
    engine_configuration_version       TEXT NOT NULL,
    cohort_id                          UUID NOT NULL
        REFERENCES arc_observation_cohorts(cohort_id),
    cohort_digest                      TEXT NOT NULL,
    window_started_at                  TIMESTAMPTZ NOT NULL,
    window_ended_at                    TIMESTAMPTZ NOT NULL,
    eligible_count                     INTEGER NOT NULL,
    observed_count                     INTEGER NOT NULL,
    expected_impact_envelope_digest    TEXT NOT NULL,
    counters_by_delta_code             JSONB NOT NULL DEFAULT '[]'::jsonb,
    unexplained_count                  INTEGER NOT NULL DEFAULT 0,
    out_of_envelope_count              INTEGER NOT NULL DEFAULT 0,
    replay_corpus_digest               TEXT
        REFERENCES arc_observation_replay_corpora(canonical_corpus_digest),
    replay_result_digest               TEXT,
    qualification_algorithm_version    TEXT NOT NULL,
    computed_decision                  TEXT NOT NULL,
    computed_at                        TIMESTAMPTZ NOT NULL,
    reason_codes                       TEXT[] NOT NULL DEFAULT '{}',
    accepted_by_issuer                 TEXT,
    accepted_by_subject                TEXT,
    accepted_by_role                   TEXT,
    accepted_at                        TIMESTAMPTZ,
    acceptance_audit_reference         TEXT,
    expires_at                         TIMESTAMPTZ,
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT uq_arc_observation_qualifications_id UNIQUE (qualification_id),
    -- The eight-column binding tuple, Appendix B.3 verbatim. `NULLS NOT
    -- DISTINCT` (not a plain UNIQUE) is load-bearing -- see this module's
    -- own docstring for why an ordinary UNIQUE would silently let every
    -- "exact retry" insert a duplicate whenever either nullable member is
    -- NULL, which is the common case for both of them.
    CONSTRAINT uq_arc_observation_qualifications_binding UNIQUE NULLS NOT DISTINCT (
        candidate_review_package_digest,
        baseline_revision_id,
        selection_engine_version,
        engine_configuration_version,
        cohort_digest,
        expected_impact_envelope_digest,
        replay_corpus_digest,
        qualification_algorithm_version
    ),
    CONSTRAINT ck_arc_observation_qualifications_classification CHECK (
        risk_classification IN (
            'global_mandatory', 'global_non_mandatory',
            'tenant_mandatory', 'tenant_non_mandatory',
            'domain_mandatory', 'domain_non_mandatory',
            'capability_mandatory', 'capability_non_mandatory',
            'task_mandatory', 'task_non_mandatory'
        )
    ),
    CONSTRAINT ck_arc_observation_qualifications_decision CHECK (
        computed_decision IN ('qualified', 'qualified_low_traffic', 'insufficient', 'failed')
    ),
    -- Acceptance is a five-column all-or-nothing group: `expires_at` is
    -- NULL until acceptance and set to exactly `accepted_at + 24h` in the
    -- same accepting transaction (service-enforced; not expressible as a
    -- CHECK against a moving `now()`), so the columns that describe *who*
    -- accepted and *that* it was accepted move together.
    CONSTRAINT ck_arc_observation_qualifications_acceptance_group CHECK (
        (accepted_by_issuer IS NULL) = (accepted_by_subject IS NULL)
        AND (accepted_by_issuer IS NULL) = (accepted_by_role IS NULL)
        AND (accepted_by_issuer IS NULL) = (accepted_at IS NULL)
        AND (accepted_by_issuer IS NULL) = (acceptance_audit_reference IS NULL)
        AND (accepted_by_issuer IS NULL) = (expires_at IS NULL)
    )
)
"""

_QUALIFICATIONS_INDEXES = [
    "CREATE INDEX ix_arc_observation_qualifications_version "
    "ON arc_observation_qualifications (proposal_id, proposal_version)",
    "CREATE INDEX ix_arc_observation_qualifications_cohort ON arc_observation_qualifications (cohort_id)",
]

# ---------------------------------------------------------------------------
# arc_observation_replay_corpora -- created before arc_observation_
# qualifications because that table's `replay_corpus_digest` column
# references this one's `canonical_corpus_digest`.
# ---------------------------------------------------------------------------

_REPLAY_CORPORA_DDL = """
CREATE TABLE arc_observation_replay_corpora (
    corpus_id                    UUID PRIMARY KEY,
    generator_version               TEXT NOT NULL,
    generator_input_digest             TEXT NOT NULL,
    canonical_corpus_digest               TEXT NOT NULL,
    fixture_class_count                     INTEGER NOT NULL,
    owning_scope                               TEXT NOT NULL,
    target_tenant_id                             UUID REFERENCES tenants(tenant_id),
    approving_authority_issuer                     TEXT NOT NULL,
    approving_authority_subject                      TEXT NOT NULL,
    approved_at                                        TIMESTAMPTZ NOT NULL,
    expires_at                                           TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_arc_observation_replay_corpora_digest UNIQUE (canonical_corpus_digest),
    CONSTRAINT ck_arc_observation_replay_corpora_scope CHECK (owning_scope IN ('global', 'tenant')),
    CONSTRAINT ck_arc_observation_replay_corpora_scope_tenant CHECK (
        (owning_scope = 'tenant') = (target_tenant_id IS NOT NULL)
    ),
    CONSTRAINT ck_arc_observation_replay_corpora_fixture_count CHECK (fixture_class_count >= 100),
    CONSTRAINT ck_arc_observation_replay_corpora_window CHECK (approved_at < expires_at)
)
"""


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    #
    # Replay corpora before qualifications: the qualifications table's
    # `replay_corpus_digest` column carries a FK to this table's own
    # `canonical_corpus_digest`, so the referenced table must exist first.
    op.execute(_COHORTS_DDL)
    for stmt in _COHORTS_INDEXES:
        op.execute(stmt)
    op.execute(_COHORT_MEMBERS_DDL)
    op.execute(_RESULTS_DDL)
    op.execute(_REPLAY_CORPORA_DDL)
    op.execute(_QUALIFICATIONS_DDL)
    for stmt in _QUALIFICATIONS_INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    # Reverse dependency order.
    op.execute("DROP TABLE arc_observation_qualifications")
    op.execute("DROP TABLE arc_observation_replay_corpora")
    op.execute("DROP TABLE arc_observation_results")
    op.execute("DROP TABLE arc_observation_cohort_members")
    op.execute("DROP TABLE arc_observation_cohorts")
