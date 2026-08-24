"""An agent is registered, and an undeclared principal is `unknown`.

E22-T7, implementing ADR 0019. Three changes to `actors`, and the first is the
one that costs something.

**The default flips from `'human'` to `'unknown'`.** The decision's reasoning,
not repeated but named: defaulting to `human` would make every unregistered
agent invisible on exactly the screens built to watch agents — the roster would
be quietly complete and quietly wrong, and the failure would present as *"we
have no agents"* rather than as *"nobody has declared any"*.

That flip is safe because **nothing reads `'human'` to make a decision.** It was
checked rather than assumed: the column has two values in use across the whole
tree, `'human'` and `'sync_worker'`, and only the second is ever selected on
(`ingest/runner.py`, and one curation path on `'system_curator'`). `'human'` is
what a row got for not being anything else, which is precisely the shape this
migration removes.

**Existing rows are not rewritten.** A row that says `human` today was given
that word by a default rather than by anybody, but rewriting them to `unknown`
would be this migration asserting something about principals it knows nothing
about — the same error one column over. The default changes for what comes
next; what is already there is left as it was found, and a roster that shows
both is showing the truth about a deployment mid-adoption.

**The vocabulary closes.** `actor_kind` had no CHECK, so its values were a
convention, which is how it came to mean "not a sync worker". Five values, and a
sixth spelling of one of them now fails instead of accumulating.

**Owner and declaration are recorded, or the roster answers half a question.**
ADR 0019's assumption 3: registration is tenant-scoped and carries an owner, so
*"who do I talk to about this agent"* is answerable from the roster. An agent
whose owner is unrecorded is a principal nobody is accountable for.
"""

from __future__ import annotations

from alembic import op

revision = "0084_actor_kind_is_declared"
down_revision: str | None = "0083_obligation_incident_reference"
branch_labels: str | None = None
depends_on: str | None = None

#: The closed set. `unknown` is first because it is the state most principals
#: are in most of the time, which is the same honesty `materiality` applies to
#: `unclassified`.
_KINDS = "'unknown', 'human', 'agent', 'sync_worker', 'system_curator'"

_STATEMENTS = [
    # The flip. `DROP DEFAULT` then `SET DEFAULT` rather than one statement, so
    # a reader of this migration sees both halves of what changed.
    "ALTER TABLE actors ALTER COLUMN actor_kind DROP DEFAULT",
    "ALTER TABLE actors ALTER COLUMN actor_kind SET DEFAULT 'unknown'",
    # Who to talk to about this principal. Nullable: an owner is part of a
    # declaration, and a principal nobody has declared has no owner rather than
    # a placeholder one.
    "ALTER TABLE actors ADD COLUMN owner_principal TEXT",
    # When somebody declared, and who did. Both or neither -- a declaration
    # with no declarer is a record of somebody having decided that nobody is
    # accountable for.
    "ALTER TABLE actors ADD COLUMN declared_at TIMESTAMPTZ",
    "ALTER TABLE actors ADD COLUMN declared_by UUID REFERENCES actors(actor_id)",
    f"ALTER TABLE actors ADD CONSTRAINT ck_actors_kind CHECK (actor_kind IN ({_KINDS}))",
    """
    ALTER TABLE actors ADD CONSTRAINT ck_actors_declaration_is_attributed CHECK (
        (declared_at IS NULL) = (declared_by IS NULL)
    )
    """,
    # A declared principal has a kind somebody chose. `unknown` with a
    # declaration attached would be a form filled in and left blank, which reads
    # afterwards as a decision nobody made.
    """
    ALTER TABLE actors ADD CONSTRAINT ck_actors_declared_kind_is_known CHECK (
        declared_at IS NULL OR actor_kind <> 'unknown'
    )
    """,
    # The roster's read: every principal in one tenant, newest first.
    "CREATE INDEX ix_actors_roster ON actors (tenant_id, created_at DESC, actor_id DESC)",
]

_DOWNGRADE = [
    "DROP INDEX ix_actors_roster",
    "ALTER TABLE actors DROP CONSTRAINT ck_actors_declared_kind_is_known",
    "ALTER TABLE actors DROP CONSTRAINT ck_actors_declaration_is_attributed",
    "ALTER TABLE actors DROP CONSTRAINT ck_actors_kind",
    "ALTER TABLE actors DROP COLUMN declared_by",
    "ALTER TABLE actors DROP COLUMN declared_at",
    "ALTER TABLE actors DROP COLUMN owner_principal",
    # Rows minted under the widened vocabulary would violate the old default's
    # assumptions, so they go back to what the prior schema could express before
    # the default is restored.
    "UPDATE actors SET actor_kind = 'human' WHERE actor_kind IN ('unknown', 'agent')",
    "ALTER TABLE actors ALTER COLUMN actor_kind SET DEFAULT 'human'",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE:
        op.execute(statement)
