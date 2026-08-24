"""The thirteenth approved disposition: `session_event`.

E6-T2. `memory_session_events` advertised a retention period, carried
`expires_at` on every row and an index built to sweep on it, and nothing swept.
Bringing it under `retention_policies` is what gives it a legal basis, an
erasure mode, and a hold that can protect it.

**This is an insert, not an edit, and the distinction is the one 0047's rule
turns on.** That migration says correcting a value is a new `POLICY_VERSION`
rather than an in-place change, because *"every tombstone and every registered
derivative names the version it was decided under, and a value that moved
underneath them would make those references unreadable."*

Nothing references `session_event` under `CP-POLICY-2026-08-A`, because the
class did not exist until now. There is no prior value to move and no reference
to break — so the rule that protects moved values does not reach a class being
added, and minting a new policy version would instead force re-propagation of
twelve dispositions that did not change.

The literals are duplicated from `contextplane/retention/policies.py` on purpose,
the same way 0047 duplicates the other twelve: history stays reproducible from
the migration alone, and
`test_every_approved_disposition_reached_the_database` is where the two copies
are held together.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0080_session_event_retention_policy"
down_revision: str | None = "0079_source_grant_revocation"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_POLICY_VERSION = "CP-POLICY-2026-08-A"

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

#: 180 is the class ceiling, and the same bound `tenants.memory_retention_days`
#: is already CHECK-constrained to. A tenant's integer is its choice *within*
#: this class; the sweep reads the `expires_at` that choice produced, so the
#: policy states the maximum rather than the period every row will actually use.
_ROW = {
    "policy_version": _POLICY_VERSION,
    "record_class": "session_event",
    "legal_basis": "contract performance",
    "retention_days": 180,
    "erasure_mode": "delete",
    "minimization_action": (
        "delete the event outright; there is no envelope worth keeping "
        "once the body is gone, unlike a receipt or a signal"
    ),
    # No tombstone. A session event's disposal is already evidenced by what
    # outlives it: claims extracted from a session survive its erasure and carry
    # a digest of the session they came from.
    "tombstone_behaviour": None,
    "verifier_disclosure": (
        "nothing beyond the record's own existence; " "this class carries no erasure disclosure of its own."
    ),
}


def upgrade() -> None:
    op.get_bind().execute(text(_INSERT), _ROW)


def downgrade() -> None:
    op.get_bind().execute(
        text("DELETE FROM retention_policies WHERE policy_version = :v AND record_class = :c"),
        {"v": _POLICY_VERSION, "c": _ROW["record_class"]},
    )
