#!/usr/bin/env python3
"""Manifest-driven Task-to-Intent nomenclature refactor engine.

The Intent cutover spans identifiers, exact wire literals, paths, and prose. A
global Task-to-Intent replacement would corrupt four vocabularies that
legitimately keep the word Task: runtime/build mechanics, already-applied
migrations, signed V1 evidence, and generated output. So this engine infers
nothing. Every transformation it will ever make is enumerated in
`refactor_intent_nomenclature.rules.json` with an exact pre-count, an exact
post-count, and a SHA-256 of the preimage and postimage of every file it
touches. A rule matching a different number of sites than declared is a stale
manifest, and the run fails before any write.

Four mutually exclusive modes: `inventory` reports candidates and emits a
candidate manifest on stdout, touching nothing; `dry-run` validates manifest and
preimages and stages the plan in memory; `apply` runs that preflight then writes
atomically; `check` is the CI mode and requires postimages, no pending rewrite,
and no unclassified residue.

Three decisions that are not obvious. Discovery is `git ls-files`, so build
residue is outside the inventory by construction. The root guard authenticates
the tree -- marker file, git toplevel identity, tracked inventory, realpath
identity -- rather than judging it by directory name; safety against writing to
the wrong tree is the preimage-digest gate, not a name heuristic. A partially
applied selection is refused rather than resumed, because counts are per-rule
and none stays exact once some files are at their postimage; a fully applied
selection is still a no-op, which makes a second `apply` idempotent.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import os
import re
import subprocess  # noqa: S404 - refactor tooling; the only argv built here is a fixed `git` read command
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

MODES: Final[tuple[str, ...]] = ("inventory", "dry-run", "apply", "check")
GROUPS: Final[tuple[str, ...]] = ("intent-memory", "arc-intent", "active-docs")
SCHEMA_VERSION: Final[int] = 1
MARKER_PATH: Final[str] = "pyproject.toml"
MARKER_TOKEN: Final[str] = 'name = "contextplane"'

#: Never source, even when something untracked left a copy of the tree inside
#: one. Tracked-file discovery excludes them too; this is the second guard.
NON_SOURCE_DIRS: Final[frozenset[str]] = frozenset(
    {".git", ".venv", ".context", ".worktrees", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build"}
)

#: Flags a rule may declare. IGNORECASE is absent deliberately: a rule that
#: cannot state which casing it produces cannot carry an exact post-count.
ALLOWED_FLAGS: Final[Mapping[str, int]] = {"MULTILINE": re.MULTILINE, "DOTALL": re.DOTALL}

#: Probes for `inventory` and `check`'s residue scan -- they find candidates,
#: the manifest decides what happens to them. Each alternative is an exact
#: domain token, never a bare word, longest first within a family so
#: `task_checkpoints` is never reported as `task_checkpoint` plus a stray `s`.
DOMAIN_PROBES: Final[tuple[tuple[str, str], ...]] = (
    (
        "camel-domain",
        r"\b(?:TaskCheckpointV1|TaskCheckpointService|TaskCheckpoint|TaskParticipantGrantV1"
        r"|TaskParticipantGrant|TaskGrantService|TaskHead|TaskKind|TaskManifest)\b",
    ),
    (
        "snake-domain",
        r"\b(?:ambiguous_task_ids|lower_scope_task_kind|parse_task_kind|task_participant_grants"
        r"|task_checkpoint_append|task_checkpoints|task_checkpoint|task_summary_template|task_summary"
        r"|task_grants|task_heads|task_memory|task_kinds?|task_ids?"
        r"|(?:list|grant|revoke)_task_participa\w+|(?:append|get)_task_checkpoint(?:_by_digest)?)\b",
    ),
    ("wire-literal", r"/v1/tasks/\{task_id\}|\btask\.(?:checkpoint|head)\.\w+|\bcontextplane_task_\w+"),
    ("wire-authority-scope", r"\bAuthorityScope\.TASK\b"),
    # Each alternative carries the enumeration context that makes a quoted
    # `"task"` the authority-scope value rather than an unrelated payload key.
    ("authority-scope-value", r'TASK = "task"|"capability", "task"|"capability": 2, "task": 1'),
)

PY_SUFFIXES: Final[frozenset[str]] = frozenset({".py"})
JSON_SUFFIXES: Final[frozenset[str]] = frozenset({".json"})

#: Bare tokens a rule must never match on its own: "an asyncio task was
#: cancelled" and "the agent's Task checkpoint" are the same bare word and
#: opposite classifications, so no bare-word rule can be correct.
BARE_WORDS: Final[tuple[str, ...]] = ("task", "tasks", "Task", "Tasks")


class EngineError(RuntimeError):
    """A refusal. Every message names the offending path, rule, or digest."""


def _obj(value: object, ctx: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise EngineError(f"{ctx}: expected an object")
    return {str(key): item for key, item in value.items()}


def _str(obj: Mapping[str, object], key: str, ctx: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise EngineError(f"{ctx}: {key} must be a non-empty string")
    return value


def _int(obj: Mapping[str, object], key: str, ctx: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise EngineError(f"{ctx}: {key} must be an integer")
    return value


def _strs(obj: Mapping[str, object], key: str, ctx: str) -> tuple[str, ...]:
    value = obj.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EngineError(f"{ctx}: {key} must be a list of strings")
    return tuple(str(item) for item in value)


def _objs(obj: Mapping[str, object], key: str, ctx: str) -> tuple[Mapping[str, object], ...]:
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise EngineError(f"{ctx}: {key} must be a list")
    return tuple(_obj(item, f"{ctx}.{key}[{index}]") for index, item in enumerate(value))


@dataclasses.dataclass(frozen=True)
class Group:
    id: str
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    group: str
    syntax: str
    pattern: str
    replacement: str
    flags: tuple[str, ...]
    rationale: str
    expected_pre_count: int
    expected_post_count: int

    def compiled(self) -> re.Pattern[str]:
        bits = 0
        for flag in self.flags:
            if flag not in ALLOWED_FLAGS:
                raise EngineError(f"rule {self.id}: flag {flag} is not permitted")
            bits |= ALLOWED_FLAGS[flag]
        try:
            return re.compile(self.pattern, bits)
        except re.error as exc:
            raise EngineError(f"rule {self.id}: invalid pattern: {exc}") from exc


@dataclasses.dataclass(frozen=True)
class FileRecord:
    path: str
    preimage_sha256: str
    postimage_sha256: str


@dataclasses.dataclass(frozen=True)
class PathRename:
    id: str
    group: str
    source: str
    destination: str


@dataclasses.dataclass(frozen=True)
class GeneratedPath:
    path: str
    regenerate_command: str


@dataclasses.dataclass(frozen=True)
class Survivor:
    id: str
    category: str
    include_paths: tuple[str, ...]
    token_pattern: str
    reason: str
    immutability_basis: str
    verification: str


@dataclasses.dataclass(frozen=True)
class Manifest:
    product_commit: str
    tracked_file_count: int
    candidate_file_count: int
    groups: tuple[Group, ...]
    rules: tuple[Rule, ...]
    files: tuple[FileRecord, ...]
    path_renames: tuple[PathRename, ...]
    generated_paths: tuple[GeneratedPath, ...]
    immutable_paths: tuple[str, ...]
    survivors: tuple[Survivor, ...]

    def group(self, group_id: str) -> Group:
        for group in self.groups:
            if group.id == group_id:
                return group
        raise EngineError(f"manifest declares no group {group_id}")

    def file_record(self, path: str) -> FileRecord | None:
        return next((record for record in self.files if record.path == path), None)


def load_manifest(path: Path) -> Manifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EngineError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EngineError(f"manifest is not valid JSON: {exc}") from exc
    root = _obj(raw, "manifest")
    version = _int(root, "schema_version", "manifest")
    if version != SCHEMA_VERSION:
        raise EngineError(f"manifest schema_version {version} is unsupported (expected {SCHEMA_VERSION})")
    identity = _obj(root.get("product_root"), "manifest.product_root")
    inventory = _obj(root.get("inventory"), "manifest.inventory")
    ctx = "manifest"
    manifest = Manifest(
        product_commit=_str(identity, "commit", ctx),
        tracked_file_count=_int(identity, "tracked_file_count", ctx),
        candidate_file_count=_int(inventory, "candidate_file_count", ctx),
        groups=tuple(
            Group(_str(i, "id", ctx), _strs(i, "include_paths", ctx), _strs(i, "exclude_paths", ctx))
            for i in _objs(root, "groups", ctx)
        ),
        rules=tuple(
            Rule(
                _str(i, "id", ctx),
                _str(i, "group", ctx),
                _str(i, "syntax", ctx),
                _str(i, "pattern", ctx),
                _str(i, "replacement", ctx),
                _strs(i, "flags", ctx),
                _str(i, "rationale", ctx),
                _int(i, "expected_pre_count", ctx),
                _int(i, "expected_post_count", ctx),
            )
            for i in _objs(root, "rules", ctx)
        ),
        files=tuple(
            FileRecord(_str(i, "path", ctx), _str(i, "preimage_sha256", ctx), _str(i, "postimage_sha256", ctx))
            for i in _objs(root, "files", ctx)
        ),
        path_renames=tuple(
            PathRename(_str(i, "id", ctx), _str(i, "group", ctx), _str(i, "source", ctx), _str(i, "destination", ctx))
            for i in _objs(root, "path_renames", ctx)
        ),
        generated_paths=tuple(
            GeneratedPath(_str(i, "path", ctx), _str(i, "regenerate_command", ctx))
            for i in _objs(root, "generated_paths", ctx)
        ),
        immutable_paths=_strs(root, "immutable_paths", ctx),
        survivors=tuple(
            Survivor(
                _str(i, "id", ctx),
                _str(i, "category", ctx),
                _strs(i, "include_paths", ctx),
                _str(i, "token_pattern", ctx),
                _str(i, "reason", ctx),
                _str(i, "immutability_basis", ctx),
                _str(i, "verification", ctx),
            )
            for i in _objs(root, "survivors", ctx)
        ),
    )
    validate_manifest(manifest)
    return manifest


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    pure = Path(path)
    for glob in globs:
        if path == glob or pure.match(glob) or (glob.endswith("*") and path.startswith(glob.rstrip("*"))):
            return True
    return False


def validate_manifest(manifest: Manifest) -> None:
    """Reject a manifest that cannot be applied deterministically.

    Whole-manifest properties only; digest and count checks belong to the plan.
    """
    group_ids = [group.id for group in manifest.groups]
    if len(set(group_ids)) != len(group_ids):
        raise EngineError("duplicate group id")
    for group_id in group_ids:
        if group_id not in GROUPS:
            raise EngineError(f"unknown group {group_id}; the closed set is {', '.join(GROUPS)}")

    seen: set[str] = set()
    by_scope: dict[tuple[str, str, str], str] = {}
    by_pattern: dict[tuple[str, str], str] = {}
    destinations: dict[tuple[str, str], str] = {}
    for rule in manifest.rules:
        if rule.id in seen:
            raise EngineError(f"duplicate rule id {rule.id}")
        seen.add(rule.id)
        if rule.group not in set(group_ids):
            raise EngineError(f"rule {rule.id}: unknown group {rule.group}")
        _reject_bare_word(rule)
        if rule.expected_pre_count < 0 or rule.expected_post_count < 0:
            raise EngineError(f"rule {rule.id}: counts must be non-negative")
        if rule.pattern == rule.replacement:
            raise EngineError(f"rule {rule.id}: source equals destination, so it can never converge")
        scope = (rule.group, rule.syntax, rule.pattern)
        if scope in by_scope:
            raise EngineError(f"rule {rule.id}: duplicates the source pattern of {by_scope[scope]} in one scope")
        by_scope[scope] = rule.id
        reach = (rule.syntax, rule.pattern)
        prior = by_pattern.get(reach)
        # Two groups may carry the same pattern only while no file is in both:
        # the hazard is one file rewritten twice, not a repeated pattern.
        if prior is not None and prior != rule.group and _groups_share_a_file(manifest, prior, rule.group):
            raise EngineError(f"rule {rule.id}: pattern {rule.pattern!r} reaches one file through two groups")
        by_pattern.setdefault(reach, rule.group)
        dest = (rule.group, rule.pattern)
        if dest in destinations and destinations[dest] != rule.replacement:
            raise EngineError(f"rule {rule.id}: pattern {rule.pattern!r} has two destinations")
        destinations[dest] = rule.replacement

    for rename in manifest.path_renames:
        if rename.id in seen:
            raise EngineError(f"duplicate rule id {rename.id}")
        seen.add(rename.id)
    _reject_rename_conflicts(manifest)
    _reject_protected_targets(manifest)
    paths = [record.path for record in manifest.files]
    if len(set(paths)) != len(paths):
        raise EngineError("duplicate file record")


def _groups_share_a_file(manifest: Manifest, first: str, second: str) -> bool:
    return bool(set(manifest.group(first).include_paths) & set(manifest.group(second).include_paths))


def _reject_bare_word(rule: Rule) -> None:
    """Refuse a rule that admits the bare word task/tasks.

    Decided by compiling the rule and asking whether the bare token standing
    alone fully matches -- stronger than inspecting pattern text, because it
    catches `\\btask\\b`, `[Tt]ask`, and an alternation branch alike.
    """
    if "IGNORECASE" in rule.flags or rule.pattern.startswith("(?i)"):
        raise EngineError(f"rule {rule.id}: case-insensitive matching cannot carry an exact post-count")
    compiled = rule.compiled()
    for probe in BARE_WORDS:
        if compiled.fullmatch(probe) is not None:
            raise EngineError(f"rule {rule.id}: pattern matches the bare word {probe!r}, so it cannot be correct")


def _reject_rename_conflicts(manifest: Manifest) -> None:
    destinations: dict[str, str] = {}
    folded: dict[str, str] = {}
    sources = {rename.source for rename in manifest.path_renames}
    for rename in manifest.path_renames:
        if rename.source == rename.destination:
            raise EngineError(f"rename {rename.id}: source equals destination")
        if rename.destination in destinations:
            claimant = destinations[rename.destination]
            raise EngineError(f"rename {rename.id}: destination {rename.destination} is claimed by {claimant}")
        destinations[rename.destination] = rename.id
        key = rename.destination.casefold()
        if key in folded:
            raise EngineError(f"rename {rename.id}: destination case-folds onto {folded[key]}, one file to the OS")
        folded[key] = rename.id
        for source in sources:
            if rename.destination.startswith(source.rstrip("/") + "/"):
                raise EngineError(f"rename {rename.id}: destination is inside renamed source {source} (cycle)")


def _reject_protected_targets(manifest: Manifest) -> None:
    protected = {generated.path for generated in manifest.generated_paths} | set(manifest.immutable_paths)
    for record in manifest.files:
        if _matches_any(record.path, protected):
            raise EngineError(f"manifest file {record.path} is generated or immutable; it is never rewritten by rule")
    for rename in manifest.path_renames:
        if _matches_any(rename.source, protected) or _matches_any(rename.destination, protected):
            raise EngineError(f"rename {rename.id} targets a generated or immutable path")


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed read-only argv built here; no caller input
            ["git", "-C", str(root), *args],  # noqa: S607 - `git` resolves from PATH, as every other gate here does
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EngineError(f"git {' '.join(args)} failed in {root}: {exc}") from exc
    return completed.stdout.strip()


def resolve_root(candidate: Path) -> Path:
    """Authenticate `candidate` as the product checkout, or refuse it."""
    if not candidate.exists():
        raise EngineError(f"product root does not exist: {candidate}")
    real = candidate.resolve()
    if real != candidate.absolute():
        raise EngineError(f"product root {candidate} resolves to {real}; pass the real path that was digested")
    if any(part == ".context" for part in real.parts):
        raise EngineError(f"{real} is inside the planning workspace; this engine only rewrites product source")
    marker = real / MARKER_PATH
    if not marker.is_file() or MARKER_TOKEN not in marker.read_text(encoding="utf-8"):
        raise EngineError(f"{real} has no {MARKER_PATH} declaring {MARKER_TOKEN!r}; it is not the product root")
    toplevel = Path(_git(real, "rev-parse", "--show-toplevel")).resolve()
    if toplevel != real:
        raise EngineError(f"{real} is not a git toplevel (its toplevel is {toplevel})")
    if not tracked_files(real):
        raise EngineError(f"{real} has an empty tracked-file inventory")
    return real


def tracked_files(root: Path) -> tuple[str, ...]:
    listing = _git(root, "ls-files", "-z")
    paths = [entry for entry in listing.split("\0") if entry]
    return tuple(sorted(path for path in paths if not set(Path(path).parts) & NON_SOURCE_DIRS))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decode(path: str, payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EngineError(f"{path}: not valid UTF-8: {exc}") from exc


def validate_syntax(path: str, text: str, *, stage: str) -> None:
    suffix = Path(path).suffix
    if suffix in PY_SUFFIXES:
        try:
            ast.parse(text, filename=path)
        except SyntaxError as exc:
            raise EngineError(f"{path}: {stage} is not parseable Python: {exc}") from exc
    elif suffix in JSON_SUFFIXES:
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngineError(f"{path}: {stage} is not valid JSON: {exc}") from exc


def _file_in_group(manifest: Manifest, group_id: str, path: str) -> bool:
    group = manifest.group(group_id)
    if _matches_any(path, group.exclude_paths):
        return False
    return _matches_any(path, group.include_paths)


@dataclasses.dataclass(frozen=True)
class FileChange:
    path: str
    before: bytes
    after: bytes


@dataclasses.dataclass(frozen=True)
class Plan:
    changes: tuple[FileChange, ...]
    renames: tuple[PathRename, ...]
    counts: Mapping[str, int]
    already_applied: tuple[str, ...]


def build_plan(root: Path, manifest: Manifest, groups: Sequence[str]) -> Plan:
    """Validate preimages, apply every selected rule in memory, and post-check."""
    rules = tuple(rule for rule in manifest.rules if rule.group in groups)
    _refuse_generated_targets(root, manifest, rules, groups)
    counts: dict[str, int] = {rule.id: 0 for rule in rules}
    changes: list[FileChange] = []
    applied: list[str] = []
    tracked = set(tracked_files(root))

    for record in manifest.files:
        if not any(_file_in_group(manifest, group, record.path) for group in groups):
            continue
        absolute = root / record.path
        if not absolute.is_file():
            raise EngineError(f"{record.path}: declared in the manifest but missing from the working tree")
        if record.path not in tracked:
            raise EngineError(f"{record.path}: not tracked by git, so it is not source")
        before = absolute.read_bytes()
        digest = sha256_bytes(before)
        if digest == record.postimage_sha256:
            applied.append(record.path)
            continue
        if digest != record.preimage_sha256:
            raise EngineError(
                f"{record.path}: digest {digest[:12]} is neither the declared preimage "
                f"{record.preimage_sha256[:12]} nor the postimage {record.postimage_sha256[:12]}; the source "
                "moved after inventory, so the whole run stops before any write"
            )
        after = _rewrite(record.path, before, manifest, rules, counts)
        if sha256_bytes(after) != record.postimage_sha256:
            got = sha256_bytes(after)[:12]
            raise EngineError(f"{record.path}: rewriting the preimage gave {got}, not {record.postimage_sha256[:12]}")
        if after != before:
            changes.append(FileChange(path=record.path, before=before, after=after))

    if applied and changes:
        raise EngineError(
            f"{len(applied)} selected file(s) are already at their postimage and {len(changes)} are not; a "
            "partially applied selection has no exact whole-selection count, so it is refused rather than "
            "resumed. Re-run the full group on a clean tree."
        )
    if not applied:
        _verify_counts(rules, counts)
    renames = tuple(rename for rename in manifest.path_renames if rename.group in groups)
    _verify_rename_targets(root, renames)
    return Plan(changes=tuple(changes), renames=renames, counts=counts, already_applied=tuple(applied))


def _rewrite(path: str, payload: bytes, manifest: Manifest, rules: Sequence[Rule], counts: dict[str, int]) -> bytes:
    newline = "\r\n" if b"\r\n" in payload else "\n"
    text = _decode(path, payload).replace("\r\n", "\n")
    validate_syntax(path, text, stage="preimage")
    suffix = Path(path).suffix
    for rule in rules:
        if not _file_in_group(manifest, rule.group, path):
            continue
        if rule.syntax == "python" and suffix not in PY_SUFFIXES:
            continue
        if rule.syntax == "json" and suffix not in JSON_SUFFIXES:
            continue
        text, hits = rule.compiled().subn(rule.replacement, text)
        counts[rule.id] += hits
    validate_syntax(path, text, stage="postimage")
    return text.replace("\n", newline).encode("utf-8")


def _verify_counts(rules: Sequence[Rule], counts: Mapping[str, int]) -> None:
    for rule in rules:
        observed = counts[rule.id]
        if observed != rule.expected_pre_count:
            raise EngineError(
                f"rule {rule.id}: matched {observed} sites, manifest declares exactly {rule.expected_pre_count}"
            )


def _refuse_generated_targets(root: Path, manifest: Manifest, rules: Sequence[Rule], groups: Sequence[str]) -> None:
    for generated in manifest.generated_paths:
        absolute = root / generated.path
        if not absolute.is_file():
            continue
        if not any(_file_in_group(manifest, group, generated.path) for group in groups):
            continue
        text = _decode(generated.path, absolute.read_bytes())
        for rule in rules:
            if rule.compiled().search(text):
                raise EngineError(
                    f"rule {rule.id} would rewrite generated {generated.path}; regenerate with "
                    f"`{generated.regenerate_command}` instead"
                )


def _verify_rename_targets(root: Path, renames: Sequence[PathRename]) -> None:
    for rename in renames:
        source = root / rename.source
        destination = root / rename.destination
        if not source.exists() and destination.exists():
            continue
        if not source.exists():
            raise EngineError(f"rename {rename.id}: source {rename.source} does not exist")
        if destination.exists():
            raise EngineError(f"rename {rename.id}: destination {rename.destination} already exists")
        parent = destination.parent
        if parent.is_dir():
            folded = destination.name.casefold()
            for entry in parent.iterdir():
                if entry.name.casefold() == folded and entry.name != destination.name:
                    raise EngineError(f"rename {rename.id}: {rename.destination} case-folds onto existing {entry.name}")


def _write_atomic(target: Path, payload: bytes, mode: int) -> None:
    staged = target.with_name(f".{target.name}.refactor-staged")
    with open(staged, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(staged, mode)
    os.replace(staged, target)


def apply_plan(root: Path, plan: Plan) -> None:
    """Apply every staged change, restoring all of them if any one fails."""
    restored: list[tuple[Path, bytes, int]] = []
    try:
        for change in plan.changes:
            target = root / change.path
            mode = target.stat().st_mode & 0o7777
            restored.append((target, change.before, mode))
            _write_atomic(target, change.after, mode)
        for rename in sorted(plan.renames, key=lambda item: len(Path(item.source).parts), reverse=True):
            source = root / rename.source
            destination = root / rename.destination
            if not source.exists() and destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
    except Exception as exc:
        for target, payload, mode in restored:
            if target.exists():
                _write_atomic(target, payload, mode)
        raise EngineError(
            f"apply failed and every written file was restored from its validated preimage: {exc}"
        ) from exc


@dataclasses.dataclass(frozen=True)
class Candidate:
    path: str
    probe: str
    token: str
    line: int
    classification: str


def _answers_to(manifest: Manifest, groups: Sequence[str], path: str) -> bool:
    """Whether a residue check over `groups` is answerable for `path`.

    A group claims a path by including it or by having renamed a file there; a
    rename destination sits in no include list, so without that second clause
    every path this refactor moves would silently stop being checked. A path no
    group claims answers to the whole refactor, so it is reported only when
    every declared group is under check -- orphan residue stays visible to the
    release gate while a single-group check speaks only for its own group.
    """
    declared = {item.id for item in manifest.groups}
    owners = {gid for gid in declared if _file_in_group(manifest, gid, path)}
    owners |= {item.group for item in manifest.path_renames if item.destination == path}
    return bool(owners & set(groups)) if owners else set(groups).issuperset(declared)


def scan_candidates(root: Path, manifest: Manifest | None, groups: Sequence[str]) -> tuple[Candidate, ...]:
    found: list[Candidate] = []
    probes = [(name, re.compile(pattern)) for name, pattern in DOMAIN_PROBES]
    for path in tracked_files(root):
        absolute = root / path
        if not absolute.is_file() or (manifest is not None and not _answers_to(manifest, groups, path)):
            continue
        try:
            text = absolute.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for name, probe in probes:
            for match in probe.finditer(text):
                found.append(
                    Candidate(
                        path=path,
                        probe=name,
                        token=match.group(0),
                        line=text.count("\n", 0, match.start()) + 1,
                        classification=_classify(path, match.group(0), manifest),
                    )
                )
    return tuple(found)


def _survivor_hits(manifest: Manifest, path: str, token: str) -> list[str]:
    return [
        survivor.id
        for survivor in manifest.survivors
        if _matches_any(path, survivor.include_paths) and re.search(survivor.token_pattern, token)
    ]


def _classify(path: str, token: str, manifest: Manifest | None) -> str:
    if manifest is None:
        return "unclassified"
    if _matches_any(path, [generated.path for generated in manifest.generated_paths]):
        return "generated"
    if _matches_any(path, manifest.immutable_paths):
        return "historical"
    hits = _survivor_hits(manifest, path, token)
    if hits:
        return f"mechanical:{hits[0]}"
    if manifest.file_record(path) is not None:
        return "active-domain"
    return "unclassified"


def mode_inventory(root: Path, manifest: Manifest | None, groups: Sequence[str]) -> int:
    candidates = scan_candidates(root, manifest, groups)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "product_root": {"commit": _git(root, "rev-parse", "HEAD"), "tracked_file_count": len(tracked_files(root))},
        "inventory": {
            "groups_selected": list(groups),
            "candidate_file_count": len({candidate.path for candidate in candidates}),
            "candidate_token_count": len(candidates),
        },
        "candidates": [dataclasses.asdict(candidate) for candidate in candidates],
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def mode_dry_run(root: Path, manifest: Manifest, groups: Sequence[str]) -> int:
    plan = build_plan(root, manifest, groups)
    for change in plan.changes:
        print(f"rewrite {change.path} ({len(change.before)} -> {len(change.after)} bytes)")
    for rename in plan.renames:
        print(f"rename  {rename.source} -> {rename.destination}")
    for rule_id, count in sorted(plan.counts.items()):
        print(f"count   {rule_id}: {count}")
    print(f"plan: {len(plan.changes)} rewrite(s), {len(plan.renames)} rename(s), {len(plan.already_applied)} no-op(s)")
    return 0


def mode_apply(root: Path, manifest: Manifest, groups: Sequence[str]) -> int:
    plan = build_plan(root, manifest, groups)
    if not plan.changes and not plan.renames:
        print("apply: nothing to do; every declared file is already at its postimage")
        return 0
    apply_plan(root, plan)  # all-or-nothing: a later failure restores every earlier write
    print(f"apply: {len(plan.changes)} file(s) rewritten, {len(plan.renames)} path(s) renamed")
    return 0


def mode_check(root: Path, manifest: Manifest, groups: Sequence[str]) -> int:
    failures: list[str] = []
    # A renamed file's content record is keyed on its pre-rename path, so the
    # postimage has to be read at the destination once the move has happened.
    moved = {rename.source: rename.destination for rename in manifest.path_renames if rename.group in groups}
    for record in manifest.files:
        if not any(_file_in_group(manifest, group, record.path) for group in groups):
            continue
        absolute = root / record.path
        if not absolute.is_file() and record.path in moved:
            absolute = root / moved[record.path]
        if not absolute.is_file():
            failures.append(f"{record.path}: missing")
            continue
        digest = sha256_bytes(absolute.read_bytes())
        if digest != record.postimage_sha256:
            failures.append(f"{record.path}: digest {digest[:12]} is not the declared postimage")
    for rename in (item for item in manifest.path_renames if item.group in groups):
        if (root / rename.source).exists():
            failures.append(f"{rename.source}: still present; rename {rename.id} is pending")
        if not (root / rename.destination).exists():
            failures.append(f"{rename.destination}: missing; rename {rename.id} did not complete")
    for candidate in scan_candidates(root, manifest, groups):
        hits = _survivor_hits(manifest, candidate.path, candidate.token)
        if candidate.classification == "active-domain":
            failures.append(f"{candidate.path}:{candidate.line}: active-domain token {candidate.token} survived")
        elif candidate.classification == "unclassified":
            failures.append(f"{candidate.path}:{candidate.line}: {candidate.token} is unclassified residue")
        elif len(hits) > 1:
            failures.append(f"{candidate.path}:{candidate.line}: ambiguous survivors {', '.join(hits)}")
    if failures:
        for failure in failures:
            print(f"check: {failure}", file=sys.stderr)
        print(f"check: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("check: postimages present, no pending rewrites, no unclassified residue")
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Task-to-Intent nomenclature refactor engine.")
    parser.add_argument("mode", choices=MODES)
    groups_help = "select one or more rule groups; repeatable; required for every mode except inventory"
    parser.add_argument("--group", dest="groups", action="append", choices=list(GROUPS), default=None, help=groups_help)
    parser.add_argument("--root", type=Path, default=None, help="product checkout to operate on")
    parser.add_argument("--manifest", type=Path, default=None, help="rule manifest (default: beside this script)")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        root = resolve_root(args.root if args.root is not None else Path.cwd())
        manifest_path = args.manifest or Path(__file__).resolve().parent / "refactor_intent_nomenclature.rules.json"
        groups = tuple(args.groups or ())
        if args.mode != "inventory" and not groups:
            raise EngineError(f"{args.mode} requires at least one --group")
        manifest = load_manifest(manifest_path) if manifest_path.is_file() else None
        if args.mode == "inventory":
            return mode_inventory(root, manifest, groups or GROUPS)
        if manifest is None:
            raise EngineError(f"manifest not found: {manifest_path}")
        head = _git(root, "rev-parse", "HEAD")
        if manifest.product_commit != head:
            frozen = manifest.product_commit[:12]
            note = f"warning: manifest frozen at {frozen}, tree at {head[:12]}; digests remain the authority"
            print(note, file=sys.stderr)
        if args.mode == "dry-run":
            return mode_dry_run(root, manifest, groups)
        if args.mode == "apply":
            return mode_apply(root, manifest, groups)
        return mode_check(root, manifest, groups)
    except EngineError as exc:
        print(f"refactor_intent_nomenclature: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
