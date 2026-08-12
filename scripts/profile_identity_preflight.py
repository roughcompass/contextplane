#!/usr/bin/env python3
"""Inventory what type-aware identity would break before anything is changed.

Two modes, because two different questions get asked before a migration runs.

`--check` answers "do the rules this migration relies on actually hold?" and
needs no database. It exercises the normalization, parsing and window arithmetic
against the awkward inputs, so a rule that has quietly stopped being true fails
here rather than halfway through a backfill. It is safe in CI and safe on a
laptop with nothing running.

`--database-url` answers "what is in this tenant that would not survive
expansion?" and reports rather than fixes. Nothing here writes. An inventory that
repaired what it found would be deciding, on its own authority, which of two
colliding names is the real one — and that decision belongs to whoever owns the
data.

The findings are deliberately separated into blocking and advisory. A blocking
finding means expansion cannot produce a correct result; an advisory one means it
can, but somebody should know. Collapsing the two would make the tool useless in
exactly the case it matters: a tenant with a hundred advisories and one blocker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from contextplane.entities.identity import (
    QUALIFIED_HANDLE_PATTERN,
    AmbiguousIdentity,
    HandleRow,
    IdentityError,
    Phase,
    QualifiedHandle,
    RollbackWindow,
    assert_phase_transition,
    lookup_key_for,
    may_contract_old_constraint,
    resolve_qualified,
    resolve_unqualified,
)


@dataclass(frozen=True)
class Finding:
    """One thing wrong, and whether it stops the migration."""

    blocking: bool
    code: str
    detail: str

    def __str__(self) -> str:
        marker = "BLOCK" if self.blocking else "note "
        return f"  {marker} {self.code}: {self.detail}"


# ---------------------------------------------------------------------------
# --check: the rules, exercised without a database
# ---------------------------------------------------------------------------


def _check_round_trip() -> list[Finding]:
    """A handle must survive parse -> str -> parse unchanged."""
    findings: list[Finding] = []
    for value in (
        "acme:service/checkout",
        "acme:capability/order-fulfilment",
        "t1:interface_version/v2.1.0",
        "n:t/name.with.dots",
    ):
        try:
            parsed = QualifiedHandle.parse(value)
        except IdentityError as error:
            findings.append(Finding(True, "parse-rejects-legal-handle", f"{value!r}: {error}"))
            continue
        if str(parsed) != value:
            findings.append(Finding(True, "round-trip-changed-handle", f"{value!r} became {str(parsed)!r}"))
    return findings


def _check_unqualified_is_refused() -> list[Finding]:
    """A bare name must not parse as a qualified handle.

    If it ever does, every caller that "qualified" its lookup by passing a name
    through `parse` would silently be doing an unqualified lookup instead.
    """
    findings: list[Finding] = []
    for value in ("checkout", "acme:checkout", "acme/checkout", "", "   ", "a:b/", "a:b/with space"):
        if QUALIFIED_HANDLE_PATTERN.match(value.strip()):
            findings.append(
                Finding(True, "unqualified-parses-as-qualified", f"{value!r} matched the qualified pattern")
            )
    return findings


def _check_normalization_agrees_with_sql() -> list[Finding]:
    """Python's key must be derived with `lower`, matching the SQL index.

    Checked against input where `lower` and `casefold` disagree. If the key were
    casefolded, these would collide in Python while remaining distinct rows in
    Postgres, and resolution would report a broken index for a database doing
    exactly what it was told.
    """
    findings: list[Finding] = []
    for left, right in (("straße", "strasse"), ("ﬁle", "file")):
        left_key = lookup_key_for("n", "t", left)
        right_key = lookup_key_for("n", "t", right)
        if left_key == right_key:
            findings.append(
                Finding(
                    True,
                    "normalization-stricter-than-sql",
                    f"{left!r} and {right!r} produce one key {left_key!r}; SQL lower() keeps them distinct, so "
                    "Python would judge two legitimate rows to be the same handle",
                )
            )
    # And the case it must still collapse, or the index and the code disagree
    # the other way.
    if lookup_key_for("N", "T", "Checkout") != lookup_key_for("n", "t", "checkout"):
        findings.append(Finding(True, "normalization-case-sensitive", "case differences did not normalize to one key"))
    return findings


def _check_ambiguity_is_loud() -> list[Finding]:
    """Two types sharing a name must raise, not pick one."""
    rows = (
        HandleRow(entity_id="e1", entity_type="service", namespace="acme", handle_name="checkout", kind="primary"),
        HandleRow(entity_id="e2", entity_type="capability", namespace="acme", handle_name="checkout", kind="primary"),
    )
    try:
        resolved = resolve_unqualified(rows, "checkout")
    except AmbiguousIdentity as ambiguous:
        if set(ambiguous.entity_types) != {"service", "capability"}:
            return [Finding(True, "ambiguity-omits-candidates", f"reported {ambiguous.entity_types!r}")]
        return []
    return [
        Finding(
            True,
            "ambiguity-resolved-silently",
            f"an unqualified name matching two types returned {resolved!r} instead of refusing",
        )
    ]


def _check_same_name_across_types_is_permitted() -> list[Finding]:
    """The whole point: qualified lookups must still work when a name is shared."""
    rows = (
        HandleRow(entity_id="e1", entity_type="service", namespace="acme", handle_name="checkout", kind="primary"),
        HandleRow(entity_id="e2", entity_type="capability", namespace="acme", handle_name="checkout", kind="primary"),
    )
    findings: list[Finding] = []
    for handle, expected in (("acme:service/checkout", "e1"), ("acme:capability/checkout", "e2")):
        try:
            got = resolve_qualified(rows, handle)
        except IdentityError as error:
            findings.append(Finding(True, "qualified-lookup-failed", f"{handle}: {error}"))
            continue
        if got != expected:
            findings.append(Finding(True, "qualified-lookup-wrong", f"{handle} -> {got!r}, expected {expected!r}"))
    return findings


def _check_phase_order() -> list[Finding]:
    """One step at a time, and a skip must be refused."""
    findings: list[Finding] = []
    try:
        assert_phase_transition(Phase.LEGACY, Phase.EXPANDED)
    except IdentityError as error:
        findings.append(Finding(True, "legal-transition-refused", f"legacy -> expanded: {error}"))
    for current, target in ((Phase.LEGACY, Phase.CUT_OVER), (Phase.LEGACY, Phase.DUAL_READ)):
        try:
            assert_phase_transition(current, target)
        except IdentityError:
            continue
        findings.append(Finding(True, "phase-skip-permitted", f"{current.value} -> {target.value} was allowed"))
    return findings


def _check_rollback_window() -> list[Finding]:
    """Thirty days, one extension to sixty, never a second."""
    findings: list[Finding] = []
    activated = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    window = RollbackWindow(activated_at=activated)
    if window.days != 30:
        findings.append(Finding(True, "window-not-30-days", f"got {window.days}"))
    extended = window.extend("operations needs a second look at the alias backfill")
    if extended.days != 60:
        findings.append(Finding(True, "extension-not-60-days", f"got {extended.days}"))
    try:
        extended.extend("again")
    except IdentityError:
        pass
    else:
        findings.append(Finding(True, "window-extended-twice", "a second extension was permitted"))

    # Contraction needs both conditions; each alone must not be enough.
    inside = activated + dt.timedelta(days=1)
    after = activated + dt.timedelta(days=31)
    allowed, reasons = may_contract_old_constraint(window=window, moment=inside, legacy_consumers=0)
    if allowed:
        findings.append(Finding(True, "contract-inside-window", "contraction allowed while the window was open"))
    allowed, reasons = may_contract_old_constraint(window=window, moment=after, legacy_consumers=1)
    if allowed:
        findings.append(Finding(True, "contract-with-consumers", "contraction allowed with a consumer remaining"))
    allowed, reasons = may_contract_old_constraint(window=window, moment=after, legacy_consumers=0)
    if not allowed:
        findings.append(Finding(True, "contract-never-allowed", f"both conditions met yet refused: {reasons}"))
    return findings


_CHECKS = (
    _check_round_trip,
    _check_unqualified_is_refused,
    _check_normalization_agrees_with_sql,
    _check_ambiguity_is_loud,
    _check_same_name_across_types_is_permitted,
    _check_phase_order,
    _check_rollback_window,
)


def run_rule_checks() -> list[Finding]:
    """Every rule check, all of them, so one failure does not hide the rest."""
    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check())
    return findings


# ---------------------------------------------------------------------------
# --database-url: what is in the data
# ---------------------------------------------------------------------------

_INVENTORY_SQL = """
SELECT entity_id::text, entity_type, name
  FROM entities
 WHERE tenant_id = %(tenant)s
   AND deleted_at IS NULL
"""

_EXISTING_HANDLES_SQL = """
SELECT count(*) FROM entity_handles WHERE tenant_id = %(tenant)s
"""


def inventory_rows(rows: Sequence[tuple[str, str, str]]) -> list[Finding]:
    """Classify entity rows for expansion readiness. Pure, so it is testable.

    Takes tuples rather than a cursor for the same reason resolution takes rows:
    the interesting cases are awkward names, and they are tedious to insert and
    trivial to write down.
    """
    findings: list[Finding] = []

    for entity_id, entity_type, name in rows:
        if not entity_type or not entity_type.strip():
            findings.append(
                Finding(True, "entity-without-type", f"{entity_id} has no entity_type, so it cannot be qualified")
            )
            continue
        if not name or not name.strip():
            findings.append(Finding(True, "entity-without-name", f"{entity_id} has no name to expand"))
            continue
        if name != unicodedata.normalize("NFC", name):
            findings.append(
                Finding(
                    False,
                    "name-not-nfc",
                    f"{entity_id} name {name!r} is not NFC; it will expand, but two visually identical names "
                    "can differ by encoding and only one of them will be found by a caller who types it",
                )
            )
        if name.lower() != name.casefold():
            findings.append(
                Finding(
                    False,
                    "name-folds-differently-than-it-lowers",
                    f"{entity_id} name {name!r}: lower() and casefold() disagree. The handle key uses lower() to "
                    "match the index, so this row is safe — but any caller comparing with casefold() will miss it",
                )
            )
        if " " in name:
            findings.append(
                Finding(
                    True,
                    "name-contains-space",
                    f"{entity_id} name {name!r} cannot appear in a qualified handle, which has no escaping",
                )
            )

    # Names shared across types are the state this migration exists to permit,
    # so they are not a finding on their own. What matters is that each one will
    # need qualifying at every call site that looks it up by bare name.
    by_name: dict[str, set[str]] = {}
    for _entity_id, entity_type, name in rows:
        if name and entity_type:
            by_name.setdefault(name.lower(), set()).add(entity_type)
    for name, types in sorted(by_name.items()):
        if len(types) > 1:
            findings.append(
                Finding(
                    False,
                    "name-will-be-ambiguous-unqualified",
                    f"{name!r} is used by {sorted(types)}; unqualified lookups of it will be refused after "
                    "expansion, which is correct and will break any caller that has not qualified",
                )
            )
    return findings


def inventory_database(database_url: str, tenant: str) -> list[Finding]:
    """Read-only inventory of one tenant. Imports the driver lazily.

    Lazily because `--check` must work on a machine with no database libraries
    configured, and a module-level import would make the DB-free mode depend on
    the DB-full one.
    """
    import psycopg2  # type: ignore[import-untyped]  # noqa: PLC0415 - see docstring: --check must not need a driver

    findings: list[Finding] = []
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(_EXISTING_HANDLES_SQL, {"tenant": tenant})
        existing = int((cursor.fetchone() or (0,))[0])
        if existing:
            findings.append(
                Finding(
                    False,
                    "handles-already-present",
                    f"{existing} handle(s) already exist for this tenant; expansion is append-only and will add "
                    "to them rather than replace them",
                )
            )
        cursor.execute(_INVENTORY_SQL, {"tenant": tenant})
        rows = [(str(row[0]), str(row[1] or ""), str(row[2] or "")) for row in cursor.fetchall()]
    findings.extend(inventory_rows(rows))
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exercise the migration's own rules. Needs no database.",
    )
    parser.add_argument("--database-url", help="Inventory a live tenant (read-only).")
    parser.add_argument("--tenant", help="Tenant UUID to inventory.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    if not arguments.check and not arguments.database_url:
        print("preflight: pass --check, or --database-url with --tenant", file=sys.stderr)
        return 2
    if arguments.database_url and not arguments.tenant:
        print("preflight: --database-url needs --tenant; a whole-instance sweep is not a preflight", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    if arguments.check:
        findings.extend(run_rule_checks())
        print(f"preflight: ran {len(_CHECKS)} rule check(s)")
    if arguments.database_url:
        findings.extend(inventory_database(arguments.database_url, arguments.tenant))

    blocking = [finding for finding in findings if finding.blocking]
    advisory = [finding for finding in findings if not finding.blocking]
    for finding in findings:
        print(str(finding), file=sys.stderr if finding.blocking else sys.stdout)

    print(f"preflight: {len(blocking)} blocking, {len(advisory)} advisory")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
