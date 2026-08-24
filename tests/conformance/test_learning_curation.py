"""Contradictions stay visible, and a promotion proposal keeps its own authority.

Two invariants hold this surface together, and both fail quietly.

**A conflict must never resolve itself.** Grouping contradictory claims is a
read: it decides *that* claims disagree and hands the question to a person. The
moment grouping or case-keeping also writes a claim, the store starts picking
winners — and a store that silently picks winners looks exactly like one that had
no conflict, which is the state this whole surface exists to make impossible.

**The three promotion targets are not interchangeable.** A canonical fact is one
row about one subject; a runbook is a procedure a person follows; an agent-
readiness artifact changes what every agent reading it will do. They differ in
who may approve, what evidence that approver needs, how far the write reaches,
what happens to what the target said before, and how it is undone. Collapsing
them into one "propose" with a free-text note would make the one that reaches
every agent look as consequential as the one that does not — so the distinctness
is asserted field by field here rather than trusted to a reviewer noticing.

Static and structural, not behavioural. A runtime test only covers the paths it
exercises; these rules have to hold for the disposition nobody has written a
handler for yet, and for the second writer nobody has added.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from contextplane.service.memory.curation_cases import (
    CASE_OPEN,
    CASE_RESOLVED,
    CASE_ROUTED,
    DISPOSITION_CONFIRM,
    DISPOSITION_MIGRATED_CANONICAL,
    DISPOSITION_PROPOSE_ARC,
    DISPOSITION_PROPOSE_CANONICAL,
    DISPOSITION_PROPOSE_RUNBOOK,
    DISPOSITION_REJECT,
    DISPOSITION_SUPERSEDE,
    DISPOSITIONS,
    TARGET_ARC_ARTIFACT,
    TARGET_CANONICAL_FACT,
    TARGET_RUNBOOK,
    policy_for,
)

_REPO = Path(__file__).parent.parent.parent
_VERSIONS = _REPO / "contextplane" / "storage" / "migrations" / "versions"
#: Where the case-status set was created and where it still lives. Named
#: directly, unlike the disposition set: nothing has widened it, and a scan would
#: imply a moving target where there is not one.
_STATUS_MIGRATION = _VERSIONS / "0042_derivation_and_curation.py"
_CURATION_QUEUE = _REPO / "contextplane" / "service" / "memory" / "curation_queue.py"
_CONTEST = _REPO / "contextplane" / "service" / "memory" / "contest.py"

# The dispositions that settle the disagreement themselves, and the three that
# ask another surface to write something.
_SETTLING = (DISPOSITION_CONFIRM, DISPOSITION_REJECT, DISPOSITION_SUPERSEDE)
_PROPOSING = (
    DISPOSITION_PROPOSE_CANONICAL,
    DISPOSITION_PROPOSE_RUNBOOK,
    DISPOSITION_PROPOSE_ARC,
)

# The one that reaches an existing target by a different evidence route, rather
# than naming a fourth target. ADR 0022: a migration is a lot, and what differs
# from promoting a canonical fact is the evidence -- a statement about a lot
# rather than about one claim. It is kept out of `_PROPOSING` deliberately, and
# `test_the_three_targets_disagree_on_every_policy_axis` is why: that rule is
# about *targets* disagreeing, and this shares one on purpose.
_ACCEPTING = (DISPOSITION_MIGRATED_CANONICAL,)

# The policy axes each proposal target must answer differently. Named here rather
# than inlined so a seventh disposition cannot be added with one axis left to
# collide with an existing target's.
_DISTINCT_AXES = ("approval_authority", "evidence_threshold", "scope", "supersession", "rollback")

# Tables a curation case must never write. Recording a decision is not performing
# it: each of these has its own approval contract on its own surface.
_PROMOTION_TARGET_TABLES = (
    "memory_claims",
    "attributes",
    "edges",
    "arc_artifacts",
    "memory_promotion_journal",
)

_WRITE_RE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", re.IGNORECASE)

#: `0088` builds its set by interpolating `0042`'s six, so resolving the pin
#: means resolving that reference. Spelled out here rather than imported for the
#: same reason the migrations spell it out: what an already-migrated database
#: *was* must not change when the Python constants do.
_SIX_LITERAL = "'confirm', 'reject', 'supersede', 'propose_canonical', 'propose_runbook', 'propose_arc'"


# ---------------------------------------------------------------------------
# The disposition vocabulary is complete and matches what the database allows.
# ---------------------------------------------------------------------------


def test_the_seven_dispositions_are_the_whole_vocabulary() -> None:
    """Three settle, three propose a target, one accepts a lot into an existing
    one. A disposition in none of those groups is one nobody classified."""
    assert set(DISPOSITIONS) == set(_SETTLING) | set(_PROPOSING) | set(_ACCEPTING)


def test_accepting_a_lot_differs_from_promoting_a_claim_only_in_its_evidence() -> None:
    """ADR 0022's core claim, pinned where the vocabulary rules live.

    Scope, supersession and rollback are properties of the *target*, so a
    migration answering them differently would be writing into a second canonical
    graph. The evidence is the one dimension that is this disposition's own: a
    statement about a lot rather than about one claim.
    """
    migrated = DISPOSITIONS[DISPOSITION_MIGRATED_CANONICAL]
    promoted = DISPOSITIONS[DISPOSITION_PROPOSE_CANONICAL]

    assert migrated.target_kind == promoted.target_kind
    for axis in ("approval_authority", "scope", "supersession", "rollback"):
        assert getattr(migrated, axis) == getattr(promoted, axis), (
            f"{axis} is a property of the canonical-fact target; a second route to it "
            "answering differently would be a second canonical graph"
        )
    assert migrated.evidence_threshold != promoted.evidence_threshold


def test_every_disposition_names_its_authority_and_evidence() -> None:
    """A stored disposition whose approver is decided afterwards is a decision
    nobody is accountable for, so no policy may leave either field blank."""
    for name, policy in DISPOSITIONS.items():
        assert policy.approval_authority.strip(), f"{name} names no approval authority"
        assert policy.evidence_threshold.strip(), f"{name} states no evidence threshold"
        assert policy.scope.strip(), f"{name} states no scope"
        assert policy.supersession.strip(), f"{name} states no supersession rule"
        assert policy.rollback.strip(), f"{name} states no rollback path"
        assert policy.audit_action.strip(), f"{name} emits no audit action"


def test_disposition_vocabulary_matches_the_database_check_constraint() -> None:
    """Drift here is the worst kind: a disposition the service accepts and the
    database rejects fails at write time, after the owner has decided."""
    # The *latest* migration that pins the set, discovered rather than named.
    # `0042` created the constraint and `0088` widened it; a test pointing at
    # whichever one was current when it was written goes stale silently the next
    # time the vocabulary moves, which is exactly the drift it exists to catch.
    pinned: tuple[str, set[str]] | None = None
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        match = re.search(r"_(?:DISPOSITIONS|SEVEN) = f?\"([^\"]+)\"", source)
        if match is None:
            continue
        raw = match.group(1).replace("{_SIX}", _SIX_LITERAL)
        pinned = (path.name, {v.strip().strip("'") for v in raw.split(",")})

    assert pinned is not None, "no migration declares the disposition set"
    name, in_db = pinned
    assert in_db == set(DISPOSITIONS), f"{name} and DISPOSITIONS disagree"


def test_case_status_vocabulary_matches_the_database_check_constraint() -> None:
    source = _STATUS_MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"_CASE_STATUSES = \"([^\"]+)\"", source)
    assert match is not None, "migration no longer declares _CASE_STATUSES"
    in_db = {v.strip().strip("'") for v in match.group(1).split(",")}
    assert in_db == {CASE_OPEN, CASE_ROUTED, CASE_RESOLVED}


def test_an_unknown_disposition_is_refused_rather_than_defaulted() -> None:
    """Defaulting would store a decision under a borrowed authority, which reads
    afterwards as something somebody was accountable for."""
    with pytest.raises(Exception, match="unknown disposition"):
        policy_for("promote_everything")


# ---------------------------------------------------------------------------
# Settling and proposing are different acts.
# ---------------------------------------------------------------------------


def test_settling_a_disagreement_asks_for_no_write() -> None:
    """`confirm`/`reject`/`supersede` decide the contested claim and nothing
    beyond it, so none of them names a promotion target."""
    for name in _SETTLING:
        assert DISPOSITIONS[name].target_kind is None, f"{name} names a promotion target"


def test_each_proposal_names_exactly_one_distinct_target() -> None:
    targets = {name: DISPOSITIONS[name].target_kind for name in _PROPOSING}
    assert set(targets.values()) == {TARGET_CANONICAL_FACT, TARGET_RUNBOOK, TARGET_ARC_ARTIFACT}
    assert len(set(targets.values())) == len(_PROPOSING), "two proposals share one target"


def test_only_proposals_name_an_approver_outside_curation() -> None:
    """The curator's own review authority settles a disagreement; a write to a
    canonical, operational, or agent-facing target needs somebody else."""
    for name in _SETTLING:
        assert DISPOSITIONS[name].approval_authority == "curation_owner"
    for name in _PROPOSING:
        assert DISPOSITIONS[name].approval_authority != "curation_owner"


@pytest.mark.parametrize("axis", _DISTINCT_AXES)
def test_the_three_targets_disagree_on_every_policy_axis(axis: str) -> None:
    """The whole reason these are three dispositions and not one: if any axis
    were shared, that axis would be a property of "proposing" rather than of the
    target, and the target that reaches every agent would stop being visibly
    more consequential than the one that does not."""
    values = [getattr(DISPOSITIONS[name], axis) for name in _PROPOSING]
    assert len(set(values)) == len(values), f"proposal targets share a {axis}: {values}"


# ---------------------------------------------------------------------------
# Neither grouping nor case-keeping writes what it is about.
# ---------------------------------------------------------------------------


def _written_tables(path: Path) -> set[str]:
    """Every table the module's SQL string literals write to.

    Reads string literals from the AST rather than the raw file so a table named
    in a docstring or a comment is not mistaken for a write. Implicit
    concatenation is joined per-expression first, because this codebase's SQL is
    written as adjacent literals and a statement split across two of them would
    otherwise hide its verb from the regex.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    written: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in node.args:
                fragment = _flatten_string(arg)
                if fragment:
                    written.update(m.group(2).lower() for m in _WRITE_RE.finditer(fragment))
    return written


def _flatten_string(node: ast.AST) -> str:
    """One SQL string built from adjacent literals and f-strings, or ""."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_string(node.left) + _flatten_string(node.right)
    return ""


def test_the_case_surface_never_writes_a_promotion_target() -> None:
    """A disposition is a proposal. If this module could write the target, the
    owner's decision and the write it proposes would be one event, and the
    approval contract on the target's own surface would be unreachable."""
    written = _written_tables(_CURATION_QUEUE)
    forbidden = written & set(_PROMOTION_TARGET_TABLES)
    assert not forbidden, f"curation_queue.py writes a promotion target: {sorted(forbidden)}"


def test_the_case_surface_writes_only_its_own_table_and_the_audit_log() -> None:
    """Stated as a closed set rather than a list of prohibitions: a new write to
    some fourth table should have to be justified here, not merely not-forbidden."""
    assert _written_tables(_CURATION_QUEUE) <= {"curation_cases", "audit_log"}


def test_contradiction_grouping_is_a_read() -> None:
    """Grouping decides that claims disagree. Deciding which one wins is a
    person's job, and a grouping pass that wrote a claim would be picking."""
    tree = ast.parse(_CONTEST.read_text(encoding="utf-8"))
    grouping = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "groups_for"]
    assert grouping, "contest.py no longer defines groups_for"

    written: set[str] = set()
    for node in ast.walk(grouping[0]):
        if isinstance(node, ast.Call):
            for arg in node.args:
                fragment = _flatten_string(arg)
                if fragment:
                    written.update(m.group(2).lower() for m in _WRITE_RE.finditer(fragment))
    assert not written, f"groups_for writes to {sorted(written)}"


# ---------------------------------------------------------------------------
# The negative fixtures: the walker detects what it claims to.
# ---------------------------------------------------------------------------


def test_the_write_detector_finds_a_planted_write(tmp_path: Path) -> None:
    """Without this, every assertion above could be passing because the AST walk
    matches nothing at all."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from sqlalchemy import text\n"
        "def go(session):\n"
        '    session.execute(text("UPDATE memory_claims SET is_contested = TRUE"))\n',
        encoding="utf-8",
    )
    assert "memory_claims" in _written_tables(planted)


def test_the_write_detector_reads_across_implicit_concatenation(tmp_path: Path) -> None:
    """The shape this codebase actually writes SQL in: a verb on one literal and
    its table on the next. A regex over raw source would miss it."""
    planted = tmp_path / "split.py"
    planted.write_text(
        "from sqlalchemy import text\n"
        "def go(session):\n"
        '    session.execute(text("INSERT INTO "\n'
        '                         "arc_artifacts (artifact_id) VALUES (:a)"))\n',
        encoding="utf-8",
    )
    assert "arc_artifacts" in _written_tables(planted)


def test_the_write_detector_ignores_a_table_named_only_in_prose(tmp_path: Path) -> None:
    """A docstring explaining why a module does *not* write a table must not be
    read as writing it -- the modules under test both contain exactly that."""
    planted = tmp_path / "prose.py"
    planted.write_text('"""This module never runs UPDATE memory_claims itself."""\n', encoding="utf-8")
    assert _written_tables(planted) == set()
