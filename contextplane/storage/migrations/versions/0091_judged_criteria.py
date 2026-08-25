"""A judged criterion, the tuple it was judged under, and a human's override.

E24-T5 and E24-T7, on ADR 0026. Two model-judged criteria per simulation, and a
reviewer who can confirm or overrule each one.

**The pinned tuple is three columns, not a digest.** `(judge_model_id,
rubric_version, prompt_template_hash)` travels on every judged row. A composite
digest would be smaller and would answer only *that* something moved; a reader
comparing two results needs to know *which* of the three did, because the three
have different remedies -- a model swap is a procurement fact, a rubric edit is a
decision somebody made, and a template edit may be a typo fix.

**Confidence is stored and contributes nothing.** It is the judge's own number on
its own scale, recorded from the very first run because a mapping can only ever
be fitted from raw scores paired with judged outcomes -- a deployment that
discards them can never stop being uncalibrated. `calibration.py` makes exactly
this argument for provider confidence and it transfers unchanged.

**Reasoning and evidence are `NOT NULL`.** A verdict a reviewer can only accept
or reject is not reviewable, and a column that is nullable is a column that will
be null on the rows where it mattered. `evidence` is required on a passing
criterion too: evidence supplied only on failures teaches a reader that passes
are not checkable.

**An override is a separate row, not an update.** The judge said what it said,
and a human disagreeing with it is a second fact rather than a correction to the
first. Overwriting would destroy the pair `(what the judge said, what the person
said)`, which is the *only* thing calibration can be fitted from -- and it would
also erase the disagreement E24-T12 renders as a visible state and escalates onto
the Judgement surface.

**The override vocabulary is the adjudication one, not a new one.**
`POST /v1/memory/claims/{claim_id}:adjudicate` already has the right shape for "a
human says whether a machine's judgement was correct": a closed verdict literal
and a range-bound observed confidence. E24-T7 follows it rather than minting a
parallel vocabulary that means the same thing in different words.

**One override per reviewer per criterion, replacing rather than accumulating.**
The same rule the run verdict already has: somebody who changed their mind has
one opinion, while two reviewers disagreeing stays two rows because that
disagreement is a fact worth keeping.
"""

from __future__ import annotations

from alembic import op

revision = "0091_judged_criteria"
down_revision: str | None = "0090_evaluation_simulations"
branch_labels: str | None = None
depends_on: str | None = None

#: The two criteria a program cannot compute. The other three are computed by the
#: deterministic scorer with no model in the loop, which is what keeps a failure
#: of those attributable to what was served rather than to the judge.
_CRITERIA = "'groundedness', 'answer_relevance'"

#: What a judge may conclude. Two values, no partial credit: a criterion passes or
#: it does not, and a scale would be a number wearing words.
_JUDGE_VERDICTS = "'pass', 'fail'"

#: What a reviewer may say about a judged criterion. `unsure` is not a third
#: verdict on the answer -- it is information about the *reviewer*, and
#: calibration excludes it for the reason `calibration.py` excludes an
#: undecidable adjudication: counting it either way would bias the fit.
_HUMAN_VERDICTS = "'confirmed', 'overruled', 'unsure'"

_JUDGEMENTS = """
CREATE TABLE evaluation_judgements (
    judgement_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    simulation_id UUID NOT NULL REFERENCES evaluation_simulations(simulation_id) ON DELETE CASCADE,
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    criterion     TEXT NOT NULL,
    verdict       TEXT NOT NULL,

    -- Required, both. See the module docstring: a verdict with no trace is one a
    -- reviewer can only accept or reject, and a nullable column is one that will
    -- be null on the rows where it mattered.
    reasoning     TEXT NOT NULL,
    evidence      JSONB NOT NULL,

    -- The judge's own number on its own scale. Recorded from the first run and
    -- contributing nothing until a fit exists for the tuple below.
    confidence    NUMERIC(4, 3) NOT NULL,

    -- The pinned tuple, three columns. A run keeps the version it ran under;
    -- old rows are never re-judged when a rubric is edited.
    judge_model_id       TEXT NOT NULL,
    judge_provider_id    TEXT NOT NULL,
    rubric_version       TEXT NOT NULL,
    prompt_template_hash TEXT NOT NULL,

    -- Which panel member this was. Zero is the single judge an interactive
    -- simulation gets; a panel of three occupies 0, 1 and 2.
    --
    -- `NOT NULL` with a zero default rather than nullable-means-single, and the
    -- reason is the unique constraint below: Postgres treats NULLs as distinct,
    -- so a nullable column here would let two "the single judge" rows coexist
    -- for one criterion and silently turn a re-judge into a second opinion.
    -- It also makes "was this a panel" a count rather than a question about
    -- nullability, which is the honest shape.
    panel_position INTEGER NOT NULL DEFAULT 0,

    created_at    TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_judgement_criterion CHECK (criterion IN (__CRITERIA__)),
    CONSTRAINT ck_judgement_verdict CHECK (verdict IN (__JUDGE_VERDICTS__)),
    CONSTRAINT ck_judgement_confidence CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT ck_judgement_reasoning CHECK (char_length(reasoning) BETWEEN 1 AND 20000),
    CONSTRAINT ck_judgement_evidence_is_array CHECK (jsonb_typeof(evidence) = 'array'),
    CONSTRAINT ck_judgement_panel_position CHECK (panel_position >= 0),
    -- One judgement per criterion per panel position. A second judging pass at
    -- the same position is a re-run and replaces it; two panel members are two
    -- rows, which is what makes a 2-1 split recordable as one.
    CONSTRAINT uq_judgement_criterion UNIQUE (simulation_id, criterion, panel_position)
)
""".replace("__CRITERIA__", _CRITERIA).replace("__JUDGE_VERDICTS__", _JUDGE_VERDICTS)

_OVERRIDES = """
CREATE TABLE evaluation_judgement_reviews (
    review_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    judgement_id UUID NOT NULL REFERENCES evaluation_judgements(judgement_id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),

    -- What the person said about the machine's judgement. A separate row rather
    -- than an update, because the pair (what the judge said, what the person
    -- said) is the only thing calibration can be fitted from.
    verdict      TEXT NOT NULL,
    -- Why. Required for anything but a confirmation, on the same rule the run
    -- verdict already carries: a disagreement with no reason is one the next
    -- reader has to reach again from scratch.
    note         TEXT,

    -- The reviewer's own confidence, range-bound. Follows the claim-adjudication
    -- contract rather than minting a parallel one.
    observed_confidence NUMERIC(4, 3),

    reviewed_by  UUID NOT NULL REFERENCES actors(actor_id),
    reviewed_at  TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_review_verdict CHECK (verdict IN (__HUMAN_VERDICTS__)),
    CONSTRAINT ck_review_is_explained CHECK (
        verdict = 'confirmed' OR char_length(coalesce(note, '')) BETWEEN 1 AND 4000
    ),
    CONSTRAINT ck_review_confidence CHECK (
        observed_confidence IS NULL OR (observed_confidence >= 0 AND observed_confidence <= 1)
    ),
    -- One per person per judged criterion. A second is a correction and replaces
    -- the first; two reviewers disagreeing stays two rows.
    CONSTRAINT uq_review_by_reviewer UNIQUE (judgement_id, reviewed_by)
)
""".replace("__HUMAN_VERDICTS__", _HUMAN_VERDICTS)

#: The score pane's read: every criterion of one simulation.
_BY_SIMULATION = """
CREATE INDEX ix_judgements_by_simulation ON evaluation_judgements (simulation_id, criterion)
"""

#: Calibration's read: every judgement under one pinned tuple. The three columns
#: in the order the fit separates by, so a population is a range scan rather than
#: a filter over everything ever judged.
_BY_TUPLE = """
CREATE INDEX ix_judgements_by_pinned_tuple
    ON evaluation_judgements (judge_model_id, rubric_version, prompt_template_hash, created_at DESC)
"""

_REVIEWS_BY_JUDGEMENT = """
CREATE INDEX ix_judgement_reviews_by_judgement ON evaluation_judgement_reviews (judgement_id)
"""


def upgrade() -> None:
    op.execute(_JUDGEMENTS)
    op.execute(_OVERRIDES)
    op.execute(_BY_SIMULATION)
    op.execute(_BY_TUPLE)
    op.execute(_REVIEWS_BY_JUDGEMENT)


def downgrade() -> None:
    op.execute("DROP TABLE evaluation_judgement_reviews")
    op.execute("DROP TABLE evaluation_judgements")
