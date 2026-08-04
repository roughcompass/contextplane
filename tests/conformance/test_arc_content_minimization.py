"""NF1.4 content minimization, enforced against the live schema.

Receipts, receipt events, selected rows, challenges, and the audit outbox may
hold only bounded identifiers, enumerated codes, counters, and digests. The
freeform source text they describe lives in artifact content columns behind
envelope encryption and reaches a caller only through JIT detail, redacted by
audience.

The rule this gate applies: **every `text` column on a request-side ARC table
must be bounded, either by an enumerating CHECK (`col IN (...)`) or by a length
CHECK (`char_length(col) …`).** An unbounded `text` column on one of these tables
is a place where caller- or source-supplied content can accumulate without limit
inside the audit record itself.

The gate deliberately has a second half. A column-type scan alone is not
sufficient, because `event_payload` columns are `JSONB` — not `text`, so a type
scan passes them, while nothing in the column definition stops arbitrarily large
nested content. Every such column must therefore be declared here with an
explicit bound, and a new one appearing undeclared fails the gate.

Placed in `conformance/` and early in the build on purpose: this is an invariant
a later change is likely to break silently, and it needs to be guarding while the
write paths are being written rather than after.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Tables that carry the audit record itself. Artifact-side tables are excluded:
# they are where governed source content legitimately lives, protected by
# envelope encryption and audience redaction rather than by length.
REQUEST_SIDE_TABLES = (
    "arc_context_challenges",
    "arc_receipts",
    "arc_receipt_events",
    "arc_receipt_event_heads",
    "arc_receipt_selected_revisions",
    "arc_receipt_selected_directives",
    "arc_audit_outbox",
)

# JSONB payloads and their declared bounds, in bytes of canonical serialization.
# Enforcement of an instance against its bound belongs to the write path; what
# this gate enforces is that no payload column exists without a declared bound.
DECLARED_PAYLOAD_BOUNDS: dict[str, int] = {
    "arc_receipt_events.event_payload": 8 * 1024,
    "arc_audit_outbox.event_payload": 8 * 1024,
    # Per-receipt snapshot of obligation detail, subject to audience redaction.
    "arc_receipt_selected_directives.obligation_fields": 8 * 1024,
    # Exact canonical profile versions used by the decision — a small fixed map.
    "arc_receipts.canonical_profile_versions": 2 * 1024,
}


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def engine(pg_container: str) -> Engine:
    eng = create_engine(_sync_url(pg_container))
    yield eng
    eng.dispose()


_UNBOUNDED_TEXT_SQL = """
SELECT c.table_name, c.column_name
FROM information_schema.columns c
WHERE c.table_name = ANY(:tables)
  AND c.data_type = 'text'
  AND NOT EXISTS (
    SELECT 1
    FROM pg_constraint pc
    JOIN pg_class pcl ON pcl.oid = pc.conrelid
    WHERE pcl.relname = c.table_name
      AND pc.contype = 'c'
      AND (
        -- bounded by length
        pg_get_constraintdef(pc.oid) LIKE '%char_length(' || c.column_name || ')%'
        -- or bounded by enumeration
        OR pg_get_constraintdef(pc.oid) LIKE '%' || c.column_name || ' = ANY %'
        OR pg_get_constraintdef(pc.oid) LIKE '%' || c.column_name || ' = ''%'
      )
  )
ORDER BY c.table_name, c.column_name
"""


def test_no_unbounded_text_on_request_side_tables(engine: Engine) -> None:
    """Every text column here is bounded by enumeration or by length."""
    with engine.connect() as conn:
        rows = conn.execute(text(_UNBOUNDED_TEXT_SQL), {"tables": list(REQUEST_SIDE_TABLES)}).all()

    offenders = [f"{r.table_name}.{r.column_name}" for r in rows]
    assert not offenders, (
        "unbounded text columns on request-side ARC tables — each is somewhere "
        "caller- or source-supplied content can grow without limit inside the "
        "audit record:\n  " + "\n  ".join(offenders)
    )


def test_every_jsonb_payload_column_has_a_declared_bound(engine: Engine) -> None:
    """A column-type scan cannot see inside JSONB, so the bound is declared here.

    If this fails because a new payload column appeared, the fix is to declare its
    bound and enforce it on the write path — not to widen the exclusion.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name = ANY(:tables) AND data_type = 'jsonb' "
                "ORDER BY table_name, column_name"
            ),
            {"tables": list(REQUEST_SIDE_TABLES)},
        ).all()

    found = {f"{r.table_name}.{r.column_name}" for r in rows}
    undeclared = found - set(DECLARED_PAYLOAD_BOUNDS)
    stale = set(DECLARED_PAYLOAD_BOUNDS) - found

    assert not undeclared, "JSONB columns with no declared size bound: " + ", ".join(sorted(undeclared))
    assert not stale, "declared bounds for columns that no longer exist: " + ", ".join(sorted(stale))


def test_declared_payload_bounds_are_actually_bounded() -> None:
    """A declared bound of zero or something enormous is not a bound."""
    for name, limit in DECLARED_PAYLOAD_BOUNDS.items():
        assert 0 < limit <= 64 * 1024, f"{name}: implausible declared bound {limit}"


def test_digest_columns_are_pinned_to_digest_length(engine: Engine) -> None:
    """A column named `*_digest` holding something other than a digest is a smell.

    Pinning the length is what stops a digest column quietly becoming a place to
    put a message.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.table_name, c.column_name
                FROM information_schema.columns c
                WHERE c.table_name = ANY(:tables)
                  AND c.data_type = 'text'
                  AND c.column_name LIKE '%_digest'
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_constraint pc
                    JOIN pg_class pcl ON pcl.oid = pc.conrelid
                    WHERE pcl.relname = c.table_name
                      AND pc.contype = 'c'
                      AND pg_get_constraintdef(pc.oid)
                          LIKE '%char_length(' || c.column_name || ') = 64%'
                  )
                ORDER BY 1, 2
                """
            ),
            {"tables": list(REQUEST_SIDE_TABLES)},
        ).all()

    offenders = [f"{r.table_name}.{r.column_name}" for r in rows]
    assert not offenders, "digest columns without an exact 64-character length CHECK:\n  " + "\n  ".join(offenders)
