"""A session event can say where it came from and when it happened there.

E2 asks for "`observed_time` and `external_record_id` caller-supplied where the
stream declares an external source". The requirement is right and three of its
particulars change on contact with the tree.

**The columns are named for the tree, not for the epic.** E2 says
`observed_time`. Two shipped tables spell it `observed_at`
(`assertion_provenance`, `context_external_references`) and one spells it
`observed_time` (`external_signals`). Following the majority is not the reason;
the reason is that `assertion_provenance` is the tree's considered provenance
vocabulary -- it also carries `event_time`, `ingested_at`, an `authority`
enum, freshness and revocation -- and a fourth spelling in a fifth table makes
the next reader ask whether the difference means something. It does not.

**One upstream clock, not two, and that is a decision rather than an
omission.** `assertion_provenance` distinguishes `event_time` (when the thing
happened) from `observed_at` (when the source saw it) from `ingested_at` (when
we stored it). For a conversational turn replayed from an external system those
first two collapse: a chat message's timestamp is both when it was said and when
the exporting system saw it, and inventing a distinction the source cannot
supply would produce two columns with the same value and no rule for when they
differ. `created_at` already plays `ingested_at`.

**This is not `assertion_provenance` reused, deliberately.** That table is the
provenance of an *assertion about the catalog* -- its three inbound foreign keys
are entity attributes, relationship metadata and ownership assignments, each
"somebody claimed X about Y". A conversational turn is not a claim about an
entity, and pointing session events at it would make every replayed message an
assertion, which is a category error that a foreign key would then make load
bearing.

**The conditional is declared per event, not per registered namespace, and this
is the weaker of the two available answers.** E1's body asks for "stream-scoped
action-class and sensitivity declarations at source-namespace registration", and
that surface does not exist -- `arc_source_connectors` registers schemes, hosts
and media types, with no such declaration. Rather than build a registry to
answer one CHECK, an event that names a `source_system` *is* an event declaring
an external source, and the CHECK keys off that.

What that gives up: a namespace cannot state once that all its events carry
external identity, so the guarantee is per row and a caller can be inconsistent
across a stream. What it avoids: inventing a registration table whose only
consumer is this constraint, and pre-empting a scoping decision that belongs to
whoever resolves E1's clause. If that registry later exists, this CHECK narrows
to reference it and no data migrates.

All four columns are nullable, because a locally-originated turn -- an agent
writing its own reasoning -- has no upstream anything, and that is the common
case rather than a degenerate one.
"""

from __future__ import annotations

from alembic import op

revision = "0067_session_event_external_provenance"
down_revision: str | None = "0066_partition_session_events"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS = (
    # Named to match `assertion_provenance`, which is the pair this table's
    # rows are describing a weaker version of.
    "ALTER TABLE memory_session_events ADD COLUMN source_system TEXT",
    "ALTER TABLE memory_session_events ADD COLUMN source_namespace TEXT",
    "ALTER TABLE memory_session_events ADD COLUMN external_record_id TEXT",
    "ALTER TABLE memory_session_events ADD COLUMN observed_at TIMESTAMPTZ",
)

#: The conditional E2 asks for. An event naming a source system is an event
#: declaring an external origin, and an external origin without an upstream
#: identity or an upstream time is a provenance claim that cannot be checked
#: against anything -- which is worse than no claim, because it reads as one.
_EXTERNAL_IS_COMPLETE = """
ALTER TABLE memory_session_events
    ADD CONSTRAINT ck_mse_external_provenance_complete CHECK (
        source_system IS NULL
        OR (source_namespace IS NOT NULL AND external_record_id IS NOT NULL AND observed_at IS NOT NULL)
    )
"""

#: And the other direction, which is the one a caller gets wrong by accident:
#: an `external_record_id` or an `observed_at` with no source system is an
#: identity in an unnamed namespace and a timestamp from an unnamed clock.
#: Neither can be compared with anything, so neither is provenance.
_NO_ORPHAN_EXTERNAL_FIELDS = """
ALTER TABLE memory_session_events
    ADD CONSTRAINT ck_mse_external_fields_need_a_source CHECK (
        source_system IS NOT NULL
        OR (source_namespace IS NULL AND external_record_id IS NULL AND observed_at IS NULL)
    )
"""

#: Dedup across a replay. An exporting system that re-sends a window must not
#: produce two events for one upstream record, and this is the only thing that
#: could notice -- `uq_mse_session_seq` counts positions in a conversation, not
#: upstream identities. Partial, so the ordinary locally-originated event with
#: no external identity is unconstrained.
#:
#: Scoped to the tenant and the namespace rather than the session: the same
#: upstream record replayed into two sessions is a duplicate of the same fact,
#: and `tenant_id` leads it so the index prunes to one partition like every
#: other index on this table.
_EXTERNAL_IDENTITY_UNIQUE = """
CREATE UNIQUE INDEX uq_mse_external_record
    ON memory_session_events (tenant_id, source_system, source_namespace, external_record_id)
    WHERE external_record_id IS NOT NULL
"""


def upgrade() -> None:
    for statement in _COLUMNS:
        op.execute(statement)
    op.execute(_EXTERNAL_IS_COMPLETE)
    op.execute(_NO_ORPHAN_EXTERNAL_FIELDS)
    op.execute(_EXTERNAL_IDENTITY_UNIQUE)


def downgrade() -> None:
    op.execute("DROP INDEX uq_mse_external_record")
    op.execute("ALTER TABLE memory_session_events DROP CONSTRAINT ck_mse_external_fields_need_a_source")
    op.execute("ALTER TABLE memory_session_events DROP CONSTRAINT ck_mse_external_provenance_complete")
    for column in ("observed_at", "external_record_id", "source_namespace", "source_system"):
        op.execute(f"ALTER TABLE memory_session_events DROP COLUMN {column}")
