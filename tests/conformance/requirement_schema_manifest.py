"""The requirement→schema manifest: which columns a requirement's amendment promised.

A requirement that names a schema shape can be amended while the code it
constrains is in flight, and nothing structural compares the two — that is how
one table gained its sizing columns while the sibling table the same rule
covered did not, and the gap stayed invisible precisely because every check
that existed went green on the table that complied.

This manifest is the comparison. Each entry maps a requirement id to the table
and columns its wording commits to; the conformance test asserts every named
column exists in the **live schema** (`information_schema.columns`), the way
this codebase's other structural gates work — against real state, not
documentation.

**The process contract, which matters more than the code:** an entry here is
added or updated *in the same change that adds or amends the requirement* —
at amendment time, not implementation time. An entry recorded when someone
gets round to implementing it can only ever confirm what already happened;
an entry recorded at amendment time goes red until the schema catches up,
which is the entire point.

Hand-maintained by design. A parser over requirement prose would be a second
thing to get wrong; a short list a test enforces is the cheap, durable version.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RequirementColumns:
    """One requirement's schema commitment."""

    requirement_id: str
    table: str
    columns: frozenset[str]


MANIFEST: tuple[RequirementColumns, ...] = (
    RequirementColumns(
        requirement_id="LMM-F1.2",  # doc-ref: intentional — the manifest exists to name requirements
        table="memory_session_events",
        columns=frozenset({"size_bytes", "token_count", "tokenizer_id"}),
    ),
    RequirementColumns(
        requirement_id="MSR-F3.7",  # doc-ref: intentional — the manifest exists to name requirements
        table="lmm_claims",
        columns=frozenset({"size_bytes", "token_count", "tokenizer_id"}),
    ),
)


def missing_columns(
    live_columns: set[tuple[str, str]],
    manifest: tuple[RequirementColumns, ...] = MANIFEST,
) -> list[str]:
    """Compare the manifest against a live-schema column set.

    *live_columns* is the set of ``(table_name, column_name)`` pairs from
    ``information_schema.columns``. Returns one human-readable line per
    missing column, empty when the schema honours every commitment. Pure, so
    the negative test can prove the gate fires without fabricating a database.
    """
    problems: list[str] = []
    for entry in manifest:
        for column in sorted(entry.columns):
            if (entry.table, column) not in live_columns:
                problems.append(
                    f"{entry.requirement_id}: {entry.table}.{column} is promised "
                    "by the requirement but absent from the live schema"
                )
    return problems
