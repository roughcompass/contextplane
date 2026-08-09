"""What outlives what, what was erased, and what still has to be rebuilt because of it.

Four concerns in one migration because the erasure lifecycle binds them: a
tombstone schedules derivative work, derivative work rebuilds aggregates, and an
erasure-triggered recompute destroys the aggregate version that preceded it. Split
across four migrations they would be four half-stories, each referencing tables
that did not exist yet.

**Two rules here are enforced by the schema rather than trusted to the code that
uses it**, because both are the kind that survive review and fail in production.

The first is the aggregation floor. No aggregate may be computed over fewer than
five actors, so `actor_count >= 5` is a CHECK on any row that carries a value. A
floor implemented only in the aggregation job is a floor that holds until the
second consumer writes its own query, and the row that violates it looks exactly
like the rows that do not.

The second is the differencing channel, and it is the one worth reading twice.
Recomputing an aggregate after an erasure leaks the erased subject's exact
contribution to anyone who read the cell before and reads it again after: they
subtract. The policy answer is that prior versions are destroyed at recompute
rather than retained. A `version` column with a "delete the old one" convention
would leave the leak one forgotten `DELETE` away, so there is no version column
-- uniqueness on (tenant, cohort, metric, window) makes a second version of one
cell unstorable. Destroying the predecessor is not a step the recompute has to
remember; it is the only way to write the successor at all.

**A tombstone proves erasure without becoming an oracle for it.** The proof is a
tenant-keyed HMAC, never a bare content digest: erased content is often guessable
and low-entropy, so a bare hash lets anyone who can guess it confirm the guess,
and equal prefixes would reveal equality across erased records. The raw digest
stays internal to chain verification and does not appear on this table. Nothing
here holds erased content, and a test asserts that no column for it appears.

**Derivatives register every source they came from, not the one that created
them.** A derivative never outlives any of its sources, and its retention is the
minimum across all of them -- which is not computable from a single source
reference. Hence a link row per source. The minimum itself cannot be a CHECK,
because it spans rows in another table; what the schema guarantees is that the
inputs to that computation are all present and that no derivative is registered
without an expiry at all, which is the case that would otherwise be unbounded.
"""

from __future__ import annotations

from alembic import op

revision = "0043_retention_and_derivatives"
down_revision: str | None = "0042_derivation_and_curation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# What erasure does to a record class. `exempt` is the accountability log case:
# it carries no values and its subject references are already pseudonymous.
_ERASURE_MODES = "'delete', 'minimize', 'minimize_and_tombstone', 'exempt'"

# The four handling classes the rest of the schema closes.
_CLASSIFICATIONS = "'public', 'internal', 'confidential', 'restricted'"

# What a registered derivative is. Open-ended in kind but closed as a set: an
# unregistered derivative is release-gating, so a kind nobody declared is a kind
# no propagation handler covers.
_DERIVATIVE_KINDS = (
    "'vector', 'embedding_chunk', 'fts_document', 'summary', 'cache', 'outbox', "
    "'log_projection', 'export', 'receipt_link', 'claim_derivative'"
)

# What propagation does to a derivative, and why it was scheduled.
_WORK_OPERATIONS = "'rebuild', 'delete', 'redact'"
_WORK_TRIGGERS = "'expiry', 'erasure', 'revocation', 'policy_change'"
_WORK_STATES = "'pending', 'claimed', 'done', 'failed'"

_PROPAGATION_STATES = "'pending', 'in_progress', 'complete', 'failed'"

# The approved minimum cohort size. A deployment may go stricter and may never go
# looser, which is why the comparison below is `>=` against this literal rather
# than against a configurable column nobody would notice being lowered.
_MIN_COHORT_ACTORS = 5


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE retention_policies (
            -- Versioned rather than mutable: correcting a period is a new policy
            -- version plus re-propagation, so every tombstone and derivative can
            -- name the version it was decided under and still be readable when
            -- the values move.
            policy_version      TEXT NOT NULL,
            record_class        TEXT NOT NULL,

            legal_basis         TEXT NOT NULL,
            -- NULL means the period is event-bounded rather than a duration
            -- ("life of tenant"), which is a different statement from "no
            -- retention limit" and must not be stored as a very large number.
            retention_days      INTEGER,
            erasure_mode        TEXT NOT NULL,

            -- What reduction satisfies erasure for this class, and what a
            -- verifier may say afterwards. Both are policy text rather than
            -- enumerations: they are read by humans deciding whether an
            -- implementation matches, and collapsing them to codes loses the
            -- part that makes that decision possible.
            minimization_action TEXT,
            tombstone_behaviour TEXT,
            verifier_disclosure TEXT NOT NULL,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            PRIMARY KEY (policy_version, record_class),

            CONSTRAINT ck_retention_erasure_mode CHECK (erasure_mode IN ({_ERASURE_MODES})),
            CONSTRAINT ck_retention_period_sane CHECK (retention_days IS NULL OR retention_days > 0),
            CONSTRAINT ck_retention_identity_present
                CHECK (
                    length(policy_version) > 0
                    AND length(record_class) > 0
                    AND length(legal_basis) > 0
                    AND length(verifier_disclosure) > 0
                ),
            -- A class that minimizes has to say what minimization means for it;
            -- otherwise "minimized" is a status nobody can verify was reached.
            CONSTRAINT ck_retention_minimizing_class_says_how
                CHECK (
                    erasure_mode NOT IN ('minimize', 'minimize_and_tombstone')
                    OR (minimization_action IS NOT NULL AND length(minimization_action) > 0)
                )
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE source_tombstones (
            tombstone_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),

            -- What was erased, by class and by id. The id is immutable under
            -- every policy: erasure removes content, never the fact that a
            -- record occupied a position.
            record_class     TEXT NOT NULL,
            subject_id       UUID NOT NULL,

            policy_version   TEXT NOT NULL,

            -- Who asked, on what basis, and when it took effect. `reason` is
            -- required: an erasure nobody can account for is indistinguishable
            -- from data loss.
            request_authority TEXT NOT NULL,
            reason            TEXT NOT NULL,
            effective_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- Tenant-keyed HMAC of the erased content, never the content digest.
            -- See the module docstring: a bare hash of guessable content is a
            -- confirmation oracle, and equal prefixes leak equality between
            -- erased records.
            proof_hmac        TEXT NOT NULL,

            propagation_state TEXT NOT NULL DEFAULT 'pending',

            CONSTRAINT ck_tombstone_propagation_state CHECK (propagation_state IN ({_PROPAGATION_STATES})),
            CONSTRAINT ck_tombstone_accountability_present
                CHECK (
                    length(record_class) > 0
                    AND length(request_authority) > 0
                    AND length(reason) > 0
                    AND length(proof_hmac) > 0
                ),
            CONSTRAINT fk_tombstone_policy
                FOREIGN KEY (policy_version, record_class)
                REFERENCES retention_policies (policy_version, record_class)
        )
        """
    )
    # One tombstone per erased record: erasing twice is not two erasures.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tombstone_subject
            ON source_tombstones (tenant_id, record_class, subject_id)
        """
    )
    # The propagation sweep's own selection.
    op.execute(
        """
        CREATE INDEX ix_tombstone_unpropagated
            ON source_tombstones (effective_at)
            WHERE propagation_state <> 'complete'
        """
    )

    op.execute(
        f"""
        CREATE TABLE derivative_registrations (
            derivative_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),

            derivative_kind  TEXT NOT NULL,
            -- Where the thing actually lives, in whatever addressing its store
            -- uses. Opaque here on purpose: a column typed to one store would
            -- have to change for the next one.
            storage_locator  TEXT NOT NULL,

            -- The audience this derivative was built for. A rebuild that changes
            -- it is a different derivative, because a cached answer computed for
            -- one audience must never be served to another.
            audience_partition TEXT NOT NULL,
            classification   TEXT NOT NULL,

            -- Versioned handlers, so a derivative built by a handler that has
            -- since changed can be identified rather than assumed rebuildable.
            rebuild_handler_version TEXT NOT NULL,
            delete_handler_version  TEXT NOT NULL,
            redact_handler_version  TEXT NOT NULL,

            policy_version   TEXT NOT NULL,
            -- Never NULL: an unbounded derivative is the case that outlives its
            -- sources silently. The value is the minimum across every source
            -- link below, computed by the registrar.
            expires_at       TIMESTAMPTZ NOT NULL,

            -- Whether reads must fail closed while this derivative is overdue
            -- after a revocation. Recorded per derivative because a stale cache
            -- and a stale authorization projection are not the same risk.
            blocking         BOOLEAN NOT NULL DEFAULT FALSE,

            last_synchronized_at TIMESTAMPTZ,
            sync_status      TEXT NOT NULL DEFAULT 'pending',

            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_derivative_kind CHECK (derivative_kind IN ({_DERIVATIVE_KINDS})),
            CONSTRAINT ck_derivative_classification CHECK (classification IN ({_CLASSIFICATIONS})),
            CONSTRAINT ck_derivative_sync_status CHECK (sync_status IN ({_PROPAGATION_STATES})),
            CONSTRAINT ck_derivative_identity_present
                CHECK (
                    length(storage_locator) > 0
                    AND length(audience_partition) > 0
                    AND length(rebuild_handler_version) > 0
                    AND length(delete_handler_version) > 0
                    AND length(redact_handler_version) > 0
                )
        )
        """
    )
    # One registration per stored thing per audience: the same locator serving
    # two audiences is two derivatives with two expiries, not one row.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_derivative_locator
            ON derivative_registrations (tenant_id, derivative_kind, storage_locator, audience_partition)
        """
    )
    # The expiry sweep, and the fail-closed read's own question.
    op.execute("CREATE INDEX ix_derivative_expiry ON derivative_registrations (expires_at)")
    op.execute(
        """
        CREATE INDEX ix_derivative_blocking_overdue
            ON derivative_registrations (tenant_id, expires_at)
            WHERE blocking
        """
    )

    op.execute(
        """
        CREATE TABLE derivative_source_links (
            link_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            derivative_id  UUID NOT NULL REFERENCES derivative_registrations(derivative_id) ON DELETE CASCADE,

            -- Every source, not the one that happened to trigger the build. A
            -- derivative's retention is the minimum across these, and a single
            -- source column makes that minimum uncomputable -- which is how a
            -- derivative comes to outlive a source nobody remembered it read.
            source_record_class TEXT NOT NULL,
            source_id      UUID NOT NULL,
            -- The revision read, where the source has revisions. NULL means the
            -- source is not revisioned, not that the revision is unknown.
            source_revision TEXT,
            -- This source's own expiry, copied at registration so the minimum is
            -- computable without joining five different tables that each store
            -- expiry differently.
            source_expires_at TIMESTAMPTZ,

            CONSTRAINT ck_derivative_source_class_present CHECK (length(source_record_class) > 0)
        )
        """
    )
    # One link per source per derivative.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_derivative_source
            ON derivative_source_links (derivative_id, source_record_class, source_id)
        """
    )
    # "What derivatives read this record?" -- the question an erasure asks.
    op.execute(
        """
        CREATE INDEX ix_derivative_source_lookup
            ON derivative_source_links (source_record_class, source_id)
        """
    )

    op.execute(
        f"""
        CREATE TABLE derivative_work_outbox (
            work_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID NOT NULL REFERENCES tenants(tenant_id),
            derivative_id  UUID NOT NULL REFERENCES derivative_registrations(derivative_id) ON DELETE CASCADE,

            operation      TEXT NOT NULL,
            trigger        TEXT NOT NULL,
            -- What caused it, when a tombstone did. NULL for expiry and policy
            -- change, which have no tombstone to name.
            tombstone_id   UUID REFERENCES source_tombstones(tombstone_id),

            state          TEXT NOT NULL DEFAULT 'pending',
            attempts       INTEGER NOT NULL DEFAULT 0,
            last_error     TEXT,

            available_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_at     TIMESTAMPTZ,
            completed_at   TIMESTAMPTZ,

            CONSTRAINT ck_work_operation CHECK (operation IN ({_WORK_OPERATIONS})),
            CONSTRAINT ck_work_trigger CHECK (trigger IN ({_WORK_TRIGGERS})),
            CONSTRAINT ck_work_state CHECK (state IN ({_WORK_STATES})),
            CONSTRAINT ck_work_attempts_nonneg CHECK (attempts >= 0),
            -- Erasure and revocation are always traceable to the tombstone that
            -- ordered them; work that cannot name its cause cannot be audited.
            CONSTRAINT ck_work_erasure_names_its_tombstone
                CHECK (trigger NOT IN ('erasure', 'revocation') OR tombstone_id IS NOT NULL),
            -- A failed attempt says why. A failure with no error is a work item
            -- nobody can act on.
            CONSTRAINT ck_work_failure_says_why
                CHECK (state <> 'failed' OR (last_error IS NOT NULL AND length(last_error) > 0))
        )
        """
    )
    # Propagation is idempotent: one cause enqueues one item per derivative per
    # operation, however many times the sweep runs. `NULLS NOT DISTINCT` is what
    # makes that hold for the expiry and policy-change triggers too -- without it
    # every NULL tombstone counts as a different key and the sweep re-enqueues
    # the same work on every pass.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_work_per_cause
            ON derivative_work_outbox (derivative_id, operation, trigger, tombstone_id)
            NULLS NOT DISTINCT
        """
    )
    # The worker's own claim query.
    op.execute(
        """
        CREATE INDEX ix_work_claimable
            ON derivative_work_outbox (available_at)
            WHERE state = 'pending'
        """
    )

    op.execute(
        f"""
        CREATE TABLE privacy_aggregates (
            aggregate_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID NOT NULL REFERENCES tenants(tenant_id),

            -- The cell: who is being counted, what is being measured, over what
            -- window.
            cohort_key     TEXT NOT NULL,
            metric         TEXT NOT NULL,
            window_start   TIMESTAMPTZ NOT NULL,
            window_end     TIMESTAMPTZ NOT NULL,

            -- How many distinct actors the cell covers. The floor is checked
            -- here, not only in the job that computes it.
            actor_count    INTEGER NOT NULL,
            -- NULL exactly when the cell is suppressed. A suppressed cell that
            -- still carried its value would defeat the suppression at the only
            -- layer that matters.
            value          JSONB,
            suppressed     BOOLEAN NOT NULL DEFAULT FALSE,
            -- A total computed over reported cells only. Never served beside
            -- suppressed cells as though it were the true total, because a
            -- reader subtracts.
            partial        BOOLEAN NOT NULL DEFAULT FALSE,

            policy_version TEXT NOT NULL,
            computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at     TIMESTAMPTZ NOT NULL,

            CONSTRAINT ck_aggregate_identity_present
                CHECK (length(cohort_key) > 0 AND length(metric) > 0),
            CONSTRAINT ck_aggregate_window_ordered CHECK (window_end > window_start),
            CONSTRAINT ck_aggregate_actor_count_nonneg CHECK (actor_count >= 0),
            -- The floor. A reported cell covers at least the approved minimum
            -- number of actors; anything smaller is suppressed and carries no
            -- value. Deployments may go stricter in code, never looser.
            CONSTRAINT ck_aggregate_meets_the_floor
                CHECK (suppressed OR actor_count >= {_MIN_COHORT_ACTORS}),
            CONSTRAINT ck_aggregate_suppressed_carries_no_value
                CHECK ((suppressed AND value IS NULL) OR (NOT suppressed AND value IS NOT NULL))
        )
        """
    )
    # One version of a cell, ever. This is the differencing defence: a recompute
    # cannot leave its predecessor in place, because there is nowhere to put it.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_aggregate_cell
            ON privacy_aggregates (tenant_id, cohort_key, metric, window_start, window_end)
        """
    )
    op.execute("CREATE INDEX ix_aggregate_expiry ON privacy_aggregates (expires_at)")


def downgrade() -> None:
    # Dependency order: work and links reference registrations; tombstones
    # reference policies.
    op.execute("DROP TABLE IF EXISTS privacy_aggregates")
    op.execute("DROP TABLE IF EXISTS derivative_work_outbox")
    op.execute("DROP TABLE IF EXISTS derivative_source_links")
    op.execute("DROP TABLE IF EXISTS derivative_registrations")
    op.execute("DROP TABLE IF EXISTS source_tombstones")
    op.execute("DROP TABLE IF EXISTS retention_policies")
