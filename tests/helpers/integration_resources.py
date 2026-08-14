"""Classify integration nodes by the host resources they actually contend for.

Running the integration tier across parallel workers is only sound if the
things two workers cannot share are known and named. This module reads that
declaration from `tests/integration_resources.toml` and answers one question
per node, with exactly three possible answers:

``ORDINARY``
    Nothing host-wide is contended. Place it anywhere.
``CO_LOCATION_GROUP``
    Must land on the same worker as the rest of its group, because the group
    shares one expensive setup.
``EXTERNAL_EXCLUSIVE``
    Drives something outside this process that cannot be duplicated, and is
    gated on a capability being present.

**There is no fourth "serial" class, and its absence is the point.** A
suite-wide serial shard preserves exactly the serial fraction that makes the
tier slow, so it would have to earn its place — and the candidates do not.
Prometheus registries, router reloads, scheduler objects, module globals, and
environment mutation are all *process*-global: isolated workers isolate them
for free, and labelling them serial would cost the entire parallel gain to
solve a problem parallelism already solved. Independent migration nodes are
likewise ungrouped; each builds its own head clone and shares nothing with the
others, so grouping them would serialize nine nodes that never collide.

The guard here is the other half. A declaration is only trustworthy if
undeclared contention fails loudly, so `scan_tree` walks the integrated tree
for fixed bind/listen ports and shared mutable server paths, and `guard`
rejects any it finds that the manifest does not name. Dynamic ports and
run-specific paths are not violations and need no entry — they *remove* a
collision rather than scheduling around it, which is always the better answer.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = _REPO_ROOT / "tests" / "integration_resources.toml"

# The tree the guard walks. Scoped to the test tier on purpose: the dev stack's
# own published ports (`scripts/devstack/config.py`) are a developer's running
# services, not a resource the test tier contends for, and pulling them in would
# make the guard fail on a file no test touches.
_GUARDED_ROOTS = ("tests",)

# The guard does not scan itself or its own tests, and the reason is structural
# rather than convenient: this module *defines* what a fixed port and a server
# data directory look like, and its test module has to contain sample violations
# to prove the guard catches them. Scanning either reports that vocabulary as
# findings — a permanent, unfixable failure whose only resolution would be to
# stop writing tests for the guard.
#
# What makes this safe is that neither file binds a socket or initialises a
# cluster; they only parse and read. A scanner that ever gains that ability has
# to come back off this list.
_SELF_EXCLUDED = (
    "tests/helpers/integration_resources.py",
    "tests/unit/test_integration_resources.py",
)

# Directory-name fragments that mean "a PostgreSQL data directory". A shared one
# is a shared mutable server: two clusters pointed at the same pgdata corrupt
# each other, and the second `pg_ctl start` silently inherits the first's
# settings rather than erroring.
_SERVER_DATA_MARKERS = ("pgdata",)

# A path is run-specific — and therefore not shared — if it carries any of
# these. `for_run` derives `run-<id>`; a temp-dir base is per-user and per-run.
_RUN_SPECIFIC_MARKERS = ("run-", "gettempdir", "mkdtemp", "tmp_path", "TMPDIR")

# Ports below this are privileged and cannot be bound by a test anyway; 0 means
# "ask the OS", which is the dynamic case the guard is trying to encourage.
_LOWEST_GUARDED_PORT = 1024
_HIGHEST_GUARDED_PORT = 65535

_VALID_RESOURCE_KINDS = ("fixed_port", "shared_server_path")


class ResourceError(RuntimeError):
    """The resource declaration or the tree violated the contract."""


class ManifestError(ResourceError):
    """The manifest itself is malformed, duplicated, conflicting, or stale."""


class Outcome(Enum):
    """The only three classes a node can have."""

    ORDINARY = "ordinary"
    CO_LOCATION_GROUP = "co-location-group"
    EXTERNAL_EXCLUSIVE = "external-exclusive"


@dataclass(frozen=True)
class CoLocationGroup:
    """Nodes that must share a worker because they share a setup."""

    name: str
    reason: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class ExternalExclusive:
    """A node driving an external thing that cannot be duplicated."""

    marker: str
    capability: str
    reason: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class DeclaredResource:
    """A fixed port or shared server path the manifest acknowledges."""

    kind: str
    value: str
    location: str
    reason: str


@dataclass(frozen=True)
class Classification:
    """One node's outcome, and the group it belongs to when it has one."""

    node: str
    outcome: Outcome
    group: str | None = None
    capability: str | None = None

    def as_evidence(self) -> dict[str, str | None]:
        return {
            "node": self.node,
            "outcome": self.outcome.value,
            "group": self.group,
            "capability": self.capability,
        }


@dataclass(frozen=True)
class Finding:
    """A host resource the tree actually uses, as found rather than declared."""

    kind: str
    value: str
    location: str
    line: int

    def describe(self) -> str:
        return f"{self.location}:{self.line}: {self.kind} {self.value}"


@dataclass(frozen=True)
class Manifest:
    """The parsed, validated resource declaration."""

    groups: tuple[CoLocationGroup, ...] = ()
    external_exclusive: tuple[ExternalExclusive, ...] = ()
    resources: tuple[DeclaredResource, ...] = ()
    version: int = 1

    # -- parsing ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None, *, root: Path | None = None) -> Manifest:
        """Parse and validate the manifest.

        Validation is not optional politeness: a duplicated member would let
        two groups claim one node, a conflicting one would make a node both
        grouped and exclusive, and a stale path would silently declare nothing
        at all — each of which produces a schedule that looks declared and
        is not.
        """
        target = path if path is not None else MANIFEST_PATH
        base = root if root is not None else _REPO_ROOT
        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ManifestError(f"{target} does not exist") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"{target} is not valid TOML: {exc}") from exc

        manifest = cls(
            version=int(raw.get("meta", {}).get("version", 1)),
            groups=tuple(_parse_groups(raw.get("groups", []), target)),
            external_exclusive=tuple(_parse_external(raw.get("external_exclusive", []), target)),
            resources=tuple(_parse_resources(raw.get("host_resources", []), target)),
        )
        manifest.validate(root=base)
        return manifest

    def validate(self, *, root: Path | None = None) -> None:
        base = root if root is not None else _REPO_ROOT

        seen_groups: set[str] = set()
        for group in self.groups:
            if group.name in seen_groups:
                raise ManifestError(f"co-location group {group.name!r} is declared more than once")
            seen_groups.add(group.name)
            if not group.members:
                raise ManifestError(f"co-location group {group.name!r} declares no members")

        # One node, one class. A member claimed twice is ambiguous rather than
        # redundant: the balancer would have to pick, and either pick is wrong.
        owner: dict[str, str] = {}
        for group in self.groups:
            for member in group.members:
                if member in owner:
                    raise ManifestError(
                        f"{member} is declared by both {owner[member]} and co-location group {group.name!r}"
                    )
                owner[member] = f"co-location group {group.name!r}"
        for external in self.external_exclusive:
            for member in external.members:
                if member in owner:
                    raise ManifestError(
                        f"{member} is declared by both {owner[member]} and external-exclusive "
                        f"marker {external.marker!r}"
                    )
                owner[member] = f"external-exclusive marker {external.marker!r}"

        # A path that no longer exists declares nothing. This is the failure
        # mode of a manifest that outlived a rename: it still parses, still
        # looks authoritative, and classifies the renamed node as ordinary.
        for member, declared_by in sorted(owner.items()):
            if not (base / member).exists():
                raise ManifestError(f"{declared_by} names {member}, which does not exist (stale declaration)")

        for external in self.external_exclusive:
            if not external.capability:
                raise ManifestError(
                    f"external-exclusive marker {external.marker!r} declares no capability; "
                    "an exclusive node that is not capability-gated cannot be skipped when its "
                    "external dependency is absent"
                )

        seen_resources: set[tuple[str, str]] = set()
        for resource in self.resources:
            if resource.kind not in _VALID_RESOURCE_KINDS:
                raise ManifestError(
                    f"host resource kind {resource.kind!r} is not one of {', '.join(_VALID_RESOURCE_KINDS)}"
                )
            key = (resource.kind, resource.value)
            if key in seen_resources:
                raise ManifestError(f"host resource {resource.kind} {resource.value} is declared more than once")
            seen_resources.add(key)
            if not resource.reason:
                raise ManifestError(
                    f"host resource {resource.kind} {resource.value} declares no reason; "
                    "an undocumented fixed resource is indistinguishable from an oversight"
                )

    # -- classification ---------------------------------------------------

    def classify(self, node: str) -> Classification:
        """The outcome for *node*, defaulting to ordinary.

        Ordinary is the default rather than an explicit declaration because the
        overwhelming majority of the tier is ordinary, and a manifest that had
        to list every ordinary node would be a curated coverage list — which
        goes stale silently and is exactly what dynamic collection replaces.
        """
        module = _module_of(node)
        for group in self.groups:
            if module in group.members:
                return Classification(node=node, outcome=Outcome.CO_LOCATION_GROUP, group=group.name)
        for external in self.external_exclusive:
            if module in external.members:
                return Classification(
                    node=node,
                    outcome=Outcome.EXTERNAL_EXCLUSIVE,
                    group=external.marker,
                    capability=external.capability,
                )
        return Classification(node=node, outcome=Outcome.ORDINARY)

    def group_of(self, node: str) -> str | None:
        classification = self.classify(node)
        return classification.group if classification.outcome is Outcome.CO_LOCATION_GROUP else None

    @property
    def declared_ports(self) -> frozenset[str]:
        return frozenset(r.value for r in self.resources if r.kind == "fixed_port")

    @property
    def declared_server_paths(self) -> frozenset[str]:
        return frozenset(r.value for r in self.resources if r.kind == "shared_server_path")

    def as_evidence(self) -> dict[str, object]:
        return {
            "version": self.version,
            "groups": {g.name: list(g.members) for g in self.groups},
            "external_exclusive": {
                e.marker: {"capability": e.capability, "members": list(e.members)} for e in self.external_exclusive
            },
            "declared_ports": sorted(self.declared_ports),
            "declared_server_paths": sorted(self.declared_server_paths),
        }


def _parse_groups(entries: object, source: Path) -> Iterator[CoLocationGroup]:
    for entry in _as_tables(entries, source, "groups"):
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ManifestError(f"{source}: a co-location group has no name")
        yield CoLocationGroup(
            name=name,
            reason=str(entry.get("reason", "")),
            members=tuple(str(m) for m in entry.get("members", [])),
        )


def _parse_external(entries: object, source: Path) -> Iterator[ExternalExclusive]:
    for entry in _as_tables(entries, source, "external_exclusive"):
        marker = str(entry.get("marker", "")).strip()
        if not marker:
            raise ManifestError(f"{source}: an external-exclusive entry has no marker")
        yield ExternalExclusive(
            marker=marker,
            capability=str(entry.get("capability", "")).strip(),
            reason=str(entry.get("reason", "")),
            members=tuple(str(m) for m in entry.get("members", [])),
        )


def _parse_resources(entries: object, source: Path) -> Iterator[DeclaredResource]:
    for entry in _as_tables(entries, source, "host_resources"):
        yield DeclaredResource(
            kind=str(entry.get("kind", "")).strip(),
            value=str(entry.get("value", "")).strip(),
            location=str(entry.get("location", "")).strip(),
            reason=str(entry.get("reason", "")).strip(),
        )


def _as_tables(entries: object, source: Path, key: str) -> list[dict[str, object]]:
    if not isinstance(entries, list):
        raise ManifestError(f"{source}: [{key}] must be an array of tables")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestError(f"{source}: every [{key}] entry must be a table, got {type(entry).__name__}")
    return list(entries)


def _module_of(node: str) -> str:
    """The module path from a pytest node ID.

    Node IDs carry `::test_name[param]`; classification is per module because a
    co-location group's shared setup is module-scoped.
    """
    return node.split("::", 1)[0]


# -- tree scanning --------------------------------------------------------


class _ResourceVisitor(ast.NodeVisitor):
    """Find fixed ports and shared server paths in one module.

    AST rather than a text search, for one specific reason: a regex for
    ``\\.bind\\(`` matches eight domain calls in
    `test_receipt_reference_queries.py` where a receipt is bound to references.
    A guard that cried wolf on those would be turned off within a day.
    """

    def __init__(self, location: str) -> None:
        self.location = location
        self.findings: list[Finding] = []
        self._statement: ast.stmt | None = None

    def visit(self, node: ast.AST) -> None:
        """Track the enclosing statement while descending.

        Run-specificity is a property of the whole path expression, not of one
        literal in it: `base / "run-abc" / "pgdata"` and
        `Path(gettempdir()) / "pgdata"` are both run-scoped, and in both the
        evidence sits in a *sibling* node of the literal that names the data
        directory. Judging `"pgdata"` alone would flag both.
        """
        if isinstance(node, ast.stmt):
            previous = self._statement
            self._statement = node
            try:
                super().visit(node)
            finally:
                self._statement = previous
            return
        super().visit(node)

    def _statement_is_run_specific(self) -> bool:
        if self._statement is None:
            return False
        for inner in ast.walk(self._statement):
            if isinstance(inner, ast.stmt) and inner is not self._statement:
                # Do not borrow a nested statement's run-scoping.
                continue
            text: str | None = None
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                text = inner.value
            elif isinstance(inner, ast.Attribute):
                text = inner.attr
            elif isinstance(inner, ast.Name):
                text = inner.id
            if text and any(marker in text for marker in _RUN_SPECIFIC_MARKERS):
                return True
        return False

    # `port=5545`, but not `port=0`
    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "port":
                self._maybe_port(keyword.value, node.lineno)
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "bind":
                self._maybe_socket_bind(node)
            elif func.attr == "get" and _is_environ_get(func):
                self._maybe_environ_port_default(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # A module constant whose name says port and whose value is a literal.
        for target in node.targets:
            if isinstance(target, ast.Name) and "PORT" in target.id.upper():
                self._maybe_port(node.value, node.lineno)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._maybe_server_path(node.value, node.lineno)
        self.generic_visit(node)

    # -- helpers ----------------------------------------------------------

    def _maybe_port(self, value: ast.expr, line: int) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
            self._record_port(value.value, line)

    def _maybe_socket_bind(self, node: ast.Call) -> None:
        # socket.bind((host, port)) — a 2-tuple whose second element is an int.
        for arg in node.args:
            if isinstance(arg, ast.Tuple) and len(arg.elts) == 2:
                self._maybe_port(arg.elts[1], node.lineno)

    def _maybe_environ_port_default(self, node: ast.Call) -> None:
        # os.environ.get("CONTEXTPLANE_TEST_PG_PORT", "5545") — the *default* is
        # the fixed port, and it is a string.
        if len(node.args) != 2:
            return
        name, default = node.args
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and "PORT" in name.value.upper()):
            return
        if isinstance(default, ast.Constant) and isinstance(default.value, str) and default.value.isdigit():
            self._record_port(int(default.value), node.lineno)

    def _record_port(self, port: int, line: int) -> None:
        # 0 is the dynamic case and the whole point of the guard's existence.
        if _LOWEST_GUARDED_PORT <= port <= _HIGHEST_GUARDED_PORT:
            self.findings.append(Finding(kind="fixed_port", value=str(port), location=self.location, line=line))

    def _maybe_server_path(self, text: str, line: int) -> None:
        if not any(marker in text for marker in _SERVER_DATA_MARKERS):
            return
        if any(marker in text for marker in _RUN_SPECIFIC_MARKERS) or self._statement_is_run_specific():
            return
        self.findings.append(Finding(kind="shared_server_path", value=text, location=self.location, line=line))


def _is_environ_get(func: ast.Attribute) -> bool:
    value = func.value
    if isinstance(value, ast.Attribute):
        return value.attr == "environ"
    return isinstance(value, ast.Name) and value.id == "environ"


def scan_module(path: Path, *, root: Path | None = None) -> list[Finding]:
    """Fixed ports and shared server paths used by one module."""
    base = root if root is not None else _REPO_ROOT
    try:
        location = path.relative_to(base).as_posix()
    except ValueError:
        location = path.as_posix()
    visitor = _ResourceVisitor(location)
    visitor.visit(ast.parse(path.read_bytes()))
    return visitor.findings


def scan_tree(
    root: Path | None = None,
    *,
    roots: Sequence[str] = _GUARDED_ROOTS,
    exclude: Sequence[str] = _SELF_EXCLUDED,
) -> list[Finding]:
    """Every fixed port and shared server path in the guarded tree."""
    base = root if root is not None else _REPO_ROOT
    excluded = set(exclude)
    findings: list[Finding] = []
    for guarded in roots:
        for path in sorted((base / guarded).rglob("*.py")):
            for finding in scan_module(path, root=base):
                if finding.location not in excluded:
                    findings.append(finding)
    return findings


# -- the guard ------------------------------------------------------------


def _path_declared(found: str, declared: Iterable[str]) -> bool:
    """Whether *found* matches a declared server path.

    A path is assembled from segments — `_REPO_ROOT / ".devstack" / "pgdata-test"`
    puts only `pgdata-test` in the source as a literal — while the manifest
    naturally spells the readable whole, `.devstack/pgdata-test`. Comparing the
    final segment matches those without the looseness of a bare substring test,
    which would let a declaration of `pgdata` silently cover every data
    directory anyone adds later.
    """
    found_leaf = found.rstrip("/").rsplit("/", 1)[-1]
    for candidate in declared:
        if found == candidate or found_leaf == candidate.rstrip("/").rsplit("/", 1)[-1]:
            return True
    return False


@dataclass(frozen=True)
class Violation:
    """A host resource the tree uses and the manifest does not declare."""

    finding: Finding
    detail: str

    def describe(self) -> str:
        return f"{self.finding.describe()} — {self.detail}"


def guard(
    manifest: Manifest | None = None,
    *,
    root: Path | None = None,
    roots: Sequence[str] = _GUARDED_ROOTS,
) -> list[Violation]:
    """Undeclared fixed ports and shared server paths in the tree.

    Returns violations rather than raising so a caller can report all of them
    at once; a guard that stopped at the first would take one commit per
    finding to get clean.
    """
    declaration = manifest if manifest is not None else Manifest.load(root=root)
    declared_ports = declaration.declared_ports
    declared_paths = declaration.declared_server_paths

    violations: list[Violation] = []
    for finding in scan_tree(root, roots=roots):
        if finding.kind == "fixed_port":
            if finding.value not in declared_ports:
                violations.append(
                    Violation(
                        finding=finding,
                        detail=(
                            f"port {finding.value} is bound with a fixed value and is not declared in "
                            "tests/integration_resources.toml; bind port 0 and read back the assigned "
                            "port, or declare it with a reason"
                        ),
                    )
                )
        elif finding.kind == "shared_server_path" and not _path_declared(finding.value, declared_paths):
            violations.append(
                Violation(
                    finding=finding,
                    detail=(
                        f"{finding.value} is a shared mutable server path and is not declared; "
                        "derive it per run instead, or declare it with a reason"
                    ),
                )
            )
    return violations


def assert_tree_declared(manifest: Manifest | None = None, *, root: Path | None = None) -> None:
    """Raise with every violation, or return silently."""
    violations = guard(manifest, root=root)
    if violations:
        rendered = "\n".join(f"  {violation.describe()}" for violation in violations)
        raise ResourceError(f"{len(violations)} undeclared host resource(s):\n{rendered}")


def classify_all(nodes: Iterable[str], manifest: Manifest | None = None) -> list[Classification]:
    """Classify many nodes, preserving order."""
    declaration = manifest if manifest is not None else Manifest.load()
    return [declaration.classify(node) for node in nodes]
