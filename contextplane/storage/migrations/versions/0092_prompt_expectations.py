"""What a prompt is checking, in a form a program can check it against.

E24-T7. `evaluation_prompts.intent_note` already carries the prose form -- *what
this prompt is checking* -- and this is the structured form, hung in the same
place.

**Before the run, always.** `scenarios.py` states the mechanism and this adopts
it without weakening it: *a scenario whose required facts were written after
seeing what the system returned would be satisfied by whatever the system
returned*. A column on the prompt rather than on the run item is what makes that
true by construction -- an expectation written after a run has nowhere to go.

**One JSONB column rather than a table of thresholds**, matching what `request`
already does beside it and for the same reason: the shape is validated on write
through `ExpectationsV1`, and a column per threshold would be a second definition
of a set that grows with the rubric. A column added for a criterion nobody
defined is the drift a version exists to prevent.

**Nullable, and null means "asserts nothing".** A real and legal state: an
evaluator exploring has not yet decided what good looks like. Defaulting to an
object full of permissive thresholds would turn that into a row of checks that
always pass, which somebody would later read as evidence.

**A preset is recorded as a name and never read back as the source of truth.**
The stored expectations are what the run is judged against; the preset name says
what shape somebody started from. A preset edited afterwards must not change what
a past prompt asserted, which is the same reason a run keeps the rubric version
it ran under rather than the current one.
"""

from __future__ import annotations

from alembic import op

revision = "0092_prompt_expectations"
down_revision: str | None = "0091_judged_criteria"
branch_labels: str | None = None
depends_on: str | None = None

_ADD_EXPECTATIONS = """
ALTER TABLE evaluation_prompts
    ADD COLUMN expectations JSONB,
    ADD CONSTRAINT ck_prompt_expectations_is_object
        CHECK (expectations IS NULL OR jsonb_typeof(expectations) = 'object')
"""


def upgrade() -> None:
    op.execute(_ADD_EXPECTATIONS)


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_prompts DROP CONSTRAINT ck_prompt_expectations_is_object")
    op.execute("ALTER TABLE evaluation_prompts DROP COLUMN expectations")
