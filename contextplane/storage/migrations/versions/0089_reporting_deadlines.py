"""The notification clock: three deadlines, stamped, and the durations they come from.

E4-T6. `0076` says the only way out of `unclassified` is somebody deciding and
being recorded as having decided, and that is still true -- nothing here
classifies anything. What this adds is what happens *after* a person classifies
an obligation as material.

**The three durations default to the regulation's own and are overridable per
tenant.** Unlike the *thresholds* -- which are a judgement about whether a given
incident is major, and which `0076` rightly refused to invent -- the reporting
durations are published text in DORA's RTS. A default sourced to it is not this
service deciding anything; it is the service not making every deployment retype
a number the regulation already fixed.

Which numbers a row was stamped from is recorded on the row, because a default
that later changes must not silently rewrite what somebody was working to.

**Stamped as three instants at classification time, never computed on read.** A
computed deadline moves when the classification timestamp is corrected, and "when
was this due" is precisely the question an audit asks. The stored instant is what
somebody was working to.

Revision ID: 0089_reporting_deadlines
Revises: 0087_instruction_delta_scope
"""

from __future__ import annotations

from alembic import op

revision = "0089_reporting_deadlines"
down_revision: str | None = "0087_instruction_delta_scope"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE reporting_deadline_policies (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(tenant_id),

            -- Seconds after classification. A row here overrides the built-in
            -- default, which is the point of the table: a deployment under a
            -- different regime, or an RTS revision, changes these without a
            -- release.
            initial_seconds      INTEGER NOT NULL,
            intermediate_seconds INTEGER NOT NULL,
            final_seconds        INTEGER NOT NULL,

            -- Where the numbers came from, in a sentence somebody is willing to
            -- have read back to them. Same discipline as a classification note:
            -- three durations with no stated source are three numbers nobody can
            -- audit.
            source_note TEXT NOT NULL,

            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            recorded_by UUID NOT NULL REFERENCES actors(actor_id),

            -- Ordered, because an intermediate report due before the initial one
            -- is not a configuration somebody meant. Caught here as well as in
            -- the service so a second writer cannot decide otherwise.
            CONSTRAINT ck_deadline_order CHECK (
                initial_seconds > 0
                AND initial_seconds < intermediate_seconds
                AND intermediate_seconds < final_seconds
            ),
            CONSTRAINT ck_deadline_source_stated CHECK (length(source_note) >= 20)
        )
        """
    )

    # All three or none. A partially stamped obligation would let a reader
    # believe the missing ones were not due rather than not recorded.
    op.execute(
        """
        ALTER TABLE reporting_obligations
            ADD COLUMN initial_report_due_at      TIMESTAMPTZ,
            ADD COLUMN intermediate_report_due_at TIMESTAMPTZ,
            ADD COLUMN final_report_due_at        TIMESTAMPTZ,

            -- Which durations produced the three instants above: the built-in
            -- default, or this tenant's own row. Recorded because a default that
            -- changes in a later release must not leave an auditor unable to say
            -- which numbers a given deadline came from -- and because a
            -- deployment silently running on defaults for a compliance clock is
            -- something an operator should be able to see.
            ADD COLUMN deadline_basis TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE reporting_obligations
            ADD CONSTRAINT ck_obligation_deadline_basis CHECK (
                (deadline_basis IS NULL AND initial_report_due_at IS NULL)
                OR deadline_basis IN ('default', 'tenant_policy')
            )
        """
    )
    op.execute(
        """
        ALTER TABLE reporting_obligations
            ADD CONSTRAINT ck_obligation_deadlines_together CHECK (
                (initial_report_due_at IS NULL
                 AND intermediate_report_due_at IS NULL
                 AND final_report_due_at IS NULL)
                OR (initial_report_due_at IS NOT NULL
                    AND intermediate_report_due_at IS NOT NULL
                    AND final_report_due_at IS NOT NULL
                    AND initial_report_due_at < intermediate_report_due_at
                    AND intermediate_report_due_at < final_report_due_at)
            )
        """
    )
    # Deadlines only exist on something somebody classified. A stamped row that
    # is still `unclassified` would be a deadline for a decision nobody made.
    op.execute(
        """
        ALTER TABLE reporting_obligations
            ADD CONSTRAINT ck_obligation_deadlines_need_classification CHECK (
                initial_report_due_at IS NULL OR classified_at IS NOT NULL
            )
        """
    )

    # The gauge's query: material rows ordered by the deadline nearest to
    # passing. Partial, because the rows without deadlines are counted by a
    # different question and putting them in this index would make the common
    # scan walk them.
    op.execute(
        "CREATE INDEX ix_obligation_next_deadline ON reporting_obligations "
        "(initial_report_due_at) WHERE initial_report_due_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_obligation_next_deadline")
    op.execute("ALTER TABLE reporting_obligations " "DROP CONSTRAINT ck_obligation_deadlines_need_classification")
    op.execute("ALTER TABLE reporting_obligations DROP CONSTRAINT ck_obligation_deadlines_together")
    op.execute("ALTER TABLE reporting_obligations DROP CONSTRAINT ck_obligation_deadline_basis")
    op.execute(
        "ALTER TABLE reporting_obligations "
        "DROP COLUMN initial_report_due_at, "
        "DROP COLUMN intermediate_report_due_at, "
        "DROP COLUMN final_report_due_at, "
        "DROP COLUMN deadline_basis"
    )
    op.execute("DROP TABLE reporting_deadline_policies")
