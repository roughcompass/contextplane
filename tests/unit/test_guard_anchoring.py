"""Every `scripts/check_*.py` gate is anchored on the repo root, not the cwd.

A guard that resolves its scan population relative to the current working
directory matches nothing when run from anywhere else, and then prints a
cheerful summary and exits 0. That failure is invisible in CI logs — a gate
reporting "the arrow holds" over zero files looks exactly like a gate reporting
it over four hundred — and it is the failure mode this module exists to catch.

Two properties are asserted for every guard, discovered by glob so a newly added
gate is covered without anyone remembering to register it here:

1. **cwd-independence.** Identical exit code and stdout from the repo root and
   from a foreign directory. This is the strongest available statement: it does
   not depend on how any particular guard phrases its summary.
2. **non-vacuity.** A guard that exits 0 while reporting a zero-sized scan
   population has inspected nothing and called it a pass.

`test_meta_test_catches_a_cwd_relative_guard` is the control. Without it, a bug
in the discovery or comparison logic here would make this whole module pass
vacuously — which would be the same defect one level up.
"""

from __future__ import annotations

import re
import subprocess  # noqa: S404 - fixed argv running this repo's own guards via sys.executable
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

# The guards are run as scripts, so `scripts/` is their import root; a test
# importing their shared library has to put it on the path the same way. This
# mirrors what the other `scripts/` tests in this directory already do, rather
# than relying on one of them having run first and left the path mutated.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from checklib import (  # noqa: E402
    GuardError,
    StaleScope,
    VacuousScan,
    repo_root,
    require_nonempty,
    require_paths_exist,
)

REPO_ROOT = repo_root()
GUARDS = sorted(p.name for p in (REPO_ROOT / "scripts").glob("check_*.py"))

# Nouns that name a guard's *scan population* — what it looked at. Deliberately
# distinct from verdict nouns ("violation(s)", "exemption(s)", "exclusion(s)"),
# which are legitimately zero on a clean tree: "0 violation(s)" is the gate
# working, while "0 file(s) scanned" is the gate not running.
_POPULATION = re.compile(r"\b(\d+) (?:script )?(file|module|surface)\(s\)")


@dataclass(frozen=True)
class Run:
    returncode: int
    stdout: str
    stderr: str


def _run(guard: str, cwd: Path) -> Run:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / guard)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return Run(proc.returncode, proc.stdout, proc.stderr)


@pytest.fixture(scope="module")
def foreign_cwd(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory outside the repo, holding no `pyproject.toml` of its own."""
    return tmp_path_factory.mktemp("foreign")


@pytest.fixture(scope="module")
def runs(foreign_cwd: Path) -> dict[str, tuple[Run, Run]]:
    """Every guard run twice — from the repo root and from a foreign cwd.

    Collected once and in parallel: these are ~36 subprocesses, each dominated
    by interpreter start-up and file walking, so serial execution would make
    this module several times slower than the entire rest of the unit suite.
    """

    def both(guard: str) -> tuple[str, tuple[Run, Run]]:
        return guard, (_run(guard, REPO_ROOT), _run(guard, foreign_cwd))

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(both, GUARDS))


def test_guards_are_discovered() -> None:
    """The glob above is what makes every other test in this module cover the
    real set. If it silently found nothing, they would all pass vacuously."""
    assert GUARDS, "no scripts/check_*.py guards discovered"
    assert "check_import_direction.py" in GUARDS


@pytest.mark.parametrize("guard", GUARDS)
def test_guard_verdict_is_cwd_independent(guard: str, runs: dict[str, tuple[Run, Run]]) -> None:
    """A guard must reach the same verdict from any directory."""
    from_root, from_foreign = runs[guard]
    assert from_foreign.returncode == from_root.returncode, (
        f"{guard} exits {from_foreign.returncode} from a foreign cwd but "
        f"{from_root.returncode} from the repo root.\n"
        f"foreign stderr: {from_foreign.stderr}"
    )
    assert from_foreign.stdout == from_root.stdout, (
        f"{guard} reports different results depending on the directory it is run from, "
        "which means its scan population is resolved against the cwd.\n"
        f"from repo root: {from_root.stdout!r}\n"
        f"from foreign:   {from_foreign.stdout!r}"
    )


@pytest.mark.parametrize("guard", GUARDS)
def test_guard_does_not_pass_having_scanned_nothing(guard: str, runs: dict[str, tuple[Run, Run]]) -> None:
    """No guard exits 0 while reporting an empty scan population."""
    _, from_foreign = runs[guard]
    if from_foreign.returncode != 0:
        return
    counts = _POPULATION.findall(from_foreign.stdout + from_foreign.stderr)
    zeroed = [f"{n} {noun}(s)" for n, noun in counts if int(n) == 0]
    assert not zeroed, (
        f"{guard} exited 0 having scanned nothing ({', '.join(zeroed)}). "
        "A gate that inspects an empty population reports success without "
        "having checked anything."
    )


def test_at_least_one_guard_reports_its_population() -> None:
    """Guards are expected to say how much they looked at.

    Without this, `test_guard_does_not_pass_having_scanned_nothing` would be
    satisfied by a fleet of guards that print no counts at all.
    """
    reporting = [g for g in GUARDS if _POPULATION.search(_run(g, REPO_ROOT).stdout)]
    assert len(reporting) >= len(GUARDS) // 2, (
        f"only {len(reporting)} of {len(GUARDS)} guards report a scan population; "
        "the non-vacuity check cannot see the rest"
    )


def test_meta_test_catches_a_cwd_relative_guard(tmp_path: Path) -> None:
    """The control: a guard with the original defect must be caught.

    This reproduces the exact shape that shipped — `Path("scripts").rglob(...)`
    resolved against the cwd — and asserts that the comparison used above
    distinguishes it from a correctly anchored guard.
    """
    broken = tmp_path / "check_broken.py"
    broken.write_text(
        "import pathlib, sys\n"
        "n = sum(1 for _ in pathlib.Path('scripts').rglob('*.py'))\n"
        "print(f'broken gate: {n} file(s) scanned, arrow holds')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    def run(cwd: Path) -> Run:
        proc = subprocess.run([sys.executable, str(broken)], cwd=cwd, capture_output=True, text=True, timeout=60)
        return Run(proc.returncode, proc.stdout, proc.stderr)

    from_root, from_foreign = run(REPO_ROOT), run(foreign)

    # Both properties this module asserts must fail for the broken guard.
    assert from_foreign.stdout != from_root.stdout, "control guard is not actually cwd-sensitive"
    zeroed = [n for n, _ in _POPULATION.findall(from_foreign.stdout) if int(n) == 0]
    assert zeroed, "the non-vacuity check would not have caught a zero-population pass"
    assert from_foreign.returncode == 0, "the defect is precisely that it exits 0"


# ---------------------------------------------------------------------------
# checklib itself
# ---------------------------------------------------------------------------


def test_repo_root_is_independent_of_cwd(tmp_path: Path) -> None:
    """`repo_root()` anchors on the marker file, not on the calling directory.

    Asserted across a process boundary because `os.chdir` inside the test would
    leak into every test that runs after it.
    """
    scripts_dir = str(REPO_ROOT / "scripts")
    probe = f"import sys; sys.path.append({scripts_dir!r}); import checklib; print(checklib.repo_root())"
    from_root = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    from_foreign = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert from_root.returncode == 0, from_root.stderr
    assert from_foreign.returncode == 0, from_foreign.stderr
    assert from_foreign.stdout.strip() == from_root.stdout.strip() == str(REPO_ROOT)


def test_repo_root_walks_up_to_the_marker(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "a" / "b").mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    assert repo_root(root / "a" / "b") == root


def test_repo_root_refuses_to_guess(tmp_path: Path) -> None:
    """No marker anywhere above means the root is unknown. Guessing one would
    scan the wrong tree and report a confident verdict about it."""
    with pytest.raises(GuardError, match="cannot be determined"):
        repo_root(tmp_path / "no" / "marker" / "above")


def test_require_nonempty_rejects_an_empty_population() -> None:
    with pytest.raises(VacuousScan, match="resolved to nothing"):
        require_nonempty([], "the scan population")


def test_require_nonempty_accepts_a_count() -> None:
    require_nonempty(3, "the scan population")
    with pytest.raises(VacuousScan):
        require_nonempty(0, "the scan population")


def test_require_nonempty_allows_a_deliberately_narrow_scope() -> None:
    """An explicit `--paths` naming a subtree with no matching file is a fair
    question with the answer "nothing there"; only a default scope resolving to
    nothing means the gate governed no file.

    Both halves are asserted together: the flag is only meaningful if the same
    empty population still fails without it.
    """
    require_nonempty([], "the scan population", allow_empty=True)
    with pytest.raises(VacuousScan):
        require_nonempty([], "the scan population", allow_empty=False)


def test_require_nonempty_message_carries_the_hint() -> None:
    with pytest.raises(VacuousScan, match="the tree may have moved"):
        require_nonempty([], "the scan population", hint="Expected files; the tree may have moved.")


def test_require_paths_exist_reports_a_stale_entry(tmp_path: Path) -> None:
    (tmp_path / "present.py").write_text("", encoding="utf-8")
    with pytest.raises(StaleScope, match="gone.py"):
        require_paths_exist(["present.py", "gone.py"], "the allowlist", root=tmp_path)


def test_require_paths_exist_passes_when_every_entry_resolves(tmp_path: Path) -> None:
    """A list whose entries all resolve is accepted, and the same list fails the
    moment one of them stops resolving — asserted together so this cannot pass
    by the check being a no-op."""
    (tmp_path / "present.py").write_text("", encoding="utf-8")
    require_paths_exist(["present.py"], "the allowlist", root=tmp_path)

    (tmp_path / "present.py").unlink()
    with pytest.raises(StaleScope, match="present.py"):
        require_paths_exist(["present.py"], "the allowlist", root=tmp_path)
