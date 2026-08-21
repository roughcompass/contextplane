"""Envelope enforcement is a per-tenant stage, and advisory refusals are recorded.

Landing "no envelope, no authority" as specified would refuse every agent in
every deployment on the day it shipped, because no principal has an envelope
yet. So enforcement graduates: `advisory` first, where the decision runs and
records what it *would* have refused, then `enforcing`, where the refusal is
real.

**Per tenant, not per deployment.** A multi-tenant deployment that can only
graduate everybody at once cannot graduate anybody: the first tenant with one
ungoverned agent pins every other tenant to advisory forever.

**A column on `tenants`, not an environment variable.** Three tenant-level
policy columns already live here in exactly this shape -- `is_regulated`,
`notification_digest_window`, `memory_retention_days`, each CHECK-constrained,
each read with a plain `SELECT` at the point of use -- so this is the house
pattern rather than a new one. The environment is deliberately not an option: a
mode read from there would be the first flag in this repository able to *widen*
authority, and it would do it without an audit row naming who widened it.

**Defaulting to `advisory` is the safe default here, which is unusual and worth
saying.** Everywhere else in this service the safe default is the restrictive
one. Not here: `enforcing` on an unmigrated tenant refuses every agent that
tenant runs, and an availability failure across the whole fleet is not a safer
outcome than a recorded one. The restrictive direction is reached by graduating,
which is a deliberate act with a pre-flight in front of it.

**`arc_envelope_advisory_records` holds refusals only, and its shape is decided
by the query that reads it** rather than by what is convenient to log. That query
asks: which principals acted in this window and had no envelope at all? So it
needs the tenant, the principal, the verdict and the instant -- and it needs
`no_envelope` kept distinct from the other three refusals, because a principal
acting *outside* a real envelope is a governance finding while a principal with
no envelope is an incomplete rollout, and only the second blocks graduation.

Permits are not recorded. A principal that always acted inside its envelope
produces no rows, and correctly is not an offender; recording permits would be
volume with no reader. The rate is one, and it is called recording -- naming it
sampling would imply a rate somebody could lower and a population somebody is
counting, and neither exists.

**No metric.** `contextplane/metrics.py` forbids tenant-labelled series, so
"how many tenants are still advisory" cannot be a gauge and has to be this
table.
"""

from __future__ import annotations

from alembic import op

revision = "0065_envelope_enforcement_stage"
down_revision: str | None = "0064_drop_entity_labels"
branch_labels: str | None = None
depends_on: str | None = None

_STAGE_COLUMN = """
ALTER TABLE tenants
    ADD COLUMN envelope_enforcement_stage TEXT NOT NULL DEFAULT 'advisory'
"""

_STAGE_CHECK = """
ALTER TABLE tenants
    ADD CONSTRAINT ck_tenants_envelope_enforcement_stage CHECK (
        envelope_enforcement_stage IN ('advisory', 'enforcing')
    )
"""

_ADVISORY_RECORDS = """
CREATE TABLE arc_envelope_advisory_records (
    record_id         UUID PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The IAM workload identity the decision was about. Bare TEXT, no foreign
    -- key, matching every other principal column in ARC: the whole point of a
    -- `no_envelope` record is that this principal is not bound to anything.
    principal_issuer  TEXT NOT NULL,
    principal_subject TEXT NOT NULL,

    -- Which refusal. `permitted` is absent from the CHECK on purpose: a permit
    -- writes no row, so admitting the value here would describe a state this
    -- table cannot hold.
    verdict           TEXT NOT NULL,

    -- Present when a binding was found, which is every verdict except
    -- `no_envelope`. An operator reading a refusal wants to know which envelope
    -- produced it more than they want to know it happened.
    binding_id        UUID,
    revision_id       UUID,

    -- Enough of the act to make a record actionable. Not the whole manifest:
    -- this table is read by a graduation scan counting principals, not by
    -- anything reconstructing what an agent did, and a manifest copy here would
    -- duplicate the receipt that already records that.
    intent_kind       TEXT NOT NULL,
    session_id        TEXT NOT NULL,

    decided_at        TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_arc_envelope_advisory_verdict CHECK (
        verdict IN ('no_envelope', 'envelope_suspended', 'envelope_withdrawn', 'outside_envelope')
    ),
    CONSTRAINT ck_arc_envelope_advisory_principal CHECK (
        char_length(btrim(principal_issuer)) > 0 AND char_length(btrim(principal_subject)) > 0
    ),
    -- A binding-bearing verdict must name its binding, and `no_envelope` must
    -- not: the graduation scan distinguishes them, and a row that claims both
    -- or neither is one the scan would have to guess about.
    CONSTRAINT ck_arc_envelope_advisory_binding CHECK (
        (verdict = 'no_envelope') = (binding_id IS NULL)
    )
)
"""

#: The graduation scan's own index: principals with no envelope, in a window,
#: for one tenant. `verdict` sits before `decided_at` because the scan filters
#: it to a single value and then ranges over the instant.
_SCAN_INDEX = """
CREATE INDEX ix_arc_envelope_advisory_scan
    ON arc_envelope_advisory_records (tenant_id, verdict, decided_at)
"""

#: "What has this principal been refused" -- the read an operator runs when
#: deciding whether an agent needs an envelope or a wider one.
_PRINCIPAL_INDEX = """
CREATE INDEX ix_arc_envelope_advisory_principal
    ON arc_envelope_advisory_records (tenant_id, principal_issuer, principal_subject, decided_at)
"""


def upgrade() -> None:
    op.execute(_STAGE_COLUMN)
    op.execute(_STAGE_CHECK)
    op.execute(_ADVISORY_RECORDS)
    op.execute(_SCAN_INDEX)
    op.execute(_PRINCIPAL_INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE arc_envelope_advisory_records")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT ck_tenants_envelope_enforcement_stage")
    op.execute("ALTER TABLE tenants DROP COLUMN envelope_enforcement_stage")
