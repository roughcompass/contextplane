"""Admit a promoted canonical claim as ARC source evidence.

Source admission had exactly two authorities: a configured connector fetched
bytes from an allowlisted location, or an authorized upload carried bytes the
caller sent. Both assume the governing artifact lives outside Context Plane
and has to be pulled in and vouched for on the way.

For a development workflow that assumption produces a dead end. The five
shipped ingest connectors all target the GitHub API, so a team on any other
host has no connector to register; and the upload path needs an enrolled
verifier and a signature produced outside the browser, for a document whose
real approval was a reviewed merge. Meanwhile the same artifact frequently is
already known here: an agent asserted it, and a second actor promoted that
claim onto the canonical graph through `memory_promotion_journal`. ARC could
not cite what the rest of the product already treats as true.

This adds a third authority, `graph_promotion`, whose verification method is
`graph_promoted`. What vouches for the evidence is the promotion journal row
-- an actor who is not the claim's author moved it onto the graph, and the
reversal columns record if that decision was later withdrawn. That is a real
chain of custody, and it is deliberately *not* spelled as one of the existing
two: recording a promotion as an `authorized_upload` nobody uploaded, or as
`source_signed` with no signature, would put a false provenance in a record
whose only purpose is to be true under audit.

Four constraints encoded the old two-authority vocabulary and are widened
here rather than dropped:

- the `verification_method` and `admission_method` value sets gain one
  literal each;
- the representation rule gains a branch requiring *both* `signature` and
  `verifier_attestation` to be NULL for `graph_promoted`, keeping the
  existing invariant that evidence cannot carry a proof shape it did not
  declare -- a promotion declares neither;
- the admission-target rule gains a branch requiring both `connector_id` and
  `policy_id` to be NULL, since a promotion is admitted through neither
  registration.

The columns themselves are untouched: `verification_method` and
`admission_method` are already TEXT, and the promotion is identified through
`verifier_id`, which every admission already carries. No existing row changes
and no existing digest is recomputed -- widening an accepted value set does
not alter how a document that never uses the new value canonicalizes.

Downgrade restores the narrower constraints. It fails, by design, if any
`graph_promotion` evidence exists: those rows cannot be re-expressed as a
fetch or an upload, and silently deleting admitted evidence to make a schema
move fit would destroy the audit record the table exists to keep.
"""

from __future__ import annotations

from alembic import op

revision = "0056_arc_graph_promoted_source_evidence"
down_revision: str | None = "0055_legal_hold_ceiling_absolute"
branch_labels: str | None = None
depends_on: str | None = None


_WIDENED = (
    (
        "ck_arc_source_evidence_verification_method",
        "verification_method IN ('source_signed', 'verifier_attested', 'graph_promoted')",
    ),
    (
        "ck_arc_source_evidence_admission_method",
        "admission_method IN ('configured_connector', 'authorized_upload', 'graph_promotion')",
    ),
    (
        "ck_arc_source_evidence_representation",
        """
        (verification_method = 'source_signed'
         AND signature IS NOT NULL AND verifier_attestation IS NULL)
        OR (verification_method = 'verifier_attested'
            AND verifier_attestation IS NOT NULL AND signature IS NULL)
        OR (verification_method = 'graph_promoted'
            AND signature IS NULL AND verifier_attestation IS NULL)
        """,
    ),
    (
        "ck_arc_source_evidence_admission_targets",
        """
        (admission_method = 'configured_connector' AND connector_id IS NOT NULL AND policy_id IS NULL)
        OR (admission_method = 'authorized_upload' AND policy_id IS NOT NULL AND connector_id IS NULL)
        OR (admission_method = 'graph_promotion' AND connector_id IS NULL AND policy_id IS NULL)
        """,
    ),
)

_NARROW = (
    (
        "ck_arc_source_evidence_verification_method",
        "verification_method IN ('source_signed', 'verifier_attested')",
    ),
    (
        "ck_arc_source_evidence_admission_method",
        "admission_method IN ('configured_connector', 'authorized_upload')",
    ),
    (
        "ck_arc_source_evidence_representation",
        """
        (verification_method = 'source_signed'
         AND signature IS NOT NULL AND verifier_attestation IS NULL)
        OR (verification_method = 'verifier_attested'
            AND verifier_attestation IS NOT NULL AND signature IS NULL)
        """,
    ),
    (
        "ck_arc_source_evidence_admission_targets",
        """
        (admission_method = 'configured_connector' AND connector_id IS NOT NULL AND policy_id IS NULL)
        OR (admission_method = 'authorized_upload' AND policy_id IS NOT NULL AND connector_id IS NULL)
        """,
    ),
)


def _replace(constraints: tuple[tuple[str, str], ...]) -> None:
    for name, expression in constraints:
        op.execute(f"ALTER TABLE arc_source_approval_evidence DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE arc_source_approval_evidence ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    _replace(_WIDENED)


def downgrade() -> None:
    # Re-adding the narrow constraint is itself the guard: PostgreSQL validates
    # a new CHECK against every existing row, so a table holding promotion-
    # admitted evidence refuses the downgrade instead of losing it.
    _replace(_NARROW)
