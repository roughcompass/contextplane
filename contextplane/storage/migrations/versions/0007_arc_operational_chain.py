"""The operational event chain: per-revision heads, and a checkpoint outbox.

Revision ID: 0007_arc_operational_chain
Revises: 0006_arc_candidate_semantics
Create Date: 2026-08-06

Every mutation to a revision's *operational* state (its freshness basis,
whether a legal hold is active, its retention floor) has to be provable
after the fact, not just recorded. `arc_operational_events` is the signed,
hash-chained ledger of those transitions; `arc_operational_event_heads` is
the one-row-per-revision append cursor that makes each append an O(1)
locked read instead of a walk of the whole chain; `arc_operational_chain_
checkpoints` is the outbox that carries a durable local record out to an
external append-only sink so a compromised database alone cannot rewrite
history and have nothing outside it disagree.

The shape mirrors `arc_receipt_events`/`arc_receipt_event_heads` from
`0001_baseline_schema.py` deliberately -- same chain-link CHECK, same
0-indexed genesis convention, same locked-head-row append discipline -- but
this is a *second*, independent chain keyed by `revision_id` rather than
`receipt_id`: a receipt's chain is the record of one resolution's own
lifecycle, this one is the record of a revision's operational lifecycle
across every resolution that will ever cite it.

`PRIMARY KEY (revision_id, sequence)` rather than `event_id` as the primary
key -- `event_id` is still globally unique (enforced below) so a caller that
only has an event id can still resolve one, but the natural clustering key
for "the next event in this revision's chain" is what an append actually
contends on, and that is `(revision_id, sequence)`.
"""

from __future__ import annotations

from alembic import op

revision = "0007_arc_operational_chain"
down_revision: str | None = "0006_arc_candidate_semantics"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_operational_events -- the signed, hash-chained ledger.
# ---------------------------------------------------------------------------

_OPERATIONAL_EVENTS_DDL = """
CREATE TABLE arc_operational_events (
    revision_id                       UUID NOT NULL REFERENCES arc_revisions(revision_id),
    sequence                          INTEGER NOT NULL,
    event_id                          UUID NOT NULL,
    artifact_id                       UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    event_type                        TEXT NOT NULL,
    event_payload                     JSONB NOT NULL,
    actor_issuer                      TEXT NOT NULL,
    actor_subject                     TEXT NOT NULL,
    actor_role                        TEXT NOT NULL,
    authorization_decision_reference  TEXT NOT NULL,
    authority_evidence_digest         TEXT NOT NULL,
    idempotency_key_digest            TEXT NOT NULL,
    previous_event_digest             TEXT,
    signer_key_id                     TEXT NOT NULL,
    event_digest                      TEXT NOT NULL,
    signature                         TEXT NOT NULL,
    signature_profile                 TEXT NOT NULL,
    request_payload_digest            TEXT NOT NULL,
    created_at                        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (revision_id, sequence),
    CONSTRAINT uq_arc_operational_events_event_id UNIQUE (event_id),
    CONSTRAINT uq_arc_operational_events_idempotency UNIQUE (revision_id, idempotency_key_digest),
    CONSTRAINT ck_arc_operational_events_sequence_nonneg CHECK (sequence >= 0),
    -- The genesis shape, both directions: sequence 0 has no predecessor and
    -- nothing else may lack one. A caller that forges a fork or omits its
    -- own predecessor fails here before the service layer ever gets a
    -- chance to reject it -- this is the DDL half of "operational head
    -- sequencing" enforcement, the service-side lock is the other half.
    CONSTRAINT ck_arc_operational_events_chain_link CHECK (
        (sequence = 0 AND previous_event_digest IS NULL)
        OR (sequence > 0 AND previous_event_digest IS NOT NULL)
    ),
    CONSTRAINT ck_arc_operational_events_type CHECK (
        event_type IN (
            'operational_state_initialized', 'freshness_downgraded',
            'legal_hold_placed', 'legal_hold_released', 'retention_extended'
        )
    ),
    CONSTRAINT ck_arc_operational_events_actor_role CHECK (actor_role IN ('system', 'human')),
    CONSTRAINT ck_arc_operational_events_digest_len CHECK (char_length(event_digest) = 64),
    CONSTRAINT ck_arc_operational_events_authority_digest_len CHECK (char_length(authority_evidence_digest) = 64),
    CONSTRAINT ck_arc_operational_events_idem_digest_len CHECK (char_length(idempotency_key_digest) = 64),
    CONSTRAINT ck_arc_operational_events_prev_digest_len CHECK (
        previous_event_digest IS NULL OR char_length(previous_event_digest) = 64
    ),
    CONSTRAINT ck_arc_operational_events_request_digest_len CHECK (char_length(request_payload_digest) = 64),
    CONSTRAINT ck_arc_operational_events_signer_key_len CHECK (char_length(signer_key_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_arc_operational_events_signature_len CHECK (char_length(signature) BETWEEN 1 AND 1024),
    CONSTRAINT ck_arc_operational_events_sig_profile_len CHECK (char_length(signature_profile) BETWEEN 1 AND 64)
)
"""

_OPERATIONAL_EVENTS_INDEXES = [
    "CREATE INDEX ix_arc_operational_events_artifact ON arc_operational_events (artifact_id)",
]

# ---------------------------------------------------------------------------
# arc_operational_event_heads -- the per-revision append cursor.
# ---------------------------------------------------------------------------

_OPERATIONAL_EVENT_HEADS_DDL = """
CREATE TABLE arc_operational_event_heads (
    revision_id       UUID PRIMARY KEY REFERENCES arc_revisions(revision_id),
    next_sequence     INTEGER NOT NULL,
    last_event_digest TEXT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    -- A head exists only once its genesis event (sequence 0) is written, so
    -- the lowest legal value is 1.
    CONSTRAINT ck_arc_operational_event_heads_next_sequence CHECK (next_sequence >= 1),
    CONSTRAINT ck_arc_operational_event_heads_digest_len CHECK (char_length(last_event_digest) = 64)
)
"""

# ---------------------------------------------------------------------------
# arc_operational_chain_checkpoints -- the durable-export outbox.
#
# `exported_at`, `sink_receipt_digest`, and `sink_receipt_signature` move
# together: a checkpoint is pending (all three NULL) until the sink
# acknowledges it, at which point all three are recorded in the same
# update. There is deliberately no state in which a receipt exists without
# an export timestamp or vice versa.
# ---------------------------------------------------------------------------

_OPERATIONAL_CHAIN_CHECKPOINTS_DDL = """
CREATE TABLE arc_operational_chain_checkpoints (
    checkpoint_id           UUID PRIMARY KEY,
    deployment_id           TEXT NOT NULL,
    revision_id             UUID NOT NULL REFERENCES arc_revisions(revision_id),
    sequence                INTEGER NOT NULL,
    event_id                UUID NOT NULL REFERENCES arc_operational_events(event_id),
    head_digest             TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    exported_at              TIMESTAMPTZ,
    sink_receipt_digest       TEXT,
    sink_receipt_signature    TEXT,
    export_attempts            INTEGER NOT NULL DEFAULT 0,
    last_export_error_code      TEXT,
    last_export_attempt_at       TIMESTAMPTZ,
    CONSTRAINT uq_arc_operational_chain_checkpoints_identity UNIQUE (deployment_id, revision_id, sequence),
    CONSTRAINT ck_arc_operational_chain_checkpoints_digest_len CHECK (char_length(head_digest) = 64),
    CONSTRAINT ck_arc_operational_chain_checkpoints_export_triple CHECK (
        (exported_at IS NULL AND sink_receipt_digest IS NULL AND sink_receipt_signature IS NULL)
        OR (exported_at IS NOT NULL AND sink_receipt_digest IS NOT NULL AND sink_receipt_signature IS NOT NULL)
    ),
    CONSTRAINT ck_arc_operational_chain_checkpoints_error_len CHECK (
        last_export_error_code IS NULL OR char_length(last_export_error_code) <= 64
    )
)
"""

_OPERATIONAL_CHAIN_CHECKPOINTS_INDEXES = [
    # The exporter's own drain query: every checkpoint still waiting on a
    # sink acknowledgment, oldest first. Partial on `exported_at IS NULL`
    # so the index stays small as most checkpoints move out of it quickly.
    "CREATE INDEX ix_arc_operational_chain_checkpoints_pending "
    "ON arc_operational_chain_checkpoints (created_at) WHERE exported_at IS NULL",
    "CREATE INDEX ix_arc_operational_chain_checkpoints_revision ON arc_operational_chain_checkpoints (revision_id)",
]


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    op.execute(_OPERATIONAL_EVENTS_DDL)
    for stmt in _OPERATIONAL_EVENTS_INDEXES:
        op.execute(stmt)

    op.execute(_OPERATIONAL_EVENT_HEADS_DDL)

    op.execute(_OPERATIONAL_CHAIN_CHECKPOINTS_DDL)
    for stmt in _OPERATIONAL_CHAIN_CHECKPOINTS_INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    # Reverse dependency order: checkpoints reference events, heads and
    # events both reference arc_revisions but not each other.
    op.execute("DROP TABLE arc_operational_chain_checkpoints")
    op.execute("DROP TABLE arc_operational_event_heads")
    op.execute("DROP TABLE arc_operational_events")
