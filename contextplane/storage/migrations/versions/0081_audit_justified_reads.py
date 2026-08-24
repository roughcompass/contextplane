"""Every per-actor read an auditor makes, and the reason they gave for it.

E11-T3. Per-actor detail is a real capability an auditor needs and nobody else
should have. The control is not the role alone — a role is a door, and a door
records nothing about who walked through it or why. The record is the control.

**Written before the data is returned, in the same transaction.** A
justification captured afterwards, or best-effort alongside, is a field that is
empty exactly when it matters: the read somebody did not want to explain is the
read that completes and leaves no note. `resolve.py` applies the same discipline
to its receipt — *"an answer nobody can later show they were given is the thing
receipts exist to prevent"* — and this is that argument about the reader rather
than the answer.

**Free text, and the length floor is the point.** A dropdown produces the reason
nearest the top; what is wanted is a sentence somebody has to be willing to have
read back to them. Twenty characters will not make a bad reason good, but it
stops "audit" and "checking" from being reasons at all.

**No foreign key on `subject_actor_id`.** An auditor may reasonably ask about an
actor who has since been erased, and a foreign key would make the *record of the
question* impossible after the answer stopped existing — which is precisely the
period an auditor is most likely to be asking about.
"""

from __future__ import annotations

from alembic import op

revision = "0081_audit_justified_reads"
down_revision: str | None = "0080_session_event_retention_policy"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = """
CREATE TABLE audit_justified_reads (
    read_id             UUID PRIMARY KEY,
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Who looked. A real actor, and the foreign key is deliberate here where it
    -- is absent below: an auditor who does not exist cannot have asked.
    auditor_actor_id    UUID NOT NULL REFERENCES actors(actor_id),

    -- Who was looked at. No foreign key -- see the module docstring.
    subject_actor_id    UUID NOT NULL,

    -- What was asked, in the same vocabulary the aggregate surface uses, so an
    -- auditor's questions can be read beside what the surface offers.
    metric              TEXT NOT NULL,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,

    justification       TEXT NOT NULL,
    read_at             TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_justified_read_reason CHECK (char_length(justification) BETWEEN 20 AND 2000),
    -- A window that ends before it starts is a query nobody meant to run, and
    -- storing one would make the record of it unreadable too.
    CONSTRAINT ck_justified_read_window CHECK (window_end > window_start)
)
"""

#: The read an oversight review actually makes: what did this auditor look at.
#: Ordered so the most recent is first, which is where a review starts.
_BY_AUDITOR = """
CREATE INDEX ix_justified_reads_by_auditor
    ON audit_justified_reads (tenant_id, auditor_actor_id, read_at DESC)
"""

#: The other direction, and the one a subject asks: who has been looking at me.
_BY_SUBJECT = """
CREATE INDEX ix_justified_reads_by_subject
    ON audit_justified_reads (tenant_id, subject_actor_id, read_at DESC)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_BY_AUDITOR)
    op.execute(_BY_SUBJECT)


def downgrade() -> None:
    op.execute("DROP INDEX ix_justified_reads_by_subject")
    op.execute("DROP INDEX ix_justified_reads_by_auditor")
    op.execute("DROP TABLE audit_justified_reads")
