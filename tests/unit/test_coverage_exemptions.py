"""Unit tests for the coverage-exemption gate.

This gate ships with both registries empty, which makes its tests the only thing
that ever exercises its failure paths. A gate whose refusals have never been
watched fire is indistinguishable from a gate that always returns green, and
this one guards the single setting that can raise reported coverage without a
new test.

So every refusal is proved by construction against a scratch tree, and the two
the contract names are proved *by removal* in both directions: an exemption
whose named test file is deleted must go red, and one whose test file has
stopped referencing the module must go red. Both start from a green state and
break it, rather than asserting redness about a state that was never green --
an assertion satisfied by both the working and the broken arrangement would
guard nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable without installation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_coverage_exemptions as gate  # noqa: E402
from check_coverage_exemptions import (  # noqa: E402
    DRAINABLE_DEBT,
    MEASURED_TIERS,
    TIER_EXEMPTIONS,
    UNMEASURED_TIERS,
    DrainableDebt,
    TierExemption,
    configured_omit,
    reachable_from_measured_tiers,
    violations,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _tree(root: Path, omit: list[str], *, module: str = "contextplane/extraction/adapter.py") -> Path:
    """A scratch repo with one omitted product module and no tests importing it."""
    (root / "pyproject.toml").write_text(
        "[tool.coverage.run]\nomit = [\n" + "".join(f'    "{p}",\n' for p in omit) + "]\n",
        encoding="utf-8",
    )
    target = root / module
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    for tier in MEASURED_TIERS:
        (root / tier).mkdir(parents=True, exist_ok=True)
        (root / tier / "test_unrelated.py").write_text(
            "def test_nothing() -> None:\n    assert True\n", encoding="utf-8"
        )
    (root / "tests/integration").mkdir(parents=True, exist_ok=True)
    return root


_MODULE = "contextplane/extraction/adapter.py"
_EXEMPT_TEST = "tests/integration/test_adapter.py"


def _exemption(path: str = _MODULE) -> TierExemption:
    return TierExemption(
        path=path,
        tier="integration",
        test_file=_EXEMPT_TEST,
        reason="Exercised end to end against a live database; no in-process test can reach it.",
    )


def _write_evidence(root: Path, *, references: bool = True) -> None:
    body = "from contextplane.extraction.adapter import VALUE\n" if references else "VALUE = 2\n"
    (root / _EXEMPT_TEST).write_text(f"{body}\n\ndef test_it() -> None:\n    assert True\n", encoding="utf-8")


# --- the shipped state ---------------------------------------------------------


def test_the_real_repository_passes_its_own_gate() -> None:
    assert violations(_REPO_ROOT) == []


def test_both_registries_ship_empty() -> None:
    """Not decoration: the one historical entry measured 92.93% under the tiers
    the ratchet runs, so it failed the eligibility bound rather than moving
    category. An entry appearing here later must clear that bound too."""
    assert TIER_EXEMPTIONS == ()
    assert DRAINABLE_DEBT == ()
    assert configured_omit(_REPO_ROOT) == []


# --- proof by removal, the two directions the contract names ---------------------


def test_a_tier_exemption_is_green_while_its_evidence_stands(tmp_path: Path) -> None:
    """The baseline the two removals below break. Without this, a red result
    proves nothing -- it could have been red for any reason at all."""
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    assert violations(root, tier_exemptions=(_exemption(),), drainable_debt=()) == []


def test_deleting_the_named_test_file_turns_the_exemption_red(tmp_path: Path) -> None:
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    assert violations(root, tier_exemptions=(_exemption(),), drainable_debt=()) == []

    (root / _EXEMPT_TEST).unlink()

    problems = violations(root, tier_exemptions=(_exemption(),), drainable_debt=())
    assert any("does not exist" in problem for problem in problems), problems


def test_dropping_the_module_reference_turns_the_exemption_red(tmp_path: Path) -> None:
    """The subtler direction: the file is still there, still a test, still
    passing -- and no longer evidence for anything."""
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    assert violations(root, tier_exemptions=(_exemption(),), drainable_debt=()) == []

    _write_evidence(root, references=False)

    problems = violations(root, tier_exemptions=(_exemption(),), drainable_debt=())
    assert any("no longer references" in problem for problem in problems), problems


# --- the eligibility bound -------------------------------------------------------


def test_a_module_a_measured_tier_imports_cannot_be_omitted(tmp_path: Path) -> None:
    """The failure the shipped state was in: a module the ratchet's own tiers
    execute has covered statements, so omitting it discards them."""
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    (root / "tests/unit/test_adapter_unit.py").write_text(
        "from contextplane.extraction.adapter import VALUE\n\n\ndef test_it() -> None:\n    assert VALUE == 1\n",
        encoding="utf-8",
    )

    problems = violations(root, tier_exemptions=(_exemption(),), drainable_debt=())
    assert any("cannot be at 0.00%" in problem for problem in problems), problems


def test_reachability_follows_imports_through_production_code(tmp_path: Path) -> None:
    """A module no test names directly is still executed when a module they do
    name imports it. A direct-reference check would miss exactly that."""
    root = _tree(tmp_path, [_MODULE])
    (root / "contextplane/extraction/facade.py").write_text(
        "from contextplane.extraction.adapter import VALUE\n", encoding="utf-8"
    )
    (root / "tests/unit/test_facade.py").write_text(
        "from contextplane.extraction.facade import VALUE\n\n\ndef test_it() -> None:\n    assert VALUE == 1\n",
        encoding="utf-8",
    )

    reached = reachable_from_measured_tiers(root)
    assert _MODULE in reached
    assert "contextplane/extraction/facade.py" in reached


def test_a_module_nothing_imports_is_not_reachable(tmp_path: Path) -> None:
    """The counterpart: a reachability check that returned everything would
    refuse every entry and be indistinguishable from banning the feature."""
    root = _tree(tmp_path, [_MODULE])
    assert reachable_from_measured_tiers(root) == set()


# --- registry and config must agree ------------------------------------------------


def test_a_bare_omit_entry_with_no_registered_reason_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, [_MODULE])
    problems = violations(root, tier_exemptions=(), drainable_debt=())
    assert any("no registered reason" in problem for problem in problems), problems


def test_a_registered_entry_that_is_not_omitted_fails(tmp_path: Path) -> None:
    """A reason for an exemption nobody is taking. Harmless in effect, and it is
    how a registry drifts into describing a config that has moved on."""
    root = _tree(tmp_path, [])
    _write_evidence(root)
    problems = violations(root, tier_exemptions=(_exemption(),), drainable_debt=())
    assert any("not in force" in problem for problem in problems), problems


def test_a_registered_file_that_no_longer_exists_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, ["contextplane/extraction/deleted.py"])
    entry = DrainableDebt(path="contextplane/extraction/deleted.py", reason="No tests reach it yet.")
    problems = violations(root, tier_exemptions=(), drainable_debt=(entry,))
    assert any("no such file" in problem for problem in problems), problems


def test_one_file_registered_twice_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    problems = violations(
        root,
        tier_exemptions=(_exemption(),),
        drainable_debt=(DrainableDebt(path=_MODULE, reason="Also claimed here."),),
    )
    assert any("registered more than once" in problem for problem in problems), problems


def test_an_empty_reason_fails(tmp_path: Path) -> None:
    """`reason` is a required field, so it cannot be omitted -- but a whitespace
    string satisfies the constructor while saying nothing."""
    root = _tree(tmp_path, [_MODULE])
    problems = violations(root, tier_exemptions=(), drainable_debt=(DrainableDebt(path=_MODULE, reason="   "),))
    assert any("empty reason" in problem for problem in problems), problems


def test_an_exemption_naming_a_measured_tier_fails(tmp_path: Path) -> None:
    """Being covered by unit tests is not a reason to leave the unit measurement."""
    root = _tree(tmp_path, [_MODULE])
    _write_evidence(root)
    entry = TierExemption(path=_MODULE, tier="unit", test_file=_EXEMPT_TEST, reason="Covered by unit tests.")
    problems = violations(root, tier_exemptions=(entry,), drainable_debt=())
    assert any("is not one of" in problem for problem in problems), problems
    assert "unit" not in UNMEASURED_TIERS


# --- the CLI --------------------------------------------------------------------


def test_the_gate_exits_zero_on_the_real_repository() -> None:
    assert gate.main([]) == 0


def test_explain_prints_the_rule_and_exits_zero() -> None:
    assert gate.main(["--explain"]) == 0
