"""What may be embedded. A closed set, shared by the schema and the code.

Constants only, and deliberately import-light: a migration imports this to render its
CHECK constraint, so anything heavier here would drag the application into alembic's
process. `registry/embedding/__init__.py` guards its provider imports behind
`TYPE_CHECKING` for the same reason, so importing this module costs nothing.

**Why the vocabulary is closed.** The retrieval paths filter on this column to decide
which rows are theirs — the capability search wants facts, the claim surface wants claims.
With an open vocabulary "filter to facts" is a guess about what else might be in the
table; with a closed one it is an invariant. The column was unconstrained `TEXT` for the
pipeline's whole life, which is how it stayed stuck at a single value.

**Why these names.** `target_type` / `target_id` is already this schema's vocabulary for a
polymorphic reference — `audit_log` uses exactly that pair to mean "which kind of row, and
which row". `source_id` was the obvious alternative and is a trap: it is already the
primary key of `sync_sources` and a foreign key in three other tables, so
`embeddings.source_id` would read as a reference to a connector.
"""

from __future__ import annotations

from typing import Final

#: A fact body — the prose artefact attached to a capability.
TARGET_FACT: Final[str] = "fact"

#: A consolidated claim — one typed assertion from the living-memory store.
TARGET_CLAIM: Final[str] = "claim"

EMBEDDING_TARGETS: Final[frozenset[str]] = frozenset({TARGET_FACT, TARGET_CLAIM})


def sql_set(values: frozenset[str]) -> str:
    """Render a closed vocabulary as a SQL `IN` list, sorted for a stable diff.

    Sorted because an unsorted `frozenset` renders in whatever order the hash gives,
    which would make the generated DDL differ between runs and turn a no-op into a
    spurious migration diff.
    """
    return ", ".join(f"'{value}'" for value in sorted(values))
