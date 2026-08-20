"""Let a binding say which extensions it activated, instead of only proving it.

`profile_bindings` records `extension_set_digest` — a hash over the set of
extension ids the binding activates — and nothing else about them. That is enough
to *verify* a set somebody already has and useless for *discovering* one. So the
question "which extensions is this tenant governed by" had no answer in the
schema, and the ids `plan_binding` was handed were discarded at the door.

The gap is invisible until something needs to read a bound extension's contents,
which the tenant-scoped scoring resolver is the first thing to do. Its first
implementation tried to work around the absence by enumerating the tenant's
extensions against the bound core revision and checking the digest matched. That
is wrong in a way worth recording, because it looks right: enumeration also finds
extensions the tenant published and never bound, so a tenant with one bound
extension and one shelved one produces a digest mismatch and the resolver refuses
a tenant whose configuration is perfectly ordinary. The test that caught it is
the one where a tenant activates a binding *without* their previously bound
extension — the rollback case, which is exactly the case the lifecycle exists
for.

So: a membership table. The digest stays and becomes what it always should have
been — an integrity check over a set the schema can state, rather than the only
trace of one it could not.

**Rows are immutable and cascade with the binding.** A binding's extension set is
fixed at plan time; changing it means planning another binding, which is the
whole point of the lifecycle. Deleting a binding takes its membership with it,
because a membership row naming a binding that does not exist describes nothing.
"""

from __future__ import annotations

from alembic import op

revision = "0059_binding_extension_members"
down_revision: str | None = "0058_predicate_churn"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = """
CREATE TABLE profile_binding_extensions (
    binding_id           UUID NOT NULL REFERENCES profile_bindings(binding_id) ON DELETE CASCADE,
    extension_revision_id UUID NOT NULL REFERENCES profile_extensions(extension_revision_id),

    PRIMARY KEY (binding_id, extension_revision_id)
)
"""

#: "Which bindings activated this extension" -- the read an operator runs before
#: retiring one, and the read that answers whether it is safe to.
_REVERSE_INDEX = """
CREATE INDEX ix_binding_extensions_extension
    ON profile_binding_extensions (extension_revision_id)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_REVERSE_INDEX)
    # No backfill. Every binding that exists today was planned with no
    # extensions -- the empty set is what `extension_set_digest([])` records and
    # what every shipped caller passes -- so an empty membership table is already
    # the correct answer for all of them. Backfilling would mean inventing
    # membership for bindings whose ids were discarded, which is the thing this
    # table exists to stop happening.


def downgrade() -> None:
    op.execute("DROP TABLE profile_binding_extensions")
