"""Adversarial tests for the Task-to-Intent refactor engine.

Every test builds a real throwaway git repository, because the engine's
candidate discovery is `git ls-files` and its root guard asks git for the
toplevel. A fake filesystem tree would let both of those pass vacuously.

The fixture repositories are deliberately tiny and hostile: identifiers that
merely contain a target token, Unicode continuations, mixed line endings, a
generated file, a case-folding rename pair, and a rename engineered to fail
after content writes have already landed so the rollback path is exercised
rather than asserted.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - builds a real fixture git repo; every argv is a fixed literal here
from pathlib import Path

import pytest
import refactor_intent_nomenclature as eng

REPO_MARKER = '[project]\nname = "contextplane"\nversion = "0.0.1"\n'


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def make_repo(tmp_path: Path, files: dict[str, bytes | str], *, commit: bool = True) -> Path:
    """A real git repo with the product marker and `files` tracked."""
    root = (tmp_path / "product").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(REPO_MARKER, encoding="utf-8")
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            target.write_bytes(body)
        else:
            target.write_text(body, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    if commit:
        _git(root, "commit", "-q", "-m", "fixture")
    return root


def digest(root: Path, rel: str) -> str:
    return eng.sha256_bytes((root / rel).read_bytes())


def manifest_dict(
    *,
    groups: list[dict[str, object]],
    rules: list[dict[str, object]],
    files: list[dict[str, str]],
    renames: list[dict[str, str]] | None = None,
    generated: list[dict[str, str]] | None = None,
    immutable: list[str] | None = None,
    survivors: list[dict[str, object]] | None = None,
    commit: str = "0" * 40,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product_root": {"commit": commit, "tracked_file_count": 1},
        "inventory": {"candidate_file_count": len(files)},
        "groups": groups,
        "rules": rules,
        "files": files,
        "path_renames": renames or [],
        "generated_paths": generated or [],
        "immutable_paths": immutable or [],
        "survivors": survivors or [],
    }


def group(
    gid: str = "intent-memory", include: list[str] | None = None, exclude: list[str] | None = None
) -> dict[str, object]:
    return {"id": gid, "include_paths": include or [], "exclude_paths": exclude or []}


def rule(
    rid: str = "R1",
    *,
    gid: str = "intent-memory",
    pattern: str = r"\btask_id\b",
    replacement: str = "intent_id",
    pre: int = 1,
    post: int = 0,
    syntax: str = "text",
    flags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": rid,
        "group": gid,
        "syntax": syntax,
        "pattern": pattern,
        "replacement": replacement,
        "flags": flags or [],
        "rationale": "fixture rule",
        "expected_pre_count": pre,
        "expected_post_count": post,
    }


PLACEHOLDER = "0" * 64


def freeze(
    tmp_path: Path,
    root: Path,
    *,
    include: list[str],
    rules: list[dict[str, object]],
    renames: list[dict[str, str]] | None = None,
    survivors: list[dict[str, object]] | None = None,
    gid: str = "intent-memory",
) -> tuple[dict[str, object], dict[str, int]]:
    """Build a manifest whose postimage digests are what the engine really produces.

    Mirrors the authoring step: rewrite once with placeholder digests, then
    freeze the measured result. Without this, a digest mismatch fires before the
    behaviour under test is ever reached.
    """
    tracked = [rel for rel in include if (root / rel).is_file()]
    skeleton = manifest_dict(
        groups=[group(gid, include=include)],
        rules=rules,
        files=[{"path": rel, "preimage_sha256": digest(root, rel), "postimage_sha256": PLACEHOLDER} for rel in tracked],
        renames=renames,
        survivors=survivors,
    )
    provisional = eng.load_manifest(write_manifest(tmp_path, skeleton))
    counts: dict[str, int] = {str(item["id"]): 0 for item in rules}
    files = []
    for rel in tracked:
        raw = (root / rel).read_bytes()
        after = eng._rewrite(rel, raw, provisional, provisional.rules, counts)
        files.append(
            {"path": rel, "preimage_sha256": eng.sha256_bytes(raw), "postimage_sha256": eng.sha256_bytes(after)}
        )
    skeleton["files"] = files
    return skeleton, counts


def write_manifest(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load(tmp_path: Path, payload: dict[str, object]) -> eng.Manifest:
    return eng.load_manifest(write_manifest(tmp_path, payload))


# --- root guard ------------------------------------------------------------


def test_root_without_the_product_marker_is_refused(tmp_path: Path) -> None:
    bare = (tmp_path / "not-product").resolve()
    bare.mkdir()
    _git(bare, "init", "-q")
    with pytest.raises(eng.EngineError, match="not the product root"):
        eng.resolve_root(bare)


def test_planning_workspace_is_refused_even_with_a_marker(tmp_path: Path) -> None:
    planning = (tmp_path / ".context" / "product").resolve()
    planning.mkdir(parents=True)
    (planning / "pyproject.toml").write_text(REPO_MARKER, encoding="utf-8")
    _git(planning, "init", "-q")
    with pytest.raises(eng.EngineError, match="planning workspace"):
        eng.resolve_root(planning)


def test_a_subdirectory_of_the_product_is_not_a_root(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"pkg/mod.py": "task_id = 1\n"})
    (root / "pkg" / "pyproject.toml").write_text(REPO_MARKER, encoding="utf-8")
    with pytest.raises(eng.EngineError, match="not a git toplevel"):
        eng.resolve_root(root / "pkg")


def test_a_symlinked_root_is_refused_because_digests_would_name_another_tree(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(eng.EngineError, match="pass the real path"):
        eng.resolve_root(link)


def test_tracked_inventory_excludes_a_nested_worktree_and_caches(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "x = 1\n", ".worktrees/copy/b.py": "y = 2\n", "build/c.py": "z = 3\n"})
    assert eng.tracked_files(root) == ("a.py", "pyproject.toml")


# --- manifest validation ---------------------------------------------------


def test_a_bare_word_rule_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])], rules=[rule(pattern=r"\btask\b", replacement="intent")], files=[]
    )
    with pytest.raises(eng.EngineError, match="bare word"):
        load(tmp_path, payload)


@pytest.mark.parametrize("pattern", [r"\btasks\b", r"[Tt]ask", r"(?:task|TaskCheckpoint)", r"Task"])
def test_bare_word_rejection_survives_spelling_tricks(tmp_path: Path, pattern: str) -> None:
    payload = manifest_dict(groups=[group(include=["a.py"])], rules=[rule(pattern=pattern)], files=[])
    with pytest.raises(eng.EngineError, match="bare word"):
        load(tmp_path, payload)


def test_case_insensitive_rules_are_refused(tmp_path: Path) -> None:
    payload = manifest_dict(groups=[group(include=["a.py"])], rules=[rule(flags=["IGNORECASE"])], files=[])
    with pytest.raises(eng.EngineError, match="case-insensitive"):
        load(tmp_path, payload)


def test_duplicate_rule_ids_are_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule("R1"), rule("R1", pattern=r"\btask_ids\b", replacement="intent_ids")],
        files=[],
    )
    with pytest.raises(eng.EngineError, match="duplicate rule id"):
        load(tmp_path, payload)


def test_one_pattern_with_two_destinations_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule("R1", replacement="intent_id"), rule("R2", replacement="goal_id", syntax="json")],
        files=[],
    )
    with pytest.raises(eng.EngineError, match="two destinations"):
        load(tmp_path, payload)


def test_a_pattern_reaching_one_file_through_two_groups_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group("intent-memory", include=["a.py"]), group("arc-intent", include=["a.py"])],
        rules=[rule("R1", gid="intent-memory"), rule("R2", gid="arc-intent")],
        files=[],
    )
    with pytest.raises(eng.EngineError, match="two groups"):
        load(tmp_path, payload)


def test_the_same_pattern_in_two_groups_is_allowed_while_no_file_is_in_both(tmp_path: Path) -> None:
    """Disjoint groups are how docs and code carry the same vocabulary."""
    payload = manifest_dict(
        groups=[group("intent-memory", include=["a.py"]), group("active-docs", include=["b.md"])],
        rules=[rule("R1", gid="intent-memory"), rule("R2", gid="active-docs")],
        files=[],
    )
    assert len(load(tmp_path, payload).rules) == 2


def test_a_rule_whose_source_equals_its_destination_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])], rules=[rule(pattern="task_id", replacement="task_id")], files=[]
    )
    with pytest.raises(eng.EngineError, match="source equals destination"):
        load(tmp_path, payload)


def test_two_renames_converging_on_one_destination_are_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule()],
        files=[],
        renames=[
            {"id": "MV1", "group": "intent-memory", "source": "a.py", "destination": "c.py"},
            {"id": "MV2", "group": "intent-memory", "source": "b.py", "destination": "c.py"},
        ],
    )
    with pytest.raises(eng.EngineError, match="is claimed by"):
        load(tmp_path, payload)


def test_case_folded_rename_destinations_are_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule()],
        files=[],
        renames=[
            {"id": "MV1", "group": "intent-memory", "source": "a.py", "destination": "Intent.py"},
            {"id": "MV2", "group": "intent-memory", "source": "b.py", "destination": "intent.py"},
        ],
    )
    with pytest.raises(eng.EngineError, match="case-folds"):
        load(tmp_path, payload)


def test_a_rename_into_its_own_source_directory_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule()],
        files=[],
        renames=[{"id": "MV1", "group": "intent-memory", "source": "pkg", "destination": "pkg/inner"}],
    )
    with pytest.raises(eng.EngineError, match="cycle"):
        load(tmp_path, payload)


def test_a_file_record_on_a_generated_path_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["openapi.json"])],
        rules=[rule()],
        files=[{"path": "openapi.json", "preimage_sha256": "a" * 64, "postimage_sha256": "b" * 64}],
        generated=[{"path": "openapi.json", "regenerate_command": "make openapi-export"}],
    )
    with pytest.raises(eng.EngineError, match="generated or immutable"):
        load(tmp_path, payload)


def test_a_file_record_on_a_historical_migration_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(
        groups=[group(include=["m/0030_task_memory.py"])],
        rules=[rule()],
        files=[{"path": "m/0030_task_memory.py", "preimage_sha256": "a" * 64, "postimage_sha256": "b" * 64}],
        immutable=["m/*"],
    )
    with pytest.raises(eng.EngineError, match="generated or immutable"):
        load(tmp_path, payload)


def test_an_unknown_group_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(groups=[group("other-group", include=["a.py"])], rules=[], files=[])
    with pytest.raises(eng.EngineError, match="unknown group"):
        load(tmp_path, payload)


def test_an_unsupported_schema_version_is_refused(tmp_path: Path) -> None:
    payload = manifest_dict(groups=[group()], rules=[], files=[])
    payload["schema_version"] = 2
    with pytest.raises(eng.EngineError, match="schema_version"):
        load(tmp_path, payload)


# --- regex boundaries ------------------------------------------------------


def test_a_token_inside_a_longer_identifier_is_not_matched(tmp_path: Path) -> None:
    body = "task_identifier = 1\nsubtask_id = 2\ntask_idx = 3\ntask_id = 4\n"
    root = make_repo(tmp_path, {"a.py": body})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
    )
    manifest = load(tmp_path, payload)
    counts: dict[str, int] = {"R1": 0}
    after = eng._rewrite("a.py", body.encode(), manifest, manifest.rules, counts).decode()
    assert counts["R1"] == 1
    assert "task_identifier = 1" in after
    assert "subtask_id = 2" in after
    assert "task_idx = 3" in after
    assert "intent_id = 4" in after


def test_a_unicode_identifier_continuation_does_not_end_the_token(tmp_path: Path) -> None:
    body = "task_idé = 1\ntask_id = 2\n"
    root = make_repo(tmp_path, {"a.py": body})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
    )
    manifest = load(tmp_path, payload)
    counts: dict[str, int] = {"R1": 0}
    after = eng._rewrite("a.py", body.encode(), manifest, manifest.rules, counts).decode()
    assert counts["R1"] == 1, "the Unicode-continued identifier must not be treated as task_id"
    assert "task_idé = 1" in after


def test_ordering_keeps_a_route_literal_whole(tmp_path: Path) -> None:
    """The route rule must run before the identifier rule that would eat it."""
    body = 'ROUTE = "/v1/tasks/{task_id}/checkpoints"\nfield = "task_id"\n'
    root = make_repo(tmp_path, {"a.py": body})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[
            rule("W1", pattern=r"/v1/tasks/\{task_id\}", replacement="/v1/intents/{intent_id}", pre=1),
            rule("S1", pattern=r"\btask_id\b", replacement="intent_id", pre=1),
        ],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
    )
    manifest = load(tmp_path, payload)
    counts = {"W1": 0, "S1": 0}
    after = eng._rewrite("a.py", body.encode(), manifest, manifest.rules, counts).decode()
    assert '"/v1/intents/{intent_id}/checkpoints"' in after
    assert 'field = "intent_id"' in after


# --- syntax safety ---------------------------------------------------------


def test_a_source_syntax_error_is_reported_not_obscured(tmp_path: Path) -> None:
    body = "def broken(\n"
    root = make_repo(tmp_path, {"a.py": body})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=0)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="preimage is not parseable Python"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_a_postimage_syntax_error_aborts_the_plan(tmp_path: Path) -> None:
    body = "task_id = 1\n"
    root = make_repo(tmp_path, {"a.py": body})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(replacement="1intent", pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="postimage is not parseable Python"):
        eng.build_plan(root, manifest, ["intent-memory"])
    assert (root / "a.py").read_text(encoding="utf-8") == body


def test_a_postimage_that_is_not_valid_json_aborts_the_plan(tmp_path: Path) -> None:
    body = '{"task_id": 1}\n'
    root = make_repo(tmp_path, {"a.json": body})
    payload = manifest_dict(
        groups=[group(include=["a.json"])],
        rules=[rule(pattern=r"\btask_id\b", replacement='intent_id"', pre=1)],
        files=[{"path": "a.json", "preimage_sha256": digest(root, "a.json"), "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="not valid JSON"):
        eng.build_plan(root, manifest, ["intent-memory"])


# --- counts and digests ----------------------------------------------------


def test_a_stale_count_refuses_the_run(tmp_path: Path) -> None:
    """The count is an independent guard, so it is corrupted on its own here.

    A wrong count normally travels with a wrong postimage digest, and the digest
    check fires first. Freezing the true digests and then editing only the count
    is what actually reaches the count comparison.
    """
    root = make_repo(tmp_path, {"a.py": "task_id = 1\ntask_id = 2\n"})
    skeleton, counts = freeze(tmp_path, root, include=["a.py"], rules=[rule(pre=2)])
    assert counts["R1"] == 2
    skeleton["rules"][0]["expected_pre_count"] = 1  # type: ignore[index]
    manifest = load(tmp_path, skeleton)
    with pytest.raises(eng.EngineError, match="matched 2 sites, manifest declares exactly 1"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_at_least_one_is_not_accepted_as_a_count(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    skeleton, _ = freeze(tmp_path, root, include=["a.py"], rules=[rule(pre=1)])
    skeleton["rules"][0]["expected_pre_count"] = 2  # type: ignore[index]
    manifest = load(tmp_path, skeleton)
    with pytest.raises(eng.EngineError, match="matched 1 sites, manifest declares exactly 2"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_a_source_that_moved_after_inventory_stops_the_run_before_writes(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": "a" * 64, "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="neither the declared preimage"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_a_postimage_that_does_not_match_the_declared_digest_is_refused(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    payload = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="rewriting the preimage gave"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_a_file_missing_from_the_tree_is_refused(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    payload = manifest_dict(
        groups=[group(include=["gone.py"])],
        rules=[rule(pre=0)],
        files=[{"path": "gone.py", "preimage_sha256": "a" * 64, "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="missing from the working tree"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_an_untracked_file_is_not_source(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    (root / "untracked.py").write_text("task_id = 1\n", encoding="utf-8")
    payload = manifest_dict(
        groups=[group(include=["untracked.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "untracked.py", "preimage_sha256": digest(root, "untracked.py"), "postimage_sha256": "b" * 64}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="not tracked by git"):
        eng.build_plan(root, manifest, ["intent-memory"])


# --- generated-file refusal -----------------------------------------------


def test_a_rule_that_would_touch_a_generated_file_names_its_generator(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n", "openapi.json": '{"task_id": 1}\n'})
    payload = manifest_dict(
        groups=[group(include=["a.py", "openapi.json"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": "b" * 64}],
        generated=[{"path": "openapi.json", "regenerate_command": "make openapi-export"}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="regenerate with `make openapi-export`"):
        eng.build_plan(root, manifest, ["intent-memory"])


# --- apply, atomicity, idempotence ----------------------------------------


def _applyable(tmp_path: Path, root: Path, body: str = "task_id = 1\n") -> eng.Manifest:
    counts: dict[str, int] = {"R1": 0}
    skeleton = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
    )
    provisional = load(tmp_path, skeleton)
    after = eng._rewrite("a.py", body.encode(), provisional, provisional.rules, counts)
    skeleton["files"] = [
        {"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": eng.sha256_bytes(after)}
    ]
    return load(tmp_path, skeleton)


def test_apply_produces_the_declared_postimage(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    manifest = _applyable(tmp_path, root)
    eng.apply_plan(root, eng.build_plan(root, manifest, ["intent-memory"]))
    assert (root / "a.py").read_text(encoding="utf-8") == "intent_id = 1\n"


def test_a_second_apply_is_a_no_op_and_dry_run_then_reports_an_empty_plan(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    manifest = _applyable(tmp_path, root)
    eng.apply_plan(root, eng.build_plan(root, manifest, ["intent-memory"]))
    second = eng.build_plan(root, manifest, ["intent-memory"])
    assert second.changes == ()
    assert second.renames == ()
    assert second.already_applied == ("a.py",)


def test_line_endings_and_file_mode_survive_a_rewrite(tmp_path: Path) -> None:
    body = b"task_id = 1\r\nother = 2\r\n"
    root = make_repo(tmp_path, {"a.py": body})
    os.chmod(root / "a.py", 0o755)  # noqa: S103 - the point of the test is that this exact mode survives
    counts: dict[str, int] = {"R1": 0}
    skeleton = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
    )
    provisional = load(tmp_path, skeleton)
    after = eng._rewrite("a.py", body, provisional, provisional.rules, counts)
    assert after == b"intent_id = 1\r\nother = 2\r\n", "CRLF must survive the round trip"
    skeleton["files"] = [
        {"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": eng.sha256_bytes(after)}
    ]
    manifest = load(tmp_path, skeleton)
    eng.apply_plan(root, eng.build_plan(root, manifest, ["intent-memory"]))
    assert (root / "a.py").read_bytes() == b"intent_id = 1\r\nother = 2\r\n"
    assert (root / "a.py").stat().st_mode & 0o777 == 0o755


def test_a_failing_rename_restores_every_file_already_written(tmp_path: Path) -> None:
    """Atomicity is proved by breaking a late step, not by asserting a comment.

    `blocker.txt` is a *file*, so creating the rename destination's parent
    directory fails after the content rewrite has already been flushed to disk.
    """
    body = "task_id = 1\n"
    root = make_repo(tmp_path, {"a.py": body, "movable.py": "x = 1\n", "blocker.txt": "no\n"})
    counts: dict[str, int] = {"R1": 0}
    skeleton = manifest_dict(
        groups=[group(include=["a.py"])],
        rules=[rule(pre=1)],
        files=[{"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER}],
        renames=[
            {"id": "MV1", "group": "intent-memory", "source": "movable.py", "destination": "blocker.txt/moved.py"}
        ],
    )
    provisional = load(tmp_path, skeleton)
    after = eng._rewrite("a.py", body.encode(), provisional, provisional.rules, counts)
    skeleton["files"] = [
        {"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": eng.sha256_bytes(after)}
    ]
    manifest = load(tmp_path, skeleton)
    plan = eng.build_plan(root, manifest, ["intent-memory"])
    assert plan.changes, "the content rewrite must be staged, or this proves nothing"

    with pytest.raises(eng.EngineError, match="restored from its validated preimage"):
        eng.apply_plan(root, plan)

    assert (root / "a.py").read_text(encoding="utf-8") == body, "the rewritten file must be rolled back"
    assert (root / "movable.py").is_file(), "the un-renamed source must still be present"
    assert not (root / "blocker.txt").is_dir()


def test_a_partially_applied_selection_is_refused_rather_than_resumed(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n", "b.py": "task_id = 2\n"})
    counts: dict[str, int] = {"R1": 0}
    skeleton = manifest_dict(
        groups=[group(include=["a.py", "b.py"])],
        rules=[rule(pre=2)],
        files=[
            {"path": "a.py", "preimage_sha256": digest(root, "a.py"), "postimage_sha256": PLACEHOLDER},
            {"path": "b.py", "preimage_sha256": digest(root, "b.py"), "postimage_sha256": PLACEHOLDER},
        ],
    )
    provisional = load(tmp_path, skeleton)
    posts = {
        rel: eng.sha256_bytes(
            eng._rewrite(rel, (root / rel).read_bytes(), provisional, provisional.rules, dict(counts))
        )
        for rel in ("a.py", "b.py")
    }
    skeleton["files"] = [
        {"path": rel, "preimage_sha256": digest(root, rel), "postimage_sha256": posts[rel]} for rel in ("a.py", "b.py")
    ]
    manifest = load(tmp_path, skeleton)
    # Move only one file to its postimage, then ask for the whole selection.
    (root / "a.py").write_text("intent_id = 1\n", encoding="utf-8")
    with pytest.raises(eng.EngineError, match="partially applied selection"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_a_rename_onto_an_existing_destination_is_refused(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    payload = manifest_dict(
        groups=[group(include=[])],
        rules=[],
        files=[],
        renames=[{"id": "MV1", "group": "intent-memory", "source": "a.py", "destination": "b.py"}],
    )
    manifest = load(tmp_path, payload)
    with pytest.raises(eng.EngineError, match="already exists"):
        eng.build_plan(root, manifest, ["intent-memory"])


def test_renames_run_deepest_first(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"pkg/deep/task_memory.py": "x = 1\n"})
    payload = manifest_dict(
        groups=[group(include=[])],
        rules=[],
        files=[],
        renames=[
            {
                "id": "MV1",
                "group": "intent-memory",
                "source": "pkg/deep/task_memory.py",
                "destination": "pkg/deep/intent_memory.py",
            }
        ],
    )
    manifest = load(tmp_path, payload)
    eng.apply_plan(root, eng.build_plan(root, manifest, ["intent-memory"]))
    assert (root / "pkg/deep/intent_memory.py").is_file()
    assert not (root / "pkg/deep/task_memory.py").exists()


# --- check mode ------------------------------------------------------------


def test_check_follows_a_rename_to_the_destination(tmp_path: Path) -> None:
    """A renamed file's record is keyed on its old path; check must not call it missing."""
    root = make_repo(tmp_path, {"task_memory.py": "task_id = 1\n"})
    counts: dict[str, int] = {"R1": 0}
    skeleton = manifest_dict(
        groups=[group(include=["task_memory.py"])],
        rules=[rule(pre=1)],
        files=[
            {
                "path": "task_memory.py",
                "preimage_sha256": digest(root, "task_memory.py"),
                "postimage_sha256": PLACEHOLDER,
            }
        ],
        renames=[
            {"id": "MV1", "group": "intent-memory", "source": "task_memory.py", "destination": "intent_memory.py"}
        ],
        survivors=[],
    )
    provisional = load(tmp_path, skeleton)
    after = eng._rewrite(
        "task_memory.py", (root / "task_memory.py").read_bytes(), provisional, provisional.rules, counts
    )
    skeleton["files"] = [
        {
            "path": "task_memory.py",
            "preimage_sha256": digest(root, "task_memory.py"),
            "postimage_sha256": eng.sha256_bytes(after),
        }
    ]
    manifest = load(tmp_path, skeleton)
    eng.apply_plan(root, eng.build_plan(root, manifest, ["intent-memory"]))
    assert eng.mode_check(root, manifest, ["intent-memory"]) == 0


def test_check_fails_on_unclassified_residue(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "task_checkpoint = 1\n"})
    payload = manifest_dict(groups=[group(include=[])], rules=[], files=[])
    manifest = load(tmp_path, payload)
    assert eng.mode_check(root, manifest, ["intent-memory"]) == 1


def test_check_accepts_residue_that_exactly_one_survivor_explains(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"m/0030_task_memory.py": "task_id = 1\n"})
    payload = manifest_dict(
        groups=[group(include=[])],
        rules=[],
        files=[],
        immutable=["m/*"],
        survivors=[
            {
                "id": "SV-1",
                "category": "historical-migrations",
                "include_paths": ["m/*"],
                "token_pattern": r"^task_id$",
                "reason": "applied revision",
                "immutability_basis": "reproduces a historical database",
                "verification": "digest manifest",
            }
        ],
    )
    manifest = load(tmp_path, payload)
    assert eng.mode_check(root, manifest, ["intent-memory"]) == 0


def test_check_fails_when_two_survivors_explain_the_same_residue(tmp_path: Path) -> None:
    """An ambiguous allowlist fails the same way a missing one does."""
    root = make_repo(tmp_path, {"m/0030_task_memory.py": "task_id = 1\n"})
    entry = {
        "category": "historical-migrations",
        "include_paths": ["m/*"],
        "token_pattern": r"^task_id$",
        "reason": "applied revision",
        "immutability_basis": "reproduces a historical database",
        "verification": "digest manifest",
    }
    payload = manifest_dict(
        groups=[group(include=[])],
        rules=[],
        files=[],
        immutable=["m/*"],
        survivors=[{"id": "SV-1", **entry}, {"id": "SV-2", **entry}],
    )
    manifest = load(tmp_path, payload)
    assert eng.mode_check(root, manifest, ["intent-memory"]) == 1


# --- modes and CLI ---------------------------------------------------------


def test_every_mode_except_inventory_requires_a_group(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"a.py": "x = 1\n"})
    for mode in ("dry-run", "apply", "check"):
        assert eng.main([mode, "--root", str(root)]) == 2


def test_inventory_emits_a_deterministic_candidate_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\nTaskCheckpoint = 2\n"})
    assert eng.main(["inventory", "--root", str(root), "--manifest", str(tmp_path / "absent.json")]) == 0
    first = capsys.readouterr().out
    assert eng.main(["inventory", "--root", str(root), "--manifest", str(tmp_path / "absent.json")]) == 0
    second = capsys.readouterr().out
    assert first == second, "the same commit must produce the same inventory"
    payload = json.loads(first)
    assert payload["inventory"]["candidate_file_count"] == 1
    assert {candidate["token"] for candidate in payload["candidates"]} == {"task_id", "TaskCheckpoint"}


def test_inventory_never_writes_to_the_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path, {"a.py": "task_id = 1\n"})
    before = digest(root, "a.py")
    assert eng.main(["inventory", "--root", str(root), "--manifest", str(tmp_path / "absent.json")]) == 0
    capsys.readouterr()
    assert digest(root, "a.py") == before


# --- the committed manifest ------------------------------------------------

COMMITTED = Path(__file__).resolve().parents[2] / "scripts" / "refactor_intent_nomenclature.rules.json"


def test_the_committed_manifest_loads_and_validates() -> None:
    manifest = eng.load_manifest(COMMITTED)
    assert {group.id for group in manifest.groups} <= set(eng.GROUPS)
    assert manifest.rules, "a manifest with no rules would make every mode vacuous"
    assert manifest.files, "a manifest with no files would make dry-run vacuous"


def test_every_committed_rule_carries_an_exact_nonzero_count_and_a_rationale() -> None:
    manifest = eng.load_manifest(COMMITTED)
    for item in manifest.rules:
        assert item.expected_pre_count > 0, f"{item.id} matches nothing and is dead weight in a closed manifest"
        assert item.expected_post_count == 0, f"{item.id} must leave no occurrence of its source behind"
        assert len(item.rationale) > 10, f"{item.id} has no semantic rationale"


def test_the_committed_manifest_names_a_generator_for_every_generated_path() -> None:
    manifest = eng.load_manifest(COMMITTED)
    assert manifest.generated_paths, "openapi.json and the ARC snapshots are hard-refusal paths"
    for generated in manifest.generated_paths:
        assert generated.regenerate_command.startswith("make ")


def test_the_committed_manifest_declares_survivors_with_every_required_field() -> None:
    manifest = eng.load_manifest(COMMITTED)
    assert manifest.survivors
    for survivor in manifest.survivors:
        assert survivor.include_paths, f"{survivor.id} must name exact paths or narrow globs"
        assert survivor.token_pattern != "task", f"{survivor.id} must not be a bare-word allowlist"
        assert survivor.reason and survivor.immutability_basis and survivor.verification


def test_the_committed_manifest_never_rewrites_a_migration_or_a_frozen_v1_fixture() -> None:
    manifest = eng.load_manifest(COMMITTED)
    for record in manifest.files:
        assert "/migrations/versions/" not in record.path
        assert "artifact_semantics_v1" not in record.path
    for rename in manifest.path_renames:
        assert "/migrations/versions/" not in rename.source
