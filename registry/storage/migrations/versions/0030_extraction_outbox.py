"""The extraction outbox: session events queued for a provider, and a dead-letter.

Extraction is never on the ingest hot path. An event write enqueues a row here in
the same transaction that stores the event, and a scheduled drain picks it up.
That is the same transactional-outbox shape the embedding pipeline already uses,
and it is reused rather than reinvented: a second queue technology would be a
second thing to operate, monitor, and reason about at failure time.

**One row per (session, strategy), not per event.** Extraction reads a window of
turns, because a claim usually spans several -- "it times out after 900 seconds"
in one turn and "the auth service" in the one before it. Enqueuing per event would
either re-extract the same window repeatedly or force each turn to be
self-contained, and neither is how conversations work. The row is upserted, so a
burst of ten events in one session leaves one pending job rather than ten.

**Strategies are separate rows.** They fail independently, retry independently,
and one strategy's defective prompt must not stall the others. A single row
covering all strategies would make the slowest one the latency of every one.

**Retry timing lives on the row.** `next_attempt_at` rather than a bare attempt
count, so backoff is a property of the queue rather than of whichever worker
happens to pick the row up. The drain's claim query filters on it, which means a
backing-off row is invisible to the claim rather than fetched and skipped -- the
difference matters when most of the queue is backing off.

**The dead-letter keeps the reason and the attempt count.** A row that exhausted
its retries is a question for a person, and "it failed" is not enough to answer
it. Kept rather than deleted, because the volume is bounded by the failure rate
and the alternative is discovering the pipeline stopped by noticing an absence.
"""

from __future__ import annotations

from alembic import op

revision = "0030_extraction_outbox"
down_revision = "0029_session_event_sizing"
branch_labels = None
depends_on = None


_OUTBOX = """
CREATE TABLE lmm_extraction_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Scoped to the actor as well as the tenant, matching how session events are
    -- scoped: a session belongs to the agent that had it.
    actor_id        UUID NOT NULL REFERENCES actors(actor_id),
    session_id      TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,

    -- The window still to extract. Advanced as work completes, so a resumed
    -- session extracts only the turns nobody has read yet.
    from_seq        BIGINT NOT NULL,
    through_seq     BIGINT NOT NULL,

    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- NULL means eligible now. Set to a future instant while backing off.
    next_attempt_at TIMESTAMPTZ,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,

    -- One pending job per session and strategy. A burst of events upserts the
    -- window rather than queueing a job per turn.
    CONSTRAINT uq_lmm_outbox_session_strategy
        UNIQUE (tenant_id, actor_id, session_id, strategy_id),
    CONSTRAINT ck_lmm_outbox_window CHECK (through_seq >= from_seq),
    CONSTRAINT ck_lmm_outbox_attempts CHECK (attempts >= 0)
)
"""

_OUTBOX_INDEXES = [
    # The claim query: eligible rows, oldest first. Partial on the backoff
    # condition so a queue that is mostly backing off is not scanned to find the
    # few rows that are ready.
    "CREATE INDEX ix_lmm_outbox_ready ON lmm_extraction_outbox "
    "(enqueued_at) WHERE next_attempt_at IS NULL",
    "CREATE INDEX ix_lmm_outbox_retry ON lmm_extraction_outbox "
    "(next_attempt_at) WHERE next_attempt_at IS NOT NULL",
    # Erasure walks from the actor, same as every other actor-scoped table.
    "CREATE INDEX ix_lmm_outbox_actor ON lmm_extraction_outbox (tenant_id, actor_id)",
]

_FAILED = """
CREATE TABLE lmm_extraction_outbox_failed (
    failed_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id        UUID NOT NULL REFERENCES actors(actor_id),
    session_id      TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    from_seq        BIGINT NOT NULL,
    through_seq     BIGINT NOT NULL,

    -- Why it died and how hard it tried. "It failed" does not let anyone decide
    -- whether to fix a prompt, raise a timeout, or replay the window.
    attempts        INTEGER NOT NULL,
    last_error      TEXT NOT NULL,
    enqueued_at     TIMESTAMPTZ NOT NULL,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_FAILED_INDEXES = [
    "CREATE INDEX ix_lmm_outbox_failed_tenant ON lmm_extraction_outbox_failed "
    "(tenant_id, failed_at)",
    "CREATE INDEX ix_lmm_outbox_failed_strategy ON lmm_extraction_outbox_failed "
    "(strategy_id, failed_at)",
    "CREATE INDEX ix_lmm_outbox_failed_actor ON lmm_extraction_outbox_failed "
    "(tenant_id, actor_id)",
]


def upgrade() -> None:
    op.execute(_OUTBOX)
    for statement in _OUTBOX_INDEXES:
        op.execute(statement)
    op.execute(_FAILED)
    for statement in _FAILED_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_extraction_outbox_failed")
    op.execute("DROP TABLE IF EXISTS lmm_extraction_outbox")
