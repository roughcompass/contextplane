"""Lint gate: every environment variable *presented as app configuration* in
the shipped docs is one `Settings` (or a named, reasoned exception) actually
reads.

`check_env_documented.py` pins two files -- `.env.example` and the
configuration reference -- against each other. This gate covers the rest of
`README.md` and `docs/**/*.md`, where a variable can be introduced in an
`export FOO=...` example, a quickstart's `.env` snippet, or a `docker run
-e FOO=...` line without ever touching either of those two files, and drift
there is invisible to that gate.

**Why this cannot be a bare all-caps token scan.** Roughly a hundred
env-var-*shaped* tokens (`TENANT_ID`, `PROGRESSION_ID`, `ENTRY_ID`, ...) exist
in these docs as path-parameter placeholders and API-response field names,
not environment variables -- a shape-only match would force every one of
them into a suppression list that never stops growing and stops meaning
anything the day it does. This gate instead only looks at *contexts* that
present a name as something to set in the process environment:

  1. `export NAME=value` (excluding `export NAME=$(...)`  and `export
     NAME=$OTHER` -- capturing a command's output or another variable into a
     name for reuse later in the same example script is a shell-scripting
     idiom, not an instruction to configure the app; `export
     TOKEN=$(make dev-jwt)` is the recurring example of this shape).
  2. `docker ... -e NAME=value` (the same signal as `export`, spelled the
     container-runtime way).
  3. A fenced code block that is *entirely* `NAME=value` lines (optionally
     with full-line `#` comments) -- a `.env` snippet, whether it is a
     single line or the multi-line block a quickstart pastes into
     `.env.dev`. A block that mixes an assignment with anything else (a
     `curl` call, a loop, an inline trailing command) is a script that
     happens to contain an assignment, not a declared configuration
     surface, and is deliberately not matched -- see the false positives
     this excludes below.
  4. Prose that explicitly calls a backticked ALL-CAPS name an "environment
     variable" or "env var" on the same line (e.g. "The `GITHUB_WEBHOOK_SECRET`
     ... env vars are read directly ...").

A name from one of those four contexts must be one of:
  - A canonical `Settings` name -- the field's own `SCREAMING_SNAKE_CASE`
    name, or its `validation_alias` when the field declares one (mechanically
    read off `Settings.model_fields`, so a renamed or added field's env name
    updates here with zero edits).
  - Listed in `ALLOWLIST` below, with a reason.

Anything else is a documented variable no running process reads -- exactly
the class of drift that put five phantom `AUTH_*` variables into the
authorization guide.

**False positives this design was built to exclude**, all real lines from
this repository's own docs during development: a `while` loop's `CURSOR=""` /
`RESP=$(curl ...)` pagination accumulator (mixed-content block, not a pure
`.env` snippet); `export TOKEN=$(make dev-jwt)` (command-substitution RHS);
`make dev-logs SVC=api` and `make PYTHON=python3.13 <target>` (a Make
command-line variable override is not an env var, and is never the first
token on its line); `EMBEDDING_DIM=1536 EMBEDDING_DIM_ALLOW_REBUILD=true
alembic upgrade head` (an env-prefixed command, not a bare assignment line --
a real limitation, not a loophole: this shape is not matched at all, so it
also cannot flag a phantom name written this way; the three matched contexts
above still catch the overwhelming majority of real drift).

Run locally:
    python scripts/check_doc_env_mentions.py
    python scripts/check_doc_env_mentions.py --explain
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

from contextplane.config import Settings

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = ("README.md", "docs")

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

# --- context 1 & 2: export / docker -e, line-scoped -------------------------

_EXPORT_RE = re.compile(r"\bexport\s+([A-Z][A-Z0-9_]{2,})=(.*)$")
_DOCKER_E_RE = re.compile(r"(?:^|\s)-e\s+([A-Z][A-Z0-9_]{2,})=(.*)$")


def _is_command_substitution_or_variable(value: str) -> bool:
    """True for a RHS that captures a command's output or another shell
    variable rather than stating a literal configuration value -- the
    `export TOKEN=$(make dev-jwt)` shape. A quoted RHS is unwrapped one level
    first (`export NAME="$(...)"` is the same idiom, just quoted)."""
    v = value.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[0] == v[-1]:
        v = v[1:-1]
    if v.startswith(("$(", "${")):
        return True
    return len(v) > 1 and v[0] == "$" and (v[1].isalpha() or v[1] == "_")


# --- context 3: a fenced code block that is a pure ".env" snippet ----------

_PURE_ASSIGNMENT_LINE_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,}=(?:"[^"\n]*"|\'[^\'\n]*\'|<[^>\n]*>|[^\s#]*)\s*(?:#.*)?$')
_ASSIGNMENT_NAME_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})=")
_COMMENT_ONLY_RE = re.compile(r"^#")
_FENCE_RE = re.compile(r"^\s*```")

# --- context 4: prose explicitly naming an environment variable ------------

_BACKTICKED_RE = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
_ENV_PHRASE_RE = re.compile(r"\benv(?:ironment)?\s*var(?:iable)?s?\b", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Mention:
    """One name found in one of the four env-var-presenting contexts."""

    name: str
    line: int
    context: str  # "export" | "docker -e" | ".env block" | "prose"


def _line_scoped_mentions(lines: list[str]) -> list[Mention]:
    out: list[Mention] = []
    for i, line in enumerate(lines, start=1):
        m = _EXPORT_RE.search(line)
        if m and not _is_command_substitution_or_variable(m.group(2)):
            out.append(Mention(m.group(1), i, "export"))
        m = _DOCKER_E_RE.search(line)
        if m and not _is_command_substitution_or_variable(m.group(2)):
            out.append(Mention(m.group(1), i, "docker -e"))
        if _ENV_PHRASE_RE.search(line):
            for bm in _BACKTICKED_RE.finditer(line):
                out.append(Mention(bm.group(1), i, "prose"))
    return out


def _pure_env_block_mentions(lines: list[str]) -> list[Mention]:
    """Every assignment line in a fenced code block where *every* non-blank
    line is either a full-line `#` comment or a single `NAME=value`
    assignment. A block that mixes in anything else -- a `curl` call, a
    loop, a trailing command after the assignment -- is not this shape and
    contributes nothing, by design (see the module docstring)."""
    out: list[Mention] = []
    in_fence = False
    block: list[tuple[int, str]] = []

    def _is_comment_or_assignment(tx: str) -> bool:
        stripped = tx.strip()
        return bool(_COMMENT_ONLY_RE.match(stripped) or _PURE_ASSIGNMENT_LINE_RE.match(stripped))

    def flush() -> None:
        non_blank = [(ln, tx) for ln, tx in block if tx.strip()]
        if not non_blank:
            return
        if not all(_is_comment_or_assignment(tx) for _, tx in non_blank):
            return
        for ln, tx in non_blank:
            am = _ASSIGNMENT_NAME_RE.match(tx.strip())
            if am:
                out.append(Mention(am.group(1), ln, ".env block"))

    for i, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            if in_fence:
                flush()
                block = []
            in_fence = not in_fence
            continue
        if in_fence:
            block.append((i, line))
    return out


def mentions_of(text: str) -> list[Mention]:
    """Every env-var-context mention in *text*, across all four contexts."""
    lines = text.splitlines()
    return _line_scoped_mentions(lines) + _pure_env_block_mentions(lines)


# ---------------------------------------------------------------------------
# Canonical Settings names
# ---------------------------------------------------------------------------


def settings_env_names() -> frozenset[str]:
    """The env-var name `Settings` actually reads for every field: the
    field's own name upper-cased, or its `validation_alias` when one is
    declared. Mechanical, not a maintained list -- a field renamed or added
    in `contextplane/config.py` changes this set with no edit here."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        else:
            names.add(field_name.upper())
    return frozenset(names)


# ---------------------------------------------------------------------------
# Allowlist -- names that are real env-var-shaped mentions and are not,
# and can never be, a Settings name.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Exemption:
    """A group of names sharing one reason they will never be a Settings name."""

    names: frozenset[str]
    reason: str


#: Every currently-held exemption. A new entry needs the same thing every
#: name here has: a reason `Settings` will never cover it, tied to the
#: concrete script or design that reads it (or explaining that nothing reads
#: it at all, because it is not really an environment variable).
ALLOWLIST: tuple[Exemption, ...] = (
    Exemption(
        names=frozenset(
            {"DEV_TENANT_SLUG", "DEV_TENANT_ID", "DEV_ACTOR_ID", "DEV_USER_ID", "CLIENT_ID", "CLIENT_SECRET"}
        ),
        reason=(
            "`make dev-token` (scripts/bootstrap_dev_tenant.py) writes these to `.env.dev` "
            "for the local mock-IDP client-credentials exchange; a developer sources the "
            "file and Make targets read it. Never read by the running app itself, so they "
            "have no Settings field to match."
        ),
    ),
    Exemption(
        names=frozenset({"GITHUB_API_TOKEN"}),
        reason=(
            "Example value for a sync source's `credentials_ref` -- an operator picks any "
            "name; `contextplane/ingest/connector.py::resolve_credential` reads it from "
            "`os.environ` by that dynamic name at sync time, never through `Settings` (the "
            "documented exception in docs/05-reference/03-configuration.md and CLAUDE.md's "
            "Settings bypass list). The name itself is illustrative, not fixed."
        ),
    ),
    Exemption(
        names=frozenset({"SIGNING_SECRET"}),
        reason=(
            "Example env-var name a *consumer's own* webhook receiver might use to hold the "
            "shared HMAC secret (docs/04-guides/02-subscribe-to-events.md). The registry "
            "never reads this name -- `webhook_hmac_secret_ref` stores the secret value "
            "itself, opaque, with no environment-variable indirection on the registry side."
        ),
    ),
    Exemption(
        names=frozenset({"PROVIDER_CAP_ID"}),
        reason=(
            "Shell-local placeholder holding a capability UUID for reuse via `$PROVIDER_CAP_ID` "
            "later in the same example script (docs/03-use-cases/04-event-driven-consumers.md) "
            "-- never read by the application; it never leaves the reader's own shell."
        ),
    ),
    Exemption(
        names=frozenset({"REGISTRY_PG_BINDIR"}),
        reason=(
            "Local dev-stack override read directly by `scripts/devstack/pg_provider.py` to "
            "locate a Postgres install for `make dev-up`; devstack tooling only, never read "
            "by a deployed process, so it has no Settings field."
        ),
    ),
)


def _allowlisted_names() -> frozenset[str]:
    out: set[str] = set()
    for exemption in ALLOWLIST:
        out |= exemption.names
    return frozenset(out)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Violation:
    """One env-var-shaped name, presented as configuration, that is neither
    a real `Settings` name nor a named exception."""

    path: str
    line: int
    name: str
    context: str


def check_file(path: Path, *, rel: str, settings_names: frozenset[str], allowlisted: frozenset[str]) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    violations: list[Violation] = []
    for mention in mentions_of(text):
        if mention.name in settings_names or mention.name in allowlisted:
            continue
        violations.append(Violation(rel, mention.line, mention.name, mention.context))
    return violations


def resolve_targets(scope: list[str], *, repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for entry in scope:
        target = (repo_root / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".md":
                out.append(target)
            continue
        for path in sorted(target.rglob("*.md")):
            if path.is_file() and not any(part in _EXCLUDE_DIRS for part in path.parts):
                out.append(path)
    return out


def _all_mentioned_names(targets: list[Path]) -> frozenset[str]:
    names: set[str] = set()
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        names.update(m.name for m in mentions_of(text))
    return frozenset(names)


def _stale_allowlist_entries(mentioned: frozenset[str], settings_names: frozenset[str]) -> list[str]:
    """A name nobody mentions in the scanned docs anymore, or one that has
    since become a real Settings name, is a permission nobody needs -- same
    principle as every sibling gate's stale-allowlist check."""
    stale: list[str] = []
    for exemption in ALLOWLIST:
        for name in sorted(exemption.names):
            if name in settings_names:
                stale.append(f"{name}: now a real Settings name -- drop from ALLOWLIST")
            elif name not in mentioned:
                stale.append(f"{name}: no longer mentioned anywhere in scope -- drop from ALLOWLIST")
    return stale


def _print_explain() -> int:
    print("doc-env-mentions gate: what it checks and how to clear a hit.\n")
    print("A name counts as an environment-variable mention only in one of four contexts:")
    print("  1. export NAME=value (not export NAME=$(...) or export NAME=$OTHER)")
    print("  2. docker ... -e NAME=value")
    print("  3. A fenced code block that is entirely NAME=value lines (a '.env' snippet)")
    print("  4. Prose that calls a backticked ALL-CAPS name an 'environment variable' / 'env var'")
    print("\nEvery matched name must be a real Settings field's name/alias, or listed in")
    print("ALLOWLIST with a reason it will never be one.\n")
    print("To clear a hit:")
    print("  1. If the doc names a real setting, check the name against `Settings` --")
    print("     a mismatch is usually a typo or a renamed field the doc did not follow.")
    print("  2. If it is genuinely not a Settings field (a dynamic credential-ref example,")
    print("     dev-bootstrap output, devstack tooling, a shell-local placeholder), add an")
    print("     Exemption to ALLOWLIST in scripts/check_doc_env_mentions.py naming why.")
    print(f"\nCurrently exempted ({len(ALLOWLIST)} entries, {len(_allowlisted_names())} names):")
    for e in ALLOWLIST:
        print(f"  {sorted(e.names)}")
        print(f"    {e.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every environment-variable mention in README.md and docs/**/*.md is real.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: README.md and docs/).",
    )
    parser.add_argument("--explain", action="store_true", help="Print the rule, the contexts, and the allowlist.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    missing = [entry for entry in args.paths if not (_REPO_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist under {_REPO_ROOT}: {', '.join(missing)}\n"
            "Nothing was checked, so this is a failure rather than a pass.",
            file=sys.stderr,
        )
        return 1

    targets = resolve_targets(args.paths, repo_root=_REPO_ROOT)
    if not targets:
        print("no .md files in scope: " + ", ".join(args.paths), file=sys.stderr)
        return 0

    settings_names = settings_env_names()
    allowlisted = _allowlisted_names()

    violations: list[Violation] = []
    for path in targets:
        rel = str(path.relative_to(_REPO_ROOT))
        violations.extend(check_file(path, rel=rel, settings_names=settings_names, allowlisted=allowlisted))

    stale = _stale_allowlist_entries(_all_mentioned_names(targets), settings_names)

    if not violations and not stale:
        print(
            f"doc-env-mentions gate: {len(targets)} file(s) scanned, "
            f"{len(settings_names)} Settings name(s), {len(ALLOWLIST)} exemption(s) held"
        )
        return 0

    for v in violations:
        print(f"{v.path}:{v.line}: `{v.name}` ({v.context}) is not a Settings name or a named exception")
    for s in stale:
        print(f"stale-allowlist-entry: {s}")

    if violations:
        print(
            f"\n{len(violations)} undocumented-as-configuration mention(s) found. "
            "Run with --explain for fix guidance.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_doc_env_mentions.py -- "
            "an exemption nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
