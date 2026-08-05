"""The memory-reachability gate is the phase's own exit criterion, so it needs
its own tests -- the same principle as every sibling structural gate in this
suite. A gate that silently matched nothing would let a quarantined service
regress to "built, tested, wired to nothing" with the docstring still claiming
the opposite. Each test here plants one violation and asserts the gate notices,
plus a control fixture proving each mutation is what made the difference, not
an accident of the scratch tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_memory_reachability import (
    QUARANTINE,
    Rule,
    _direct_callers,
    _is_directly_reachable,
    _named_extra_caller,
    _path_to_dotted,
    _transitive_callers,
    evaluate,
    main,
    resolve_targets,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_memory_reachability._REPO_ROOT", tmp_path)
    return tmp_path


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _stub(root: Path, rel: str) -> Path:
    return _write(root, rel, "# stub module\n")


def _general_caller_files(root: Path) -> list[Path]:
    return resolve_targets(
        [
            str(root / "registry/api/routers"),
            str(root / "registry/api/mcp/tools"),
            str(root / "registry/wiring/jobs.py"),
        ]
    )


#: One scratch tree in which every rule in QUARANTINE is satisfied -- the
#: minimal shape check_memory_reachability expects of the real tree, so
#: individual tests can break exactly one piece of it and see exactly one
#: failure, rather than reconstructing the whole tree each time.
_BASELINE_MODULES = (
    "registry/service/memory/promotion.py",
    "registry/service/memory/curation_queue.py",
    "registry/service/memory/contest.py",
    "registry/service/memory/confirmation.py",
    "registry/service/memory/calibration.py",
    "registry/service/memory/capability_requests.py",
    "registry/service/memory/source_governance.py",
    "registry/service/memory/source_ingest.py",
    # Not themselves quarantined, but needed for contest.py's transitive rule.
    "registry/service/memory/claims.py",
    "registry/service/memory/consolidation.py",
)


def _build_baseline(root: Path) -> None:
    for rel in _BASELINE_MODULES:
        _stub(root, rel)

    _write(
        root,
        "registry/api/routers/memory_curation.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.curation_queue import CurationQueueService\n"
        "from registry.service.memory.confirmation import ConfirmationService\n"
        "from registry.service.memory.capability_requests import CapabilityRequestService\n",
    )
    _write(
        root,
        "registry/api/mcp/tools/memory_curation.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.curation_queue import CurationQueueService\n"
        "from registry.service.memory.confirmation import ConfirmationService\n"
        "from registry.service.memory.capability_requests import CapabilityRequestService\n",
    )
    _write(
        root,
        "registry/wiring/jobs.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.calibration import CalibrationService\n"
        "from registry.service.memory.source_governance import SourceGovernanceService\n"
        "from registry.service.memory.claims import ClaimService\n"
        "from registry.service.memory.consolidation import ConsolidationService\n",
    )
    # The named extra_caller for source_ingest.py.
    _write(
        root,
        "registry/ingest/runner.py",
        "from registry.service.memory.source_ingest import Candidate, SourceIngestService\n",
    )
    # The two named intermediates for contest.py's transitive rule -- each
    # calls into contest.py from its own write path.
    _write(
        root,
        "registry/service/memory/claims.py",
        "from registry.service.memory.contest import ContestOutcome, detect_for_claim\n",
    )
    _write(
        root,
        "registry/service/memory/consolidation.py",
        "from registry.service.memory.contest import resolve_contests_for\n",
    )


# ---------------------------------------------------------------------------
# The gate's own subject
# ---------------------------------------------------------------------------


def test_the_real_tree_passes() -> None:
    """The gate's own reason to exist. Fails the moment a quarantined module
    loses its last production caller."""
    assert main([]) == 0


def test_every_rule_states_a_reason() -> None:
    for rule in QUARANTINE:
        assert rule.reason.strip(), f"{rule.module_path} has no stated reason"


def test_quarantine_list_is_not_empty() -> None:
    """A rename that silently drops every entry must not read as a clean run."""
    assert len(QUARANTINE) == 8


def test_exactly_two_named_exceptions_exist() -> None:
    """contest.py (transitive) and source_ingest.py (extra_caller) are the only
    two rules that deviate from the general three-location rule -- if a third
    quietly appeared, or one of these two silently reverted, that is worth
    seeing in a diff, not discovering by reading every rule by hand."""
    extra = [r.module_path for r in QUARANTINE if r.extra_caller]
    transitive = [r.module_path for r in QUARANTINE if r.transitive_via]
    assert extra == ["registry/service/memory/source_ingest.py"]
    assert transitive == ["registry/service/memory/contest.py"]


def test_path_to_dotted_converts_a_module_file_to_its_import_name() -> None:
    assert _path_to_dotted("registry/service/memory/promotion.py") == "registry.service.memory.promotion"


# ---------------------------------------------------------------------------
# The baseline itself -- the control every mutation test below is checked against
# ---------------------------------------------------------------------------


def test_the_baseline_scratch_tree_is_itself_reachable(repo_root: Path) -> None:
    """Without this, every mutation test below could be failing for the wrong
    reason -- a baseline that was never reachable in the first place."""
    _build_baseline(repo_root)
    assert main([]) == 0


# ---------------------------------------------------------------------------
# The general rule: routers / mcp tools / wiring/jobs.py
# ---------------------------------------------------------------------------


def test_a_router_import_alone_satisfies_the_general_rule(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/promotion.py")
    _write(
        repo_root,
        "registry/api/routers/memory_curation.py",
        "from registry.service.memory.promotion import PromotionService\n",
    )
    rule = Rule(module_path="registry/service/memory/promotion.py", reason="")
    callers = _direct_callers(rule, _general_caller_files(repo_root))
    assert [c.path for c in callers] == ["registry/api/routers/memory_curation.py"]


def test_an_mcp_tool_import_alone_satisfies_the_general_rule(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/confirmation.py")
    _write(
        repo_root,
        "registry/api/mcp/tools/memory_curation.py",
        "from registry.service.memory.confirmation import ConfirmationService\n",
    )
    rule = Rule(module_path="registry/service/memory/confirmation.py", reason="")
    callers = _direct_callers(rule, _general_caller_files(repo_root))
    assert [c.path for c in callers] == ["registry/api/mcp/tools/memory_curation.py"]


def test_a_wiring_jobs_import_alone_satisfies_the_general_rule(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/calibration.py")
    _write(
        repo_root,
        "registry/wiring/jobs.py",
        "from registry.service.memory.calibration import CalibrationService\n",
    )
    rule = Rule(module_path="registry/service/memory/calibration.py", reason="")
    callers = _direct_callers(rule, _general_caller_files(repo_root))
    assert [c.path for c in callers] == ["registry/wiring/jobs.py"]


def test_a_test_file_import_does_not_count(repo_root: Path) -> None:
    """A quarantined module's own integration test constructs it directly --
    that is the exact 'tested but unreachable' gap this gate exists to catch,
    not evidence of closing it."""
    _stub(repo_root, "registry/service/memory/promotion.py")
    test_file = _write(
        repo_root,
        "tests/integration/test_promotion.py",
        "from registry.service.memory.promotion import PromotionService\n",
    )
    rule = Rule(module_path="registry/service/memory/promotion.py", reason="")
    assert _direct_callers(rule, [test_file]) == ()


def test_the_modules_own_file_does_not_count_as_its_own_caller(repo_root: Path) -> None:
    own_file = _write(
        repo_root,
        "registry/service/memory/promotion.py",
        "from registry.service.memory.promotion import PromotionService\n",
    )
    rule = Rule(module_path="registry/service/memory/promotion.py", reason="")
    assert _direct_callers(rule, [own_file]) == ()


def test_a_module_with_no_caller_anywhere_is_unreachable(repo_root: Path) -> None:
    """The gate's sharp edge: nothing anywhere imports this module. If this
    doesn't fail, the gate enforces nothing for the exact case it exists for."""
    _stub(repo_root, "registry/service/memory/promotion.py")
    rule = Rule(module_path="registry/service/memory/promotion.py", reason="")
    finding = evaluate(rule, _general_caller_files(repo_root))
    assert finding.reachable is False
    assert finding.callers == ()


# ---------------------------------------------------------------------------
# The named exception: source_ingest.py -> registry/ingest/runner.py
# ---------------------------------------------------------------------------


def test_the_runner_extra_caller_alone_satisfies_source_ingest(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/source_ingest.py")
    _write(
        repo_root,
        "registry/ingest/runner.py",
        "from registry.service.memory.source_ingest import SourceIngestService\n",
    )
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/source_ingest.py")
    finding = evaluate(rule, _general_caller_files(repo_root))
    assert finding.reachable is True
    assert [c.path for c in finding.callers] == ["registry/ingest/runner.py"]


def test_named_extra_caller_returns_none_when_the_file_does_not_import_it(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/source_ingest.py")
    _write(repo_root, "registry/ingest/runner.py", "# does not import the service\n")
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/source_ingest.py")
    assert _named_extra_caller(rule) is None


def test_named_extra_caller_returns_none_when_the_file_is_missing(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/source_ingest.py")
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/source_ingest.py")
    assert _named_extra_caller(rule) is None


def test_source_ingest_is_reported_unreachable_with_neither_caller(repo_root: Path) -> None:
    """Full-tree proof: strip both source_ingest.py's job-wiring import and its
    runner-bridge import, and the gate must call it out by name."""
    _build_baseline(repo_root)
    _write(
        repo_root,
        "registry/wiring/jobs.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.calibration import CalibrationService\n"
        "from registry.service.memory.source_governance import SourceGovernanceService\n"
        "from registry.service.memory.claims import ClaimService\n"
        "from registry.service.memory.consolidation import ConsolidationService\n",
    )
    _write(repo_root, "registry/ingest/runner.py", "# no longer bridges to source_ingest\n")

    assert main([]) == 1


# ---------------------------------------------------------------------------
# The named exception: contest.py -> transitive via claims.py / consolidation.py
# ---------------------------------------------------------------------------


def test_transitive_caller_succeeds_when_the_intermediate_is_itself_reachable(repo_root: Path) -> None:
    _stub(repo_root, "registry/service/memory/contest.py")
    _write(
        repo_root,
        "registry/service/memory/claims.py",
        "from registry.service.memory.contest import detect_for_claim\n",
    )
    _write(
        repo_root,
        "registry/wiring/jobs.py",
        "from registry.service.memory.claims import ClaimService\n",
    )
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/contest.py")
    callers = _transitive_callers(rule, _general_caller_files(repo_root))
    assert [c.path for c in callers] == ["registry/service/memory/claims.py"]


def test_transitive_caller_fails_when_the_intermediate_itself_has_no_caller(repo_root: Path) -> None:
    """The sharp half of the transitive check: claims.py imports contest.py,
    but nothing imports claims.py itself. If the gate only checked the first
    half, this would pass on the strength of a chain nobody actually closed."""
    _stub(repo_root, "registry/service/memory/contest.py")
    _write(
        repo_root,
        "registry/service/memory/claims.py",
        "from registry.service.memory.contest import detect_for_claim\n",
    )
    # No caller anywhere imports claims.py.
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/contest.py")
    assert _transitive_callers(rule, _general_caller_files(repo_root)) == ()
    assert _is_directly_reachable("registry/service/memory/claims.py", _general_caller_files(repo_root)) is False


def test_contest_becomes_unreachable_if_both_intermediates_lose_their_own_caller(repo_root: Path) -> None:
    """Full-tree proof of the same edge: both intermediates still import
    contest.py, but neither is reachable itself once wiring/jobs.py stops
    naming them. contest.py must flip to unreachable, not stay green on the
    strength of an import that no longer leads anywhere."""
    _build_baseline(repo_root)
    _write(
        repo_root,
        "registry/wiring/jobs.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.calibration import CalibrationService\n"
        "from registry.service.memory.source_governance import SourceGovernanceService\n",
    )

    out = main([])
    assert out == 1


def test_transitive_via_names_exactly_the_two_intermediates_it_relies_on() -> None:
    rule = next(r for r in QUARANTINE if r.module_path == "registry/service/memory/contest.py")
    assert rule.transitive_via == frozenset(
        {
            "registry/service/memory/claims.py",
            "registry/service/memory/consolidation.py",
        }
    )


# ---------------------------------------------------------------------------
# Reporting and CLI behavior
# ---------------------------------------------------------------------------


def test_main_names_the_unreachable_module_and_exits_nonzero(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_baseline(repo_root)
    # Strip curation_queue.py's only two callers.
    _write(
        repo_root,
        "registry/api/routers/memory_curation.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.confirmation import ConfirmationService\n"
        "from registry.service.memory.capability_requests import CapabilityRequestService\n",
    )
    _write(
        repo_root,
        "registry/api/mcp/tools/memory_curation.py",
        "from registry.service.memory.promotion import PromotionService\n"
        "from registry.service.memory.confirmation import ConfirmationService\n"
        "from registry.service.memory.capability_requests import CapabilityRequestService\n",
    )

    assert main([]) == 1
    out = capsys.readouterr()
    assert "registry/service/memory/curation_queue.py: UNREACHABLE" in out.out
    assert "registry/service/memory/curation_queue.py" in out.err
    assert "quarantined module(s) have no production caller" in out.err


def test_a_missing_quarantine_module_file_is_reported(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _build_baseline(repo_root)
    (repo_root / "registry/service/memory/promotion.py").unlink()

    assert main([]) == 1
    err = capsys.readouterr().err
    assert "no longer exists" in err
    assert "registry/service/memory/promotion.py" in err


def test_an_out_of_scope_path_fails_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_baseline(repo_root)
    assert main(["--paths", "does/not/exist"]) == 1
    assert "scope does not exist" in capsys.readouterr().err


def test_explain_lists_every_quarantined_module(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    for rule in QUARANTINE:
        assert rule.module_path in out


def test_a_reachable_module_prints_its_caller_sites(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _build_baseline(repo_root)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "registry/service/memory/promotion.py: reachable (" in out
    assert "all reachable" in out
