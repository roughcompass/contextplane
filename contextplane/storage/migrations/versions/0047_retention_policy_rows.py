"""The approved retention dispositions, as rows a tombstone can point at.

`source_tombstones` holds a foreign key into `retention_policies` on
`(policy_version, record_class)`, deliberately: a tombstone that cannot name the
approved policy it was written under is a record of an erasure nobody can check.
The table was created empty and nothing has ever filled it, so that foreign key
has refused *every* tombstone write since it existed — the compliance path was
dead at its first statement, and no test noticed because none of them ran an
erasure against a real database.

**Literal values, not a projection of the policy module.** The same twelve
dispositions live in `contextplane/retention/policies.py`, and the obvious move
is to import that module here and write whatever it currently says. That is the
wrong shape for the same reason the policy is versioned at all: a migration is
history. Re-running this revision on a fresh database years from now must
reproduce the rows that were approved under `CP-POLICY-2026-08-A`, not whatever
the module has since become — otherwise a database migrated today and one
migrated later disagree about what the same policy version *was*, and every
tombstone naming that version becomes unreadable. Correcting a value is a new
policy version and a new revision, never an edit to this one.

That leaves the two copies free to drift, so a test pins them: it reads these
rows back from a migrated database and compares them field by field against the
module's dispositions for this version. Drift fails there, loudly, rather than
here, silently.

**The downgrade will refuse while tombstones exist, and should.** Deleting a
policy row that a tombstone still points at breaks the reference that makes the
tombstone meaningful. Postgres refuses it; the alternative — cascading — would
delete the erasure records themselves to make a schema downgrade succeed.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0047_retention_policy_rows"
down_revision: str | None = "0044_signal_reference_bindings"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The policy version these rows were approved under. A later correction ships as
#: a new version in its own revision; this constant is never edited.
_POLICY_VERSION = "CP-POLICY-2026-08-A"

# The three verifier sentences, spelled once each because they repeat across
# classes and a hand-copied sentence that differs by a word reads as a different
# rule to whoever is deciding whether an implementation matches it.
_VERIFIER_STRUCTURAL = (
    "structural integrity and tombstone metadata only: that the record existed, "
    "its chain position and internally-held digest are intact, and it was erased "
    "on the recorded date under the recorded policy version. Never the erased "
    "content, its size or shape, or any subject identity beyond the derived id."
)
_VERIFIER_NONE = "nothing beyond the record's own existence; this class carries no erasure disclosure of its own."
_VERIFIER_EXEMPT = (
    "the record itself, unmodified: it carries no values and its subject "
    "references are one-way derived ids, so there is nothing to withhold."
)

#: One row per record class: what holds it, for how long, what erasure does to it,
#: and what may be said afterwards. A NULL period is bounded by tenant or workspace
#: deletion rather than by a duration, which is a different statement from "kept
#: forever" and is why it is not stored as a very large number.
_ROWS: tuple[dict[str, object], ...] = (
    {
        "record_class": "task_checkpoint",
        "legal_basis": "contract performance",
        "retention_days": None,
        "erasure_mode": "minimize_and_tombstone",
        "minimization_action": (
            "clear the body fields (goal, decisions, assumptions, evidence, "
            "completed checks, open questions, next action); keep id, tenant, "
            "sequence, predecessor linkage, digest and recorded_at"
        ),
        "tombstone_behaviour": "one tombstone per erased checkpoint, holding no part of the body",
        "verifier_disclosure": _VERIFIER_STRUCTURAL,
    },
    {
        "record_class": "context_receipt",
        "legal_basis": "legitimate interest (verification)",
        "retention_days": 730,
        "erasure_mode": "minimize_and_tombstone",
        "minimization_action": (
            "minimize the receipt's items and exclusions; keep the envelope and its resolution facts"
        ),
        "tombstone_behaviour": "one tombstone per minimized receipt",
        "verifier_disclosure": _VERIFIER_STRUCTURAL,
    },
    {
        "record_class": "receipt_item",
        "legal_basis": "legitimate interest (verification)",
        "retention_days": None,
        "erasure_mode": "minimize",
        "minimization_action": (
            "replace item_key with a tenant-keyed erased marker; keep block, source and the item's contract id"
        ),
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "receipt_exclusion",
        "legal_basis": "legitimate interest (verification)",
        "retention_days": None,
        "erasure_mode": "minimize",
        "minimization_action": (
            "replace item_key with a tenant-keyed erased marker; keep block and the withholding reason"
        ),
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "external_signal",
        "legal_basis": "legitimate interest",
        "retention_days": 730,
        "erasure_mode": "delete",
        "minimization_action": "clear the payload and evidence handle at the payload clock; the envelope outlives it",
        "tombstone_behaviour": "one tombstone per erased signal, so dependents can be invalidated by cause",
        "verifier_disclosure": _VERIFIER_STRUCTURAL,
    },
    {
        "record_class": "context_feedback",
        "legal_basis": "contract performance",
        "retention_days": 730,
        "erasure_mode": "minimize",
        "minimization_action": "clear the free-text note; the discriminant, rating and receipt linkage survive",
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "memory_claim",
        "legal_basis": "legitimate interest",
        "retention_days": None,
        "erasure_mode": "minimize",
        "minimization_action": (
            "minimize excerpts, invalidate the claim, retain the shell for audit and serve it nowhere"
        ),
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "derivative",
        "legal_basis": "inherited from every source",
        "retention_days": None,
        "erasure_mode": "delete",
        "minimization_action": "redact where the derivative's kind supports it, delete where it does not",
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "audit_log",
        "legal_basis": "legitimate interest (accountability)",
        "retention_days": 1095,
        "erasure_mode": "exempt",
        "minimization_action": None,
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_EXEMPT,
    },
    {
        "record_class": "pii_detection_log",
        "legal_basis": "legitimate interest",
        "retention_days": 730,
        "erasure_mode": "exempt",
        "minimization_action": None,
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_EXEMPT,
    },
    {
        "record_class": "export",
        "legal_basis": "contract performance",
        "retention_days": 30,
        "erasure_mode": "delete",
        "minimization_action": None,
        "tombstone_behaviour": None,
        "verifier_disclosure": _VERIFIER_NONE,
    },
    {
        "record_class": "workspace_entry",
        "legal_basis": "contract performance",
        "retention_days": None,
        "erasure_mode": "delete",
        "minimization_action": None,
        "tombstone_behaviour": "one tombstone per deleted entry, so the deletion is accountable",
        "verifier_disclosure": _VERIFIER_STRUCTURAL,
    },
)

_INSERT = """
INSERT INTO retention_policies (
    policy_version, record_class, legal_basis, retention_days,
    erasure_mode, minimization_action, tombstone_behaviour, verifier_disclosure
)
VALUES (
    :policy_version, :record_class, :legal_basis, :retention_days,
    :erasure_mode, :minimization_action, :tombstone_behaviour, :verifier_disclosure
)
"""


def upgrade() -> None:
    """Insert the twelve approved dispositions for this policy version.

    Plainly, with no conflict handling: a row already present under this version
    was written by something other than this revision, and the two may disagree
    about an approved value. Failing is the only outcome that surfaces that.
    """
    bind = op.get_bind()
    for row in _ROWS:
        bind.execute(text(_INSERT), {"policy_version": _POLICY_VERSION, **row})


def downgrade() -> None:
    """Remove this version's rows, and refuse while any tombstone still names one."""
    op.execute(f"DELETE FROM retention_policies WHERE policy_version = '{_POLICY_VERSION}'")  # noqa: S608
