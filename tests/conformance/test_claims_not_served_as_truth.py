"""Staged claims stay staged: no capability read path can return one.

A claim is not a fact. It is something a session or a connector asserted, not
yet corroborated, scored, or promoted — and the whole design rests on a consumer
being unable to mistake one for the graph's answer to "what is true". That
separation is not a property of the claim tables; it is a property of which code
reads them. So it is a gate.

The failure this prevents is quiet and one-directional. A capability endpoint
that joins in a staged claim to look more complete starts returning unverified
assertions with the same shape and the same authority as canonical rows, and
nothing downstream can tell the difference afterwards — not the consumer, not
the audit log, not a later reader of the response. There is no route from a
claim to the canonical graph at all until consolidation and promotion exist, and
until then the only safe rule is that the capability read surface cannot see the
claim tables.

Two routes in, so two checks. Naming a claim table is the obvious one. Holding a
`ClaimService` is the other, and it would read claims without any table name
appearing in the file. The table check runs across the whole package rather than
only the enumerated capability modules, because a claim leaks through whichever
module reads it — not only through the ones this gate thought to list.

Static, not behavioural, and deliberately so: a runtime assertion only covers the
paths a test happens to exercise, while the rule has to hold for the endpoint
nobody wrote a test for yet.

Negative fixtures matter as much as the real assertions: they prove the walker
detects what it claims to, rather than passing because it matches nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).parent.parent.parent / "registry"

# The claim tables. Reading either from a capability path is the failure.
_CLAIM_TABLES = ("lmm_claims", "lmm_claim_provenance")

_CLAIM_TABLE_RE = re.compile("|".join(re.escape(t) for t in _CLAIM_TABLES), re.IGNORECASE)

# Modules permitted to touch the claim tables at all. Deliberately short: the
# write path, and the routers/services that exist specifically to serve claims
# as claims. A capability path is never on this list.
_CLAIM_AWARE: frozenset[str] = frozenset(
    {
        "service/claims.py",
        # Reads claims to find ones that disagree, and sets the derived flag that
        # records it. Serves nothing: it has no route to a response, and a
        # disagreement lowers confidence and blocks promotion rather than
        # publishing anything. On this list because it must read the tables, not
        # because it may expose them.
        "service/contest.py",
        # Reads a claim to decide whether it can be confirmed, and marks the
        # original superseded. Creates nothing: the write itself goes through the
        # claim service, so the one-writer rule holds. Serves nothing either -- a
        # confirmation raises a score and blocks nothing from being reviewed.
        "service/confirmation.py",
        # Joins claims to judged outcomes when fitting a mapping. Reads the strategy
        # a claim came from and nothing a consumer would see: the output is a
        # calibration row, not a response. Writes nothing to the claim tables.
        "service/calibration.py",
        # Reads a claim's neighbourhood to decide whether it adds anything, and closes
        # what it supersedes. Writes only status, the successor pointer, and the
        # reconciliation timestamp -- never a field describing what is asserted. Serves
        # nothing: the output is a decision and an audit row, not a response.
        "service/consolidation.py",
    }
)

# The capability read surface — what a consumer calls to ask what is true.
# Every module here is checked, and the set is asserted non-empty below so a
# rename cannot silently empty the gate.
_CAPABILITY_SURFACE: tuple[str, ...] = (
    "service/catalog.py",
    "service/retrieval.py",
    "service/visibility.py",
    "service/projections.py",
    "service/facts.py",
    "api/routers/capabilities.py",
    "api/routers/retrieval.py",
    "api/routers/graph.py",
    "api/routers/concepts.py",
)


def _existing_capability_modules() -> list[Path]:
    return [_PACKAGE / rel for rel in _CAPABILITY_SURFACE if (_PACKAGE / rel).is_file()]


def test_the_capability_surface_is_not_empty() -> None:
    """A gate that checks nothing passes trivially. If every path here was
    renamed, this fails rather than the suite going quietly green."""
    found = _existing_capability_modules()
    assert len(found) >= 4, f"capability surface shrank to {[p.name for p in found]}"


@pytest.mark.parametrize("module", _existing_capability_modules(), ids=lambda p: p.name)
def test_no_capability_module_names_a_claim_table(module: Path) -> None:
    """A capability endpoint joining in a staged claim to look more complete
    returns unverified assertions with canonical authority, and nothing
    downstream can tell afterwards."""
    hits = [
        (i + 1, line.strip())
        for i, line in enumerate(module.read_text(encoding="utf-8").splitlines())
        if _CLAIM_TABLE_RE.search(line)
    ]
    rel = module.relative_to(_PACKAGE)
    assert not hits, f"{rel} references a claim table: {hits}"


def test_only_the_write_path_references_the_claim_tables() -> None:
    """Wider than the capability surface: any unexpected reader is a finding,
    because a claim leaks through whichever module reads it, not only through
    the ones this gate happened to enumerate."""
    offenders: dict[str, list[int]] = {}
    for path in _PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts or "migrations" in path.parts:
            continue
        rel = str(path.relative_to(_PACKAGE))
        if rel in _CLAIM_AWARE:
            continue
        lines = [
            i + 1
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if _CLAIM_TABLE_RE.search(line)
        ]
        if lines:
            offenders[rel] = lines

    assert not offenders, (
        "modules outside the claim write path reference the claim tables: "
        f"{offenders}. Serving a staged claim through a capability read path is "
        "how an unverified assertion acquires canonical authority. If a module "
        "genuinely serves claims as claims, add it to _CLAIM_AWARE."
    )


def test_the_walker_detects_a_planted_reference(tmp_path: Path) -> None:
    """Negative fixture. Without this, the gate above could be passing because
    the pattern matches nothing rather than because the rule holds."""
    planted = tmp_path / "rogue.py"
    planted.write_text('SQL = "SELECT value_jsonb FROM lmm_claims"\n', encoding="utf-8")
    assert _CLAIM_TABLE_RE.search(planted.read_text(encoding="utf-8"))


def test_the_walker_does_not_match_an_unrelated_table() -> None:
    """The converse fixture: a pattern that matched everything would also pass
    the assertions above."""
    assert not _CLAIM_TABLE_RE.search('SQL = "SELECT * FROM entities"')


def test_no_capability_module_imports_the_claim_service() -> None:
    """The table names are one route in; the service is the other. A capability
    module holding a ClaimService could read claims without naming a table."""
    offenders: list[str] = []
    for module in _existing_capability_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("service.claims"):
                offenders.append(f"{module.relative_to(_PACKAGE)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{module.relative_to(_PACKAGE)}:{node.lineno}"
                    for alias in node.names
                    if alias.name.endswith("service.claims")
                )
    assert not offenders, f"capability modules import the claim service: {offenders}"
