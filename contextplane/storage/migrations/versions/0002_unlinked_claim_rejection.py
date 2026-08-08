"""Legalize terminal rejection of a claim whose subject never resolved.

Revision ID: 0002_unlinked_claim_rejection
Revises: 0001_baseline_schema
Create Date: 2026-08-04

An unlinked claim -- no resolved subject, never scored -- had no way to close.
`ClaimService.discard` refused every one of them: the schema itself made a
`rejected` row with no subject impossible, because two CHECK constraints tie
`status` to `subject_entity_id` and to `confidence` as a strict biconditional
that only ever anticipated `staged` and `unlinked` for a subjectless claim,
never `rejected`. A reference that will never resolve -- a typo, a
decommissioned system, a name nobody will ever create -- had no exit from the
queue: discarding it was refused by the database, and linking it to some
other entity just to make it closeable would misattribute it to something it
was never about.

This relaxes `ck_memory_claims_unlinked` and `ck_memory_claims_confidence_scored`
to additionally accept one new terminal shape: `status = 'rejected' AND
subject_entity_id IS NULL AND confidence IS NULL`. `ck_memory_claims_unattributed`
is included for the same shape though it turns out not to need it -- that
constraint compares `source_authority` against `subject_entity_id` alone and
never mentions `status`, so a claim that keeps its `unattributed` authority
and its NULL subject through the transition already satisfies it. Widening it
too costs nothing (the added clause is already implied) and keeps the three
constraints that jointly used to block this transition documented in one
place, in case a future change to any of them re-derives the same coupling.

Nothing else about the three constraints changes: a `rejected` claim that
does have a subject and a score -- the ordinary case, discarded after being
staged -- is governed exactly as it was before this migration.
"""

from __future__ import annotations

from alembic import op

revision = "0002_unlinked_claim_rejection"
down_revision: str | None = "0001_baseline_schema"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# The one new shape a subjectless claim may terminally settle into: rejected,
# still without a subject, still unscored -- exactly as it was staged, minus
# the queue entry it no longer needs. Named once and interpolated into all
# three ALTERs so the exception can't drift between the constraints that all
# have to agree on it.
_UNLINKED_REJECTED = "(status = 'rejected' AND subject_entity_id IS NULL AND confidence IS NULL)"


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_unlinked")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_unlinked CHECK ("
        "(subject_entity_id IS NULL) = (status = 'unlinked') "
        f"OR {_UNLINKED_REJECTED}"
        ")"
    )

    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_confidence_scored")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_confidence_scored CHECK ("
        "(confidence IS NULL) = (status = 'unlinked') "
        f"OR {_UNLINKED_REJECTED}"
        ")"
    )

    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_unattributed")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_unattributed CHECK ("
        "(source_authority = 'unattributed') = (subject_entity_id IS NULL) "
        f"OR {_UNLINKED_REJECTED}"
        ")"
    )


def downgrade() -> None:
    # Reverses cleanly only if no `rejected AND subject_entity_id IS NULL` row
    # exists yet -- the same caveat any constraint-tightening downgrade carries.
    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_unattributed")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_unattributed CHECK ("
        "(source_authority = 'unattributed') = (subject_entity_id IS NULL)"
        ")"
    )

    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_confidence_scored")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_confidence_scored CHECK ("
        "(confidence IS NULL) = (status = 'unlinked')"
        ")"
    )

    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_unlinked")
    op.execute(
        "ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_unlinked CHECK ("
        "(subject_entity_id IS NULL) = (status = 'unlinked')"
        ")"
    )
