"""Requests from the teams that consume a capability, and governance for new sources.

**A request is not a claim, and the difference is the whole reason this table exists.**
A claim asserts what is true and gets scored, decayed, consolidated, and possibly
promoted into the graph. A request expresses what somebody needs. It is never true or
false, so scoring it would be meaningless, and consolidating two requests would destroy
the fact that two teams independently asked. Storing them in one table with a
discriminator would mean every query about belief had to remember to exclude wishes.

**A declined request is kept, not deleted.** It remains demand signal: three declined
requests for the same thing is information about the capability, and deleting them
leaves the owner's decision looking unanimous. It is also the record the requester reads
to know they were heard.

**Declining requires a reason, enforced here rather than in the service.** A decline
with no reason is indistinguishable from neglect from the requester's side, and the
whole point of the surface is that an invisible queue reads as being ignored.

**Every connector declares its authority before it may write.** A Confluence page is not
an owner's OpenAPI sync, and a source whose tier was implicit would default to whatever
the code happened to pass. The column is NOT NULL with no default, so registering a
source without deciding is a database error rather than a silent tier.

**The ingest ceiling is per tenant and per source.** Unbounded ingest is a
denial-of-usefulness risk even when every individual claim is valid: a staging store
nobody can review is not better than an empty one. The breaker state lives on the row so
it survives a process restart -- a breaker that reset on deploy would reopen the tap
every time anybody shipped.
"""

from __future__ import annotations

from alembic import op

revision = "0043_capability_requests"
down_revision = "0041_drop_private_annotation_entry_kind"
branch_labels = None
depends_on = None


_REQUESTS = """
CREATE TABLE lmm_capability_request (
    request_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Who owns the capability being asked about, and who is asking. Both recorded,
    -- because the routing rule is "the subject's owner decides" and the requester
    -- must never be able to decide their own request.
    owner_tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    requester_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    requester_actor_id  UUID REFERENCES actors(actor_id),

    subject_entity_id UUID NOT NULL REFERENCES entities(entity_id),

    -- What kind of thing is being asked for. Shares the ontology's category
    -- mechanism rather than inventing a second taxonomy.
    request_category  TEXT NOT NULL,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL,

    status            TEXT NOT NULL DEFAULT 'raised',
    -- Set on every transition out of `raised`, so "who decided this and when" is
    -- answerable without reading the audit log.
    decided_by        UUID REFERENCES actors(actor_id),
    decided_at        TIMESTAMPTZ,
    decision_reason   TEXT,

    -- Where a request led to a graph change, the promotion that made it. This is
    -- what closes the loop visibly: the requester sees not just "accepted" but the
    -- change that resulted.
    resulting_promotion_id UUID REFERENCES lmm_promotion_journal(promotion_id),

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_lmm_request_status CHECK (
        status IN ('raised', 'acknowledged', 'accepted', 'declined', 'duplicate', 'resolved')
    ),
    CONSTRAINT ck_lmm_request_title CHECK (char_length(trim(title)) > 0),
    CONSTRAINT ck_lmm_request_body CHECK (char_length(trim(body)) > 0),
    -- A decline with no reason reads as neglect from the requester's side, and the
    -- surface exists precisely so being ignored is distinguishable from being
    -- answered.
    CONSTRAINT ck_lmm_request_decline_reason CHECK (
        status <> 'declined' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    -- A duplicate points somewhere; saying "duplicate" without saying of what is
    -- less useful than saying nothing.
    CONSTRAINT ck_lmm_request_duplicate_reason CHECK (
        status <> 'duplicate' OR char_length(trim(coalesce(decision_reason, ''))) > 0
    ),
    CONSTRAINT ck_lmm_request_decided CHECK (
        (decided_at IS NULL) = (decided_by IS NULL)
    ),
    -- Only an accepted or resolved request can have produced a change. A declined
    -- request linked to a promotion would describe a decision nobody made.
    CONSTRAINT ck_lmm_request_promotion CHECK (
        resulting_promotion_id IS NULL OR status IN ('accepted', 'resolved')
    )
)
"""

_REQUEST_INDEXES = [
    # The owner's queue: what is waiting on me. This is the read NF8.1 bounds.
    "CREATE INDEX ix_lmm_request_owner_open ON lmm_capability_request "
    "  (owner_tenant_id, created_at) WHERE status IN ('raised', 'acknowledged')",
    # The requester's view: what did I ask for and where has it got to.
    "CREATE INDEX ix_lmm_request_requester ON lmm_capability_request " "  (requester_tenant_id, created_at)",
    # Requests shown alongside the claims about the same capability.
    "CREATE INDEX ix_lmm_request_subject ON lmm_capability_request (subject_entity_id)",
]

# Append-only. A lifecycle whose history could be rewritten would let a request that
# was declined for a month read as though it had been acknowledged promptly.
#
# Ordered by an insertion sequence rather than by `occurred_at`. Two transitions can
# legitimately share a timestamp, and a history that sorted by timestamp alone would
# fall back to the primary key -- a random UUID, so the same history could read in
# either order on successive queries.
_TRANSITIONS = """
CREATE TABLE lmm_request_transition (
    transition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Insertion order, because two transitions can share a timestamp: a request
    -- acknowledged and declined in the same second is ordinary, and ordering those
    -- by a random primary key would show them in either order on any given read.
    seq           BIGSERIAL NOT NULL,
    request_id    UUID NOT NULL REFERENCES lmm_capability_request(request_id)
                    ON DELETE CASCADE,
    from_status   TEXT NOT NULL,
    to_status     TEXT NOT NULL,
    reason        TEXT,
    actor_id      UUID REFERENCES actors(actor_id),
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_lmm_transition_moves CHECK (from_status <> to_status)
)
"""

_TRANSITION_INDEXES = [
    # Keyed on the sequence rather than the timestamp, because that is the order the
    # history is read in.
    "CREATE INDEX ix_lmm_transition_request ON lmm_request_transition (request_id, seq)",
]

# One row per source a tenant has registered. `authority_tier` has no default on
# purpose: a source whose tier was implicit would inherit whatever the code passed.
_SOURCE_GOVERNANCE = """
CREATE TABLE lmm_source_governance (
    source_id       UUID PRIMARY KEY REFERENCES sync_sources(source_id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    authority_tier  TEXT NOT NULL,

    -- Claims this source may write per window. Enforced rather than advisory: a
    -- staging store nobody can review is not better than an empty one.
    ingest_ceiling  INTEGER NOT NULL DEFAULT 1000,
    window_seconds  INTEGER NOT NULL DEFAULT 3600,

    -- Breaker state on the row rather than in memory, so it survives a restart. A
    -- breaker that reset on deploy would reopen the tap every time anybody shipped.
    breaker_open_until TIMESTAMPTZ,
    breach_count    INTEGER NOT NULL DEFAULT 0,

    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_count    INTEGER NOT NULL DEFAULT 0,

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_lmm_source_authority CHECK (
        authority_tier IN (
            'owner_human', 'owner_extraction', 'owner_inference',
            'observer_human', 'observer_extraction', 'observer_inference',
            'unattributed'
        )
    ),
    CONSTRAINT ck_lmm_source_ceiling CHECK (ingest_ceiling > 0),
    CONSTRAINT ck_lmm_source_window CHECK (window_seconds > 0),
    CONSTRAINT ck_lmm_source_counts CHECK (window_count >= 0 AND breach_count >= 0)
)
"""

_SOURCE_INDEXES = [
    "CREATE INDEX ix_lmm_source_governance_tenant ON lmm_source_governance (tenant_id)",
    "CREATE INDEX ix_lmm_source_governance_open ON lmm_source_governance "
    "  (breaker_open_until) WHERE breaker_open_until IS NOT NULL",
]


def upgrade() -> None:
    op.execute(_REQUESTS)
    for statement in _REQUEST_INDEXES:
        op.execute(statement)
    op.execute(_TRANSITIONS)
    for statement in _TRANSITION_INDEXES:
        op.execute(statement)
    op.execute(_SOURCE_GOVERNANCE)
    for statement in _SOURCE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_source_governance")
    op.execute("DROP TABLE IF EXISTS lmm_request_transition")
    op.execute("DROP TABLE IF EXISTS lmm_capability_request")
