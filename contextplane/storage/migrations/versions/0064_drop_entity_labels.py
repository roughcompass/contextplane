"""Drop `entity_labels`: a selector nothing could evaluate, that widened instead.

`arc_applicability_rules.entity_labels` was written, stored, snapshotted,
materialised and round-tripped everywhere, and **never matched**.
`selection.rule_applies` had no branch for it and `IntentManifest` had no labels
field to match against. Meanwhile both guards admitted labels *alone* as
satisfying entity scope -- `ApplicabilityRule.__post_init__` in Python, and
`ck_arc_rules_entity_scope_target` here. So an entity-scoped rule naming one
label and no ids matched **every** manifest, and the narrowest scope above
`intent` became the widest.

That was confirmed through the real paths rather than inferred: a labels-only
row inserted through `insert_applicability_rule` -- the function `submission.py`
calls -- was accepted, rehydrated through `corpus._rule_from_row`, and returned
`True` from `rule_applies` for a manifest with an unrelated entity and an
unrelated domain.

**It could not be made to work, and that is why it goes rather than getting a
matcher.** Three things stand in the way, and the last is decisive:

- **There is nothing to resolve a label against.** `entities` carries no label
  or tag column, and no label vocabulary exists anywhere in the catalog. The
  selector never had a referent.
- **An exception could never match its granularity.**
  `arc_approved_exceptions.lower_scope_entity_id` is a single UUID and
  `ck_arc_exceptions_scope_selectors` admits no label, so authority granted by a
  label-selected rule could never be narrowed by an exception. Half the model
  cannot express it.
- **The obligation tombstone forces freezing anyway.** An obligation outlives
  the revision behind it and must answer "who did this apply to" from
  `applicability_snapshot` alone, with no live rule and no receipt. Resolving a
  label there means resolving it against the catalog *as it was* at the original
  decision, which the snapshot does not record -- so the only workable answer is
  to freeze resolved entity ids at authoring time, which is `entity_ids` with
  extra steps.

**No alias and no deprecation window**, following `0061_arc_entity_scope`: the
field is on `openapi.json` and `contextplane-ui` vendors that contract, so the
cost is a contract regeneration in one in-org consumer rather than nothing --
but this service has never been released, so there are no stored rules, no
generated third-party clients and no signed bytes spelled the old way.

**The V1 authoring shape loses the field too, and that is not a mistake.**
`_applicability_rule(narrowest_scope, selector)` generates the V1 and V2 shapes
from one description, so V1 already spells its arrays `entity_ids` while its
narrowest scope is still `task`. V1 is regenerated in lockstep by construction
rather than frozen; it will only become frozen at first release, and the word in
the source should be read that way until then.

**`downgrade()` restores the column and the three-way CHECK**, because
`0061_arc_entity_scope` is symmetric by design and renames `entity_labels` back
to `capability_labels` on the way down. Without the restore, downgrading past
this revision to 0061 would fail on a column that no longer exists.
"""

from __future__ import annotations

from alembic import op

revision = "0064_drop_entity_labels"
down_revision: str | None = "0063_rule_scope_selectors"
branch_labels: str | None = None
depends_on: str | None = None

#: Entity scope now means exactly one thing: at least one entity id.
_ENTITY_SCOPE_TARGET_WITHOUT_LABELS = """
ALTER TABLE arc_applicability_rules
    ADD CONSTRAINT ck_arc_rules_entity_scope_target CHECK (
        scope <> 'entity'
        OR (entity_ids IS NOT NULL AND array_length(entity_ids, 1) >= 1)
    )
"""

#: The shape 0061 leaves behind, restored on the way down so that revision's own
#: `RENAME COLUMN entity_labels TO capability_labels` still has a column to move.
_ENTITY_SCOPE_TARGET_WITH_LABELS = """
ALTER TABLE arc_applicability_rules
    ADD CONSTRAINT ck_arc_rules_entity_scope_target CHECK (
        scope <> 'entity'
        OR (entity_ids IS NOT NULL AND array_length(entity_ids, 1) >= 1)
        OR (entity_labels IS NOT NULL AND array_length(entity_labels, 1) >= 1)
    )
"""


def upgrade() -> None:
    # The check first: it references the column, so dropping the column while it
    # stands would take the constraint with it silently rather than by decision.
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_entity_scope_target")
    op.execute("ALTER TABLE arc_applicability_rules DROP COLUMN entity_labels")
    op.execute(_ENTITY_SCOPE_TARGET_WITHOUT_LABELS)


def downgrade() -> None:
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_entity_scope_target")
    op.execute("ALTER TABLE arc_applicability_rules ADD COLUMN entity_labels TEXT[]")
    op.execute(_ENTITY_SCOPE_TARGET_WITH_LABELS)
