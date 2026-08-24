"""An upstream record you cannot date, and a revision of nothing.

E12-T2. `assertion_provenance` already keeps the three times apart -- its own
comment says why: *"when it happened, when we saw it, when we stored it.
Collapsing them makes staleness unmeasurable."* `ingested_at` is `NOT NULL` and
server-set; `event_time` and `observed_at` are nullable and caller-supplied.
That is the right shape and it was enforced by nothing.

**The enforcement is the point, not the shape.** E2 built this exact property
for a different record class in migration 0067, which requires
`source_namespace`, `external_record_id` and `observed_at` to be present
together or not at all on `memory_session_events`, *"so an importer that forgets
one is refused by the database rather than by a code review."* This is the same
move on the table an import actually writes.

**The discriminator differs, and copying 0067's would be wrong.**
`source_namespace` is `NOT NULL` here, so it cannot say whether a record came
from upstream. `external_record_id` can: 0051 states that it is *"NULL for a
record with no upstream identity of its own, which is a different statement from
an empty external id."* A derived assertion legitimately has none. So the rule
is conditional on it rather than symmetric with it:

**An external record must be dated.** Naming a specific upstream record while
declining to say when you saw it produces a citation nobody can age. The
converse is not required: `observed_at` without an external id is a real
statement -- we saw this, the source has no record id for it.

**A revision must revise something.** `external_revision` with no
`external_record_id` is a version of nothing, and it reads afterwards as though
somebody knew which record it belonged to.

**What this migration does not add: a default.** A `NOT NULL DEFAULT now()` on
`observed_time` would be a server-defaulted value wearing a caller-supplied
name, indistinguishable afterwards from a genuine one -- which is the whole
reason the epic names this property. The absence is what
`test_provenance_is_never_server_defaulted` reads back off the live schema, so
adding one later fails a test rather than passing review.
"""

from __future__ import annotations

from alembic import op

revision = "0082_provenance_completeness"
down_revision: str | None = "0081_audit_justified_reads"
branch_labels: str | None = None
depends_on: str | None = None

_EXTERNAL_RECORD_IS_DATED = """
ALTER TABLE assertion_provenance
    ADD CONSTRAINT ck_assertion_provenance_external_record_is_dated CHECK (
        external_record_id IS NULL OR observed_at IS NOT NULL
    )
"""

_REVISION_NEEDS_A_RECORD = """
ALTER TABLE assertion_provenance
    ADD CONSTRAINT ck_assertion_provenance_revision_needs_a_record CHECK (
        external_revision IS NULL OR external_record_id IS NOT NULL
    )
"""


def upgrade() -> None:
    op.execute(_EXTERNAL_RECORD_IS_DATED)
    op.execute(_REVISION_NEEDS_A_RECORD)


def downgrade() -> None:
    op.execute("ALTER TABLE assertion_provenance DROP CONSTRAINT ck_assertion_provenance_revision_needs_a_record")
    op.execute("ALTER TABLE assertion_provenance DROP CONSTRAINT ck_assertion_provenance_external_record_is_dated")
