"""Confidence: how likely a claim is to be correct, and everything needed to re-derive it.

Confidence is the estimated probability that a claim is correct as asserted over
its effective interval, on a documented scale. It is not a model's opinion of its
own output: a self-reported score is an *input*, and only once somebody has checked
what that provider's numbers turn out to be worth.

**Confidence is not authority.** Authority says where a claim came from and decides
which claim supersedes which. Confidence says how likely it is to be right.
Independent sources agreeing raises the second and never the first.

**Stored with its inputs, not just its result.** A number nobody can re-derive is
not auditable, and "why did this claim score as it did" is a question about the
past: the neighbourhood that produced the score may have changed since, so the
inputs are recorded rather than re-read. The scorer version is stored for the same
reason -- without it, a change to the scoring code makes every historical score
unreproducible and turns a calibration set into a mixture of numbers from different
functions.

**A claim with no subject has no score, not a low one.** Such a claim is excluded
from scoring, consolidation, promotion and serving. A number there would assert a
determination nobody made, and nothing would mark it stale once curation links the
claim -- the same reasoning that gives an unlinked claim no authority tier.

**The stored value never sits below the floor that age decays toward.** That is
load-bearing rather than cosmetic: it makes ageing monotone non-increasing, so a
minimum-confidence query can prefilter on the indexed column before applying the
exact age adjustment. No claim stored below a threshold can age up through it.

**The provider's raw number is stored and, for now, unused.** Nothing has checked
what it predicts, so using it would launder an unexamined number into an
authoritative-looking signal. But a mapping can only ever be fitted from raw scores
paired with judged outcomes, so discarding them would make the uncalibrated state
permanent.
"""

from __future__ import annotations

from alembic import op

revision = "0035_claim_confidence"
down_revision = "0034_claim_contest"
branch_labels = None
depends_on = None


_CLAIM_COLUMNS = """
ALTER TABLE lmm_claims
    ADD COLUMN confidence            NUMERIC(4, 3),
    ADD COLUMN confidence_scored_at  TIMESTAMPTZ,
    ADD COLUMN confidence_inputs     JSONB,
    ADD COLUMN scorer_version        TEXT,
    ADD COLUMN calibration_version   TEXT,
    -- The provider's own number, on whatever scale it used.
    ADD COLUMN provider_confidence   NUMERIC(5, 4),
    -- Resolved when the claim is scored, so working out what a score is worth
    -- right now stays a function of this row and the clock.
    ADD COLUMN decay_half_life_days  NUMERIC(8, 2),
    -- When a human confirmation stops holding decay off. Null for a claim nobody
    -- has confirmed.
    ADD COLUMN confidence_hold_until TIMESTAMPTZ,

    -- Same shape as the null-pair checks already on this table. A claim whose
    -- subject did not resolve is excluded from scoring, so it has no score.
    ADD CONSTRAINT ck_lmm_claims_confidence_scored CHECK (
        (confidence IS NULL) = (status = 'unlinked')
    ),
    -- A score without the inputs that produced it cannot be re-derived, and a
    -- score nobody can re-derive is not auditable.
    ADD CONSTRAINT ck_lmm_claims_confidence_paired CHECK (
        (confidence IS NULL) = (confidence_scored_at IS NULL)
        AND (confidence IS NULL) = (confidence_inputs IS NULL)
        AND (confidence IS NULL) = (scorer_version IS NULL)
        AND (confidence IS NULL) = (calibration_version IS NULL)
        AND (confidence IS NULL) = (decay_half_life_days IS NULL)
    ),
    -- Never below the decay floor, so ageing can only lower it. That is what
    -- lets a minimum-confidence query prefilter on the index.
    ADD CONSTRAINT ck_lmm_claims_confidence_range CHECK (
        confidence IS NULL OR (confidence >= 0.100 AND confidence <= 0.980)
    ),
    ADD CONSTRAINT ck_lmm_claims_half_life CHECK (
        decay_half_life_days IS NULL OR decay_half_life_days > 0
    ),
    ADD CONSTRAINT ck_lmm_claims_provider_confidence CHECK (
        provider_confidence IS NULL
        OR (provider_confidence >= 0 AND provider_confidence <= 1)
    )
"""

_CLAIM_INDEXES = [
    "CREATE INDEX ix_lmm_claims_confidence ON lmm_claims "
    "(subject_entity_id, confidence DESC) WHERE status = 'staged'",
]

# Repetition through one source is not corroboration, so scoring counts
# independent sources rather than rows. Which class a piece of evidence belongs to
# is resolved once, on the write path, and stored: several turns of one
# conversation share a class, as do several runs of one connector over one source.
#
# Stored as a digest rather than as the identifiers it came from. A session's
# events are physically removed by an erasure request while claims derived from
# them survive, and a raw key would leave an actor and session identifier on a row
# that outlives them. A digest compares for equality just as well.
#
# Corroboration is therefore not withdrawn when evidence is erased. The agreement
# was real when it was recorded; erasure removes personal data rather than
# retracting an observation, and making a score depend on privacy requests would
# couple two things that have no business being coupled.
_PROVENANCE_COLUMNS = """
ALTER TABLE lmm_claim_provenance
    ADD COLUMN independence_key   TEXT,
    ADD COLUMN independence_group TEXT,
    ADD CONSTRAINT ck_lmm_prov_independence CHECK (
        (independence_key IS NULL) = (independence_group IS NULL)
    )
"""

_PROVENANCE_INDEXES = [
    "CREATE INDEX ix_lmm_prov_independence ON lmm_claim_provenance "
    "(claim_id, independence_key) WHERE independence_key IS NOT NULL",
]

# Per-tenant weighting, following the convention the strategy table set: an absent
# row means the shipped defaults, not a disabled feature. A deployment needing a
# row per tenant before scoring worked would look broken on every new tenant.
#
# What is here and what is deliberately not. The weights a tenant may tune are
# columns; the bucket boundaries are not, and are not reachable from any operator
# surface. Those boundaries are part of the interface contract -- a caller
# filtering at 0.8 is asserting something specific, and it has to mean the same
# thing in every tenant or the filter means nothing. Which authority tier a claim
# receives is likewise absent: it is derived from provenance on the write path and
# is not configuration.
_POLICY = """
CREATE TABLE lmm_confidence_policy (
    policy_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(tenant_id),

    base_owner_human         NUMERIC(4, 3) NOT NULL DEFAULT 0.800,
    base_owner_extraction    NUMERIC(4, 3) NOT NULL DEFAULT 0.620,
    base_owner_inference     NUMERIC(4, 3) NOT NULL DEFAULT 0.450,
    base_observer_human      NUMERIC(4, 3) NOT NULL DEFAULT 0.420,
    base_observer_extraction NUMERIC(4, 3) NOT NULL DEFAULT 0.320,
    base_observer_inference  NUMERIC(4, 3) NOT NULL DEFAULT 0.230,

    corroboration_headroom   NUMERIC(4, 3) NOT NULL DEFAULT 0.600,
    corroboration_scale      NUMERIC(4, 2) NOT NULL DEFAULT 2.00,
    contradiction_penalty    NUMERIC(4, 3) NOT NULL DEFAULT 0.250,
    confirmed_confidence     NUMERIC(4, 3) NOT NULL DEFAULT 0.920,
    confirmation_hold_days   INTEGER       NOT NULL DEFAULT 180,

    -- A multiplier on the shipped half-life, never the half-life itself. A tenant
    -- knows how fast its own capabilities move; it does not get to decide that
    -- ownership changes faster than an interface.
    decay_multiplier         NUMERIC(4, 2) NOT NULL DEFAULT 1.00,

    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by               UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_lmm_confidence_policy UNIQUE (tenant_id),

    -- The authority ladder is deployment-wide. A tenant may move the weights
    -- apart or together but may not invert them: a configuration in which a
    -- non-owner outranks an owner contradicts the rule that only owners assert
    -- authoritative facts, and it would do so silently.
    CONSTRAINT ck_lmm_confidence_ladder CHECK (
        base_owner_human > base_owner_extraction
        AND base_owner_extraction > base_owner_inference
        AND base_owner_inference > base_observer_human
        AND base_observer_human > base_observer_extraction
        AND base_observer_extraction > base_observer_inference
        AND base_observer_inference >= 0.100
        AND base_owner_human <= 0.980
    ),
    CONSTRAINT ck_lmm_confidence_bounds CHECK (
        corroboration_headroom > 0 AND corroboration_headroom <= 0.800
        AND corroboration_scale >= 0.50 AND corroboration_scale <= 10.00
        AND contradiction_penalty >= 0 AND contradiction_penalty <= 0.800
        AND confirmed_confidence >= 0.850 AND confirmed_confidence <= 0.980
        AND confirmation_hold_days >= 1 AND confirmation_hold_days <= 730
        AND decay_multiplier >= 0.25 AND decay_multiplier <= 4.00
    )
)
"""

_POLICY_INDEXES = [
    "CREATE INDEX ix_lmm_confidence_policy_tenant ON lmm_confidence_policy (tenant_id)",
]


def upgrade() -> None:
    op.execute(_CLAIM_COLUMNS)
    for statement in _CLAIM_INDEXES:
        op.execute(statement)
    op.execute(_PROVENANCE_COLUMNS)
    for statement in _PROVENANCE_INDEXES:
        op.execute(statement)
    op.execute(_POLICY)
    for statement in _POLICY_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_confidence_policy")
    op.execute("DROP INDEX IF EXISTS ix_lmm_prov_independence")
    op.execute(
        "ALTER TABLE lmm_claim_provenance "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_prov_independence, "
        "  DROP COLUMN IF EXISTS independence_group, "
        "  DROP COLUMN IF EXISTS independence_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_confidence")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_provider_confidence, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_half_life, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_confidence_range, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_confidence_paired, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_confidence_scored, "
        "  DROP COLUMN IF EXISTS confidence_hold_until, "
        "  DROP COLUMN IF EXISTS decay_half_life_days, "
        "  DROP COLUMN IF EXISTS provider_confidence, "
        "  DROP COLUMN IF EXISTS calibration_version, "
        "  DROP COLUMN IF EXISTS scorer_version, "
        "  DROP COLUMN IF EXISTS confidence_inputs, "
        "  DROP COLUMN IF EXISTS confidence_scored_at, "
        "  DROP COLUMN IF EXISTS confidence"
    )
