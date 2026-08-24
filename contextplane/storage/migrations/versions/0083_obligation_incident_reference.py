"""An obligation may cite the incident it is about, which a decision promised.

E4-T5d. The decision that named the governed object says plainly what the
relationship is:

    A `reporting_obligation` may *reference* an `incident` in the sense the tree
    already uses -- the external record -- and a claim may cite that same
    incident as evidence. Three objects, one relationship, no shared name.

Nothing implemented it. That is the fourth time an ADR's Consequences named an
artefact nobody filed, and it is why E4-T7 could not be built: an evidence
bundle scoped to an obligation would have carried the four fields its detail
route already returns and no evidence at all, because the obligation had no way
to say what it was about.

**No new table and no new column.** `context_external_references` already models
an external record with `kind = 'incident'`, and `context_reference_bindings`
already binds one to a subject. The whole of the missing relationship is that
`reporting_obligation` was not a legal `subject_type` -- so the promise was one
CHECK value away from being expressible the entire time.

Shaped exactly like 0044, which widened the same constraint to admit
`external_signal`: the constraint's name spelled once, both directions
re-creating it under that name, and the prior set kept so the downgrade restores
it rather than guessing.

**The reference stays optional, and 0076 already said why.** That migration
made `summary` free text rather than a reference *"because an obligation can be
nominated before anybody knows which record it concerns, and refusing the
nomination until the link exists would lose the nomination."* Nothing here
changes that: an obligation with no binding is a nomination somebody has not yet
matched to a record, which is the state most of them start in.
"""

from __future__ import annotations

from alembic import op

revision = "0083_obligation_incident_reference"
down_revision: str | None = "0082_provenance_completeness"
branch_labels: str | None = None
depends_on: str | None = None

#: The constraint's own name, spelled once. Both directions re-create it under
#: this name, and a test matches a refusal against it.
_CHECK = "ck_reference_binding_subject_type"

#: What may cite a reference, after this revision.
_SUBJECT_TYPES = "'intent_checkpoint', 'context_item', 'external_signal', 'reporting_obligation'"

#: What could before it, and what the downgrade restores.
_PRIOR_SUBJECT_TYPES = "'intent_checkpoint', 'context_item', 'external_signal'"


def _recreate(subject_types: str) -> None:
    op.execute(f"ALTER TABLE context_reference_bindings DROP CONSTRAINT {_CHECK}")
    op.execute(
        f"ALTER TABLE context_reference_bindings ADD CONSTRAINT {_CHECK} " f"CHECK (subject_type IN ({subject_types}))"
    )


def upgrade() -> None:
    _recreate(_SUBJECT_TYPES)


def downgrade() -> None:
    # Bindings written under the widened set would violate the narrower one, so
    # they go first. Deleting them is right rather than harsh: downgrading past
    # this revision removes the only vocabulary under which they mean anything,
    # and a row whose `subject_type` no rule admits is not a record, it is a
    # join that finds nothing.
    op.execute("DELETE FROM context_reference_bindings WHERE subject_type = 'reporting_obligation'")
    _recreate(_PRIOR_SUBJECT_TYPES)
