"""The task-record gate needs its own tests, like every other gate here.

A gate that matches nothing reads as enforcement in review while the drift it
exists to catch continues, so each test below breaks a record one way and asserts
the gate notices. The negative cases matter as much: a gate that fires on a
healthy plan gets switched off, and then it is not a gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_task_records import main

_HEALTHY_TASK = """### ABC-T01 — does a thing

**Status:** done
**Owner:** backend
**Effort:** S
**Depends on:** —
**Path scope:** registry/service/thing.py
**Verify:** pytest tests/unit/test_thing.py -q
**Traces to:** the-requirement-it-implements
"""


def _plan(root: Path, slug: str, phase: str, body: str, *, pointer: str | None = None) -> Path:
    folder = root / "development" / slug / phase
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tasks.md").write_text(body, encoding="utf-8")
    if pointer is not None:
        (root / "development" / slug / ".current-phase").write_text(pointer, encoding="utf-8")
    return folder / "tasks.md"


def _run(root: Path) -> int:
    return main(["--context-root", str(root)])


def test_a_healthy_plan_passes(tmp_path: Path) -> None:
    _plan(tmp_path, "thing", "phase-1", f"# Tasks\n\n**Status:** Done\n\n{_HEALTHY_TASK}", pointer="phase-1")

    assert _run(tmp_path) == 0


def test_a_phase_claiming_done_over_open_tasks_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The shape that let a task record assert a schema change nobody made."""
    open_task = _HEALTHY_TASK.replace("**Status:** done", "**Status:** pending")
    body = "# Tasks\n\n**Status:** Done — shipped\n\n" + open_task
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 1
    assert "phase-done-with-open-tasks" in capsys.readouterr().out


def test_an_in_progress_task_without_a_blocker_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The shape that let one task sit in-progress for three months."""
    body = "# Tasks\n\n" + _HEALTHY_TASK.replace("**Status:** done", "**Status:** in-progress")
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 1
    assert "blocked-without-blocker" in capsys.readouterr().out


def test_an_in_progress_task_with_a_blocker_passes(tmp_path: Path) -> None:
    body = (
        "# Tasks\n\n"
        + _HEALTHY_TASK.replace("**Status:** done", "**Status:** in-progress")
        + "\n**Blocker:** waiting on the platform team to confirm the mirror.\n"
    )
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 0


def test_an_unknown_status_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The orchestrator matches these literally and drops what it cannot parse,
    # so a prose status reads as a task with no status at all.
    body = "# Tasks\n\n" + _HEALTHY_TASK.replace("**Status:** done", "**Status:** needs-human-review")
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 1
    assert "unknown-status" in capsys.readouterr().out


def test_a_status_with_trailing_prose_still_passes(tmp_path: Path) -> None:
    # "done (partial — see commit)" is ordinary in this workspace and is not the
    # failure being looked for.
    body = "# Tasks\n\n" + _HEALTHY_TASK.replace("**Status:** done", "**Status:** done (partial — see commit)")
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 0


def test_an_open_task_missing_its_verify_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No `Verify:` means no gate, and nothing about that is visible in review."""
    body = "# Tasks\n\n" + _HEALTHY_TASK.replace("**Status:** done", "**Status:** pending").replace(
        "**Verify:** pytest tests/unit/test_thing.py -q\n", ""
    )
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 1
    assert "missing-field" in capsys.readouterr().out


def test_a_shipped_task_missing_a_field_is_left_alone(tmp_path: Path) -> None:
    # Archaeology, not risk. Reporting it would bury the open-task findings.
    body = "# Tasks\n\n" + _HEALTHY_TASK.replace("**Traces to:** the-requirement-it-implements\n", "")
    _plan(tmp_path, "thing", "phase-1", body, pointer="phase-1")

    assert _run(tmp_path) == 0


def test_a_pointer_at_a_nonexistent_phase_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _plan(tmp_path, "thing", "phase-1", f"# Tasks\n\n{_HEALTHY_TASK}", pointer="phase-9")

    assert _run(tmp_path) == 1
    assert "current-phase-missing" in capsys.readouterr().out


def test_no_pointer_at_all_is_fine(tmp_path: Path) -> None:
    # Absence is how "this slug has no active phase" is spelled; a pointer at a
    # shipped phase for a superseded slug is worse than none.
    _plan(tmp_path, "thing", "phase-1", f"# Tasks\n\n{_HEALTHY_TASK}")

    assert _run(tmp_path) == 0


def test_duplicate_task_ids_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _plan(tmp_path, "thing", "phase-1", f"# Tasks\n\n{_HEALTHY_TASK}\n{_HEALTHY_TASK}", pointer="phase-1")

    assert _run(tmp_path) == 1
    assert "duplicate-task-id" in capsys.readouterr().out


def test_templates_are_not_treated_as_work(tmp_path: Path) -> None:
    # A template's statuses are placeholders for whoever copies it. Counting them
    # is how a scan reports pending work that does not exist.
    folder = tmp_path / "development" / "_templates" / "some-sweep"
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text("# Tasks\n\n### TPL-T01 — placeholder\n\n**Status:** pending\n", encoding="utf-8")
    _plan(tmp_path, "thing", "phase-1", f"# Tasks\n\n{_HEALTHY_TASK}", pointer="phase-1")

    assert _run(tmp_path) == 0


def test_an_absent_workspace_fails_by_default(tmp_path: Path) -> None:
    # Failing by default is the point: the alternative is a gate that passes
    # because it could not find anything to check.
    assert main(["--context-root", str(tmp_path / "nowhere")]) == 1


def test_an_absent_workspace_can_be_skipped_explicitly(tmp_path: Path) -> None:
    assert main(["--context-root", str(tmp_path / "nowhere"), "--if-present"]) == 0


def test_explain_covers_every_rule(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.check_task_records import _RULES

    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    for rule in _RULES:
        assert rule in out
