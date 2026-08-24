"""The seventh disposition, and the constraint that would have refused it.

ADR 0022 decides what a migrated claim's disposition commits to. The vocabulary
lives in two places that have to agree: `DISPOSITIONS` in
`service/memory/curation_cases.py`, and `ck_case_disposition` here.

**The constraint is the reason this migration exists rather than the code change
being enough.** `curation_cases.disposition` is pinned to six values by
`0042_derivation_and_curation`, so adding the seventh in Python alone would give
a write that passes every service check and is refused by the database -- a 500
on the one path an operator reaches during a migration.

`0042`'s own comment says why the pin is there: an authority that may confirm a
claim is not one that may propose a canonical write, and the column is not free
text. That argument is unchanged; this widens the set it names by exactly one.

Revision ID: 0088_migrated_canonical_disposition
Revises: 0087_instruction_delta_scope
"""

from __future__ import annotations

from alembic import op

revision = "0088_migrated_canonical_disposition"
down_revision: str | None = "0087_instruction_delta_scope"
branch_labels: str | None = None
depends_on: str | None = None

#: The six `0042` pinned, and the seventh ADR 0022 adds. Spelled out rather than
#: built from the Python constants: a migration that imported them would rewrite
#: itself the next time the vocabulary changed, and what an already-migrated
#: database *was* is exactly what a migration has to keep saying.
_SIX = "'confirm', 'reject', 'supersede', 'propose_canonical', 'propose_runbook', 'propose_arc'"
_SEVEN = f"{_SIX}, 'migrated_canonical'"


def _repin(values: str) -> None:
    # One statement per `op.execute`: asyncpg prepares statements and cannot
    # take a semicolon-joined script.
    op.execute("ALTER TABLE curation_cases DROP CONSTRAINT ck_case_disposition")
    op.execute(
        "ALTER TABLE curation_cases ADD CONSTRAINT ck_case_disposition "
        f"CHECK (disposition IS NULL OR disposition IN ({values}))"
    )


def upgrade() -> None:
    _repin(_SEVEN)


def downgrade() -> None:
    # Rows carrying the seventh value are deleted rather than left to fail the
    # narrowed constraint. A downgrade that cannot complete is worse than one
    # that says what it removed, and a `migrated_canonical` case has no meaning
    # under a schema that does not know the disposition.
    op.execute("DELETE FROM curation_cases WHERE disposition = 'migrated_canonical'")
    _repin(_SIX)
