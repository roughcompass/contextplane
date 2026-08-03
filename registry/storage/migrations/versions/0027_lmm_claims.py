"""Staged claims: typed, entity-linked, provenanced triples that are not truth.

A claim is `(subject_entity, predicate, value)` with a declared type. Free text
is never the claim -- the originating text survives only as provenance -- because
a claim that is prose cannot be corroborated, contradicted, or compared, which
is the entire reason for having claims rather than documents.

**Staged, and unmistakably so.** Nothing here is served through the capability
read paths, and there is no route from a claim to the canonical graph at all
until a later phase builds one. `status` is on every row so no reader can lose
track of which side of that line it is on.

**Provenance is a join table, not an array column.** The requirement leaves the
shape open and asks for exactly one mechanism. A join table is what makes
resolution work in both directions: from a claim to its evidence, and from a
piece of evidence to everything derived from it. The reverse direction is the
one an array column cannot index, and it is the direction that matters when a
session event is erased and its descendants have to be found.

**Unresolvable subjects are stored, not dropped and not guessed.** A claim whose
subject does not resolve to a real entity is `unlinked`: excluded from scoring,
consolidation, promotion and serving, and surfaced for a human. Dropping it
loses information nobody knows is missing; guessing attaches an assertion to the
wrong thing, which is worse than losing it.
"""

from __future__ import annotations

from alembic import op

revision = "0027_lmm_claims"
down_revision = "0026_global_claim_predicates"
branch_labels = None
depends_on = None


_CLAIMS_DDL = """
CREATE TABLE lmm_claims (
    claim_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The tenant that owns the *subject*, not the tenant that authored the
    -- claim. A claim about somebody else's capability is governed by them.
    owning_tenant_id    UUID REFERENCES tenants(tenant_id),
    author_tenant_id    UUID NOT NULL REFERENCES tenants(tenant_id),
    author_actor_id     UUID REFERENCES actors(actor_id),

    -- NULL exactly when the subject could not be resolved. The reference text
    -- is kept either way so a curator can see what the claim was about.
    subject_entity_id   UUID REFERENCES entities(entity_id),
    subject_reference   TEXT NOT NULL,

    predicate           TEXT NOT NULL,
    value_type          TEXT NOT NULL,
    claim_category      TEXT NOT NULL,

    -- One canonical key so equality is byte-comparable. A later phase decides
    -- whether a new claim is a genuine change or a restatement, and that
    -- decision cannot depend on JSON key ordering.
    value_jsonb         JSONB NOT NULL,

    -- When the claim *holds*, which is not when the row was written. A claim
    -- can be recorded today about something that was true last quarter.
    asserted_valid_from TIMESTAMPTZ NOT NULL,
    asserted_valid_to   TIMESTAMPTZ,

    status              TEXT NOT NULL,
    visibility          TEXT NOT NULL,
    source_authority    TEXT NOT NULL,

    -- The numerator of the compression story: how much smaller the claim is
    -- than the text it came from. Recorded per row because the corpus moves.
    size_bytes          INTEGER NOT NULL,
    token_count         INTEGER,
    tokenizer_id        TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_lmm_claims_status CHECK (
        status IN ('staged', 'unlinked', 'superseded', 'rejected')
    ),
    -- The two must agree. A claim with no subject that calls itself staged
    -- would enter scoring and promotion with nothing to attach to.
    CONSTRAINT ck_lmm_claims_unlinked CHECK (
        (subject_entity_id IS NULL) = (status = 'unlinked')
    ),
    -- An owning tenant is derived from the subject, so an unlinked claim has
    -- none to derive.
    CONSTRAINT ck_lmm_claims_owner CHECK (
        (owning_tenant_id IS NULL) = (subject_entity_id IS NULL)
    ),
    CONSTRAINT ck_lmm_claims_visibility CHECK (
        visibility IN ('private', 'tenant-shared', 'public')
    ),
    CONSTRAINT ck_lmm_claims_interval CHECK (
        asserted_valid_to IS NULL OR asserted_valid_to > asserted_valid_from
    ),
    -- `null` is never a value. An unknown is the absence of a claim, not a
    -- claim of nothing, and the two must not be storable as the same row.
    CONSTRAINT ck_lmm_claims_value_not_null CHECK (
        jsonb_typeof(value_jsonb) <> 'null'
    ),
    CONSTRAINT ck_lmm_claims_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_lmm_claims_tokenizer CHECK (
        (token_count IS NULL) = (tokenizer_id IS NULL)
    )
)
"""

_CLAIMS_INDEXES = [
    # The lookup the serving phase is built on: what do we believe about this
    # subject, under this predicate.
    "CREATE INDEX ix_lmm_claims_subject_predicate ON lmm_claims "
    "(subject_entity_id, predicate) WHERE status = 'staged'",
    # The curation queue. Partial because unlinked is the rare case and the
    # queue should not scan the whole corpus to find it.
    "CREATE INDEX ix_lmm_claims_unlinked ON lmm_claims (author_tenant_id, created_at) "
    "WHERE status = 'unlinked'",
    "CREATE INDEX ix_lmm_claims_owning_tenant ON lmm_claims (owning_tenant_id, predicate)",
    # Erasure and provenance walks start from the author.
    "CREATE INDEX ix_lmm_claims_author ON lmm_claims (author_actor_id)",
]

# One mechanism, both directions. A claim resolves to its evidence and a piece
# of evidence resolves to everything derived from it -- the second is what an
# erasure request needs, and what an array column on the claim could not index.
_PROVENANCE_DDL = """
CREATE TABLE lmm_claim_provenance (
    claim_id        UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,

    -- What kind of evidence, and its identifier within that kind. Kept as a
    -- pair rather than separate nullable columns per source type: a new
    -- evidence kind should not require a migration, and a row can only ever
    -- point at one thing.
    evidence_kind   TEXT NOT NULL,
    evidence_ref    TEXT NOT NULL,

    -- The originating text, where there was one. This is where prose lives:
    -- the claim is typed, and the sentence it came from survives here so a
    -- human can check the extraction rather than trust it.
    evidence_excerpt TEXT,

    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (claim_id, evidence_kind, evidence_ref),

    CONSTRAINT ck_lmm_prov_kind CHECK (
        evidence_kind IN (
            'session_event', 'document_revision', 'commit', 'work_item', 'connector_run', 'curator'
        )
    )
)
"""

_PROVENANCE_INDEXES = [
    # The reverse direction: everything derived from one piece of evidence.
    "CREATE INDEX ix_lmm_prov_evidence ON lmm_claim_provenance (evidence_kind, evidence_ref)",
]


def upgrade() -> None:
    op.execute(_CLAIMS_DDL)
    for statement in _CLAIMS_INDEXES:
        op.execute(statement)
    op.execute(_PROVENANCE_DDL)
    for statement in _PROVENANCE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_claim_provenance")
    op.execute("DROP TABLE IF EXISTS lmm_claims")
