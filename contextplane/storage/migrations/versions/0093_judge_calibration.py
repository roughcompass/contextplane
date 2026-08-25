"""Bins per pinned tuple, fitted from what people said about the judge.

E24-T6, on ADR 0026 part 3. The same shape `memory_calibration_mapping` already
has, with one substitution: the separation key is the pinned tuple rather than
`(provider_id, model_id, strategy_id)`.

**A separate table rather than a widened one, and the reason is what a row
means.** A mapping in `memory_calibration_mapping` says *how often claims this
extraction model scored at 0.9 turned out correct*. A row here says *how often
verdicts this judge reported at 0.9 a reviewer confirmed*. Two different
populations, two different ground truths, and a shared table would make
`SELECT ... WHERE status = 'active'` return a mixture nobody could interpret.

**A fit that misses its bound is stored and never selected.** `status` carries
which, exactly as it does next door: a mapping worse than the bound is worse than
no mapping, because it carries a version string that reads as calibrated — and
deleting it would leave *"why are we still uncalibrated"* with no answer.

**No tenant column.** The thing being calibrated is a model's self-report, which
is a property of the model rather than of a tenant, and pooling is what makes a
fit reachable at all: 200 decided reviews is a lot for one tenant and ordinary
across a deployment. `memory_calibration_mapping` reads deployment-wide for the
same reason.

**Bins, not a curve.** A curve assumes a shape relating self-reports to
correctness and nothing here has measured that shape. A bin's value is a sentence
anybody can check — *of the judged verdicts whose raw confidence landed here,
this fraction were confirmed* — and that sentence is the audit record.
"""

from __future__ import annotations

from alembic import op

revision = "0093_judge_calibration"
down_revision: str | None = "0092_prompt_expectations"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = """
CREATE TABLE evaluation_judge_calibration (
    -- The pinned tuple, three columns, matching the judged rows this is fitted
    -- from. A fit made under one judge model does not describe another, and a
    -- rubric edit is a new population for the same reason.
    judge_model_id       TEXT NOT NULL,
    rubric_version       TEXT NOT NULL,
    prompt_template_hash TEXT NOT NULL,

    -- Identifies this fit by everything that would invalidate it, including how
    -- much evidence stood behind it -- so a verdict's record shows that without
    -- looking anything up.
    version      TEXT PRIMARY KEY,

    -- Observed confirmation rate per bin, pulled toward the pooled rate. Thin
    -- bins are pulled rather than trusted, which is what stops four observations
    -- from setting a number.
    bins         JSONB NOT NULL,

    n_adjudicated  INTEGER NOT NULL,
    measured_error NUMERIC(4, 3) NOT NULL,

    -- `active`, `superseded` or `failed`. A failing fit is stored and never
    -- selected: discarding it would leave "why are we still uncalibrated" with
    -- no answer.
    status       TEXT NOT NULL,

    fitted_at    TIMESTAMPTZ NOT NULL,
    fitted_by    UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_judge_calibration_status CHECK (status IN ('active', 'superseded', 'failed')),
    CONSTRAINT ck_judge_calibration_error CHECK (measured_error >= 0 AND measured_error <= 1),
    CONSTRAINT ck_judge_calibration_n CHECK (n_adjudicated >= 0),
    CONSTRAINT ck_judge_calibration_bins_is_array CHECK (jsonb_typeof(bins) = 'array')
)
"""

#: At most one active fit per tuple. A partial unique index rather than a plain
#: constraint, because superseded and failed rows are kept deliberately and a
#: full uniqueness rule would forbid the history that answers "what has this
#: judge been measured at".
_ONE_ACTIVE = """
CREATE UNIQUE INDEX uq_judge_calibration_active
    ON evaluation_judge_calibration (judge_model_id, rubric_version, prompt_template_hash)
    WHERE status = 'active'
"""

#: The score pane's read: which tuples are calibrated at all.
_BY_STATUS = """
CREATE INDEX ix_judge_calibration_by_status ON evaluation_judge_calibration (status, fitted_at DESC)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_ONE_ACTIVE)
    op.execute(_BY_STATUS)


def downgrade() -> None:
    op.execute("DROP TABLE evaluation_judge_calibration")
