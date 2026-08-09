"""The ledger of things other systems observed, kept as observations rather than facts.

A signal is this system's record that some *other* system said something happened.
The external source stays authoritative; nothing here replaces it, and the columns
are chosen so a later reader cannot forget that. Two of them carry the whole idea:
`authority` records what the source was entitled to assert, and the payload column
holds a projection the producer allowlisted rather than whatever arrived.

**Three times, three columns, never substituted for one another.** When the source
says it happened, when the producer learned of it, and when this server stored it
are different instants, and collapsing any pair destroys the only evidence of lag.
The first two are nullable because a source that does not publish them exists and
the honest record of that is NULL -- inventing `now()` for a missing event time
would make every such signal look instantaneous. `ingested_at` is the one that
cannot be NULL and cannot be supplied by a caller: it defaults to `now()` here so
a client-set value has nowhere to enter, which is what makes it usable as the
audit anchor when the other two are absent or wrong.

**Two unique keys, because they answer two different questions.** The source event
key says "this is the same external occurrence"; the idempotency key says "this is
the same submission". A redelivery of one event under a new submission must not
create a second row, and two genuinely different events from one producer must
not collide. Both are scoped by `(tenant_id, producer_id)`: producer-scoped
because one producer's id space is its own, and tenant-scoped because two tenants
observing the same external run is normal and must not be a conflict -- the same
reason `0031` scopes its collision key per tenant.

**`content_digest` is what makes replay decidable.** Exact redelivery converges to
a no-op and changed content under a reused key is a refusal, and the database
cannot tell those apart by comparing a payload it is not required to keep. Storing
the digest makes the distinction one comparison against an indexed column instead
of a structural diff the service and the schema would have to agree about.

**Exactly one of payload or evidence handle.** A signal carries the projection
inline, or a handle to authorized evidence held elsewhere -- never both, because
two copies of one observation drift, and never neither, because a signal that
carries no observation is a row asserting only that something happened somewhere.
`num_nonnulls` states that as a constraint rather than a convention.

**No workspace body columns, deliberately and permanently.** Workspace content
reaching this table would put unauthorized text on a path that later derives
claims, which is the exact escape the phase's privacy floor exists to prevent.
The absence is the design; anything needing workspace content binds by reference
instead of copying.

**Revocation and supersession are signal state, so they live on the signal.** A
source withdrawing an event and a source superseding it are different events with
different consequences -- withdrawal invalidates dependents, while a re-run leaves
both runs true and only marks the earlier one as no longer the thing to learn
from. Keeping them apart here is what lets a later reader refuse promotion on
superseded-only evidence without having to reconstruct which of the two happened.
"""

from __future__ import annotations

from alembic import op

revision = "0040_external_signals"
down_revision: str | None = "0033_receipt_evidence"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The same four handling classes `0031` closes, and closed here for the same
# reason: a classification nobody declared is one no retention policy covers.
_CLASSIFICATIONS = "'public', 'internal', 'confidential', 'restricted'"

# Who produced the observation. `external` is a system speaking for itself;
# `human` and `agent` are this system's own participants. Closed so an adapter
# cannot invent a fourth kind by passing a new string -- the privacy and
# learning-eligibility rules downstream are written against exactly these.
_PRODUCER_TYPES = "'human', 'agent', 'external'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE external_signals (
            signal_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),

            -- Scope below the tenant. Nullable because not every producer knows
            -- a team or a project, and a placeholder string would be indexed and
            -- grouped as though it meant something.
            team_key            TEXT,
            project_key         TEXT,

            -- Folded to lowercase like `0031`'s source_system, so two spellings
            -- of one source cannot occupy two rows.
            source_system       TEXT NOT NULL,
            -- The registered producer, and what kind of thing it is.
            producer_id         TEXT NOT NULL,
            producer_type       TEXT NOT NULL,

            -- The other system's identifier for this occurrence. Trimmed, not
            -- folded: its case belongs to the system that issued it.
            source_event_id     TEXT NOT NULL,
            -- The submission's own key, and the digest of what that submission
            -- carried. Together they make a redelivery decidable without keeping
            -- the body: same key and same digest is the same submission, same
            -- key and a different digest is a conflict.
            idempotency_key     TEXT NOT NULL,
            content_digest      TEXT NOT NULL,

            -- What the source says it is entitled to assert. Stored per signal
            -- rather than per producer because a producer's boundary can be
            -- narrowed, and a claim derived last month must still be readable
            -- against the authority that actually backed it.
            authority           TEXT NOT NULL,
            classification      TEXT NOT NULL,

            -- Three different instants; see the module docstring.
            event_time          TIMESTAMPTZ,
            observed_time       TIMESTAMPTZ,
            ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- When this observation stops being usable as current. NULL means
            -- no expiry was declared, which is not the same as never expires and
            -- is left for policy to interpret rather than decided here.
            expires_at          TIMESTAMPTZ,

            -- The envelope contract version this row was written under, so a
            -- later reader can tell which shape it is looking at.
            schema_version      TEXT NOT NULL,

            -- The allowlisted projection, or a handle to authorized evidence
            -- held elsewhere. Exactly one, per the CHECK below.
            payload             JSONB,
            evidence_handle     TEXT,

            -- Withdrawn by the source: dependents are invalidated.
            revoked_at          TIMESTAMPTZ,
            -- Overtaken by a later attempt: both occurrences remain true, and
            -- only this one stops being the thing to learn from.
            superseded_for_learning BOOLEAN NOT NULL DEFAULT FALSE,

            CONSTRAINT ck_external_signal_classification
                CHECK (classification IN ({_CLASSIFICATIONS})),
            CONSTRAINT ck_external_signal_producer_type
                CHECK (producer_type IN ({_PRODUCER_TYPES})),

            -- The identity parts must be present and non-empty: a signal missing
            -- one of them collides with everything else missing it.
            CONSTRAINT ck_external_signal_identity_present
                CHECK (
                    length(source_system) > 0
                    AND length(producer_id) > 0
                    AND length(source_event_id) > 0
                    AND length(idempotency_key) > 0
                    AND length(content_digest) > 0
                    AND length(authority) > 0
                    AND length(schema_version) > 0
                ),
            -- Normalization is enforced, not assumed, so a row written around
            -- the service cannot carry a spelling that never collides.
            CONSTRAINT ck_external_signal_normalized
                CHECK (
                    source_system = lower(source_system)
                    AND source_event_id = btrim(source_event_id)
                    AND idempotency_key = btrim(idempotency_key)
                ),
            -- An observation, or a pointer to one. Never both, never neither.
            --
            -- The `jsonb_typeof` half is not defensive noise: JSON `null` is a
            -- value, so `num_nonnulls` counts a payload of `'null'::jsonb` as
            -- present and the first clause alone would admit a signal carrying
            -- no observation at all. A client binding a Python `None` through a
            -- JSONB-typed parameter sends exactly that, which is a spelling any
            -- ordinary ORM call can produce by accident rather than an exotic
            -- one -- so the constraint has to reject the value, not trust the
            -- caller to send SQL NULL.
            CONSTRAINT ck_external_signal_body_exclusive
                CHECK (
                    num_nonnulls(payload, evidence_handle) = 1
                    AND (payload IS NULL OR jsonb_typeof(payload) <> 'null')
                )
        )
        """
    )

    # The same external occurrence, seen twice, is one row.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_external_signal_source_event
            ON external_signals (tenant_id, producer_id, source_event_id)
        """
    )
    # The same submission, replayed, is one row. Separate from the key above
    # because a producer may resubmit one occurrence under a new submission and
    # may submit two occurrences under one -- neither is a duplicate.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_external_signal_idempotency
            ON external_signals (tenant_id, producer_id, idempotency_key)
        """
    )
    # The read path: a tenant's recent signals, newest first. Anchored on
    # `ingested_at` rather than `event_time` because that is the column that is
    # always present and always this server's own.
    op.execute(
        """
        CREATE INDEX ix_external_signal_recent
            ON external_signals (tenant_id, ingested_at DESC)
        """
    )
    # Retention and revocation sweeps select by expiry across tenants, so this
    # one is deliberately not tenant-leading. Partial, because the rows without
    # a declared expiry are the majority and never match.
    op.execute(
        """
        CREATE INDEX ix_external_signal_expiry
            ON external_signals (expires_at)
            WHERE expires_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_signals")
