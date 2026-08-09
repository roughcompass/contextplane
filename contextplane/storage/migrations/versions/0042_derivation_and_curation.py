"""What was derived from what, and what happens when two derivations disagree.

A derivation attempt is the record that some evidence was read and an assertion
came out. It is kept whether or not it produced anything, because "we looked and
concluded nothing" and "we never looked" are different states and only one of
them should be retried.

**Authority and classification are carried on both sides, per evidence item and
per attempt.** A derived claim may inherit at most the authority its weakest
source had -- a CI system asserting a run failed does not license a claim that a
change was wrong. The ceiling cannot be a CHECK, because authority is a source-
issued string with no ordering the database knows; what the schema can do is make
the ceiling *computable and auditable* by refusing to let an attempt exist
without its own recorded authority while every evidence link records the
authority it came with. A reviewer can then ask whether the attempt claimed more
than its inputs allowed, which is impossible when only the result is stored.

**Evidence links are a discriminated union for the same reason feedback is.** An
attempt cites signals, receipts, exact receipt items, external references, or
immutable checkpoints, and each of those needs a different pointer. One table
with every pointer nullable and no discriminant would leave "what is this link
evidence of?" answerable only by inspecting which column is filled. The exact
receipt item is again a composite reference against the receipt's own items, so
an attempt cannot cite an item belonging to a different receipt -- the failure a
single-column pointer permits while every column looks individually valid.

**Excerpts are bounded and are never a workspace copy.** The extractor is allowed
to keep the smallest quotation that makes an assertion checkable; it is not
allowed to copy the workspace content it read. There is no workspace column here
and there is a test asserting there is none, because the column that creates that
leak would look entirely reasonable in the diff that added it.

**A curation case routes a contradiction to an owner; it never writes the thing
it is about.** Each disposition names its own approval authority and evidence
threshold, and those are required the moment a disposition is recorded -- a
proposal without a named authority is a decision nobody is accountable for. No
column here points at a canonical target to be written, deliberately: dispositions
are proposals, and the surfaces that act on them have their own approval paths.
"""

from __future__ import annotations

from alembic import op

revision = "0042_derivation_and_curation"
down_revision: str | None = "0041_discriminated_feedback"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Where an attempt is in its life. `pending` and `rejected` are both terminal
# statements about the attempt rather than the claim: one has not concluded, the
# other concluded against. Neither may carry a created claim.
_DERIVATION_STATUSES = "'pending', 'staged', 'rejected', 'superseded', 'invalidated'"

# What a link points at. Closed so a sixth kind cannot appear by a writer passing
# a new string and leaving every pointer column NULL.
_EVIDENCE_KINDS = "'signal', 'receipt', 'receipt_item', 'external_reference', 'checkpoint'"

# The same four handling classes the rest of the schema closes.
_CLASSIFICATIONS = "'public', 'internal', 'confidential', 'restricted'"

# What a curator may decide. The three `propose_*` members are proposals to other
# surfaces, each with its own approval authority; none of them writes a target.
_DISPOSITIONS = "'confirm', 'reject', 'supersede', 'propose_canonical', 'propose_runbook', 'propose_arc'"

_CASE_STATUSES = "'open', 'routed', 'resolved'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE claim_derivations (
            derivation_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),

            -- Which extractor, and which version of it. Both, because an
            -- assertion that a later version would not have made must be
            -- identifiable without re-running anything.
            profile           TEXT NOT NULL,
            profile_version   TEXT NOT NULL,

            status            TEXT NOT NULL,

            -- Where the assertion is claimed to hold. Recorded rather than
            -- inferred from the evidence, because narrowing applicability is a
            -- judgement the extractor made and a later reader must see it.
            applicability     TEXT NOT NULL,
            -- The normalized assertion, digested. Two attempts that concluded
            -- the same thing collide here rather than in a text comparison two
            -- readers would disagree about.
            assertion_digest  TEXT NOT NULL,

            -- The ceiling this attempt claimed for itself. See the module
            -- docstring: not enforceable as a CHECK, deliberately stored so the
            -- comparison against the evidence below can be made at all.
            source_authority  TEXT NOT NULL,
            classification    TEXT NOT NULL,

            -- The claim this attempt produced, when it produced one.
            created_claim_id  UUID REFERENCES memory_claims(claim_id),

            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_derivation_status CHECK (status IN ({_DERIVATION_STATUSES})),
            CONSTRAINT ck_derivation_classification CHECK (classification IN ({_CLASSIFICATIONS})),
            CONSTRAINT ck_derivation_identity_present
                CHECK (
                    length(profile) > 0
                    AND length(profile_version) > 0
                    AND length(applicability) > 0
                    AND length(assertion_digest) > 0
                    AND length(source_authority) > 0
                ),
            -- An attempt that has not concluded, or concluded against, created
            -- nothing. Storing a claim id on either is how a rejected assertion
            -- acquires a citation it was never entitled to.
            CONSTRAINT ck_derivation_unconcluded_creates_nothing
                CHECK (status NOT IN ('pending', 'rejected') OR created_claim_id IS NULL)
        )
        """
    )
    # The same assertion, from the same extractor version, is one attempt.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_derivation_assertion
            ON claim_derivations (tenant_id, profile, profile_version, assertion_digest)
        """
    )
    # The retry sweep's own selection: attempts that have not concluded.
    op.execute(
        """
        CREATE INDEX ix_derivation_pending
            ON claim_derivations (tenant_id, created_at DESC)
            WHERE status = 'pending'
        """
    )

    op.execute(
        f"""
        CREATE TABLE derivation_evidence_links (
            link_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Evidence belongs to its attempt: deleting the attempt takes the
            -- links with it, because a link to nothing is not evidence of
            -- anything.
            derivation_id    UUID NOT NULL REFERENCES claim_derivations(derivation_id) ON DELETE CASCADE,

            evidence_kind    TEXT NOT NULL,

            -- One pointer per kind; the discriminant below says which must be
            -- present and which must be absent.
            signal_id        UUID REFERENCES external_signals(signal_id),
            receipt_id       UUID,
            receipt_item_id  TEXT,
            reference_id     UUID REFERENCES context_external_references(reference_id),
            -- Checkpoints are cited by immutable id *and* digest: the id says
            -- which one, the digest says it had not changed when it was read.
            checkpoint_id    UUID,
            checkpoint_digest TEXT,

            -- What this particular source was entitled to assert, at the
            -- authority it carried. The attempt's own ceiling is checked against
            -- these rather than assumed from the strongest one.
            source_authority TEXT NOT NULL,
            classification   TEXT NOT NULL,

            -- The smallest quotation that makes the assertion checkable. Never a
            -- workspace copy; see the module docstring.
            excerpt          TEXT,

            CONSTRAINT ck_evidence_kind CHECK (evidence_kind IN ({_EVIDENCE_KINDS})),
            CONSTRAINT ck_evidence_classification CHECK (classification IN ({_CLASSIFICATIONS})),
            CONSTRAINT ck_evidence_authority_present CHECK (length(source_authority) > 0),

            -- Each kind names exactly what it cites and what it must not. A row
            -- matching none of them has no referent and is refused rather than
            -- stored as evidence pointing nowhere.
            CONSTRAINT ck_evidence_discriminant
                CHECK (
                    (evidence_kind = 'signal'
                        AND signal_id IS NOT NULL
                        AND receipt_id IS NULL AND receipt_item_id IS NULL
                        AND reference_id IS NULL AND checkpoint_id IS NULL)
                    OR (evidence_kind = 'receipt'
                        AND receipt_id IS NOT NULL AND receipt_item_id IS NULL
                        AND signal_id IS NULL AND reference_id IS NULL AND checkpoint_id IS NULL)
                    OR (evidence_kind = 'receipt_item'
                        AND receipt_id IS NOT NULL AND receipt_item_id IS NOT NULL
                        AND signal_id IS NULL AND reference_id IS NULL AND checkpoint_id IS NULL)
                    OR (evidence_kind = 'external_reference'
                        AND reference_id IS NOT NULL
                        AND signal_id IS NULL AND receipt_id IS NULL
                        AND receipt_item_id IS NULL AND checkpoint_id IS NULL)
                    OR (evidence_kind = 'checkpoint'
                        AND checkpoint_id IS NOT NULL AND checkpoint_digest IS NOT NULL
                        AND signal_id IS NULL AND receipt_id IS NULL
                        AND receipt_item_id IS NULL AND reference_id IS NULL)
                ),

            -- The exact-item rule, as a pair. Silent when either column is NULL,
            -- which is what lets the other four kinds share this table.
            CONSTRAINT fk_evidence_exact_receipt_item
                FOREIGN KEY (receipt_id, receipt_item_id)
                REFERENCES context_receipt_items (receipt_id, receipt_item_id),
            -- Receipt-level evidence still names a receipt that exists; the
            -- composite key above cannot carry that, being silent in exactly
            -- that shape.
            CONSTRAINT fk_evidence_receipt
                FOREIGN KEY (receipt_id) REFERENCES context_receipts (receipt_id)
        )
        """
    )
    # "What did this attempt read?" -- the audit read, and the one an authority
    # review runs before trusting a staged claim.
    op.execute("CREATE INDEX ix_evidence_by_derivation ON derivation_evidence_links (derivation_id)")
    # "What was derived from this signal?" -- the revocation sweep's own path.
    op.execute(
        "CREATE INDEX ix_evidence_by_signal ON derivation_evidence_links (signal_id) WHERE signal_id IS NOT NULL"
    )

    op.execute(
        f"""
        CREATE TABLE curation_cases (
            case_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),

            -- The axis the contradiction is on: two assertions about the same
            -- subject and predicate that cannot both stand.
            subject_reference   TEXT NOT NULL,
            predicate           TEXT NOT NULL,

            -- What raised it, when a derivation did. NULL when a curator opened
            -- the case directly, which is a real path and not a defect.
            raised_by_derivation_id UUID REFERENCES claim_derivations(derivation_id),

            status              TEXT NOT NULL,
            -- Who it is routed to. Contradiction that reaches nobody is
            -- contradiction that stays, so routing is recorded rather than
            -- implied by a queue position.
            owner_id            TEXT,
            routed_at           TIMESTAMPTZ,

            disposition         TEXT,
            -- The authority entitled to approve *this* disposition, and the
            -- evidence it requires. Distinct per disposition on purpose: what
            -- may confirm a claim is not what may propose a canonical write.
            approval_authority  TEXT,
            evidence_threshold  TEXT,

            resolved_at         TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_case_status CHECK (status IN ({_CASE_STATUSES})),
            CONSTRAINT ck_case_disposition CHECK (disposition IS NULL OR disposition IN ({_DISPOSITIONS})),
            CONSTRAINT ck_case_axis_present
                CHECK (length(subject_reference) > 0 AND length(predicate) > 0),

            -- A disposition without a named approving authority and a stated
            -- evidence threshold is a decision nobody is accountable for.
            CONSTRAINT ck_case_disposition_names_its_authority
                CHECK (
                    disposition IS NULL
                    OR (approval_authority IS NOT NULL AND length(approval_authority) > 0
                        AND evidence_threshold IS NOT NULL AND length(evidence_threshold) > 0)
                ),
            -- Resolved means decided. A case cannot leave the queue without
            -- saying what was decided.
            CONSTRAINT ck_case_resolved_has_a_disposition
                CHECK (status <> 'resolved' OR disposition IS NOT NULL),
            -- Routed means routed to somebody.
            CONSTRAINT ck_case_routed_has_an_owner
                CHECK (status <> 'routed' OR (owner_id IS NOT NULL AND length(owner_id) > 0))
        )
        """
    )
    # The curator's own queue: what is open or routed, oldest first, because a
    # contradiction that ages is the one worth surfacing.
    op.execute(
        """
        CREATE INDEX ix_curation_open_cases
            ON curation_cases (tenant_id, created_at)
            WHERE status <> 'resolved'
        """
    )
    # "Is there an unresolved case on this axis?" -- asked before staging another
    # assertion about the same subject and predicate.
    op.execute("CREATE INDEX ix_curation_by_axis ON curation_cases (tenant_id, subject_reference, predicate)")


def downgrade() -> None:
    # Dropped in dependency order: cases and links reference derivations.
    op.execute("DROP TABLE IF EXISTS curation_cases")
    op.execute("DROP TABLE IF EXISTS derivation_evidence_links")
    op.execute("DROP TABLE IF EXISTS claim_derivations")
