"""A prompt set, a run over it, and a verdict that outlives the page.

E22-T15. Context Lab resolves one prompt and forgets it. What it cannot do, and
what these five tables add: a *set* of prompts, resolved repeatedly, with the
results comparable across runs and a human judgement that survives the tab
being closed.

**The boundary Context Lab already gets right is kept, verbatim.** The resolver
retrieves context only -- it does not call a language model, generate an answer,
or invent an evaluation score. Nothing here scores anything. A run resolves and
records; a verdict is a person saying what they thought.

**A prompt is a request, not a query string.** `evaluation_prompts.request`
holds the whole `ContextResolveRequest` as JSON rather than nine columns, and
the reason is the tenth: that contract gained `instruction_digest` in the wave
before this one. A column per field would be a second definition of a shape the
committed contract already owns, and the copy is stale from the first field
nobody mirrored. Writes validate through the contract model, so the JSON is not
a place unvalidated shapes accumulate.

**A run pins what produced it, and pins both halves.** The entry left this open;
it is decided here, because a comparison across a configuration change is
meaningless if neither side records which configuration produced it. The request
half is already in each resolution's receipt. The *deployment* half is not
anywhere, so `evaluation_runs.resolver_fingerprint` carries it: a digest over
the facts a resolution depends on that no request can express -- the recall
branch, whether semantic is approved and available, the arm bounds and timeout.
Two runs with different fingerprints are not comparable, and a surface that
compared them anyway would be reporting a policy change as a quality change.

**An errored prompt stays in the run.** `evaluation_run_items.receipt_id` is
nullable and `failure` says why, because a resolution that raised has no receipt
and dropping it is how a number improves without anything improving. The
evaluation harness in `context/evaluation/harness.py` already states this rule
for the research campaign -- *a system error is a failure, never an exclusion* --
and it is the same rule for the same reason.

**A verdict is on one prompt's resolution, not on a run.** A run of twenty
prompts where three were wrong is not "bad"; it is right seventeen times and
wrong three, and the three are the ones somebody has to look at. A run-level
summary is then an aggregate over items, computed rather than asserted, which is
how the envelope already treats its own state. One verdict per person per item:
a second one from the same reviewer is a correction, and it replaces rather than
accumulating into a tally nobody can read back.
"""

from __future__ import annotations

from alembic import op

revision = "0086_evaluation_runs"
down_revision: str | None = "0085_declared_instruction_sets"
branch_labels: str | None = None
depends_on: str | None = None

#: What a reviewer may say about one resolution. Closed, and deliberately three
#: rather than a score: the boundary this task inherits is that nothing here
#: invents a number, and a five-point scale is a number wearing words.
#:
#: `unusable` is separate from `wrong` because they have different remedies. A
#: wrong answer means retrieval selected badly; an unusable one means the reader
#: could not tell whether it was right, which is a defect in what was served
#: rather than in what was selected.
_VERDICTS = "'right', 'wrong', 'unusable'"

_PROMPT_SETS = """
CREATE TABLE evaluation_prompt_sets (
    set_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),

    name         TEXT NOT NULL,
    description  TEXT,

    created_by   UUID NOT NULL REFERENCES actors(actor_id),
    created_at   TIMESTAMPTZ NOT NULL,

    -- Retired rather than deleted. A run cites its set, and deleting the set
    -- would leave every past run naming something nobody can look at -- which
    -- is the same as deleting the runs.
    retired_at   TIMESTAMPTZ,
    retired_by   UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_prompt_set_name CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_prompt_set_retirement_is_attributed CHECK (
        (retired_at IS NULL) = (retired_by IS NULL)
    ),
    CONSTRAINT uq_prompt_set_name UNIQUE (tenant_id, name)
)
"""

_PROMPTS = """
CREATE TABLE evaluation_prompts (
    prompt_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id      UUID NOT NULL REFERENCES evaluation_prompt_sets(set_id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Where in the set. Runs report in this order, so two runs of one set are
    -- read side by side rather than reconciled by hand.
    position    INTEGER NOT NULL,

    -- The whole request, validated through the contract model on write. See the
    -- module docstring for why this is not nine columns.
    request     JSONB NOT NULL,

    -- What the author was checking. Free text and optional: a prompt set is
    -- read by somebody who did not write it, and "why is this one here" is the
    -- question they arrive with.
    intent_note TEXT,

    added_at    TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_prompt_position CHECK (position >= 0),
    CONSTRAINT uq_prompt_position UNIQUE (set_id, position)
)
"""

_RUNS = """
CREATE TABLE evaluation_runs (
    run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id       UUID NOT NULL REFERENCES evaluation_prompt_sets(set_id),
    tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The deployment half of what produced this run. See the module docstring:
    -- two runs with different fingerprints are not comparable, and the surface
    -- has to be able to say so rather than diff them silently.
    resolver_fingerprint TEXT NOT NULL,

    -- How many prompts the set held when this ran. Stored rather than counted
    -- from the items, because a set can gain a prompt afterwards and the run
    -- would then look as though it had skipped one.
    prompt_count INTEGER NOT NULL,

    started_by   UUID NOT NULL REFERENCES actors(actor_id),
    started_at   TIMESTAMPTZ NOT NULL,
    -- Null while the run is in flight. A run that never finished is visible as
    -- one rather than as a run with missing items.
    finished_at  TIMESTAMPTZ,

    CONSTRAINT ck_run_prompt_count CHECK (prompt_count >= 0),
    CONSTRAINT ck_run_fingerprint CHECK (resolver_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_run_finished_after_started CHECK (finished_at IS NULL OR finished_at >= started_at)
)
"""

_RUN_ITEMS = """
CREATE TABLE evaluation_run_items (
    item_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),

    -- No cascade from the prompt: a prompt removed from a set does not unmake
    -- the runs that resolved it, and a run missing the row that says what it
    -- asked is a run nobody can read.
    prompt_id   UUID NOT NULL REFERENCES evaluation_prompts(prompt_id),

    -- Null when the resolution raised. Nullable rather than the item being
    -- absent, because an errored prompt stays in the run -- see the module
    -- docstring, and `context/evaluation/harness.py` for the same rule stated
    -- for the research campaign.
    receipt_id  UUID,
    -- `complete`, `degraded`, `blocked`, or null alongside a failure.
    envelope_state TEXT,
    failure     TEXT,

    duration_ms INTEGER NOT NULL,

    CONSTRAINT ck_run_item_outcome CHECK (
        (receipt_id IS NOT NULL AND envelope_state IS NOT NULL AND failure IS NULL)
        OR (receipt_id IS NULL AND envelope_state IS NULL AND failure IS NOT NULL)
    ),
    CONSTRAINT ck_run_item_envelope_state CHECK (
        envelope_state IS NULL OR envelope_state IN ('complete', 'degraded', 'blocked')
    ),
    CONSTRAINT ck_run_item_duration CHECK (duration_ms >= 0),
    CONSTRAINT uq_run_item_prompt UNIQUE (run_id, prompt_id)
)
"""

_VERDICT_TABLE = f"""
CREATE TABLE evaluation_verdicts (
    verdict_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id     UUID NOT NULL REFERENCES evaluation_run_items(item_id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),

    verdict     TEXT NOT NULL,
    -- Why. Required for anything other than `right`, because "wrong" with no
    -- reason is a signal nobody can act on and the next reader has to re-derive
    -- the judgement from scratch.
    note        TEXT,

    recorded_by UUID NOT NULL REFERENCES actors(actor_id),
    recorded_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_verdict_value CHECK (verdict IN ({_VERDICTS})),
    CONSTRAINT ck_verdict_is_explained CHECK (
        verdict = 'right' OR char_length(coalesce(note, '')) BETWEEN 1 AND 4000
    ),
    -- One per person per item. A second is a correction and replaces the first,
    -- rather than accumulating into a tally nobody can read back.
    CONSTRAINT uq_verdict_by_reviewer UNIQUE (item_id, recorded_by)
)
"""

#: The list read: this tenant's sets, live ones first.
_SETS_BY_TENANT = """
CREATE INDEX ix_prompt_sets_by_tenant
    ON evaluation_prompt_sets (tenant_id, created_at DESC)
"""

#: The comparison read: runs of one set, newest first, so "what changed since
#: last time" is the first two rows rather than a scan.
_RUNS_BY_SET = """
CREATE INDEX ix_runs_by_set ON evaluation_runs (tenant_id, set_id, started_at DESC)
"""

#: One run's items in the set's own order.
_ITEMS_BY_RUN = """
CREATE INDEX ix_run_items_by_run ON evaluation_run_items (run_id)
"""

_VERDICTS_BY_ITEM = """
CREATE INDEX ix_verdicts_by_item ON evaluation_verdicts (item_id)
"""


def upgrade() -> None:
    op.execute(_PROMPT_SETS)
    op.execute(_PROMPTS)
    op.execute(_RUNS)
    op.execute(_RUN_ITEMS)
    op.execute(_VERDICT_TABLE)
    op.execute(_SETS_BY_TENANT)
    op.execute(_RUNS_BY_SET)
    op.execute(_ITEMS_BY_RUN)
    op.execute(_VERDICTS_BY_ITEM)


def downgrade() -> None:
    op.execute("DROP TABLE evaluation_verdicts")
    op.execute("DROP TABLE evaluation_run_items")
    op.execute("DROP TABLE evaluation_runs")
    op.execute("DROP TABLE evaluation_prompts")
    op.execute("DROP TABLE evaluation_prompt_sets")
