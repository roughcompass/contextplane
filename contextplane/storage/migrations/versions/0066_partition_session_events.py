"""`memory_session_events` is partitioned by tenant, not by time.

E2 asks for "one partitioned insert" on the session-event write path, and the
table is plain (`relkind = 'r'`). The clause is right and the obvious reading of
it is wrong, which is why this migration says which partitioning and why.

**Range-partitioning by `created_at` -- the shape `audit_log` uses -- would break
the invariant sequence allocation depends on.** Postgres requires every unique
key on a partitioned table to contain the partition key, so
`uq_mse_session_seq (tenant_id, actor_id, session_id, seq)` would have to become
`(..., seq, created_at)`. That is a strictly weaker constraint: one session
whose events straddle a partition boundary could then hold two events with the
same `seq`. Sessions crossing a month boundary are ordinary, and `seq` is what
replay orders by, so the failure is duplicate turns in a replayed conversation
rather than an error anybody sees.

`session_events.py` also allocates the next `seq` by inserting and retrying on
unique violation. Weaken that key and the retry loop stops converging on one
winner, which is the same defect wearing a different hat.

**Hash by `tenant_id` keeps every invariant and prunes both hot reads.**
`tenant_id` is already the leading column of `uq_mse_session_seq`, so the
partition key is a subset of the unique key with nothing added. It is also the
leading column of `ix_mse_replay (tenant_id, actor_id, session_id, seq)` and
`ix_mse_listing (tenant_id, actor_id, created_at DESC)`, so a replay and a
listing each prune to one partition instead of fanning out -- which range
partitioning by time would not do for either, since neither read is bounded by
time.

This is also the shipped precedent rather than a new idea: `embeddings` is
`PARTITION BY HASH (tenant_id)` with `PRIMARY KEY (embedding_id, tenant_id)`, and
this table takes the same shape for the same reasons.

**Disposal does not need time partitions.** The reason to reach for range
partitioning would be detaching an old month wholesale, and that is not the
disposal this table is designed for: a session event carries `expires_at`,
derived per write from the tenant's `memory_retention_days`, and `ix_mse_expiry`
exists to sweep on it. Row-wise expiry operates on rows, not on physical layout,
so the partition scheme owes it nothing -- and a scheme chosen for a disposal
mechanism this table does not use would cost the invariant above and buy
nothing.

*Amended by E6-T3, twice, because the first correction was also wrong.*

This paragraph originally justified the same conclusion by saying "the retention
design is crypto-shredding -- disposal by destroying the key, recorded as an
auditable deletion event". **There is no content key and no shredding**, and no
`shred` among `retention/policies.py`'s four erasure modes (`delete`,
`minimize`, `minimize_and_tombstone`, `exempt`) -- so the claim rested on a
mechanism with neither an implementation nor a name in the vocabulary that would
have to carry it.

**And the replacement must not overstate either.** `expires_at` is the *designed*
disposal and it is written on every row, but **nothing sweeps it.**
`RetentionExpiryWorker` operates on the twelve record classes in
`retention_policies`, and `session_event` is not one of them -- `retention/`
does not reference this table at all. So the paragraph below about sweep cost is
describing a job that does not run. That gap is E6-T2's subject, not this
migration's, and it is recorded here so the next reader does not have to
rediscover it.

The conclusion was never in doubt through any of this. The hash key is a subset
of `uq_mse_session_seq` and leads both read indexes, so range partitioning would
break that invariant regardless of how disposal works. Only the premise moved.

**Rebuilt rather than converted, because the table is empty.** Postgres cannot
turn a populated plain table into a partitioned one in place; the shipped route
for that is the `audit_log_new` shadow plus `scripts/partition_migrate.py`. None
of that is needed here: this service has never been released, so the table is
empty in every deployment that will run this revision, and a rename-and-copy
would be ceremony over zero rows. A future migration converting a *populated*
table still needs the shadow route.

`expires_at` sweeps would now fan out across partitions. Accepted: `ix_mse_expiry`
would be a background sweep with no latency budget, and paying there to make the
two foreground reads prune is the right side of that trade.

Stated conditionally because, as the amendment above records, **no such sweep
runs today.** The trade is still the right one to have made -- it is about what
the sweep will cost when it exists -- but the original wording asserted a
present-tense job, and a reader costing out retention would have taken it at its
word.
"""

from __future__ import annotations

import os

from alembic import op

revision = "0066_partition_session_events"
down_revision: str | None = "0065_envelope_enforcement_stage"
branch_labels: str | None = None
depends_on: str | None = None


def _hash_buckets() -> int:
    """How many hash partitions the table gets.

    Read from the environment at creation time and genuinely fixed afterwards:
    hash partitioning cannot redistribute rows across a different modulus, so
    changing it later means rebuilding the table. Same knob shape and same
    default as `EMBEDDINGS_PARTITION_COUNT`, because a deployment sizing one of
    these is sizing both.
    """
    raw = os.environ.get("SESSION_EVENTS_PARTITION_COUNT", "8")  # config: intentional
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"SESSION_EVENTS_PARTITION_COUNT must be an integer, got {raw!r}"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"SESSION_EVENTS_PARTITION_COUNT must be positive, got {value}"
        raise ValueError(msg)
    return value


#: The partitioned replacement. Column list, defaults and CHECKs are the
#: original's; the two differences are `PRIMARY KEY (event_id, tenant_id)` --
#: which Postgres requires, since the partition key must be in every unique
#: key -- and the `PARTITION BY` clause itself.
_TABLE = """
CREATE TABLE memory_session_events (
    event_id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id            UUID NOT NULL REFERENCES actors(actor_id),
    session_id          TEXT NOT NULL,
    seq                 BIGINT NOT NULL,
    kind                TEXT NOT NULL,
    body                TEXT NOT NULL,
    tool_name           TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    invalidated_at      TIMESTAMPTZ,
    invalidated_reason  TEXT,

    -- Event-corpus sizing: the denominator the compression ratio needs, in the
    -- same shape memory_claims records for the numerator.
    size_bytes          INTEGER NOT NULL,
    token_count         INTEGER,
    tokenizer_id        TEXT,

    CONSTRAINT ck_mse_kind CHECK (kind IN ('user_message', 'agent_action', 'tool_invocation')),
    CONSTRAINT ck_mse_session_len CHECK (char_length(session_id) BETWEEN 1 AND 200),
    -- Bytes, not characters: a 16 KB cap on characters would admit roughly
    -- four times that in multi-byte text, which is the text most likely to
    -- arrive from a real conversation.
    CONSTRAINT ck_mse_body_bytes CHECK (octet_length(body) <= 16384),
    -- A tool invocation with no tool name is unattributable; any other kind
    -- carrying one is a caller confusing the vocabulary.
    CONSTRAINT ck_mse_tool_name CHECK ((kind = 'tool_invocation') = (tool_name IS NOT NULL)),
    CONSTRAINT ck_mse_invalidation CHECK ((invalidated_at IS NULL) = (invalidated_reason IS NULL)),
    CONSTRAINT ck_mse_reason_len CHECK (
        invalidated_reason IS NULL OR char_length(invalidated_reason) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_mse_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_mse_tokenizer CHECK ((token_count IS NULL) = (tokenizer_id IS NULL)),
    CONSTRAINT uq_mse_session_seq UNIQUE (tenant_id, actor_id, session_id, seq),

    -- Postgres requires the partition key in every unique key, so the
    -- primary key gains `tenant_id`. `uq_mse_session_seq` needs no change:
    -- `tenant_id` already leads it, which is the whole reason this table is
    -- hashed on tenant rather than ranged on time.
    PRIMARY KEY (event_id, tenant_id)
) PARTITION BY HASH (tenant_id)

"""

_PARTITION = (
    "CREATE TABLE memory_session_events_p{n} PARTITION OF memory_session_events "
    "FOR VALUES WITH (modulus {modulus}, remainder {n})"
)

#: The three indexes the original carried, recreated verbatim. Declared on the
#: parent so Postgres creates and attaches one per partition.
_INDEXES = (
    "CREATE INDEX ix_mse_replay ON memory_session_events "
    "(tenant_id, actor_id, session_id, seq) WHERE invalidated_at IS NULL",
    "CREATE INDEX ix_mse_listing ON memory_session_events (tenant_id, actor_id, created_at DESC)",
    "CREATE INDEX ix_mse_expiry ON memory_session_events (expires_at) WHERE invalidated_at IS NULL",
    "CREATE INDEX ix_mse_metadata ON memory_session_events USING GIN (metadata)",
)

#: The plain shape, for the way back.
_PLAIN_TABLE = """
CREATE TABLE memory_session_events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id            UUID NOT NULL REFERENCES actors(actor_id),
    session_id          TEXT NOT NULL,
    seq                 BIGINT NOT NULL,
    kind                TEXT NOT NULL,
    body                TEXT NOT NULL,
    tool_name           TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    invalidated_at      TIMESTAMPTZ,
    invalidated_reason  TEXT,

    -- Event-corpus sizing: the denominator the compression ratio needs, in the
    -- same shape memory_claims records for the numerator.
    size_bytes          INTEGER NOT NULL,
    token_count         INTEGER,
    tokenizer_id        TEXT,

    CONSTRAINT ck_mse_kind CHECK (kind IN ('user_message', 'agent_action', 'tool_invocation')),
    CONSTRAINT ck_mse_session_len CHECK (char_length(session_id) BETWEEN 1 AND 200),
    -- Bytes, not characters: a 16 KB cap on characters would admit roughly
    -- four times that in multi-byte text, which is the text most likely to
    -- arrive from a real conversation.
    CONSTRAINT ck_mse_body_bytes CHECK (octet_length(body) <= 16384),
    -- A tool invocation with no tool name is unattributable; any other kind
    -- carrying one is a caller confusing the vocabulary.
    CONSTRAINT ck_mse_tool_name CHECK ((kind = 'tool_invocation') = (tool_name IS NOT NULL)),
    CONSTRAINT ck_mse_invalidation CHECK ((invalidated_at IS NULL) = (invalidated_reason IS NULL)),
    CONSTRAINT ck_mse_reason_len CHECK (
        invalidated_reason IS NULL OR char_length(invalidated_reason) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_mse_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_mse_tokenizer CHECK ((token_count IS NULL) = (tokenizer_id IS NULL)),
    CONSTRAINT uq_mse_session_seq UNIQUE (tenant_id, actor_id, session_id, seq)
)
"""


def upgrade() -> None:
    # Dropped and rebuilt rather than converted: the table is empty in every
    # deployment that will run this, and nothing references it -- there are no
    # inbound foreign keys, which is what would otherwise make a drop
    # impossible. A populated table needs the `audit_log_new` shadow route.
    op.execute("DROP TABLE memory_session_events")
    op.execute(_TABLE)
    buckets = _hash_buckets()
    for n in range(buckets):
        op.execute(_PARTITION.format(n=n, modulus=buckets))
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE memory_session_events")
    op.execute(_PLAIN_TABLE)
    for statement in _INDEXES:
        op.execute(statement)
