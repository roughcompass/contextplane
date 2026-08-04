"""What a provider's self-reported numbers turn out to be worth.

Providers report confidence on internal scales that are not comparable with each
other and are not probabilities. A mapping is what makes one usable, and it is
fitted from claims a person has judged correct or incorrect -- never assumed.

**There is no mapping yet, and the honest form of that is no mapping at all.** Not
an identity mapping. Identity asserts that a model reporting 0.9 is right nine times
in ten, which nobody has checked, and storing that assertion under a version string
is exactly how an unexamined number acquires an authoritative look.

**Deployment-wide, with no tenant column.** The thing being measured is a shared
model, and a tenant cannot recalibrate somebody else's. Fits therefore pool judged
outcomes across tenants, which does mean one tenant's judgements move another's
scores -- stated here rather than left to be discovered later.

**A table rather than a config file or a constant.** This is data fitted from
outcomes: it changes on a review cadence without a release, it is versioned, and a
claim has to be able to name the exact fit that scored it.

**A fit that misses the accuracy target is stored, not discarded, and never
selected.** A mapping worse than the bound is worse than no mapping, because it
carries a version string that reads as calibrated. Keeping the failed fit is what
makes "why are we still uncalibrated" answerable.

**The key includes the model.** Swapping provider or model matches no existing row,
so scoring reverts to uncalibrated with nobody having to remember to act. That is
what makes recalibration a mechanism rather than a procedure.
"""

from __future__ import annotations

from alembic import op

revision = "0037_calibration"
down_revision = "0036_claim_confirmation"
branch_labels = None
depends_on = None


_MAPPING = """
CREATE TABLE lmm_calibration_mapping (
    mapping_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    provider_id      TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    strategy_id      TEXT NOT NULL,
    version          TEXT NOT NULL,

    -- Observed correctness per input bin, smoothed toward the pooled rate. Bins
    -- rather than a fitted curve: a curve assumes a shape relating self-reports to
    -- correctness, and nothing has ever measured that shape. A bin's value is a
    -- sentence anybody can check -- of the judged claims whose raw score landed
    -- here, this fraction were right -- and that sentence is the audit record.
    bins             JSONB NOT NULL,
    n_adjudicated    INTEGER NOT NULL,
    measured_error   NUMERIC(4, 3) NOT NULL,

    -- 'active' is the one scoring reads. A fit missing the accuracy target is
    -- stored as 'failed' rather than dropped: it is never selected, and keeping it
    -- is what makes "why is this still uncalibrated" answerable.
    status           TEXT NOT NULL,

    fitted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    fitted_by        UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_lmm_calibration_version UNIQUE (version),
    CONSTRAINT ck_lmm_calibration_status CHECK (
        status IN ('active', 'superseded', 'failed')
    ),
    -- The token meaning "nothing has been fitted" must never be claimable by a
    -- row, or a claim carrying it would resolve to a mapping.
    CONSTRAINT ck_lmm_calibration_not_sentinel CHECK (version <> 'uncalibrated'),
    CONSTRAINT ck_lmm_calibration_n CHECK (n_adjudicated >= 200),
    -- A fit that misses the target cannot be the active one.
    CONSTRAINT ck_lmm_calibration_error CHECK (
        status <> 'active' OR measured_error <= 0.150
    )
)
"""

_MAPPING_INDEXES = [
    # One active fit per provider, model, and strategy. Keyed on the model so that
    # changing it matches nothing and scoring reverts to uncalibrated on its own.
    "CREATE UNIQUE INDEX uq_lmm_calibration_active ON lmm_calibration_mapping "
    "(provider_id, model_id, strategy_id) WHERE status = 'active'",
    "CREATE INDEX ix_lmm_calibration_fitted ON lmm_calibration_mapping (fitted_at DESC)",
]


def upgrade() -> None:
    op.execute(_MAPPING)
    for statement in _MAPPING_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_calibration_mapping")
