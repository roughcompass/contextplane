"""Participation may be granted, revoked and granted again.

E7-T5. `uq_task_participant_grant` is `(tenant_id, intent_id, actor_id)` with no
temporal component, and revoke is a soft delete -- `revoke_grant` sets
`expires_at` and keeps the row, because the module that owns it says so in as
many words:

    **Revocation is temporal, never a delete.** The row stays and stops
    applying. A deleted grant erases the fact that the actor ever had access,
    which is exactly the fact an audit of a past read needs.

Those two facts are incompatible. A revoked participant cannot be granted
participation again: the second insert collides with the retained first row, and
because both adapters catch only `AudienceDenied`, the caller gets an unhandled
`IntegrityError` -- a 500 -- rather than a refusal. **Participation was
effectively one-shot per actor per task**, and nothing tested it.

**The fix is the constraint this table should have had, and the repository has
already made this exact decision once.** Migration 0052 replaced a unique
constraint on `relationship_metadata` with a temporal exclusion, and its comment
states the case without needing a word changed:

    Written as a temporal exclusion rather than a unique constraint because the
    same relationship may legitimately be asserted, ended and re-asserted --
    what may not exist is two in force at once, which is when "is this in
    force?" acquires two answers.

Substitute "grant" for "relationship" and that is this migration. `btree_gist`
is already installed, by the profile-publication revision, so the equality
operators on `uuid` and `text` are available to the gist index.

**Why the alternative was rejected.** The obvious cheaper fix is an upsert:
re-grant reactivates the existing row, one row per actor per task forever. It
preserves every read unchanged and it is wrong, because overwriting
`granted_at`, `granted_by` and `expires_at` destroys precisely the fact the
module's docstring says the soft delete exists to keep. It is a delete wearing
an update, and `list_grants` -- *"an audit of a past read needs the grants that
applied then"* -- is the reader it would lie to.

**The cost the plan entry predicted does not exist.** E7-T5 warned that letting
revoked rows accumulate means "every audience read has to learn to ignore the
dead rows, and a missed predicate is a revoked participant who can still read".
That work is already done: `_active_grant_predicate` is the single definition of
"this actor participates right now", every read composes it rather than
restating it, and `_AUDIENCE_EXISTS` inlines the same window into SQL. The reads
were already temporal; only the constraint was not.

**One subtlety worth stating, because it is load-bearing in the other
direction.** `fetch_actor_role` ends in `scalar_one_or_none()`, which raises if
two rows match. With this constraint that cannot happen -- at most one grant's
window contains any given instant -- so the exclusion is not merely permitting
history, it is what keeps that existing read correct now that several rows may
exist. `ck_grant_window` (`expires_at > granted_at`) does the supporting work:
without it a zero-width range would be empty, and an empty range overlaps
nothing, so a degenerate row could slip past the exclusion entirely.
"""

from __future__ import annotations

from alembic import op

revision = "0070_grant_temporal_exclusion"
down_revision: str | None = "0069_receipt_hydration_state"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "ex_intent_participant_grants_no_overlap"

#: The window is half-open by `tstzrange`'s default bounds, `[)`, which is the
#: reading revoke already implements: `revoke_grant` writes `expires_at = moment`
#: and every read asks for `expires_at > moment`, so the instant of revocation
#: belongs to nobody. A closed upper bound would make the revoking instant
#: overlap the re-granting one and refuse a same-instant re-grant, which is a
#: legitimate sequence under a fake clock and in a fast test.
_ADD_EXCLUSION = f"""
ALTER TABLE intent_participant_grants
    ADD CONSTRAINT {_CONSTRAINT}
    EXCLUDE USING gist (
        tenant_id WITH =,
        intent_id WITH =,
        actor_id WITH =,
        tstzrange(granted_at, expires_at) WITH &&
    )
"""


#: `uq_task_participant_grant` is a unique *index*, not a table constraint --
#: migration 0030 created it with `CREATE UNIQUE INDEX`. The ORM model called it
#: a `UniqueConstraint`, which is what the first draft of this migration
#: believed, and `DROP CONSTRAINT` duly failed against a real database. Named
#: here so the two halves below cannot drift apart again.
_OLD_INDEX = "uq_task_participant_grant"


def upgrade() -> None:
    # Dropped first. Adding the exclusion while the unique index still stands
    # would leave a window in which the table rejects the very sequence this
    # migration exists to allow, and the two together are simply the stricter of
    # the two.
    op.execute(f"DROP INDEX {_OLD_INDEX}")
    op.execute(_ADD_EXCLUSION)


def downgrade() -> None:
    # Reversing this can fail, and failing is correct. Once participation has
    # been granted, revoked and re-granted, two rows exist for one
    # (tenant, intent, actor) and the unique index cannot be rebuilt without
    # deleting one -- which would destroy the audit fact the exclusion was
    # installed to preserve. A downgrade that silently dropped history in order
    # to succeed would be the worst available outcome, so this lets the index
    # build refuse and leaves the operator to decide.
    op.execute(f"ALTER TABLE intent_participant_grants DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"CREATE UNIQUE INDEX {_OLD_INDEX} ON intent_participant_grants (tenant_id, intent_id, actor_id)")
