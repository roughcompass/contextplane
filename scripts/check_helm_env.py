#!/usr/bin/env python3
"""Lint gate: the Helm chart's env-var surface agrees with itself and with
the canonical inventory.

`scripts/check_env_documented.py` pins `.env.example` against the
configuration reference. This gate extends the same idea one deployment
target further: `deploy/helm/values.yaml` and `deploy/helm/templates/secret.yaml`
are two more places the same names have to agree, and a chart can install
cleanly while quietly dropping a value an operator believes they set (the
2026-05-12 security report's SEC-01: three secret keys documented, three
more silently unrendered, plus a rendered `API_TOKEN` no `Settings` field
ever read).

**Three independent drift classes, each with its own failure mode:**

1. **`secret_not_rendered`** -- `values.yaml`'s `secrets:` block documents a
   commented placeholder (e.g. `# pgbouncerUrl: ""`) that
   `templates/secret.yaml` never tests. An operator who follows the comment
   and passes `--set secrets.pgbouncerUrl=...` gets a value that is silently
   dropped: the Secret never carries it, the pod never sees it. This is the
   original SEC-01 shape.
2. **`secret_not_documented`** -- the reverse: `secret.yaml` renders a
   `.Values.secrets.X` key that `values.yaml`'s own comments never mention.
   The chart *can* accept the value, but nothing in the chart's own
   documentation tells an operator it exists.
3. **`dead_key`** -- a ConfigMap or Secret key the chart actually renders
   (from `values.yaml`'s `env:` map, or a literal key in `secret.yaml`) that
   corresponds to no real `Settings` field and no `.env.example` entry --
   the old `API_TOKEN` (no `Settings` field at all) and `PGBOUNCER_HOST`/
   `PGBOUNCER_PORT` (real env names, just never read) shape. `.env.example`
   is consulted in addition to `Settings` because a small number of real,
   deployable env vars -- `EMBEDDINGS_PARTITION_COUNT` is the one that
   exists today -- are read directly at migration time and have no
   `Settings` field at all (see CLAUDE.md's "Secrets and config" section).

A fourth class, **`secret_uncharted`**, is checked separately: a small,
curated set of canonical names that are secret-shaped by their own nature
(database/scheduler URLs, webhook secrets, the extraction API key, the
metrics bearer token) must be renderable through the chart *somehow* --
this is deliberately a short, explicit list rather than a mechanical
derivation, because "is this a secret" is not inferable from a `Settings`
field alone. A name legitimately not exposed through this chart (a future
credential intentionally deployment-target-specific) is excluded with a
reason in `EXCLUSIONS`, exactly like every other bypass in this codebase.

Run locally:
    python scripts/check_helm_env.py
    python scripts/check_helm_env.py --explain
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys

import yaml
from checklib import repo_root, require_nonempty, run_guard

from contextplane.config import Settings

try:
    from pydantic import AliasChoices
except ImportError:  # pragma: no cover - pydantic always ships with this app
    AliasChoices = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = repo_root()
_VALUES_YAML = _REPO_ROOT / "deploy" / "helm" / "values.yaml"
_SECRET_TEMPLATE = _REPO_ROOT / "deploy" / "helm" / "templates" / "secret.yaml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Canonical names that are secret-shaped by nature -- credentials and
# connection strings that embed them -- and must be renderable through the
# chart's Secret somehow. This is deliberately curated, not derived: nothing
# about a Settings field marks it as "a secret" mechanically.
_CANONICAL_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "PGBOUNCER_URL",
        "SCHEDULER_JOBSTORE_URL",
        "OIDC_DISCOVERY_URL",
        "GITHUB_WEBHOOK_SECRET",
        "GITLAB_WEBHOOK_SECRET",
        "CLAUDE_API_KEY",
        # The canonical spelling of the same credential, plus the extra-header
        # value. Both are secret-shaped by nature and neither is inferable from
        # a Settings field -- nothing marks a field "secret" mechanically -- so
        # a name missing here is a name this gate silently governs nothing
        # about, which is how CLAUDE_API_KEY ended up being the only extraction
        # credential the chart was ever checked for.
        "EXTRACTION_API_KEY",
        "EXTRACTION_EXTRA_HEADERS",
        # The simulation and judge credentials (E24). Listed for the same reason
        # the extraction pair is: a name missing here is a name this gate
        # silently governs nothing about, and these two are the credentials a
        # deployment sends a resolved envelope under.
        "SIMULATION_API_KEY",
        "JUDGE_API_KEY",
        "JUDGE_2_API_KEY",
        "JUDGE_3_API_KEY",
        "METRICS_BEARER_TOKEN",
    }
)


@dataclasses.dataclass(frozen=True)
class Exclusion:
    """One name excluded from one check, and why it is not, and will not
    become, a defect that check would otherwise report."""

    name: str
    check: str  # "dead_key" | "secret_uncharted"
    reason: str


#: Every currently-held exclusion. Empty today -- the chart renders every
#: canonical secret-shaped name and nothing it renders is dead -- but the
#: mechanism exists for the next real, reasoned gap rather than a loosened
#: check. A new entry needs a reason tied to why the name genuinely cannot
#: (or need not) be reconciled, not "this one is fine."
EXCLUSIONS: tuple[Exclusion, ...] = ()


def _excluded(name: str, check: str, exclusions: tuple[Exclusion, ...]) -> bool:
    return any(e.name == name and e.check == check for e in exclusions)


# ---------------------------------------------------------------------------
# Canonical inventory: Settings + .env.example
# ---------------------------------------------------------------------------


def settings_env_names() -> frozenset[str]:
    """Every env-var name `Settings` reads: the field's own upper-cased name,
    its `validation_alias`, or -- for a field aliased to more than one name
    via `AliasChoices` -- every choice, not just the first. Mechanical, not a
    maintained list, mirroring `check_doc_env_mentions.py`'s own derivation
    with one addition: that gate's `isinstance(alias, str)` check silently
    drops every name past the first for an `AliasChoices` field, which this
    gate's own `extraction_anthropic_api_key` field would otherwise fall
    through (see contextplane/config.py)."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif AliasChoices is not None and isinstance(alias, AliasChoices):
            names.update(choice for choice in alias.choices if isinstance(choice, str))
        else:
            names.add(field_name.upper())
    return frozenset(names)


_ENV_EXAMPLE_ASSIGNMENT_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)


def env_example_names(text: str) -> frozenset[str]:
    """Every name `.env.example` offers, live or commented-out -- the same
    shape `check_env_documented.py`'s `_ASSIGNMENT` matches."""
    return frozenset(_ENV_EXAMPLE_ASSIGNMENT_RE.findall(text))


# ---------------------------------------------------------------------------
# What the chart actually renders
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SecretDocEntry:
    """One `# camelCaseKey: ""` placeholder documented in values.yaml's
    `secrets:` block."""

    key: str
    line: int


_SECRETS_BLOCK_RE = re.compile(r"^secrets:\s*\{\}\s*$", re.MULTILINE)
_SECRET_DOC_LINE_RE = re.compile(r'^  #\s*([a-zA-Z][a-zA-Z0-9]*):\s*""')
_INDENTED_COMMENT_RE = re.compile(r"^  #")


def parse_values_secrets_doc(text: str) -> list[SecretDocEntry]:
    """Every commented `secrets.<key>` placeholder in values.yaml, in the
    contiguous comment block right after `secrets: {}`. A `key: ""` line adds
    an entry; a continuation line explaining that same key (indented `#` with
    no `key:` shape, e.g. the multi-line note on `pgbouncerUrl`) is skipped
    without ending the block. The block ends at the first line that is not an
    indented `#` comment at all -- the real file's blank-line terminator."""
    lines = text.splitlines()
    marker = _SECRETS_BLOCK_RE.search(text)
    if marker is None:
        return []
    start_line = text[: marker.start()].count("\n") + 1  # 1-indexed line of `secrets: {}`
    out: list[SecretDocEntry] = []
    for lineno in range(start_line + 1, len(lines) + 1):
        line = lines[lineno - 1]
        if _INDENTED_COMMENT_RE.match(line) is None:
            break
        m = _SECRET_DOC_LINE_RE.match(line)
        if m is not None:
            out.append(SecretDocEntry(key=m.group(1), line=lineno))
    return out


@dataclasses.dataclass(frozen=True)
class SecretRenderEntry:
    """One `{{- if (.Values.secrets).<key> }} ... <ENV_NAME>: ... {{- end }}`
    pair actually rendered by secret.yaml."""

    key: str
    env_name: str
    line: int  # of the ENV_NAME: line, for a diagnostic that points at the render


_SECRET_RENDER_RE = re.compile(
    r"\{\{-\s*if\s+\(\.Values\.secrets\)\.([A-Za-z][A-Za-z0-9]*)\s*\}\}\s*\n\s*([A-Z][A-Z0-9_]*):",
)


def parse_secret_template(text: str) -> list[SecretRenderEntry]:
    """Every `secrets.<key>` -> `<ENV_NAME>` pair secret.yaml actually
    renders, conditioned on that exact key (not the unconditional metrics
    line, handled separately by `metrics_token_env_name`)."""
    out: list[SecretRenderEntry] = []
    for m in _SECRET_RENDER_RE.finditer(text):
        env_line = text[: m.start(2)].count("\n") + 1
        out.append(SecretRenderEntry(key=m.group(1), env_name=m.group(2), line=env_line))
    return out


def metrics_token_env_name(values_text: str) -> str | None:
    """The env-var name the metrics bearer credential renders under, if
    secret.yaml renders it at all. Resolved from `values.yaml`'s own
    `metrics.auth.secretKey` (default `METRICS_BEARER_TOKEN`) rather than
    hard-coded, so a renamed key updates here with no edit."""
    try:
        data = yaml.safe_load(values_text) or {}
    except yaml.YAMLError:
        return None
    metrics = data.get("metrics") or {}
    auth = metrics.get("auth") or {}
    key = auth.get("secretKey")
    return key if isinstance(key, str) and key else None


def parse_configmap_env_names(values_text: str) -> dict[str, int]:
    """Every key under `values.yaml`'s `env:` map -> its line number.
    `templates/configmap.yaml` ranges over this whole map verbatim (see that
    template's own docstring), so the map's keys ARE the rendered ConfigMap
    keys -- no need to also parse the template."""
    try:
        data = yaml.safe_load(values_text) or {}
    except yaml.YAMLError:
        return {}
    env = data.get("env") or {}
    out: dict[str, int] = {}
    for name in env:
        m = re.search(rf"^  {re.escape(name)}:", values_text, re.MULTILINE)
        out[name] = (values_text[: m.start()].count("\n") + 1) if m else 0
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    detail: str
    #: The specific env-var (or secrets.<key>) name this violation is about,
    #: for stale-exclusion bookkeeping -- kept separate from `detail` so
    #: nothing has to re-parse a human-readable message to find it.
    name: str = ""


def check_secret_doc_vs_render(values_text: str, secret_text: str) -> list[Violation]:
    documented = {e.key: e.line for e in parse_values_secrets_doc(values_text)}
    rendered = {e.key: e.line for e in parse_secret_template(secret_text)}

    out: list[Violation] = []
    for key, line in documented.items():
        if key not in rendered:
            out.append(
                Violation(
                    path=str(_VALUES_YAML.relative_to(_REPO_ROOT)),
                    line=line,
                    kind="secret_not_rendered",
                    name=key,
                    detail=(
                        f"secrets.{key} is documented in values.yaml but templates/secret.yaml never "
                        f"renders it -- an operator who sets it gets the value silently dropped."
                    ),
                )
            )
    for key, line in rendered.items():
        if key not in documented:
            out.append(
                Violation(
                    path=str(_SECRET_TEMPLATE.relative_to(_REPO_ROOT)),
                    line=line,
                    kind="secret_not_documented",
                    name=key,
                    detail=(
                        f"templates/secret.yaml renders secrets.{key} but values.yaml's own comments "
                        f"never mention it -- an operator has no way to discover it exists."
                    ),
                )
            )
    return out


def check_dead_keys(values_text: str, secret_text: str, *, exclusions: tuple[Exclusion, ...]) -> list[Violation]:
    canonical = settings_env_names() | env_example_names(_ENV_EXAMPLE.read_text(encoding="utf-8"))

    out: list[Violation] = []
    for name, line in parse_configmap_env_names(values_text).items():
        if name not in canonical and not _excluded(name, "dead_key", exclusions):
            out.append(
                Violation(
                    path=str(_VALUES_YAML.relative_to(_REPO_ROOT)),
                    line=line,
                    kind="dead_key",
                    name=name,
                    detail=(
                        f"env.{name} is rendered into every pod's ConfigMap but is not a real Settings "
                        f"field or .env.example entry -- nothing reads it."
                    ),
                )
            )
    for entry in parse_secret_template(secret_text):
        if entry.env_name not in canonical and not _excluded(entry.env_name, "dead_key", exclusions):
            out.append(
                Violation(
                    path=str(_SECRET_TEMPLATE.relative_to(_REPO_ROOT)),
                    line=entry.line,
                    kind="dead_key",
                    name=entry.env_name,
                    detail=(
                        f"{entry.env_name} is rendered into the Secret but is not a real Settings field "
                        f"or .env.example entry -- nothing reads it."
                    ),
                )
            )
    metrics_name = metrics_token_env_name(values_text)
    if metrics_name and metrics_name not in canonical and not _excluded(metrics_name, "dead_key", exclusions):
        out.append(
            Violation(
                path=str(_VALUES_YAML.relative_to(_REPO_ROOT)),
                line=0,
                kind="dead_key",
                name=metrics_name,
                detail=f"metrics.auth.secretKey names {metrics_name!r}, which is not a real Settings field.",
            )
        )
    return out


def check_canonical_secrets_rendered(
    values_text: str, secret_text: str, *, exclusions: tuple[Exclusion, ...]
) -> list[Violation]:
    rendered_names = {e.env_name for e in parse_secret_template(secret_text)}
    metrics_name = metrics_token_env_name(values_text)
    if metrics_name:
        rendered_names.add(metrics_name)

    out: list[Violation] = []
    for name in sorted(_CANONICAL_SECRET_NAMES):
        if name not in rendered_names and not _excluded(name, "secret_uncharted", exclusions):
            out.append(
                Violation(
                    path=str(_SECRET_TEMPLATE.relative_to(_REPO_ROOT)),
                    line=0,
                    kind="secret_uncharted",
                    name=name,
                    detail=(
                        f"{name} is a canonical secret-shaped name (see _CANONICAL_SECRET_NAMES) that "
                        f"no path in templates/secret.yaml renders. Add it, or add an Exclusion in "
                        f"scripts/check_helm_env.py naming why this chart does not carry it."
                    ),
                )
            )
    return out


def _stale_exclusions(values_text: str, secret_text: str, exclusions: tuple[Exclusion, ...]) -> list[str]:
    """An Exclusion naming a name that no longer triggers the violation it
    was written to excuse is a permission nobody needs -- same principle as
    every sibling gate's stale-allowlist check. Detected by re-running the two
    exclusion-aware checks with no exclusions at all: whatever they report
    without help is the current, real set each exclusion is entitled to cover."""
    raw_dead = {v.name for v in check_dead_keys(values_text, secret_text, exclusions=())}
    raw_uncharted = {v.name for v in check_canonical_secrets_rendered(values_text, secret_text, exclusions=())}

    stale: list[str] = []
    for exclusion in exclusions:
        if exclusion.check == "dead_key" and exclusion.name not in raw_dead:
            stale.append(f"{exclusion.name} ({exclusion.check}): no longer a dead key -- drop from EXCLUSIONS")
        elif exclusion.check == "secret_uncharted" and exclusion.name not in raw_uncharted:
            stale.append(f"{exclusion.name} ({exclusion.check}): now rendered by the chart -- drop from EXCLUSIONS")
    return stale


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print(__doc__)
    print(f"Canonical secret-shaped names tracked ({len(_CANONICAL_SECRET_NAMES)}):")
    for name in sorted(_CANONICAL_SECRET_NAMES):
        print(f"  {name}")
    print(f"\nCurrently held exclusions ({len(EXCLUSIONS)}):")
    for e in EXCLUSIONS:
        print(f"  {e.name} ({e.check}): {e.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Helm chart's env-var surface is internally consistent.")
    parser.add_argument("--explain", action="store_true", help="Describe what the gate checks and why, then exit 0.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    for path in (_VALUES_YAML, _SECRET_TEMPLATE, _ENV_EXAMPLE):
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 1

    # The canonical secret names are what every check below is stated against.
    # An empty inventory makes all three checks trivially true.
    require_nonempty(_CANONICAL_SECRET_NAMES, "the canonical secret-name inventory")

    values_text = _VALUES_YAML.read_text(encoding="utf-8")
    secret_text = _SECRET_TEMPLATE.read_text(encoding="utf-8")

    violations: list[Violation] = []
    violations.extend(check_secret_doc_vs_render(values_text, secret_text))
    violations.extend(check_dead_keys(values_text, secret_text, exclusions=EXCLUSIONS))
    violations.extend(check_canonical_secrets_rendered(values_text, secret_text, exclusions=EXCLUSIONS))
    stale = _stale_exclusions(values_text, secret_text, EXCLUSIONS)

    if not violations and not stale:
        print(
            f"helm-env gate: {len(_CANONICAL_SECRET_NAMES)} canonical secret name(s) tracked, "
            f"{len(EXCLUSIONS)} exclusion(s) held"
        )
        return 0

    for v in violations:
        line = f":{v.line}" if v.line else ""
        print(f"{v.path}{line}: [{v.kind}] {v.detail}")
    for s in stale:
        print(f"stale-exclusion: {s}")

    if violations:
        print(
            f"\n{len(violations)} helm-env violation(s) found. Run with --explain for the rule.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from EXCLUSIONS in scripts/check_helm_env.py -- "
            "an exclusion nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(run_guard(main))
