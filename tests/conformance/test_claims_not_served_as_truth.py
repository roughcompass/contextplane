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

_PACKAGE = Path(__file__).parent.parent.parent / "contextplane"

# The claim tables. Reading either from a capability path is the failure.
_CLAIM_TABLES = ("memory_claims", "memory_claim_provenance")

_CLAIM_TABLE_RE = re.compile("|".join(re.escape(t) for t in _CLAIM_TABLES), re.IGNORECASE)

# Modules permitted to touch the claim tables at all. Deliberately short: the
# write path, and the routers/services that exist specifically to serve claims
# as claims. A capability path is never on this list.
_CLAIM_AWARE: frozenset[str] = frozenset(
    {
        # The only entry here that is not a reader. It *declares* the foreign key
        # binding a derivation attempt to the claim it produced -- declares, never
        # queries: the module is mapped-column definitions with no session, no
        # query, and no route to a response, so there is nothing for a claim to
        # leak through. `memory_claims` has no mapped class anywhere in the tree
        # (it is reached by raw SQL), so the target has no class attribute to
        # reference and a foreign key can only name it as a string. Spelling it
        # some other way to slip past this sweep would hide a real reference from
        # a guard whose whole value is seeing them.
        "service/memory/models.py",
        # The write path, split across four cooperating modules: the
        # machine/system path and lifecycle operations, the two curator
        # decisions, the read-only resolution/authority checks both of those
        # depend on (it reads memory_claim_provenance to re-derive authority
        # once a subject resolves), and the actor-erasure participant's
        # claims-table writer.
        "service/memory/claim_writer.py",
        "service/memory/claim_curator_actions.py",
        "service/memory/claim_authority.py",
        "service/memory/claim_erasure_writes.py",
        # Provenance-scoped quarantine. Reads both tables to decide which
        # claims a predicate reaches -- `memory_claim_provenance` is where a
        # connector run is recorded, so selecting by provenance is not possible
        # without it -- and writes `quarantined_at` on the rows it withholds.
        #
        # On this list for the narrowest possible reason: it can only ever make
        # *fewer* claims servable. It has no route to a response that carries
        # claim content; `preview` returns bare ids, `apply` returns the ids it
        # withheld, and neither exposes a value, a predicate, a subject or a
        # confidence. The risk this whole gate exists for -- a staged claim
        # acquiring canonical authority by leaking through a capability read --
        # is not reachable from a surface whose entire output is "these stopped
        # being served".
        "service/memory/quarantine.py",
        # Erasure must select the claims to delete — its two selection queries
        # read the tables to decide what dies, and every row it touches stops
        # existing. Serves nothing: its only output is per-table delete counts.
        "service/memory/claim_erasure.py",
        # Reads claims to find ones that disagree, and sets the derived flag that
        # records it. Serves nothing: it has no route to a response, and a
        # disagreement lowers confidence and blocks promotion rather than
        # publishing anything. On this list because it must read the tables, not
        # because it may expose them.
        "service/memory/contest.py",
        # Reads a claim to decide whether it can be confirmed, and marks the
        # original superseded. Creates nothing: the write itself goes through the
        # claim service, so the one-writer rule holds. Serves nothing either -- a
        # confirmation raises a score and blocks nothing from being reviewed.
        "service/memory/confirmation.py",
        # Joins claims to judged outcomes when fitting a mapping. Reads the strategy
        # a claim came from and nothing a consumer would see: the output is a
        # calibration row, not a response. Writes nothing to the claim tables.
        "service/memory/calibration.py",
        # Reads a claim's neighbourhood to decide whether it adds anything, and closes
        # what it supersedes. Writes only status, the successor pointer, and the
        # reconciliation timestamp -- never a field describing what is asserted. Serves
        # nothing: the output is a decision and an audit row, not a response.
        "service/memory/consolidation.py",
        # The claim-specific read surface. Reading claims is its entire purpose, and
        # that is permitted: what the rule forbids is a *capability* path returning a
        # claim as though it were canonical. This one answers "what did we believe, and
        # when did it change", which only makes sense as a question about claims.
        "service/memory/claim_history.py",
        # Finds claims that need reconciling and hands each to consolidation. Reads ids
        # and nothing a consumer would see.
        "workers/consolidation_sweep.py",
        # Counts how often claims of one predicate were superseded, to fit that
        # predicate's decay half-life. Reads two columns -- the predicate and whether
        # a supersession happened -- and never the asserted value, so no claim's
        # content passes through it. Its output is a rate per predicate, and even that
        # is not selected until a human has inspected it.
        "service/memory/predicate_churn.py",
        # Reads a claim to decide whether it may become canonical, and records where
        # it stands afterwards. The promotion state write goes through the claim
        # service, so the one-writer rule holds. This is the module that *stops* a
        # claim being served as truth without an owner's decision, so forbidding it
        # from reading claims would forbid the check itself.
        # The claim read surface itself. Reading claims is its entire purpose, and it
        # is where the rule is *enforced*: every claim leaving it carries citations
        # and an untrusted-recall label, and is filtered by both its own visibility
        # and its subject's. Forbidding it from reading claims would forbid the
        # governed path and leave only ungoverned ones.
        # Reads a claim to decide whether it belongs in the embedding index, and to
        # render the text that gets embedded. Writes nothing to the claim tables and has
        # no route to a response -- its output is a queue row and, eventually, a vector.
        # It is also the module that *retracts* a retired claim's vectors, so forbidding
        # it from reading claims would forbid the cleanup that keeps unservable claims
        # out of ranked results.
        "service/retrieval/embedding_index.py",
        "service/memory/claim_serving.py",
        "service/memory/promotion.py",
        # Reads a claim's status, subject, and neighbourhood to decide eligibility and
        # impact. Writes nothing at all. The output is a classification a reviewer
        # sees, never a response a consumer sees.
        "service/memory/promotion_eligibility.py",
        # ARC's third source-admission authority reads a claim to turn a completed
        # promotion into citable source evidence. It cannot reach a staged claim at
        # all: its one claim query INNER JOINs `memory_promotion_journal`, so a claim
        # nobody promoted returns no row, and a promotion that was later reversed is
        # refused by the service above it. That is the rule this gate states, not an
        # exception to it -- what becomes ARC evidence is the promotion decision, and
        # the claim content it carries is already canonical by the time it is read.
        # Writes nothing: every function in the module is a SELECT.
        "arc/service/queries/source_admission_graph.py",
        # The curator's read surface. Lists claims that need a human precisely because
        # they are *not* canonical -- unlinked, contested, below floor, or awaiting an
        # owner. Serving those as truth is what it exists to prevent.
        "service/memory/curation_queue.py",
        # Walks staged claims to find promotion candidates and reads the pending
        # count for a gauge. Reads ids and pipeline state, never asserted content;
        # every acceptance goes through the promotion service, so the one-writer
        # rule holds. Its output is a proposal or a metric, not a response.
        "workers/promotion_sweep.py",
        # Joins claims to judged outcomes to find which strategies have adjudications
        # worth fitting -- the same join `service/memory/calibration.py` itself makes.
        # Reads a strategy id and nothing a consumer would see; the output is a
        # calibration mapping row, not a response.
        "workers/calibration_refit.py",
        # Counts claims matching the curation queue's own backlog predicate, for
        # the operator console's single "how big is the backlog" reading. The
        # query is a bare `COUNT(*)`; no row, field, or claim content ever
        # leaves it. Not a capability path -- it is the admin health surface,
        # gated separately -- and its output is one number, not a response
        # that could be mistaken for canonical truth.
        "service/operations/health.py",
        # Reads claim ids and timestamps to bucket how long staged claims have been
        # waiting. Selects claims to count them and nothing else: the output is a
        # coarse age histogram over the whole tenant, floored so a bucket only
        # carries a number once enough distinct authors contributed to it. No route
        # returns a claim from here, no claim field reaches a response, and the
        # aggregate cannot be narrowed to a person -- which is the property that
        # makes reading the table safe rather than the absence of a read.
        "service/memory/learning_reads.py",
        # Reads claim ids to find what an erased actor asserted, so it can schedule
        # removal of the vectors, chunks, summaries, caches and exports built from
        # those claims. Every row it selects is on its way out: the query decides
        # what dies, exactly as the claims-erasure entries above it do. It serves
        # nothing -- no route reaches it, and its only output is a per-kind count of
        # propagation items it enqueued. Forbidding it from reading claims would
        # leave the erased person's words in every artefact derived from them, which
        # is the failure this module exists to close.
        "context/derivatives.py",
    }
)

# The capability read surface — what a consumer calls to ask what is true.
# Every module here is checked, and the set is asserted non-empty below so a
# rename cannot silently empty the gate.
_CAPABILITY_SURFACE: tuple[str, ...] = (
    "service/catalog/core.py",
    "service/retrieval/search.py",
    "service/retrieval/graph_cte.py",
    "service/retrieval/graph_traversal.py",
    "service/retrieval/graph_closure_cache.py",
    "service/retrieval/listing.py",
    "service/governance/visibility.py",
    "service/catalog/projections.py",
    "service/catalog/facts.py",
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
    planted.write_text('SQL = "SELECT value_jsonb FROM memory_claims"\n', encoding="utf-8")
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
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("service.memory.claim_writer"):
                offenders.append(f"{module.relative_to(_PACKAGE)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{module.relative_to(_PACKAGE)}:{node.lineno}"
                    for alias in node.names
                    if alias.name.endswith("service.memory.claim_writer")
                )
    assert not offenders, f"capability modules import the claim service: {offenders}"


def test_the_mcp_reference_does_not_claim_workspace_search_is_semantic() -> None:
    """Workspace search is full-text only, and saying otherwise misdirects agents.

    A doc claim rather than a code path, so it is gated here: the incorrect text has
    already been reintroduced once by a merge, and an agent reading it would reach
    for the wrong surface and conclude semantic recall does not work.
    """
    from pathlib import Path

    reference = Path(__file__).resolve().parents[2] / "docs" / "05-reference" / "02-mcp-tools.md"
    body = reference.read_text(encoding="utf-8")

    assert "Full-text + semantic search across entries" not in body
    assert "search_claims" in body, "the semantic-memory surface is named instead"
