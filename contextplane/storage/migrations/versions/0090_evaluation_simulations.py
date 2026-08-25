"""A generated answer, its citations, and what the call cost.

E24-T3, on ADR 0025. The resolver still does not generate. A simulation is a
separate operation that resolves through the same resolver every caller uses and
*then* asks a model to answer from what came back.

**Two records, not one fused row, and this is the ADR's dissent answered in
DDL.** The resolution keeps its own receipt, written by the resolver; this table
references that receipt rather than embedding it. A reader can ask "what was
served" and "what was said about it" independently, and a change to one does not
rewrite the other. One row holding both would recreate the fusion ADR 0011
refused for envelope blocks, and would make a receipt's meaning depend on whether
a simulation happened to run against it.

**Citations are rows, not a JSON blob, because they are queried both ways.** The
improvement surface asks "which served item did no assertion cite" and "which
assertion cited nothing", and both are joins. A JSON array on the simulation
would answer the second and make the first a scan with a parse in it.

**An assertion citing nothing is a row with no citations, never an absent row.**
That state is the whole point: an assertion resting on nothing served is either a
fact the graph is missing or a groundedness failure, and E24-T13 offers both
readings rather than choosing. Dropping the assertion would delete the finding.

**Usage is reported or explicitly unknown, and the check enforces it.** The
provider contract already forbids guessing -- *usage is reported, not estimated,
and never substitutes zero for a field the API omitted* -- and `usage_source`
carries which it was. A partially-filled record is the dangerous shape: it looks
usable, and summing it treats the gap as zero, so a spend total is wrong without
anything looking wrong.

**A simulated principal is named, and it is a declared agent.** ADR 0019
established that an undeclared principal is `unknown` and never `human`, because
nothing can infer the kind from the transport. `simulated_actor_id` records which
principal was simulated; the service refuses an undeclared one, and the column
exists so a reader of a past simulation can see who it claimed to be.

**The material the model was shown is recorded, because nothing else holds it.**
`context_receipt_items` records *which* items a resolution served -- their
identity and their trust metadata -- and deliberately not their content. So a
judge asked whether an answer is grounded in what was served has nothing to
check it against, and re-resolving would grade a different envelope. A simulation
nobody can reproduce is the unreceiptable failure ADR 0025 rejected when it
declined a browser-side call, arriving from the other direction.

**The pinned tuple is on the *judgement*, not here.** A simulation is a candidate
answer; what grades it carries `(judge_model_id, rubric_version,
prompt_template_hash)` per ADR 0026, and that table lands with the judge. Putting
a judge column here would leave it null on every unjudged simulation and make
"has this been judged" a question about nullability.
"""

from __future__ import annotations

from alembic import op

revision = "0090_evaluation_simulations"
down_revision: str | None = "0089_reporting_deadlines"
branch_labels: str | None = None
depends_on: str | None = None

#: How a usage count was arrived at. Mirrors `extraction/provider.py`'s
#: `UsageSource` exactly: an estimate is a number somebody computed from token
#: heuristics, and averaging it with provider-reported figures produces a total
#: that is neither.
_USAGE_SOURCES = "'provider_reported', 'estimated', 'unknown'"

#: The three states ADR 0020's third assumption requires be kept apart. A
#: simulation run against an agent whose instructions nobody declared is not the
#: same experiment as one against an agent that declared it has none.
#:
#: **Named for the states rather than for "dispositions", and the collision is
#: worth recording.** `tests/conformance/test_learning_curation.py` scans every
#: migration for a constant spelled the curation way and holds the last one it
#: finds equal to the *curation* disposition vocabulary. A second, unrelated
#: vocabulary under that spelling does not shadow the check -- it becomes what
#: the check believes it is checking, and the curation set would have gone
#: ungoverned from this migration onward. The gate caught it; the rename is the
#: fix, and it is why this constant is deliberately not named after a word
#: another vocabulary already owns.
_INSTRUCTION_STATES = "'not_declared', 'declared_unknown', 'declared_known'"

_SIMULATIONS = """
CREATE TABLE evaluation_simulations (
    simulation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The resolution half. A reference, never a copy: the receipt is the
    -- resolver's record and this row is the generation's, and the whole
    -- diagnostic value of the split is that they stay separately addressable.
    receipt_id    UUID NOT NULL,

    -- Which principal this answered as. Refused by the service unless declared
    -- an agent per ADR 0019; recorded here so a reader of a past simulation can
    -- see who it claimed to be without re-deriving it from the receipt.
    simulated_actor_id UUID NOT NULL REFERENCES actors(actor_id),

    -- What was asked, kept verbatim. Not a foreign key to a stored prompt: a
    -- simulation is reachable from Context Lab with a prompt nobody saved, and
    -- a nullable prompt reference would make "was this from a set" a question
    -- about nullability rather than a fact.
    prompt        TEXT NOT NULL,

    -- Optionally, the run item this simulation belongs to. Null for an
    -- interactive one. `ON DELETE CASCADE` because a simulation that outlived
    -- the run item it was part of would be an orphan nobody could interpret.
    run_item_id   UUID REFERENCES evaluation_run_items(item_id) ON DELETE CASCADE,

    -- What the model said, and under which identity.
    answer        TEXT NOT NULL,
    provider_id   TEXT NOT NULL,
    model_id      TEXT NOT NULL,

    -- Which of the three instruction states this ran under. See the docstring:
    -- collapsing them would score two different experiments under one number.
    instruction_disposition TEXT NOT NULL,

    -- Exactly reported, or explicitly unknown. Never estimated into a number
    -- that reads as measured.
    prompt_tokens        INTEGER,
    completion_tokens    INTEGER,
    cached_prompt_tokens INTEGER,
    usage_source         TEXT NOT NULL,

    -- The cardinality that produced the token figure. Paired with it because
    -- `limit` is the only lever the product offers when a run comes back too
    -- large, and a token count with no item count says nothing about what to do.
    served_item_count INTEGER NOT NULL,

    duration_ms   INTEGER,
    created_by    UUID NOT NULL REFERENCES actors(actor_id),
    created_at    TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_simulation_usage_source CHECK (usage_source IN (__USAGE__)),
    CONSTRAINT ck_simulation_disposition CHECK (instruction_disposition IN (__DISPOSITIONS__)),
    -- All three counts or none, and absent exactly when the source is unknown.
    -- The biconditional is the schema's, not the service's, because a row that
    -- broke it would be summed as though the gaps were zero.
    CONSTRAINT ck_simulation_usage_complete CHECK (
        (prompt_tokens IS NULL AND completion_tokens IS NULL AND cached_prompt_tokens IS NULL
             AND usage_source = 'unknown')
        OR (prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
             AND cached_prompt_tokens IS NOT NULL AND usage_source <> 'unknown')
    ),
    CONSTRAINT ck_simulation_counts_non_negative CHECK (
        coalesce(prompt_tokens, 0) >= 0
        AND coalesce(completion_tokens, 0) >= 0
        AND coalesce(cached_prompt_tokens, 0) >= 0
        AND served_item_count >= 0
        AND coalesce(duration_ms, 0) >= 0
    ),
    CONSTRAINT ck_simulation_prompt_length CHECK (char_length(prompt) BETWEEN 1 AND 20000)
)
""".replace("__USAGE__", _USAGE_SOURCES).replace("__DISPOSITIONS__", _INSTRUCTION_STATES)

_SERVED = """
CREATE TABLE evaluation_simulation_served_items (
    simulation_id   UUID NOT NULL REFERENCES evaluation_simulations(simulation_id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The digest the envelope served, which is what a citation names.
    receipt_item_id TEXT NOT NULL,
    block           TEXT NOT NULL,
    item_key        TEXT NOT NULL,

    -- The item's payload as the model saw it, serialized once by the service so
    -- two providers are sent byte-identical material and a difference between
    -- two answers is a difference in the models.
    --
    -- Stored rather than joined because the receipt does not carry content and
    -- was never meant to: a receipt says what was served, and a judge grading
    -- groundedness needs what was *said*. Re-resolving to recover it would grade
    -- a different envelope than the one the answer came from.
    payload         JSONB NOT NULL,

    PRIMARY KEY (simulation_id, receipt_item_id)
)
"""

_ASSERTIONS = """
CREATE TABLE evaluation_simulation_assertions (
    assertion_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    simulation_id UUID NOT NULL REFERENCES evaluation_simulations(simulation_id) ON DELETE CASCADE,
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Where in the answer. The model returns them in order and the order is
    -- what a reader follows, so it is stored rather than recovered.
    position      INTEGER NOT NULL,
    text          TEXT NOT NULL,

    CONSTRAINT ck_assertion_position CHECK (position >= 0),
    CONSTRAINT uq_assertion_position UNIQUE (simulation_id, position)
)
"""

_CITATIONS = """
CREATE TABLE evaluation_simulation_citations (
    assertion_id    UUID NOT NULL REFERENCES evaluation_simulation_assertions(assertion_id)
                        ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The receipt item id the assertion rested on, as the digest the envelope
    -- served. Text rather than a foreign key: receipt items are rows on the
    -- receipt, and a citation is what a *model* said it used -- including, on a
    -- bad day, an id that was never served. Storing it as declared is what makes
    -- "cited something that was not served" an answerable question rather than
    -- an insert that fails.
    receipt_item_id TEXT NOT NULL,

    -- Whether the cited id was actually in the envelope. Computed once at write
    -- time by the service, which holds both sides; recomputing it later would
    -- mean re-reading a receipt to answer a question the write already knew.
    was_served      BOOLEAN NOT NULL,

    PRIMARY KEY (assertion_id, receipt_item_id)
)
"""

#: The read a Context Lab session makes: this tenant's simulations, newest first.
_BY_TENANT = """
CREATE INDEX ix_simulations_by_tenant ON evaluation_simulations (tenant_id, created_at DESC)
"""

#: The join from a run item to the simulation that answered it.
_BY_RUN_ITEM = """
CREATE INDEX ix_simulations_by_run_item ON evaluation_simulations (run_item_id)
    WHERE run_item_id IS NOT NULL
"""

#: The trace from a resolution to what was said about it.
_BY_RECEIPT = """
CREATE INDEX ix_simulations_by_receipt ON evaluation_simulations (tenant_id, receipt_id)
"""

_ASSERTIONS_BY_SIMULATION = """
CREATE INDEX ix_assertions_by_simulation ON evaluation_simulation_assertions (simulation_id, position)
"""

#: The improvement surface's read: which served items nobody cited.
_CITATIONS_BY_ITEM = """
CREATE INDEX ix_citations_by_item ON evaluation_simulation_citations (tenant_id, receipt_item_id)
"""


def upgrade() -> None:
    op.execute(_SIMULATIONS)
    op.execute(_SERVED)
    op.execute(_ASSERTIONS)
    op.execute(_CITATIONS)
    op.execute(_BY_TENANT)
    op.execute(_BY_RUN_ITEM)
    op.execute(_BY_RECEIPT)
    op.execute(_ASSERTIONS_BY_SIMULATION)
    op.execute(_CITATIONS_BY_ITEM)


def downgrade() -> None:
    op.execute("DROP TABLE evaluation_simulation_citations")
    op.execute("DROP TABLE evaluation_simulation_assertions")
    op.execute("DROP TABLE evaluation_simulation_served_items")
    op.execute("DROP TABLE evaluation_simulations")
