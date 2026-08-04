"""Human confirmation, and the judged outcomes a calibration can be fitted from.

**Confirmation supersedes rather than mutates.** A person putting their name to a
claim is a new, stronger observation, so it produces a new row with its own
authority, its own score, and its own decay origin. The confirmed claim keeps its
original score and its original provenance, which is what lets a reader see both
what a machine estimated and what a human then said -- mutating in place would erase
the first half of that.

**Its decay origin is the confirmation, not the original assertion.** Resuming from
where decay would have been would make a confirmation worthless the moment its
window closed: a long-lived claim confirmed today would snap back toward the floor
on the day the hold expired, and somebody who spent time reviewing it would rightly
call that a bug. A human confirmation is measured from when the human made it.

**Judged outcomes are the point of this migration.** A provider's self-reported
number cannot be turned into a probability without claims a person has marked
correct or incorrect, so this table is the only thing that makes leaving the
uncalibrated state possible at all -- which is why it ships before anything that
consumes it.

**Three verdicts, not two.** A reviewer who cannot tell has said something, and
folding that into "incorrect" would bias every fit downward.

**The confidence the reviewer saw is recorded, not recomputed.** A score works out
differently at a different instant, so calibrating against a number nobody ever
looked at would measure the wrong thing.
"""

from __future__ import annotations

from alembic import op

revision = "0036_claim_confirmation"
down_revision = "0035_claim_confidence"
branch_labels = None
depends_on = None


_CLAIM_COLUMNS = """
ALTER TABLE lmm_claims
    -- The claim this one confirms. Null for an ordinary claim.
    ADD COLUMN confirms_claim_id UUID REFERENCES lmm_claims(claim_id),
    ADD COLUMN confirmed_by      UUID REFERENCES actors(actor_id),
    ADD COLUMN confirmed_at      TIMESTAMPTZ,

    -- Set on the claim that was confirmed, so a reader of the original sees that
    -- a stronger assertion exists without walking forward. Superseded claims are
    -- excluded from the disagreement sweep and from serving.
    ADD COLUMN superseded_by     UUID REFERENCES lmm_claims(claim_id),

    ADD CONSTRAINT ck_lmm_claims_confirmation CHECK (
        (confirms_claim_id IS NULL) = (confirmed_by IS NULL)
        AND (confirms_claim_id IS NULL) = (confirmed_at IS NULL)
    ),
    -- A claim cannot confirm or supersede itself.
    ADD CONSTRAINT ck_lmm_claims_confirms_other CHECK (
        confirms_claim_id IS NULL OR confirms_claim_id <> claim_id
    ),
    ADD CONSTRAINT ck_lmm_claims_supersedes_other CHECK (
        superseded_by IS NULL OR superseded_by <> claim_id
    )
"""

_CLAIM_INDEXES = [
    "CREATE INDEX ix_lmm_claims_confirms ON lmm_claims (confirms_claim_id) " "WHERE confirms_claim_id IS NOT NULL",
    # The serving and sweep paths both want live claims only, and a superseded
    # claim is neither served nor compared.
    "CREATE INDEX ix_lmm_claims_live ON lmm_claims (subject_entity_id, predicate) "
    "WHERE status = 'staged' AND superseded_by IS NULL",
]

# One judgement per reviewer per claim. Two reviewers disagreeing about the same
# claim is real signal and both rows are kept; one reviewer judging twice is a
# correction, and the unique constraint makes that an update rather than a second
# vote.
_ADJUDICATION = """
CREATE TABLE lmm_claim_adjudication (
    adjudication_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_id          UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,
    adjudicated_by    UUID NOT NULL REFERENCES actors(actor_id),

    verdict           TEXT NOT NULL,

    -- What the reviewer was actually looking at, aged to that instant. Recorded
    -- rather than recomputed later: a score works out differently at a different
    -- time, and calibrating against a number nobody saw measures the wrong thing.
    observed_confidence NUMERIC(4, 3) NOT NULL,
    observed_bucket     TEXT NOT NULL,
    -- Which mapping produced it, so a later fit can exclude its own output.
    calibration_version TEXT NOT NULL,
    -- The provider's raw self-report, copied so a fit needs no join and survives
    -- the claim being superseded.
    provider_confidence NUMERIC(5, 4),
    -- Which authority tier the claim held, so a fit can be checked per tier as
    -- well as in aggregate.
    source_authority    TEXT NOT NULL,

    note              TEXT,
    adjudicated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_lmm_adjudication UNIQUE (claim_id, adjudicated_by),
    -- Three outcomes. A reviewer who cannot tell has said something, and folding
    -- it into "incorrect" would bias every fit downward.
    CONSTRAINT ck_lmm_adjudication_verdict CHECK (
        verdict IN ('correct', 'incorrect', 'undecidable')
    ),
    CONSTRAINT ck_lmm_adjudication_confidence CHECK (
        observed_confidence >= 0 AND observed_confidence <= 1
    )
)
"""

_ADJUDICATION_INDEXES = [
    "CREATE INDEX ix_lmm_adjudication_claim ON lmm_claim_adjudication (claim_id)",
    # The fitting query: every judged outcome under one mapping.
    "CREATE INDEX ix_lmm_adjudication_fit ON lmm_claim_adjudication " "(calibration_version, adjudicated_at)",
    "CREATE INDEX ix_lmm_adjudication_tenant ON lmm_claim_adjudication " "(tenant_id, adjudicated_at)",
]


def upgrade() -> None:
    op.execute(_CLAIM_COLUMNS)
    for statement in _CLAIM_INDEXES:
        op.execute(statement)
    op.execute(_ADJUDICATION)
    for statement in _ADJUDICATION_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_claim_adjudication")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_live")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_confirms")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_supersedes_other, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_confirms_other, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_confirmation, "
        "  DROP COLUMN IF EXISTS superseded_by, "
        "  DROP COLUMN IF EXISTS confirmed_at, "
        "  DROP COLUMN IF EXISTS confirmed_by, "
        "  DROP COLUMN IF EXISTS confirms_claim_id"
    )
