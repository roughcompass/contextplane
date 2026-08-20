"""Prove the documented quickstarts work from a genuinely clean clone.

Every other doc-truthfulness check in this repository re-reads a file and
reasons about it. This one is different on purpose: a quickstart can be
individually plausible sentence by sentence and still fail as a *sequence*,
and the only way to catch that is to actually run the sequence somewhere that
has none of a working checkout's accumulated state -- no pre-built `.venv`,
no already-migrated database, no `.env.dev` left over from a previous session,
no uncommitted fix sitting in the tree that made a step "work" by accident.

What this module does:

1. Clone the exact current commit into a temporary directory (real mode), or
   accept an injected command executor that never touches the network or the
   filesystem outside a test's own `tmp_path` (test mode).
2. Run every command `README.md` and `docs/02-get-started/01-quickstart.md`
   tell a reader to run, in order, for the native devstack path: clone,
   install, `make dev-up`, prove `/healthz`, bootstrap a tenant, mint a real
   JWT, seed the demo data, and call the two documented authenticated routes.
3. Do the same again from a *second* fresh clone for the Docker Compose path.
4. Tear each stack down (including volumes for Compose) whether or not the
   path passed, and always remove the temporary clone.
5. Write a redacted transcript: commit SHA, tool versions, every command,
   exit code, bounded output, duration, and cleanup result.

**Shell state across steps.** Each step runs as its own subprocess (that is
the seam a unit test replaces), but the documented sequence depends on shell
state one step creates and a later step consumes -- an activated venv's
`PATH`, `export TOKEN=...`. Rather than special-case `source` and `export`,
every step's script is run with a trailing `env -0` dump; the parsed result
becomes the environment for the *next* step. This reproduces what a real
terminal session does without hand-parsing which doc lines are shell
builtins.

**Environment-limited vs. failed.** A step can stop the path for two very
different reasons: the documentation is wrong (a real defect -- the run
should fail loudly and the doc gets fixed), or the *host running the proof*
is missing something the docs already name as a precondition (no PostgreSQL
16 available locally, matching `make dev-up`'s own "here is what I tried and
why each failed" message). The second case is not a documentation defect and
is reported as such rather than as a failure -- see `_ENV_LIMIT_SIGNATURES`.

Run locally:
    python scripts/prove_quickstart.py --source-repository . --fresh-clone --output /tmp/quickstart-proof.md
    python scripts/prove_quickstart.py --source-repository . --fresh-clone --paths compose --output /tmp/compose-only.md
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import re
import shlex
import shutil
import subprocess  # noqa: S404 - proof-runner tooling; every call site builds its own fixed argv, never caller/network input
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAX_OUTPUT_CHARS = 4000
_DEFAULT_STEP_TIMEOUT = 300.0
_DEFAULT_BUILD_TIMEOUT = 1800.0  # `docker compose up -d`'s first-ever build: fetches + bakes the embedding model

# ---------------------------------------------------------------------------
# Redaction -- shape-based, not a fixed name list. The mock IDP and
# entitlement service hand out a fresh JWT and client secret every run, so
# there is no static string to allowlist against.
# ---------------------------------------------------------------------------

_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")
_SECRET_ASSIGN_RE = re.compile(r'(?i)\b(client_secret|access_token)(["\']?\s*[:=]\s*["\']?)([^\s"\'&,}]+)')


def redact(text: str) -> str:
    """Strip anything shaped like a bearer token, JWT, or secret value."""
    text = _JWT_RE.sub("<redacted-jwt>", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}<redacted-token>", text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return text


def _bound(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    """Redact, then cap length so one runaway command can't blow up the transcript."""
    text = redact(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} more chars]"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Execution seam -- the "injected command executor" the contract calls for.
# Production code only ever calls through this; a test supplies a fake and
# never touches a subprocess, the network, or a real clone.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ExecResult:
    """What running one command produced."""

    returncode: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float) -> ExecResult: ...


def real_executor(argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float) -> ExecResult:
    """Actually run a command. The only function here that touches a subprocess."""
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built by this module; see module docstring
            list(argv),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ExecResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        return ExecResult(124, out, f"{err}\n[proof runner: timed out after {timeout:.0f}s]")


# ---------------------------------------------------------------------------
# A "fresh terminal" environment -- built from scratch rather than inherited,
# so nothing from *this* process (an activated tool venv, a stray
# DATABASE_URL, a leftover VIRTUAL_ENV) leaks into a run that exists to prove
# the docs work with none of that. `python_bin_dir` lets a caller point at a
# specific interpreter satisfying the documented ">=3.12" precondition
# without mutating any shell profile or global interpreter config.
# ---------------------------------------------------------------------------


def baseline_env(*, python_bin_dir: str | None = None, extra_path: Sequence[str] = ()) -> dict[str, str]:
    parts = list(extra_path)
    if python_bin_dir:
        parts.append(python_bin_dir)
    parts += ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - config: intentional; fallback only, never written to directly
        "USER": os.environ.get("USER", "proof"),  # config: intentional
        "PATH": ":".join(parts),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),  # noqa: S108 - config: intentional; fallback only, `tempfile` resolves the real dir
    }
    # Docker's CLI needs to find the daemon socket / context config; both are
    # resolved from HOME above, so nothing Docker-specific needs to be added
    # unless DOCKER_HOST is pinned in the calling environment (CI containers
    # sometimes do this deliberately).
    if "DOCKER_HOST" in os.environ:  # config: intentional
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]  # config: intentional
    return env


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

Check = Callable[[str], "str | None"]
EnvCheck = Callable[[dict[str, str]], "str | None"]


@dataclasses.dataclass(frozen=True)
class Step:
    """One documented, executable action.

    *script* is bash source, run verbatim as written in the doc named by
    *source* (modulo substituting the real clone source for the `<repo-url>`
    placeholder -- everything else is copied character for character).
    """

    step_id: str
    label: str
    source: str
    script: str
    manual: bool = False
    check: Check | None = None
    env_check: EnvCheck | None = None
    timeout: float | None = None  # None -> caller's default for this path


@dataclasses.dataclass
class StepOutcome:
    step: Step
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    started_at: str = ""
    skipped: bool = False
    skip_reason: str = ""
    failure_reason: str = ""
    env_limited_reason: str = ""

    @property
    def ok(self) -> bool:
        if self.skipped:
            return True
        return self.exit_code == 0 and not self.failure_reason


def _parse_env_dump(raw: bytes) -> dict[str, str]:
    """Parse `env -0` output (NUL-separated `NAME=value` records)."""
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        name, sep, value = entry.partition(b"=")
        if not sep:
            continue
        try:
            env[name.decode()] = value.decode()
        except UnicodeDecodeError:
            continue
    return env


def run_step(
    executor: CommandExecutor,
    step: Step,
    *,
    cwd: Path,
    env: dict[str, str],
    default_timeout: float,
) -> tuple[StepOutcome, dict[str, str]]:
    """Run one step, returning its outcome and the environment the *next*
    step inherits (see the module docstring on shell-state carryover)."""
    if step.manual:
        outcome = StepOutcome(
            step=step,
            started_at=_now_iso(),
            skipped=True,
            skip_reason="documented as a manual/browser action, not an executable command",
        )
        return outcome, env

    dump_path = cwd / f".proof-env-{uuid.uuid4().hex}.tmp"
    wrapped = (
        "set -uo pipefail\n"
        f"{step.script}\n"
        "__rc=$?\n"
        f"env -0 > {shlex.quote(str(dump_path))} 2>/dev/null || true\n"
        "exit $__rc\n"
    )
    started_at = _now_iso()
    t0 = time.monotonic()
    result = executor(["bash", "-c", wrapped], cwd=cwd, env=env, timeout=step.timeout or default_timeout)
    duration = time.monotonic() - t0

    next_env = env
    if dump_path.exists():
        next_env = _parse_env_dump(dump_path.read_bytes())
        dump_path.unlink(missing_ok=True)

    outcome = StepOutcome(
        step=step,
        exit_code=result.returncode,
        stdout=_bound(result.stdout),
        stderr=_bound(result.stderr),
        duration_s=duration,
        started_at=started_at,
    )
    if outcome.exit_code == 0:
        if step.check is not None:
            reason = step.check(result.stdout)
            if reason:
                outcome.failure_reason = reason
        if outcome.failure_reason == "" and step.env_check is not None:
            reason = step.env_check(next_env)
            if reason:
                outcome.failure_reason = reason
    return outcome, next_env


# ---------------------------------------------------------------------------
# Environment-limited classification -- a documented precondition this host
# does not meet, not a doc defect. See the module docstring.
# ---------------------------------------------------------------------------

_ENV_LIMIT_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        "no usable postgresql 16 with pgvector was found",
        "no PostgreSQL 16 source is available on this machine for the native devstack path (no "
        "Postgres.app, no `initdb` on PATH, no Python-3.12-or-earlier wheel for the `devstack` extra "
        "on this interpreter, and no DATABASE_URL supplied). The docs already name this as a "
        "precondition and `make dev-up` refuses cleanly rather than half-starting -- not a documentation "
        "defect. The Docker Compose path is the documented equivalent and is proven separately.",
    ),
)


def _classify_env_limit(outcome: StepOutcome) -> None:
    haystack = f"{outcome.stdout}\n{outcome.stderr}".lower()
    for needle, reason in _ENV_LIMIT_SIGNATURES:
        if needle in haystack:
            outcome.env_limited_reason = reason
            return


# ---------------------------------------------------------------------------
# Content checks -- the contract asks for proof of behavior, not just an
# exit code. Each of these reads a step's raw (pre-redaction) stdout.
# ---------------------------------------------------------------------------


def _check_healthz(stdout: str) -> str | None:
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return f"GET /healthz did not return JSON: {stdout[:200]!r}"
    if body.get("status") != "ok":
        return f"GET /healthz returned {body!r}, not {{'status': 'ok'}}"
    return None


def _check_token_exported(env: dict[str, str]) -> str | None:
    token = env.get("TOKEN", "")
    if not token:
        return "TOKEN was not set in the environment after `export TOKEN=$(make dev-jwt)`"
    if token.count(".") != 2:
        return "TOKEN is not JWT-shaped (expected three dot-separated segments)"
    return None


def _check_capabilities_list(stdout: str) -> str | None:
    """Not an exact count -- the demo dataset's size is not this runner's
    contract to pin, and the endpoint returns every seeded entity type, not
    only capabilities (see the doc's own note to add `entity_type=capability`
    for a narrower view). What the doc promises, and what this checks, is
    that seeding worked at all -- a real, present, non-empty page. Whether
    the specific `salt-design-system` capability exists is the *next*
    step's job (a deterministic by-name lookup, not a first-page-of-a-
    growing-dataset guess)."""
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return f"GET /v1/capabilities did not return JSON: {stdout[:200]!r}"
    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return f"GET /v1/capabilities response has no list of entities: {body!r}"
    if not items:
        return "expected at least the seeded demo dataset, got an empty list"
    return None


def _check_capability_by_name(stdout: str) -> str | None:
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return f"GET /v1/capabilities/salt-design-system did not return JSON: {stdout[:200]!r}"
    if body.get("name") != "salt-design-system":
        return f"expected name='salt-design-system', got {body.get('name')!r}"
    return None


# ---------------------------------------------------------------------------
# Doc-derived step sequences
#
# Command text is copied verbatim from README.md / docs/02-get-started/
# 01-quickstart.md (see tests/unit/test_prove_quickstart.py, which asserts
# the two stay in sync) with exactly one substitution: `<repo-url>` becomes
# the real clone source this run was given. Everything else -- line breaks,
# flags, quoting -- is character-for-character what a reader sees.
# ---------------------------------------------------------------------------


# The clone destination name. `run_path` below advances its working
# directory into this same name once the `clone` step succeeds -- the two
# must agree, since a step's `cd contextplane` only affects that one throwaway
# bash subprocess, never the *next* step's subprocess.
CLONE_DEST_NAME = "contextplane"


def clone_step(source: str, *, doc: str) -> Step:
    return Step(
        step_id="clone",
        label="git clone + enter the checkout",
        source=doc,
        script=f"git clone {shlex.quote(source)} {CLONE_DEST_NAME}\ncd {CLONE_DEST_NAME}",
    )


def native_steps(source: str) -> list[Step]:
    quickstart = "docs/02-get-started/01-quickstart.md"
    return [
        clone_step(source, doc=quickstart),
        Step(
            step_id="venv",
            label="create + activate the virtualenv",
            source=quickstart,
            script="python -m venv .venv && source .venv/bin/activate",
        ),
        Step(
            step_id="pip-install",
            label="install the project (dev + devstack extras)",
            source=quickstart,
            script='pip install -e ".[dev,devstack]"',
            timeout=600.0,
        ),
        Step(
            step_id="dev-up",
            label="make dev-up",
            source=quickstart,
            script="make dev-up",
            timeout=180.0,
        ),
        Step(
            step_id="healthz",
            label="curl http://localhost:8000/healthz",
            source=quickstart,
            script="curl http://localhost:8000/healthz",
            check=_check_healthz,
        ),
        *_shared_steps(quickstart),
    ]


def compose_steps(source: str) -> list[Step]:
    quickstart = "docs/02-get-started/01-quickstart.md"
    return [
        clone_step(source, doc=quickstart),
        Step(
            step_id="venv",
            label="create + activate the virtualenv",
            source=quickstart,
            script="python -m venv .venv && source .venv/bin/activate",
        ),
        Step(
            step_id="pip-install",
            label="install the project (dev extras)",
            source=quickstart,
            script='pip install -e ".[dev]"',
            timeout=600.0,
        ),
        Step(
            step_id="compose-up",
            label="docker compose up -d",
            source=quickstart,
            script="docker compose up -d",
            timeout=_DEFAULT_BUILD_TIMEOUT,
        ),
        Step(
            step_id="migrate",
            label="make migrate",
            source=quickstart,
            script="make migrate",
            timeout=180.0,
        ),
        *_shared_steps(quickstart),
    ]


def _shared_steps(quickstart: str) -> list[Step]:
    """Steps 2-4: identical text for both paths per the doc's own claim
    ("the ports, credentials, and every make command are the same")."""
    return [
        Step(
            step_id="dev-token",
            label="make dev-token",
            source=quickstart,
            script="make dev-token",
            timeout=120.0,
        ),
        Step(
            step_id="dev-jwt",
            label="export TOKEN=$(make dev-jwt)",
            source=quickstart,
            script="export TOKEN=$(make dev-jwt)",
            env_check=_check_token_exported,
        ),
        Step(
            step_id="dev-seed",
            label="make dev-seed",
            source=quickstart,
            script="make dev-seed",
            timeout=120.0,
        ),
        Step(
            step_id="capabilities-list",
            label="curl the capabilities list with the minted JWT",
            source=quickstart,
            script='curl -H "Authorization: Bearer $TOKEN" \\\n     http://localhost:8000/v1/capabilities',
            check=_check_capabilities_list,
        ),
        Step(
            step_id="capabilities-by-name",
            label="curl one capability by name",
            source=quickstart,
            script=(
                'curl -H "Authorization: Bearer $TOKEN" \\\n'
                "     http://localhost:8000/v1/capabilities/salt-design-system"
            ),
            check=_check_capability_by_name,
        ),
    ]


def native_teardown() -> list[Step]:
    return [
        Step(
            step_id="teardown",
            label="make dev-down",
            source="docs/02-get-started/01-quickstart.md",
            script="make dev-down",
            timeout=60.0,
        ),
    ]


def compose_teardown() -> list[Step]:
    return [
        Step(
            step_id="teardown",
            label="docker compose down -v",
            source="docs/02-get-started/01-quickstart.md",
            script="docker compose down -v",
            timeout=120.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Path runner
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PathResult:
    name: str
    steps: list[StepOutcome] = dataclasses.field(default_factory=list)
    cleanup: list[StepOutcome] = dataclasses.field(default_factory=list)
    aborted_step_id: str | None = None

    @property
    def env_limited_reason(self) -> str | None:
        for outcome in self.steps:
            if outcome.env_limited_reason:
                return outcome.env_limited_reason
        return None

    @property
    def passed(self) -> bool:
        """True only when every step ran and succeeded. A path that stopped
        for an environment-limited reason is neither a pass nor a doc
        defect -- callers check `env_limited_reason` to tell the two apart."""
        if self.aborted_step_id is not None:
            return False
        return all(outcome.ok for outcome in self.steps)

    @property
    def defect_found(self) -> bool:
        """True when this path aborted for a reason that is NOT a
        pre-declared environment limitation -- i.e., an actual doc defect."""
        return self.aborted_step_id is not None and self.env_limited_reason is None


def run_path(
    executor: CommandExecutor,
    *,
    name: str,
    steps: Sequence[Step],
    teardown: Sequence[Step],
    cwd: Path,
    base_env: dict[str, str],
    default_timeout: float = _DEFAULT_STEP_TIMEOUT,
) -> PathResult:
    """*cwd* is where the `clone` step runs. A step's own `cd` only affects
    its own throwaway bash subprocess -- never the *next* step's -- so once
    `clone` succeeds, every later step (setup, teardown alike) runs from
    `cwd / CLONE_DEST_NAME`, matching what actually happens on disk."""
    result = PathResult(name=name)
    env = dict(base_env)
    current_cwd = cwd
    for step in steps:
        outcome, env = run_step(executor, step, cwd=current_cwd, env=env, default_timeout=default_timeout)
        result.steps.append(outcome)
        if step.step_id == "clone" and outcome.ok:
            current_cwd = cwd / CLONE_DEST_NAME
        if not outcome.ok:
            _classify_env_limit(outcome)
            result.aborted_step_id = step.step_id
            break

    # Teardown is always attempted, pass or fail -- a half-started stack left
    # running is its own defect, independent of whether the proof passed.
    for step in teardown:
        outcome, env = run_step(executor, step, cwd=current_cwd, env=env, default_timeout=default_timeout)
        result.cleanup.append(outcome)

    return result


# ---------------------------------------------------------------------------
# Real-mode plumbing: clone, tool-version probe, cleanup
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolVersions:
    git: str
    make: str
    docker: str
    docker_compose: str
    os_platform: str


def probe_tool_versions(executor: CommandExecutor, *, env: dict[str, str]) -> ToolVersions:
    def _v(argv: list[str]) -> str:
        result = executor(argv, cwd=REPO_ROOT, env=env, timeout=15.0)
        return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else "unavailable"

    return ToolVersions(
        git=_v(["git", "--version"]),
        make=_v(["make", "--version"]),
        docker=_v(["docker", "--version"]),
        docker_compose=_v(["docker", "compose", "version"]),
        os_platform=platform.platform(),
    )


def resolve_commit_sha(executor: CommandExecutor, *, source_repository: Path, env: dict[str, str]) -> str:
    result = executor(["git", "rev-parse", "HEAD"], cwd=source_repository, env=env, timeout=15.0)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


@dataclasses.dataclass
class ProofReport:
    commit_sha: str
    generated_at: str
    tool_versions: ToolVersions
    native: PathResult | None
    compose: PathResult | None
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def overall_ok(self) -> bool:
        """A run is trustworthy when nothing that ran hit an actual defect.
        A path skipped or environment-limited does not fail the run; a path
        that aborted for any other reason does."""
        for result in (self.native, self.compose):
            if result is not None and result.defect_found:
                return False
        return True


def render_transcript(report: ProofReport) -> str:
    lines: list[str] = []
    lines.append("# Quickstart clean-clone proof")
    lines.append("")
    lines.append(f"- **Commit:** `{report.commit_sha}`")
    lines.append(f"- **Generated:** {report.generated_at}")
    lines.append(f"- **OS:** {report.tool_versions.os_platform}")
    lines.append(f"- **git:** {report.tool_versions.git}")
    lines.append(f"- **make:** {report.tool_versions.make}")
    lines.append(f"- **docker:** {report.tool_versions.docker}")
    lines.append(f"- **docker compose:** {report.tool_versions.docker_compose}")
    lines.append("")
    if report.notes:
        lines.append("## Notes")
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")

    for result in (report.native, report.compose):
        if result is None:
            continue
        lines.append(f"## {result.name}")
        lines.append("")
        if result.passed:
            lines.append("**Result: PASSED** -- every documented step ran and every check held.")
        elif result.env_limited_reason:
            lines.append(
                f"**Result: ENVIRONMENT-LIMITED** at step `{result.aborted_step_id}` -- {result.env_limited_reason}"
            )
        else:
            lines.append(f"**Result: FAILED** at step `{result.aborted_step_id}`.")
        lines.append("")
        lines.append("| step | source | exit | duration | result |")
        lines.append("|---|---|---|---|---|")
        for outcome in result.steps:
            state = "skipped" if outcome.skipped else ("ok" if outcome.ok else "FAILED")
            lines.append(
                f"| `{outcome.step.step_id}` | {outcome.step.source} | {outcome.exit_code} "
                f"| {outcome.duration_s:.1f}s | {state} |"
            )
        lines.append("")
        for outcome in result.steps:
            lines.append(f"### `{outcome.step.step_id}` -- {outcome.step.label}")
            lines.append("")
            lines.append(f"Ran at {outcome.started_at}.")
            lines.append("")
            lines.append("```bash")
            lines.append(outcome.step.script)
            lines.append("```")
            if outcome.skipped:
                lines.append("")
                lines.append(f"Skipped: {outcome.skip_reason}")
            else:
                lines.append("")
                lines.append(f"Exit code: {outcome.exit_code} ({outcome.duration_s:.1f}s)")
                if outcome.failure_reason:
                    lines.append("")
                    lines.append(f"**Check failed:** {outcome.failure_reason}")
                if outcome.stdout.strip():
                    lines.append("")
                    lines.append("<details><summary>stdout</summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append(outcome.stdout)
                    lines.append("```")
                    lines.append("")
                    lines.append("</details>")
                if outcome.stderr.strip():
                    lines.append("")
                    lines.append("<details><summary>stderr</summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append(outcome.stderr)
                    lines.append("```")
                    lines.append("")
                    lines.append("</details>")
            lines.append("")
        if result.cleanup:
            lines.append("#### Cleanup")
            lines.append("")
            for outcome in result.cleanup:
                state = "ok" if outcome.ok else "FAILED"
                lines.append(f"- `{outcome.step.script.strip()}` -- exit {outcome.exit_code} ({state})")
            lines.append("")

    return "\n".join(lines) + "\n"


def _run_real_path(
    *,
    slug: str,
    name: str,
    steps: Sequence[Step],
    teardown: Sequence[Step],
    source_repository: Path,
    workdir: Path,
    env: dict[str, str],
) -> PathResult:
    """*slug* (not *name*) becomes the on-disk directory name. *name* is a
    human-readable label that may contain spaces, backticks, or parens for
    the transcript -- exactly the characters that corrupt a venv's shebang
    line if they end up in its install prefix path."""
    clone_dir = workdir / f"{slug}-{uuid.uuid4().hex[:8]}"
    clone_dir.mkdir(parents=True)
    try:
        return run_path(
            real_executor,
            name=name,
            steps=steps,
            teardown=teardown,
            cwd=clone_dir,
            base_env=env,
        )
    finally:
        shutil.rmtree(clone_dir / "contextplane", ignore_errors=True)
        shutil.rmtree(clone_dir, ignore_errors=True)


def run_real_proof(
    *,
    source_repository: str,
    paths: Sequence[str] = ("native", "compose"),
    python_bin_dir: str | None = None,
    compose_project_name: str | None = None,
) -> ProofReport:
    source_path = Path(source_repository).resolve()
    probe_env = baseline_env(python_bin_dir=python_bin_dir)
    commit_sha = resolve_commit_sha(real_executor, source_repository=source_path, env=probe_env)
    tool_versions = probe_tool_versions(real_executor, env=probe_env)

    notes: list[str] = []
    native_result: PathResult | None = None
    compose_result: PathResult | None = None

    with tempfile.TemporaryDirectory(prefix="quickstart-proof-") as tmp:
        workdir = Path(tmp)

        if "native" in paths:
            native_result = _run_real_path(
                slug="native",
                name="Native devstack path (`make dev-up`)",
                steps=native_steps(str(source_path)),
                teardown=native_teardown(),
                source_repository=source_path,
                workdir=workdir,
                env=baseline_env(python_bin_dir=python_bin_dir),
            )

        if "compose" in paths:
            compose_env = baseline_env(python_bin_dir=python_bin_dir)
            project_name = compose_project_name or f"quickstart-proof-{uuid.uuid4().hex[:8]}"
            compose_env["COMPOSE_PROJECT_NAME"] = project_name
            notes.append(
                f"Compose path ran with `COMPOSE_PROJECT_NAME={project_name}` so its containers, network, "
                "and named volume can never collide with a same-named checkout already running on this "
                "host -- an isolation property of this proof harness, not a documented step."
            )
            compose_result = _run_real_path(
                slug="compose",
                name="Docker Compose path (`docker compose up -d`)",
                steps=compose_steps(str(source_path)),
                teardown=compose_teardown(),
                source_repository=source_path,
                workdir=workdir,
                env=compose_env,
            )

    return ProofReport(
        commit_sha=commit_sha,
        generated_at=_now_iso(),
        tool_versions=tool_versions,
        native=native_result,
        compose=compose_result,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-repository", default=".", help="Path to the git repo to clone from.")
    parser.add_argument(
        "--fresh-clone",
        action="store_true",
        help="Real mode: actually clone and run commands. Without this flag, exits 2 (nothing to do).",
    )
    parser.add_argument("--output", required=True, help="Where to write the redacted Markdown transcript.")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=["native", "compose"],
        choices=["native", "compose"],
        help="Which documented path(s) to run (default: both).",
    )
    parser.add_argument(
        "--python-bin-dir",
        default=None,
        help="Prepend this directory to PATH so `python`/`pip` resolve to a specific interpreter "
        "satisfying the documented >=3.12 precondition, without touching any global interpreter config.",
    )
    parser.add_argument("--compose-project-name", default=None, help="Override the Compose project name.")
    args = parser.parse_args(argv)

    if not args.fresh_clone:
        print("prove_quickstart: --fresh-clone was not given; nothing to do in this mode.", file=sys.stderr)
        return 2

    report = run_real_proof(
        source_repository=args.source_repository,
        paths=args.paths,
        python_bin_dir=args.python_bin_dir,
        compose_project_name=args.compose_project_name,
    )
    transcript = render_transcript(report)
    Path(args.output).write_text(transcript, encoding="utf-8")

    for result in (report.native, report.compose):
        if result is None:
            continue
        state = "PASSED" if result.passed else ("ENV-LIMITED" if result.env_limited_reason else "FAILED")
        print(f"{result.name}: {state}", file=sys.stderr)

    return 0 if report.overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
