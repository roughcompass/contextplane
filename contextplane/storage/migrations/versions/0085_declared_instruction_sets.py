"""What the agent was told, declared by digest and submitted once.

E22-T14, implementing ADR 0020. Three tables: two for the declaration channel,
one for the material served back through it.

**`declared_instruction_sets` is the content, keyed by its digest.** Submitted
once per distinct set and never again: an agent's base instructions change on
the order of days while it resolves many times a minute, so re-sending them per
resolution would be paying the whole cost on every request to record a fact that
changed last Tuesday.

**Contextplane is not the store of record and this table is not a copy.** The
distinction is real rather than semantic: the agent's instructions live with the
agent, and what is stored here is *the set that was in force at a resolution*,
which is a historical fact about a resolution rather than a current fact about
an agent. Nothing reads this table to tell an agent what its instructions are;
it is read to tell an evaluator what they were.

**`resolution_instruction_declarations` is the per-resolution record**, and it
distinguishes three states rather than two:

- no row — the caller declared nothing;
- a row whose digest matches no `declared_instruction_sets` entry — declared,
  content unknown;
- a row whose digest matches — declared and known, and a delta is computable.

Collapsing the first two is what would make partial adoption invisible, which is
exactly the failure ADR 0020's dissent predicts. So the surfaces can tell them
apart, and the schema is what makes that possible.

**An unknown digest is recorded, not refused.** Refusing would fail a first-run
resolve for a state the service is in rather than one the caller caused, so the
declaration row is written whether or not the content has arrived. There is
deliberately **no foreign key** from the declaration to the content: the content
may arrive later, or never, and a foreign key would make the honest middle state
unstorable.

**`instruction_deltas` is what Contextplane says back**, and it is the one
table here that holds material the product owns rather than material the caller
declared. That distinction is the whole of ADR 0020's title: the instruction set
is declared and never stored as truth; the *delta* is context, authored here,
served through the envelope with a receipt like everything else.

A delta targets one declared digest. Serving on any broader basis -- to every
caller in a tenant, or to a caller whose declared content was never submitted --
is a selection rule, and selection is explicitly not decided by ADR 0020 or by
this task. The schema admits only the narrow case so that a later retrieval
policy is an addition rather than a reinterpretation of rows already written
under a different assumption.
"""

from __future__ import annotations

from alembic import op

revision = "0085_declared_instruction_sets"
down_revision: str | None = "0084_actor_kind_is_declared"
branch_labels: str | None = None
depends_on: str | None = None

#: A digest is `sha256:` and 64 hex characters, the same spelling
#: `signals/feedback.py` already uses. Checked in the schema so a second writer
#: cannot introduce a bare-hex variant that never joins.
_DIGEST_SHAPE = "digest ~ '^sha256:[0-9a-f]{64}$'"

_CONTENT = f"""
CREATE TABLE declared_instruction_sets (
    digest        TEXT NOT NULL,
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The instruction text as the caller declared it. Bounded because an
    -- unbounded body on a table every resolution may join is how a read that
    -- was fast becomes one nobody runs.
    content       TEXT NOT NULL,

    -- Who submitted it and when. A set nobody can attribute is one an evaluator
    -- cannot ask about.
    submitted_by  UUID NOT NULL REFERENCES actors(actor_id),
    submitted_at  TIMESTAMPTZ NOT NULL,

    -- One row per (tenant, digest). The digest is the content, so a second row
    -- with the same digest is the same set submitted twice.
    PRIMARY KEY (tenant_id, digest),

    CONSTRAINT ck_instruction_set_digest_shape CHECK ({_DIGEST_SHAPE}),
    CONSTRAINT ck_instruction_set_content_present CHECK (
        char_length(content) BETWEEN 1 AND 262144
    )
)
"""

_DECLARATIONS = f"""
CREATE TABLE resolution_instruction_declarations (
    declaration_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The resolution this was declared for. No foreign key to a receipt: a
    -- resolution that is refused before a receipt exists still declared
    -- something, and losing that would hide exactly the calls worth studying.
    receipt_id      UUID,

    actor_id        UUID NOT NULL REFERENCES actors(actor_id),

    -- What the caller said was in force. No foreign key to the content table --
    -- see the module docstring: the content may arrive later, or never, and a
    -- foreign key would make "declared, content unknown" unstorable.
    digest          TEXT NOT NULL,

    -- Whether the content was on hand when the resolution ran. Stored rather
    -- than derived by a later join, because it is a fact about *that*
    -- resolution: content submitted afterwards does not mean the delta was
    -- computable at the time.
    content_known   BOOLEAN NOT NULL,

    -- Whether the served delta contradicted the declared set, and what it
    -- contradicted. Both or neither: a contradiction nobody can name is a flag
    -- an evaluator cannot act on.
    contradicted    BOOLEAN NOT NULL DEFAULT FALSE,
    contradiction_note TEXT,

    declared_at     TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_declaration_digest_shape CHECK ({_DIGEST_SHAPE}),
    CONSTRAINT ck_declaration_contradiction_is_named CHECK (
        contradicted = FALSE OR char_length(coalesce(contradiction_note, '')) BETWEEN 1 AND 2000
    )
)
"""

_DELTAS = """
CREATE TABLE instruction_deltas (
    delta_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The declared set this corrects. A foreign key here, unlike on the
    -- declaration table: a delta written against content nobody submitted was
    -- authored against a set its author could not have read.
    target_digest   TEXT NOT NULL,

    -- What is served. The correction itself, in the author's words.
    body            TEXT NOT NULL,

    -- Whether this contradicts the declared set, and what it contradicts.
    -- ADR 0020 serves a contradiction flagged rather than silently or not at
    -- all, so the flag has to be authored here rather than inferred at serve
    -- time -- inferring it would mean the product deciding, per request,
    -- whether it was overriding the operator.
    contradicts     BOOLEAN NOT NULL DEFAULT FALSE,
    contradiction_note TEXT,

    authored_by     UUID NOT NULL REFERENCES actors(actor_id),
    authored_at     TIMESTAMPTZ NOT NULL,

    -- Withdrawable, and withdrawal is what suppression acts on. Deleting
    -- instead would remove the row that explains a resolution which already
    -- served it.
    withdrawn_at    TIMESTAMPTZ,
    withdrawn_by    UUID REFERENCES actors(actor_id),

    FOREIGN KEY (tenant_id, target_digest)
        REFERENCES declared_instruction_sets (tenant_id, digest),

    CONSTRAINT ck_delta_body_present CHECK (char_length(body) BETWEEN 1 AND 32768),
    CONSTRAINT ck_delta_contradiction_is_named CHECK (
        contradicts = FALSE OR char_length(coalesce(contradiction_note, '')) BETWEEN 1 AND 2000
    ),
    CONSTRAINT ck_delta_withdrawal_is_attributed CHECK (
        (withdrawn_at IS NULL) = (withdrawn_by IS NULL)
    )
)
"""

#: The serving read: the live deltas for one declared set. Partial, because a
#: withdrawn delta is never served and indexing it would grow the index the
#: resolver walks with rows the resolver skips.
_DELTAS_LIVE = """
CREATE INDEX ix_instruction_deltas_live
    ON instruction_deltas (tenant_id, target_digest, authored_at)
    WHERE withdrawn_at IS NULL
"""

#: The two receipt tables that name a block, widened for the fifth.
#:
#: `0032` pinned the four block names in a CHECK on each, and that CHECK is the
#: reason the block set is not something a branch can widen by editing one
#: constant. It fired here, in the integration tier, on the first resolution
#: after the fifth block existed -- which is the tier working: nothing that reads
#: the baseline schema could have caught it.
#:
#: Dropped and recreated rather than altered, because Postgres has no
#: `ALTER CONSTRAINT` for a CHECK's expression and a second constraint with a
#: different name would leave two rules about one column for the next reader to
#: reconcile.
#:
#: One statement per `op.execute`. asyncpg prepares every statement, and a
#: prepared statement holds exactly one command -- so a semicolon-joined script
#: fails at the driver rather than at the database.
_BLOCK_CHECKS: tuple[tuple[str, str], ...] = (
    ("context_receipt_arms", "ck_receipt_arm_block"),
    ("context_receipt_items", "ck_receipt_item_block"),
)

_FIVE_BLOCKS = "'canonical', 'arc', 'observed_claims', 'workspace', 'instructions'"
_FOUR_BLOCKS = "'canonical', 'arc', 'observed_claims', 'workspace'"


def _reset_block_check(names: str) -> None:
    for table, constraint in _BLOCK_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK (block IN ({names}))")


#: The evaluator's read: what did this actor declare, most recent first.
_BY_ACTOR = """
CREATE INDEX ix_instruction_declarations_by_actor
    ON resolution_instruction_declarations (tenant_id, actor_id, declared_at DESC)
"""

#: The adoption read: how many resolutions declared nothing, declared an unknown
#: set, or declared a known one. A surface built on this signal has to be able
#: to say how much of the fleet it covers.
_BY_KNOWN = """
CREATE INDEX ix_instruction_declarations_by_known
    ON resolution_instruction_declarations (tenant_id, content_known, declared_at DESC)
"""


def upgrade() -> None:
    op.execute(_CONTENT)
    op.execute(_DECLARATIONS)
    op.execute(_DELTAS)
    op.execute(_DELTAS_LIVE)
    op.execute(_BY_ACTOR)
    op.execute(_BY_KNOWN)
    _reset_block_check(_FIVE_BLOCKS)


def downgrade() -> None:
    _reset_block_check(_FOUR_BLOCKS)
    op.execute("DROP INDEX ix_instruction_declarations_by_known")
    op.execute("DROP INDEX ix_instruction_declarations_by_actor")
    op.execute("DROP INDEX ix_instruction_deltas_live")
    op.execute("DROP TABLE instruction_deltas")
    op.execute("DROP TABLE resolution_instruction_declarations")
    op.execute("DROP TABLE declared_instruction_sets")
