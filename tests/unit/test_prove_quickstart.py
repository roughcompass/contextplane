"""Unit tests for the clean-clone quickstart proof runner.

Everything here runs through an injected `CommandExecutor` -- no subprocess,
no network, no real clone, no Docker. That is the seam the module exists to
provide: the sequencing, redaction, environment carryover, failure handling,
and transcript rendering are all real-code paths a test can drive without
paying for (or depending on the availability of) a live stack.

One test class is different on purpose: `TestDocFidelity` extracts the
literal fenced ```bash blocks from README.md and
docs/02-get-started/01-quickstart.md and asserts the hardcoded `Step` scripts
in `prove_quickstart.py` are character-for-character the same commands. This
is the mechanical guard against the module's own copy drifting from the docs
it exists to prove -- exactly the failure mode the rest of this phase found
by hand and gated against everywhere else.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from scripts import prove_quickstart as pq

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Fake executor
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Replays canned results in call order, or defers to a *responder*
    callable when one is supplied (needed for a test that must inspect the
    wrapped script -- e.g. to fabricate the `env -0` dump a real `export`
    would have produced). Records every call it saw either way.

    (`__call__` is looked up on the type, not the instance, for `obj(...)`
    -- reassigning `some_instance.__call__` is a no-op. A constructor
    argument is the seam that actually works.)
    """

    def __init__(
        self,
        results: list[pq.ExecResult] | None = None,
        *,
        responder: pq.CommandExecutor | None = None,
    ) -> None:
        self._queue = list(results or [])
        self._responder = responder
        self.calls: list[tuple[list[str], Path, dict[str, str], float]] = []

    def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> pq.ExecResult:
        self.calls.append((list(argv), cwd, dict(env), timeout))
        if self._responder is not None:
            return self._responder(argv, cwd=cwd, env=env, timeout=timeout)
        if not self._queue:
            return pq.ExecResult(0, "", "")
        return self._queue.pop(0)


def _dump_path_of(argv: list[str]) -> Path:
    """Pull the `env -0 > <path>` target out of a wrapped step script, so a
    test can simulate a step exporting a variable or activating a venv."""
    script = argv[-1]
    match = re.search(r"env -0 > ([^\s]+)", script)
    assert match, f"no env dump redirect found in: {script!r}"
    return Path(shlex.split(match.group(1))[0])


def _write_dump(argv: list[str], values: dict[str, str]) -> None:
    path = _dump_path_of(argv)
    path.write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in values.items()))


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_hides_a_jwt() -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJkZXYtYWRtaW4ifQ.c2lnbmF0dXJlYnl0ZXNoZXJl"
    text = f"token issued: {jwt}\n"
    out = pq.redact(text)
    assert jwt not in out
    assert "<redacted-jwt>" in out


def test_redact_hides_a_bearer_header() -> None:
    out = pq.redact("Authorization: Bearer abc123.def456.ghi789\n")
    assert "abc123" not in out
    assert "Bearer <redacted-token>" in out


def test_redact_hides_a_client_secret_assignment() -> None:
    out = pq.redact("CLIENT_SECRET=dev-secret-xyz\n")
    assert "dev-secret-xyz" not in out
    assert "<redacted>" in out


def test_redact_hides_an_access_token_json_field() -> None:
    out = pq.redact('{"access_token": "abc.def.ghi", "expires_in": 3600}')
    assert "abc.def.ghi" not in out
    assert "expires_in" in out  # unrelated fields survive


def test_bound_truncates_long_output() -> None:
    text = "x" * (pq._MAX_OUTPUT_CHARS + 500)
    out = pq._bound(text)
    assert len(out) < len(text)
    assert "truncated" in out


def test_bound_leaves_short_output_untouched() -> None:
    assert pq._bound("hello") == "hello"


# ---------------------------------------------------------------------------
# run_step: env carryover, manual steps, checks
# ---------------------------------------------------------------------------


def test_manual_step_is_skipped_and_never_executed(tmp_path: Path) -> None:
    executor = FakeExecutor()
    step = pq.Step(step_id="browse", label="open swagger", source="doc.md", script="echo nope", manual=True)
    outcome, _env = pq.run_step(executor, step, cwd=tmp_path, env={}, default_timeout=5.0)
    assert outcome.skipped
    assert outcome.ok
    assert executor.calls == []


def test_a_failing_step_is_not_ok_and_carries_no_new_env(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(1, "", "boom")])
    step = pq.Step(step_id="fails", label="fails", source="doc.md", script="false")
    outcome, env = pq.run_step(executor, step, cwd=tmp_path, env={"A": "1"}, default_timeout=5.0)
    assert not outcome.ok
    assert outcome.exit_code == 1
    assert env == {"A": "1"}  # no dump was written, so env is unchanged


def test_export_in_one_step_is_visible_to_the_next(tmp_path: Path) -> None:
    """Simulates `export TOKEN=...` -- the fake executor writes the same env
    dump a real `bash -c "... ; env -0 > path"` invocation would produce."""

    def responder(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> pq.ExecResult:
        _write_dump(argv, {**env, "TOKEN": "header.payload.signature"})
        return pq.ExecResult(0, "", "")

    executor = FakeExecutor(responder=responder)
    step = pq.Step(step_id="export", label="export TOKEN", source="doc.md", script="export TOKEN=$(mint)")
    _outcome, next_env = pq.run_step(executor, step, cwd=tmp_path, env={"PATH": "/bin"}, default_timeout=5.0)
    assert next_env["TOKEN"] == "header.payload.signature"


def test_env_check_runs_against_the_carried_forward_environment(tmp_path: Path) -> None:
    def responder(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> pq.ExecResult:
        _write_dump(argv, {**env})  # TOKEN never actually got set
        return pq.ExecResult(0, "", "")

    executor = FakeExecutor(responder=responder)
    step = pq.Step(
        step_id="export",
        label="export TOKEN",
        source="doc.md",
        script="export TOKEN=$(mint)",
        env_check=pq._check_token_exported,
    )
    outcome, _env = pq.run_step(executor, step, cwd=tmp_path, env={}, default_timeout=5.0)
    assert not outcome.ok
    assert "TOKEN was not set" in outcome.failure_reason


def test_check_runs_only_when_exit_code_is_zero(tmp_path: Path) -> None:
    def always_fail_check(_stdout: str) -> str | None:
        raise AssertionError("must not be called when the command itself already failed")

    executor = FakeExecutor([pq.ExecResult(1, "", "boom")])
    step = pq.Step(step_id="s", label="s", source="doc.md", script="false", check=always_fail_check)
    outcome, _env = pq.run_step(executor, step, cwd=tmp_path, env={}, default_timeout=5.0)
    assert outcome.exit_code == 1
    assert outcome.failure_reason == ""  # the exit code is the failure, not a fabricated check reason


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------


def test_check_healthz_accepts_the_documented_body() -> None:
    assert pq._check_healthz('{"status":"ok"}') is None


def test_check_healthz_rejects_anything_else() -> None:
    assert pq._check_healthz('{"status":"degraded"}') is not None
    assert pq._check_healthz("not json") is not None


def test_check_capabilities_list_wants_a_non_empty_page() -> None:
    """Not an exact count: the seeded demo dataset returns every entity
    type on this route (capabilities, concepts, integrations, people), and
    its size is not this checker's contract to pin -- only that seeding
    produced a real, non-empty page."""
    assert pq._check_capabilities_list('{"items": [{"name": "a"}], "next_cursor": null}') is None
    assert pq._check_capabilities_list('{"items": [{"name": "a"}, {"name": "b"}], "next_cursor": null}') is None
    assert pq._check_capabilities_list('{"items": [], "next_cursor": null}') is not None


def test_check_capabilities_list_accepts_a_bare_array_too() -> None:
    """The runner's check does not assume the envelope shape -- a bare
    non-empty array satisfies the documented claim just as well."""
    assert pq._check_capabilities_list('[{"name": "a"}]') is None


def test_check_capability_by_name_wants_the_documented_slug() -> None:
    assert pq._check_capability_by_name('{"name": "salt-design-system"}') is None
    assert pq._check_capability_by_name('{"name": "something-else"}') is not None


def test_check_token_exported_wants_jwt_shape() -> None:
    assert pq._check_token_exported({"TOKEN": "a.b.c"}) is None
    assert pq._check_token_exported({}) is not None
    assert pq._check_token_exported({"TOKEN": "not-a-jwt"}) is not None


# ---------------------------------------------------------------------------
# Environment-limited classification
# ---------------------------------------------------------------------------


def test_classify_env_limit_recognizes_the_real_devstack_message() -> None:
    outcome = pq.StepOutcome(
        step=pq.Step(step_id="dev-up", label="l", source="d", script="make dev-up"),
        exit_code=1,
        stderr="No usable PostgreSQL 16 with pgvector was found.\n\nThe dev stack can get Postgres from...",
    )
    pq._classify_env_limit(outcome)
    assert outcome.env_limited_reason != ""
    assert "PostgreSQL 16" in outcome.env_limited_reason


def test_classify_env_limit_leaves_an_unrelated_failure_alone() -> None:
    outcome = pq.StepOutcome(
        step=pq.Step(step_id="dev-up", label="l", source="d", script="make dev-up"),
        exit_code=1,
        stderr="ModuleNotFoundError: no module named 'registry'",
    )
    pq._classify_env_limit(outcome)
    assert outcome.env_limited_reason == ""


# ---------------------------------------------------------------------------
# run_path: sequencing, abort-on-failure, cleanup always runs
# ---------------------------------------------------------------------------


def test_run_path_runs_every_step_in_order_when_all_pass(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(0, "", "")] * 3)
    steps = [pq.Step(step_id=f"s{i}", label=f"s{i}", source="d", script="true") for i in range(3)]
    result = pq.run_path(executor, name="p", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0)
    assert result.passed
    assert [o.step.step_id for o in result.steps] == ["s0", "s1", "s2"]
    assert len(executor.calls) == 3


def test_run_path_advances_cwd_into_the_clone_dir_once_clone_succeeds(tmp_path: Path) -> None:
    """Regression guard for a real bug this proof runner hit on its first
    live run: `cd registry` inside the `clone` step's own throwaway bash
    subprocess never reached the *next* step's subprocess, so every later
    step ran from the parent directory -- `pip install` failed with "does
    not appear to be a Python project" because it was one level too high."""
    executor = FakeExecutor([pq.ExecResult(0, "", "")] * 3)
    steps = [
        pq.Step(step_id="clone", label="clone", source="d", script="git clone x registry\ncd registry"),
        pq.Step(step_id="pip-install", label="pip", source="d", script="pip install -e ."),
    ]
    teardown = [pq.Step(step_id="teardown", label="down", source="d", script="make dev-down")]
    pq.run_path(executor, name="p", steps=steps, teardown=teardown, cwd=tmp_path, base_env={}, default_timeout=5.0)

    clone_cwd, pip_cwd, teardown_cwd = (call[1] for call in executor.calls)
    assert clone_cwd == tmp_path
    assert pip_cwd == tmp_path / pq.CLONE_DEST_NAME
    assert teardown_cwd == tmp_path / pq.CLONE_DEST_NAME


def test_run_path_does_not_advance_cwd_when_the_clone_step_itself_fails(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(1, "", "clone failed")])
    steps = [pq.Step(step_id="clone", label="clone", source="d", script="git clone x registry")]
    teardown = [pq.Step(step_id="teardown", label="down", source="d", script="make dev-down")]
    pq.run_path(executor, name="p", steps=steps, teardown=teardown, cwd=tmp_path, base_env={}, default_timeout=5.0)

    teardown_cwd = executor.calls[-1][1]
    assert teardown_cwd == tmp_path  # nothing to descend into -- the clone never landed


def test_run_path_stops_at_first_failure_and_skips_the_rest(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(0, "", ""), pq.ExecResult(1, "", "boom")])
    steps = [
        pq.Step(step_id="ok", label="ok", source="d", script="true"),
        pq.Step(step_id="bad", label="bad", source="d", script="false"),
        pq.Step(step_id="never-reached", label="n", source="d", script="true"),
    ]
    result = pq.run_path(executor, name="p", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0)
    assert not result.passed
    assert result.aborted_step_id == "bad"
    assert [o.step.step_id for o in result.steps] == ["ok", "bad"]  # never-reached truly never ran
    assert len(executor.calls) == 2


def test_run_path_always_attempts_teardown_even_after_a_failure(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(1, "", "boom"), pq.ExecResult(0, "", "")])
    steps = [pq.Step(step_id="bad", label="bad", source="d", script="false")]
    teardown = [pq.Step(step_id="teardown", label="down", source="d", script="make dev-down")]
    result = pq.run_path(
        executor, name="p", steps=steps, teardown=teardown, cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    assert not result.passed
    assert len(result.cleanup) == 1
    assert result.cleanup[0].ok


def test_run_path_env_limited_is_not_a_pass_but_is_distinguished(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(1, "", "No usable PostgreSQL 16 with pgvector was found.")])
    steps = [pq.Step(step_id="dev-up", label="dev-up", source="d", script="make dev-up")]
    result = pq.run_path(
        executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    assert not result.passed
    assert result.env_limited_reason is not None
    assert not result.defect_found  # this is the whole point: not a doc defect


def test_run_path_a_non_env_limited_failure_is_a_defect(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(1, "", "SyntaxError: invalid syntax")])
    steps = [pq.Step(step_id="s", label="s", source="d", script="false")]
    result = pq.run_path(
        executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    assert result.defect_found


# ---------------------------------------------------------------------------
# ProofReport / transcript rendering
# ---------------------------------------------------------------------------


def _report_with(native: pq.PathResult | None, compose: pq.PathResult | None) -> pq.ProofReport:
    return pq.ProofReport(
        commit_sha="abc123",
        generated_at="2026-01-01T00:00:00+00:00",
        tool_versions=pq.ToolVersions(
            git="git 2.0", make="make 4.0", docker="docker 27", docker_compose="v2", os_platform="test"
        ),
        native=native,
        compose=compose,
    )


def test_overall_ok_when_both_paths_pass(tmp_path: Path) -> None:
    executor = FakeExecutor([pq.ExecResult(0, "", "")] * 2)
    steps = [pq.Step(step_id="s", label="s", source="d", script="true")]
    native = pq.run_path(
        executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    compose = pq.run_path(
        executor, name="compose", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    report = _report_with(native, compose)
    assert report.overall_ok


def test_overall_ok_survives_an_environment_limited_path(tmp_path: Path) -> None:
    limited_executor = FakeExecutor([pq.ExecResult(1, "", "No usable PostgreSQL 16 with pgvector was found.")])
    passing_executor = FakeExecutor([pq.ExecResult(0, "", "")])
    steps = [pq.Step(step_id="s", label="s", source="d", script="make dev-up")]
    native = pq.run_path(
        limited_executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    compose = pq.run_path(
        passing_executor, name="compose", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    report = _report_with(native, compose)
    assert report.overall_ok  # a known, disclosed environment limit does not fail the run


def test_overall_ok_is_false_when_a_path_hits_a_real_defect(tmp_path: Path) -> None:
    bad_executor = FakeExecutor([pq.ExecResult(1, "", "unexpected traceback")])
    steps = [pq.Step(step_id="s", label="s", source="d", script="false")]
    native = pq.run_path(
        bad_executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    report = _report_with(native, None)
    assert not report.overall_ok


def test_render_transcript_never_leaks_a_raw_jwt_or_secret(tmp_path: Path) -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJkZXYifQ.abcdefghijklmnopqrstuvwxyz"

    def responder(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> pq.ExecResult:
        return pq.ExecResult(0, jwt, "")

    executor = FakeExecutor(responder=responder)
    steps = [pq.Step(step_id="mint", label="mint", source="d", script="mint-a-token")]
    result = pq.run_path(
        executor, name="native", steps=steps, teardown=[], cwd=tmp_path, base_env={}, default_timeout=5.0
    )
    report = _report_with(result, None)
    transcript = pq.render_transcript(report)
    assert jwt not in transcript
    assert "<redacted-jwt>" in transcript


def test_render_transcript_reports_a_passed_path() -> None:
    steps_outcome = pq.StepOutcome(step=pq.Step(step_id="s", label="s", source="d", script="true"), exit_code=0)
    result = pq.PathResult(name="native", steps=[steps_outcome])
    transcript = pq.render_transcript(_report_with(result, None))
    assert "PASSED" in transcript


def test_render_transcript_reports_an_environment_limited_path() -> None:
    outcome = pq.StepOutcome(
        step=pq.Step(step_id="dev-up", label="dev-up", source="d", script="make dev-up"),
        exit_code=1,
        stderr="No usable PostgreSQL 16 with pgvector was found.",
    )
    pq._classify_env_limit(outcome)
    result = pq.PathResult(name="native", steps=[outcome], aborted_step_id="dev-up")
    transcript = pq.render_transcript(_report_with(result, None))
    assert "ENVIRONMENT-LIMITED" in transcript


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_without_fresh_clone_does_nothing_and_exits_nonzero(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    rc = pq.main(["--source-repository", ".", "--output", str(out)])
    assert rc == 2
    assert not out.exists()


# ---------------------------------------------------------------------------
# Doc fidelity -- the mechanical guard against this module's copy of the
# doc commands drifting from the docs themselves.
# ---------------------------------------------------------------------------


def _bash_blocks(markdown: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", markdown, flags=re.DOTALL)


class TestDocFidelity:
    quickstart_text = (REPO_ROOT / "docs" / "02-get-started" / "01-quickstart.md").read_text(encoding="utf-8")
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_native_setup_steps_match_step_1s_fenced_block(self) -> None:
        blocks = _bash_blocks(self.quickstart_text)
        block1 = blocks[0].rstrip("\n")

        source = "/scratch/example-source"
        steps = pq.native_steps(source)
        setup = [s for s in steps if s.step_id in ("clone", "venv", "pip-install", "dev-up")]
        assembled = "\n".join(step.script for step in setup)

        # One deliberate addition on top of the doc's own text: an explicit
        # `git clone <repo-url> registry` destination, rather than relying on
        # git's own default (the URL's basename) -- so this proof works
        # from a source path that is not itself named "registry" (a scratch
        # snapshot clone, say), while still producing exactly what a reader
        # cloning the real project gets, since that project *is* named
        # "registry". Every other line is unmodified doc text.
        expected = block1.replace("git clone <repo-url>", f"git clone {shlex.quote(source)} registry")
        assert assembled == expected

    def test_native_healthz_step_matches_its_fenced_block(self) -> None:
        blocks = _bash_blocks(self.quickstart_text)
        healthz_block = blocks[1].rstrip("\n")
        steps = pq.native_steps("/scratch/example")
        healthz_step = next(s for s in steps if s.step_id == "healthz")
        assert healthz_step.script == healthz_block

    def test_shared_steps_2_through_4_match_their_fenced_blocks(self) -> None:
        blocks = _bash_blocks(self.quickstart_text)
        # Blocks, in document order, after the Step-1 pair: dev-token,
        # export TOKEN, dev-seed, capabilities list, capability by name.
        dev_token_block, export_block, dev_seed_block, list_block, by_name_block = blocks[2:7]

        steps = {s.step_id: s.script for s in pq._shared_steps("docs/02-get-started/01-quickstart.md")}
        assert steps["dev-token"] == dev_token_block.rstrip("\n")
        assert steps["dev-jwt"] == export_block.rstrip("\n")
        assert steps["dev-seed"] == dev_seed_block.rstrip("\n")
        assert steps["capabilities-list"] == list_block.rstrip("\n")
        assert steps["capabilities-by-name"] == by_name_block.rstrip("\n")

    def test_compose_setup_steps_match_the_alternative_fenced_block(self) -> None:
        blocks = _bash_blocks(self.quickstart_text)
        # index 7 is the "Stopping the stack" block (dev-down / dev-reset);
        # the compose clone+venv+install+up+migrate block is the one after it.
        compose_block = blocks[8].rstrip("\n")

        source = "/scratch/example-source"
        steps = pq.compose_steps(source)
        setup = [s for s in steps if s.step_id in ("clone", "venv", "pip-install", "compose-up", "migrate")]
        assembled = "\n".join(step.script for step in setup)

        # Same deliberate addition as the native path's clone step -- see
        # the comment in test_native_setup_steps_match_step_1s_fenced_block.
        expected = compose_block.replace("git clone <repo-url>", f"git clone {shlex.quote(source)} registry")
        assert assembled == expected

    def test_teardown_commands_are_literally_present_in_the_docs(self) -> None:
        assert "make dev-down" in self.quickstart_text
        assert "docker compose down" in self.quickstart_text
        assert "-v" in self.quickstart_text  # "(`-v` to wipe the database)"

    def test_readmes_quickstart_teaser_is_a_subset_of_the_proven_step_1(self) -> None:
        """README's condensed block is not run as its own separate proof
        path (same commands, same first failure mode as quickstart.md's
        Step 1) -- this asserts it stays a textual subset rather than
        silently drifting into its own, unverified procedure."""
        readme_blocks = _bash_blocks(self.readme_text)
        readme_block = readme_blocks[0].rstrip("\n")
        for line in readme_block.splitlines():
            if line.strip().startswith("#"):
                # README inlines the expected output as a trailing comment
                # in the same fenced block; quickstart.md shows the same
                # information as its own separate ```json block instead.
                continue
            assert line in self.quickstart_text, f"README quickstart line not found in quickstart.md: {line!r}"


def test_the_real_module_source_has_no_stray_dashfss_curl_flags() -> None:
    """Regression guard for a specific mistake this file's own author made
    once while drafting: adding `-fsS` to a curl step "for robustness" is a
    silent deviation from what the doc actually tells a reader to type."""
    source = Path(pq.__file__).read_text(encoding="utf-8")
    assert "-fsS" not in source


def test_real_path_clone_directories_use_a_plain_slug_not_the_display_label() -> None:
    """Regression guard for a real bug this proof runner hit on its first
    live run: the on-disk clone directory was named from the human-readable
    path label ("Native devstack path (`make dev-up`)"), whose backticks and
    parentheses corrupted a freshly created venv's shebang line -- `pip`
    failed with "No such file or directory" for a reason that had nothing to
    do with the documented commands. The directory name must come from a
    plain identifier."""
    source = Path(pq.__file__).read_text(encoding="utf-8")
    assert 'slug="native"' in source
    assert 'slug="compose"' in source
