"""The obligation record, with `unclassified` as a first-class state.

E4-T5b. The decision that this object exists was recorded and nothing
implemented it -- neither `reporting_obligation` nor `materiality` appeared
anywhere in the tree. This is the record, and only the record.

**`materiality`, not `severity`, and the reason is enforceable rather than
stylistic.** `severity` already names the PII scanner's `advisory < warn <
block` ordering in three modules. Two orderings sharing one field name is a
defect waiting for a reader who has only ever seen the other one, and
`scripts/check_reserved_vocabulary.py` now refuses the reuse rather than leaving
it to the next grep.

**`reporting_obligation`, not `incident`.** That word is taken twice already --
a `LIFECYCLE_REFERENCE_KINDS` entry and an `evidence_kind` in
`memory_claim_provenance`'s CHECK -- and in both it means an *external*
operational incident something points at. A record and a pointer to a record
must not share a noun. The table is named for what is tracked rather than for
what triggered it.

**`unclassified` is a state, not a null.** It is the state most obligations will
be in most of the time, because classification needs thresholds this deployment
does not have. Modelling it as `materiality IS NULL` would make "nobody has
decided" indistinguishable from "the column was added later", and would make the
delay gauge -- whose healthy value is not zero -- unwritable.

**Nothing here classifies anything.** Automatic classification needs a ratified
threshold set that is not this team's to write, so `major` is reachable only by
an explicit human decision that records who made it. A placeholder threshold
presented as a compliance feature is worse than an absent one.
"""

from __future__ import annotations

from alembic import op

revision = "0076_reporting_obligations"
down_revision: str | None = "0075_claim_sampling_policy"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = """
CREATE TABLE reporting_obligations (
    obligation_id      UUID PRIMARY KEY,
    tenant_id          UUID NOT NULL REFERENCES tenants(tenant_id),

    -- What the obligation is about, in the reporter's own words. Free text
    -- rather than a reference: an obligation can be nominated before anybody
    -- knows which record it concerns, and refusing the nomination until the
    -- link exists would lose the nomination.
    summary            TEXT NOT NULL,

    -- The state most rows are in most of the time, and the default on purpose:
    -- a nomination that arrived without a decision is unclassified, and saying
    -- so is more honest than a system where everything is classified promptly
    -- on paper.
    materiality        TEXT NOT NULL DEFAULT 'unclassified',

    -- When the obligation entered the system. The delay gauge measures from
    -- here, so it is the nomination instant and never the classification one.
    nominated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    nominated_by       UUID NOT NULL REFERENCES actors(actor_id),

    -- Set together, by an explicit decision, or both null. There is no
    -- automatic path to either column: classification needs thresholds that do
    -- not exist here, so the only way out of `unclassified` is somebody
    -- deciding and being recorded as having decided.
    classified_at      TIMESTAMPTZ,
    classified_by      UUID REFERENCES actors(actor_id),

    -- Why, in a sentence somebody has to be willing to have read back to them.
    -- Bounded below because a one-word rationale is the same as none.
    classification_note TEXT,

    CONSTRAINT ck_obligation_materiality
        CHECK (materiality IN ('unclassified', 'not_material', 'material')),

    -- A classified obligation carries all three of when, who and why; an
    -- unclassified one carries none of them. Stated as one constraint rather
    -- than three so a row can never be half-classified: a `classified_at` with
    -- no actor is a decision with nobody behind it.
    CONSTRAINT ck_obligation_classification_is_attributed CHECK (
        (materiality = 'unclassified'
            AND classified_at IS NULL
            AND classified_by IS NULL
            AND classification_note IS NULL)
        OR (materiality <> 'unclassified'
            AND classified_at IS NOT NULL
            AND classified_by IS NOT NULL
            AND classification_note IS NOT NULL)
    ),

    CONSTRAINT ck_obligation_summary_len CHECK (char_length(summary) BETWEEN 10 AND 4000),
    CONSTRAINT ck_obligation_note_len CHECK (
        classification_note IS NULL OR char_length(classification_note) BETWEEN 20 AND 2000
    )
)
"""

#: Partial, on the state the gauge scans. The classified rows are the ones that
#: accumulate, and an index over all of them would grow without ever being read
#: by the only query that needs speed here.
_UNCLASSIFIED_INDEX = """
CREATE INDEX ix_reporting_obligations_unclassified
    ON reporting_obligations (tenant_id, nominated_at)
    WHERE materiality = 'unclassified'
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_UNCLASSIFIED_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX ix_reporting_obligations_unclassified")
    op.execute("DROP TABLE reporting_obligations")
