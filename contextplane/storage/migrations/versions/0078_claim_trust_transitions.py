"""When a claim fell out of a trust class, and what it was worth at the time.

E5-T5. Decay is computed on read, so **nothing happens when a claim crosses a
bucket boundary**: it is `strong` on one read and `moderate` on the next, and no
code ran in between. There is no moment to hang a record on, which is why this
table is written by a sweep rather than by the thing that caused the change.

**`observed_at`, not `transitioned_at`, and the name is the point.** A sweep
records when it *noticed*, and the crossing happened at some earlier instant
inside the interval since the last pass. Calling the column `transitioned_at`
would answer "when did we let this decay" with a number that is wrong by up to
one sweep period, and a review reading it would have no way to tell. The honest
name forces the reader to ask about cadence, which is the question they should
be asking.

**The frozen value is `effective_confidence` and `confidence_inputs`, and needs
no new noun.** What a review wants afterwards is what the claim was worth when
it was let go, and both of those already name it — `memory_claims` carries
`confidence_inputs` for exactly this purpose. The plan entry reached for
"materiality" because that is what its sentence wanted; that word now names the
reporting obligation's classification and `check_reserved_vocabulary.py` refuses
a second meaning.

**Downward only.** A claim that regains trust does so because something happened
— a confirmation, a corroborating source, a rescore — and those already leave
records on paths that own them. Decay is the only direction with no event
behind it, so it is the only direction that needs a sweep to notice.
"""

from __future__ import annotations

from alembic import op

from contextplane.service.memory.confidence import BUCKET_LOWER_BOUNDS

revision = "0078_claim_trust_transitions"
down_revision: str | None = "0077_disposition_actor_kind"
branch_labels: str | None = None
depends_on: str | None = None

#: Built from the published buckets rather than typed beside them, the way
#: migration 0075 builds its CHECK from `CLAIM_CATEGORIES`. A bucket added there
#: must not need a migration hunt to become recordable.
_BUCKETS = ", ".join(f"'{name}'" for name, _ in sorted(BUCKET_LOWER_BOUNDS))

_TABLE = f"""
CREATE TABLE claim_trust_transitions (
    transition_id          UUID PRIMARY KEY,
    tenant_id              UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_id               UUID NOT NULL REFERENCES memory_claims(claim_id) ON DELETE CASCADE,

    from_bucket            TEXT NOT NULL,
    to_bucket              TEXT NOT NULL,

    -- What the claim was worth when the sweep saw it, and the inputs the stored
    -- score was computed from. Frozen here because both drift: the effective
    -- number keeps falling, and the inputs are corrected when a source is
    -- re-scored. A review asking "what was this worth when we let it go" is
    -- asking about this instant and no other.
    effective_confidence   NUMERIC(4, 3) NOT NULL,
    confidence_inputs      JSONB,

    -- When the sweep noticed. Not when the crossing happened -- see this
    -- migration's docstring. The two differ by up to one sweep interval and
    -- nothing in this row can tell them apart, so the column says which it is.
    observed_at            TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_trust_transition_from CHECK (from_bucket IN ({_BUCKETS})),
    CONSTRAINT ck_trust_transition_to CHECK (to_bucket IN ({_BUCKETS})),
    -- A transition to the bucket it was already in is not a transition. Recording
    -- one would make a sweep that ran twice look like a claim that decayed twice.
    CONSTRAINT ck_trust_transition_moved CHECK (from_bucket <> to_bucket),
    CONSTRAINT ck_trust_transition_confidence CHECK (
        effective_confidence >= 0 AND effective_confidence <= 1
    )
)
"""

#: The sweep's own read: the latest transition per claim, which is what tells it
#: which bucket a claim was last seen in.
_LATEST_INDEX = """
CREATE INDEX ix_trust_transitions_latest
    ON claim_trust_transitions (claim_id, observed_at DESC)
"""

#: A reviewer's read: what fell out of trust in this tenant recently.
_TENANT_INDEX = """
CREATE INDEX ix_trust_transitions_by_tenant
    ON claim_trust_transitions (tenant_id, observed_at DESC)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_LATEST_INDEX)
    op.execute(_TENANT_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX ix_trust_transitions_by_tenant")
    op.execute("DROP INDEX ix_trust_transitions_latest")
    op.execute("DROP TABLE claim_trust_transitions")
