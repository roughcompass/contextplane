"""Fitted per-predicate supersession rates, stored and not selected until inspected.

ADR 0003 reverses the recorded decay model: the rate a claim's confidence falls
at is measured per predicate rather than authored per category. The argument the
shipped docstring makes against per-predicate rates -- twenty-six figures nobody
could defend one at a time -- applies to *authored* numbers and not to *measured*
ones, and measuring twenty-six is no harder than measuring six.

This is the table those measurements land in. It is shaped after
`memory_calibration_mapping`, and for the same reason: a fit is evidence, so it
has to be storable while being unusable.

**A fit is stored and never selected until it has been inspected.** The ADR names
the assumption most likely to break the whole model -- that supersession tracks
*churn*, a claim becoming untrue, rather than *correction*, a claim having been
wrong when written. Those produce identical bitemporal history and opposite
conclusions: a predicate whose extractions are often wrong would be measured as
fast-moving and decayed aggressively, which hides an extraction defect behind a
confidence curve. So `status` starts at `fitted` and only a recorded inspection
moves it to `active`, with the inspector and their finding on the row. There is
no path that sets `active` without them.

**Below the observation floor, no rate.** A predicate with a handful of
supersessions carries no fitted rate at all rather than a noisy one, and the
category figure governs. Same discipline as confidence decay's own
`MIN_CHANGE_OBSERVATIONS`: an entity nobody has watched change is not an entity
that changes slowly, and a predicate nobody has watched supersede is not a
predicate that never does.

**One live rate per predicate**, by partial unique index rather than by
convention, so two active fits for one predicate is a state the database refuses
rather than one a reader has to notice.
"""

from __future__ import annotations

from alembic import op

revision = "0059_predicate_churn"
down_revision: str | None = "0058_claim_salience"
branch_labels: str | None = None
depends_on: str | None = None

#: Global rather than per tenant. A predicate's churn is a property of the thing
#: the predicate describes -- how often an interface version actually changes --
#: and one tenant's corpus is a sample of that, not a different question. Tenant
#: scoping would also make every fit thinner by exactly the factor that decides
#: whether it clears the observation floor.
_TABLE = """
CREATE TABLE memory_predicate_churn (
    fit_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    predicate         TEXT NOT NULL,

    -- The measurement. Half-life in days, and the sample it came from, so a
    -- reader sees how much evidence stood behind the number without a join.
    half_life_days    NUMERIC(8, 2) NOT NULL,
    observed_supersessions INTEGER NOT NULL,
    observation_window_days INTEGER NOT NULL,

    -- `fitted` until somebody has looked at it and said what they found;
    -- `active` once they have; `rejected` when the inspection concluded the
    -- supersessions were corrections rather than change. A rejected fit is kept
    -- deliberately -- it is the record of why this predicate has no rate.
    status            TEXT NOT NULL DEFAULT 'fitted',

    -- Who looked, when, and what they concluded about the correction-versus-
    -- change question the ADR names. All three or none: an inspection nobody
    -- signed is not an inspection.
    inspected_by      UUID REFERENCES actors(actor_id),
    inspected_at      TIMESTAMPTZ,
    inspection_finding TEXT,

    fitted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_predicate_churn_status
        CHECK (status IN ('fitted', 'active', 'rejected')),

    -- A positive half-life over a positive sample. A zero or negative rate is
    -- not a slow predicate, it is a fitting bug, and it would divide by zero in
    -- the decay curve.
    CONSTRAINT ck_predicate_churn_measurement
        CHECK (half_life_days > 0 AND observed_supersessions >= 0 AND observation_window_days > 0),

    -- The rule, in the schema rather than only in the writer. A fit cannot be
    -- active without a recorded inspection, and an inspection is not recorded
    -- unless all three of its fields are present.
    CONSTRAINT ck_predicate_churn_inspection_complete
        CHECK (
            (inspected_by IS NULL) = (inspected_at IS NULL)
            AND (inspected_by IS NULL) = (inspection_finding IS NULL)
        ),
    CONSTRAINT ck_predicate_churn_active_needs_inspection
        CHECK (status <> 'active' OR inspected_by IS NOT NULL)
)
"""

#: One selectable rate per predicate. Partial, so superseded and rejected fits
#: accumulate freely -- they are the history of what was measured and why it was
#: or was not believed.
_ACTIVE_INDEX = """
CREATE UNIQUE INDEX uq_predicate_churn_active
    ON memory_predicate_churn (predicate)
    WHERE status = 'active'
"""

_LOOKUP_INDEX = "CREATE INDEX ix_predicate_churn_predicate ON memory_predicate_churn (predicate, fitted_at DESC)"


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_ACTIVE_INDEX)
    op.execute(_LOOKUP_INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE memory_predicate_churn")
