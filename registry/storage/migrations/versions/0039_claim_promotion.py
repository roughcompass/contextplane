"""Promotion: proposals, the journal that makes reversal exact, and the guardrails.

**Promotion state is a separate axis from staging status.** A claim that an owner
rejected stays `staged` -- it remains readable, still serves, still carries evidence.
What changed is only that it may not become canonical. Folding that into `status` would
force a rejected claim to stop being a claim, and the requirement is the opposite: the
rejection itself becomes evidence.

**The journal records what the promotion observed, not just what it wrote.** Reversal
has to restore the state that preceded a specific promotion, and "the previous value"
is not the same thing. If promotion P1 changed A to B and a later P2 changed B to C,
then reversing P1 by writing A back would silently destroy P2. So each promotion
records the canonical row it created *and* the row it superseded, by id. Reversal
refuses unless the row it created is still the live one -- which is exactly the
condition under which restoring the predecessor is sound.

**The auto-promote allowlist is per tenant and empty by default.** A fresh deployment
promotes nothing. There is no seed row, no default predicate, and no configuration that
turns the whole thing on at once: an entry names one predicate. A default that promoted
anything would mean the safe posture depended on an operator knowing to switch it off.

**A rejection is keyed by what was asserted, not by which row asserted it, nor by
when.** Claims are immutable, so the same assertion arriving again is a new row: keyed
by row id a rejection would be defeated by repetition, and keyed by the asserted
interval it would be defeated by waiting a day. The fingerprint is the subject, the
predicate, and the value.

That is a strong suppression, so it is paired with one way out: the authority that was
refused is recorded, and a claim carrying *stronger* standing may still be proposed.
Volume cannot overturn a rejection; new standing can.
"""

from __future__ import annotations

from alembic import op

revision = "0039_claim_promotion"
down_revision = "0038_claim_supersession"
branch_labels = None
depends_on = None


# NULL means the claim has never been through promotion at all, which is the common
# case and must not be confused with any decided state.
_CLAIM_COLUMNS = """
ALTER TABLE lmm_claims
    ADD COLUMN promotion_state TEXT,
    ADD CONSTRAINT ck_lmm_claims_promotion_state CHECK (
        promotion_state IS NULL
        OR promotion_state IN ('proposed', 'promoted', 'rejected', 'reversed')
    )
"""

_PROPOSALS = """
CREATE TABLE lmm_promotion_proposal (
    proposal_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id          UUID NOT NULL REFERENCES lmm_claims(claim_id),

    -- The tenant that must act, which is the subject's owner and never the author.
    -- A proposal exists precisely so a claim about somebody else's capability
    -- reaches them instead of their graph.
    owner_tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    author_tenant_id  UUID NOT NULL REFERENCES tenants(tenant_id),

    subject_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    predicate         TEXT NOT NULL,
    target_kind       TEXT NOT NULL,
    target_key        TEXT NOT NULL,
    mapping_version   INTEGER NOT NULL,

    -- What the graph says now, and what acceptance would make it say. Stored rather
    -- than recomputed at review time: a reviewer is deciding about the change that
    -- was proposed, and recomputing lets the graph move underneath the decision.
    current_value     JSONB,
    proposed_value    JSONB NOT NULL,

    valid_from        TIMESTAMPTZ NOT NULL,
    valid_to          TIMESTAMPTZ,

    -- Empty means not high-impact. Every reason is listed, not just the first one
    -- found: a reviewer deciding whether to accept needs all of them, and a
    -- classifier that stopped at the first would hide the rest.
    high_impact_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,

    state             TEXT NOT NULL DEFAULT 'open',
    decided_by        UUID REFERENCES actors(actor_id),
    decided_at        TIMESTAMPTZ,
    decision_reason   TEXT,
    -- Set only on accept-with-amendment. Both this and proposed_value are kept, so
    -- the record shows what was proposed as well as what was actually promoted.
    amended_value     JSONB,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_lmm_proposal_state CHECK (
        state IN ('open', 'accepted', 'amended', 'rejected', 'withdrawn')
    ),
    CONSTRAINT ck_lmm_proposal_target CHECK (target_kind IN ('attribute', 'edge')),
    -- An open proposal has no decision; a closed one has both a decider and a time.
    -- Half a decision is not a state anybody can act on.
    CONSTRAINT ck_lmm_proposal_decision CHECK (
        (state = 'open') = (decided_at IS NULL)
        AND (decided_at IS NULL) = (decided_by IS NULL)
    ),
    -- A rejection without a reason is indistinguishable from an accident, and the
    -- reason is what the re-arrival rule and the audit both read.
    CONSTRAINT ck_lmm_proposal_reject_reason CHECK (
        state <> 'rejected' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    CONSTRAINT ck_lmm_proposal_amendment CHECK (
        (amended_value IS NOT NULL) = (state = 'amended')
    ),
    CONSTRAINT ck_lmm_proposal_interval CHECK (
        valid_to IS NULL OR valid_to > valid_from
    ),
    CONSTRAINT ck_lmm_proposal_reasons CHECK (
        jsonb_typeof(high_impact_reasons) = 'array'
    )
)
"""

_PROPOSAL_INDEXES = [
    # The owner's queue: what is waiting for me. This is the read whose latency
    # the proposal-queue budget bounds.
    "CREATE INDEX ix_lmm_proposal_owner_open ON lmm_promotion_proposal "
    "  (owner_tenant_id, created_at) WHERE state = 'open'",
    "CREATE INDEX ix_lmm_proposal_claim ON lmm_promotion_proposal (claim_id)",
    # One open proposal per claim. A claim queued twice would let two reviewers
    # decide the same thing differently, and the second write would silently win.
    "CREATE UNIQUE INDEX uq_lmm_proposal_open_per_claim ON lmm_promotion_proposal " "  (claim_id) WHERE state = 'open'",
]

_JOURNAL = """
CREATE TABLE lmm_promotion_journal (
    promotion_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id        UUID NOT NULL REFERENCES lmm_promotion_proposal(proposal_id),
    claim_id           UUID NOT NULL REFERENCES lmm_claims(claim_id),
    tenant_id          UUID NOT NULL REFERENCES tenants(tenant_id),

    target_kind        TEXT NOT NULL,
    -- The canonical row this promotion created, and the one it closed. Both by id:
    -- reversal that matched on value could not tell two identical values apart, and
    -- would restore the wrong row whenever a value repeated.
    created_row_id     UUID NOT NULL,
    superseded_row_id  UUID,
    -- What the superseded row's validity was before this promotion narrowed it, so
    -- reversal restores the interval and not merely the value.
    superseded_valid_to TIMESTAMPTZ,

    promoted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_by        UUID REFERENCES actors(actor_id),

    reversed_at        TIMESTAMPTZ,
    reversed_by        UUID REFERENCES actors(actor_id),
    reversal_reason    TEXT,

    CONSTRAINT ck_lmm_journal_target CHECK (target_kind IN ('attribute', 'edge')),
    CONSTRAINT ck_lmm_journal_reversal CHECK (
        (reversed_at IS NULL) = (reversed_by IS NULL)
    ),
    -- A superseded row implies there was an interval to restore; restoring nothing
    -- would leave the predecessor closed after a reversal that claimed to undo the
    -- closing.
    CONSTRAINT ck_lmm_journal_superseded CHECK (
        superseded_row_id IS NOT NULL OR superseded_valid_to IS NULL
    )
)
"""

_JOURNAL_INDEXES = [
    "CREATE INDEX ix_lmm_journal_claim ON lmm_promotion_journal (claim_id)",
    # Reversal asks whether the row it created is still the live one.
    "CREATE INDEX ix_lmm_journal_created_row ON lmm_promotion_journal (created_row_id)",
    "CREATE INDEX ix_lmm_journal_live ON lmm_promotion_journal " "  (tenant_id, promoted_at) WHERE reversed_at IS NULL",
]

# One row per predicate a tenant has explicitly opted in. There is no row meaning
# "all predicates" on purpose -- a wildcard is how an allowlist stops being one.
_ALLOWLIST = """
CREATE TABLE lmm_autopromote_allowlist (
    entry_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    predicate   TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by    UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_lmm_autopromote UNIQUE (tenant_id, predicate),
    CONSTRAINT ck_lmm_autopromote_predicate CHECK (
        char_length(trim(predicate)) > 0 AND predicate <> '*'
    )
)
"""

# Per-tenant review policy. A missing row means defaults, and the defaults are the
# cautious ones: promotion needs review and the blast-radius threshold is low.
_POLICY = """
CREATE TABLE lmm_promotion_policy (
    tenant_id            UUID PRIMARY KEY REFERENCES tenants(tenant_id),
    blast_radius_threshold INTEGER NOT NULL DEFAULT 5,
    -- Predicates this tenant always wants a human to see, whatever else is true.
    always_review        JSONB NOT NULL DEFAULT '[]'::JSONB,
    confidence_floor     NUMERIC(4, 3) NOT NULL DEFAULT 0.000,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by           UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_lmm_policy_threshold CHECK (blast_radius_threshold >= 0),
    CONSTRAINT ck_lmm_policy_floor CHECK (
        confidence_floor >= 0 AND confidence_floor <= 1
    ),
    CONSTRAINT ck_lmm_policy_always_review CHECK (
        jsonb_typeof(always_review) = 'array'
    )
)
"""

# Keyed by what was asserted rather than by which row asserted it, so restating the
# same thing lands here instead of queueing a fresh proposal.
_REJECTION = """
CREATE TABLE lmm_promotion_rejection (
    rejection_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    subject_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    predicate         TEXT NOT NULL,
    -- The canonical form of the value, so two spellings of one assertion collide.
    value_digest      TEXT NOT NULL,
    -- The authority the refused claim carried. A stronger source may revive the
    -- assertion; an equal or weaker one may not, which is what stops repetition
    -- from wearing a decision down while still letting new standing overturn it.
    rejected_authority TEXT NOT NULL,

    reason            TEXT NOT NULL,
    proposal_id       UUID NOT NULL REFERENCES lmm_promotion_proposal(proposal_id),
    rejected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    rejected_by       UUID REFERENCES actors(actor_id),

    -- Deliberately not keyed on the asserted interval. A restatement naturally
    -- carries a later timestamp, so including it would let anyone defeat a
    -- rejection by simply saying the same thing again tomorrow.
    CONSTRAINT uq_lmm_rejection UNIQUE (
        tenant_id, subject_entity_id, predicate, value_digest
    ),
    CONSTRAINT ck_lmm_rejection_reason CHECK (char_length(trim(reason)) > 0)
)
"""


def upgrade() -> None:
    op.execute(_CLAIM_COLUMNS)
    op.execute(_PROPOSALS)
    for statement in _PROPOSAL_INDEXES:
        op.execute(statement)
    op.execute(_JOURNAL)
    for statement in _JOURNAL_INDEXES:
        op.execute(statement)
    op.execute(_ALLOWLIST)
    op.execute(_POLICY)
    op.execute(_REJECTION)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_promotion_rejection")
    op.execute("DROP TABLE IF EXISTS lmm_promotion_policy")
    op.execute("DROP TABLE IF EXISTS lmm_autopromote_allowlist")
    op.execute("DROP TABLE IF EXISTS lmm_promotion_journal")
    op.execute("DROP TABLE IF EXISTS lmm_promotion_proposal")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_promotion_state, "
        "  DROP COLUMN IF EXISTS promotion_state"
    )
