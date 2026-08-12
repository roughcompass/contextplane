"""Write-once evidence: exclusive creation, atomic finalization, and redaction.

Evidence that can be rewritten is not evidence. Every run gets its own
directory created with `O_EXCL`, every raw file is fsynced and renamed into
place rather than written where a reader can see it half-finished, and the
manifest that checksums them all is written last. A reader that finds a
manifest can trust that everything it names is complete; a reader that finds no
manifest knows the run did not finish, which is a different fact from a run
that finished badly and must not look the same.

The redaction rules here are not defensive habit. A measurement bundle is
copied between worktrees, quoted in task records, and read by whoever is
adjudicating a result months later — so a database URL serialized into one is
a credential with a long life and no owner. Identities appear only as
run-scoped digests: stable enough to prove two records refer to the same
database, useless for connecting to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_MANIFEST_NAME: Final = "manifest.json"
_CHECKSUM_CHUNK: Final = 1 << 20

# Anything shaped like a connection string, a token, or a bare credential. The
# scan is a backstop rather than the mechanism — callers pass digests, not
# secrets — but a backstop that never fires is indistinguishable from one that
# cannot, so the fault tests drive real secret-shaped values through it.
_FORBIDDEN_PATTERNS: Final = (
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']*"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[=:]"),
)

# Keys whose values are never serialized whatever they contain. A key named
# `database_url` holding the string "redacted" is still a schema that invites
# the next writer to put a URL in it.
_FORBIDDEN_KEYS: Final = frozenset(
    {
        "database_url",
        "dsn",
        "password",
        "secret",
        "sequence_secret",
        "token",
        "control",
        "control_document",
        "broker_manifest",
    }
)


class EvidenceError(RuntimeError):
    """The artifact cannot be written, or would leak something."""


class SecretLeak(EvidenceError):
    """A value that must never reach disk was about to be serialized."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHECKSUM_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_scoped_digest(run_id: str, identity: str) -> str:
    """A database or server identity, stable within a run and inert outside it.

    Salted with the run ID so the same database name in two runs produces two
    digests. That is deliberate: a cross-run correlation of raw identities is
    exactly the leak a digest is supposed to prevent, and comparisons that
    matter all happen inside one run's evidence.
    """
    return hashlib.sha256(f"{run_id}\x00{identity}".encode()).hexdigest()[:32]


def assert_no_secrets(payload: Any, *, where: str = "$") -> None:  # noqa: ANN401 - walks arbitrary JSON-shaped evidence; narrowing the type here would exempt whatever shape it failed to name
    """Walk a structure and refuse anything credential-shaped.

    Runs before every write. The alternative — trusting callers — has a bad
    failure mode: the leak lands in an artifact that is then copied, quoted,
    and archived, and no later check can unpublish it.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                msg = f"{where}.{key}: key may never be serialized into evidence"
                raise SecretLeak(msg)
            assert_no_secrets(value, where=f"{where}.{key}")
        return
    if isinstance(payload, list | tuple):
        for index, value in enumerate(payload):
            assert_no_secrets(value, where=f"{where}[{index}]")
        return
    if isinstance(payload, str):
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(payload):
                msg = f"{where}: value matches {pattern.pattern!r} and looks like a credential"
                raise SecretLeak(msg)


def _fsync_dir(path: Path) -> None:
    """Durably record a rename. Without this the entry can outlive its data."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, payload: str) -> str:
    """Write, fsync, and rename into place. Returns the content checksum.

    A reader must never observe a partial artifact. Writing to a temporary
    name in the same directory and renaming makes the file appear complete or
    not at all, and fsyncing both the file and its directory means a crash
    cannot leave a directory entry pointing at unwritten blocks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_dir(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return sha256_text(payload)


@dataclass(frozen=True)
class RunDirectory:
    """A run's own directory, which it created and nobody else may reuse."""

    path: Path
    run_id: str

    @property
    def manifest_path(self) -> Path:
        return self.path / _MANIFEST_NAME

    @property
    def finalized(self) -> bool:
        return self.manifest_path.exists()


def create_run_directory(root: Path, run_id: str) -> RunDirectory:
    """Create this run's directory, or refuse because it already exists.

    Exclusive creation is what makes a duplicate run ID a hard failure instead
    of a silent overwrite. Reusing a run ID is how a stale artifact gets
    presented as a fresh measurement, and it is one of the cases the verifier
    is required to reject — but rejecting at write time is better, because by
    verification the original evidence is already gone.
    """
    if not run_id or "/" in run_id or run_id.startswith("."):
        msg = f"unusable run ID: {run_id!r}"
        raise EvidenceError(msg)
    path = root / run_id
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        msg = f"run ID {run_id!r} already has evidence at {path}; refusing to overwrite"
        raise EvidenceError(msg) from error
    _fsync_dir(root)
    return RunDirectory(path=path, run_id=run_id)


class EvidenceWriter:
    """Accumulates one run's raw files, then seals them with a manifest.

    Records are appended as complete JSON documents and JSONL streams; nothing
    is checksummed until finalization, because a checksum taken before the
    file stops growing describes a file that no longer exists. Finalization is
    the only operation that makes the run readable to a report generator, and
    it happens exactly once.
    """

    def __init__(self, directory: RunDirectory) -> None:
        self._directory = directory
        self._raw: dict[str, Path] = {}
        self._finalized = False

    @property
    def run_id(self) -> str:
        return self._directory.run_id

    @property
    def path(self) -> Path:
        return self._directory.path

    def write_json(self, name: str, payload: Mapping[str, Any]) -> Path:
        assert_no_secrets(payload)
        target = self._named(name)
        atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self._raw[name] = target
        return target

    def write_jsonl(self, name: str, records: list[Mapping[str, Any]]) -> Path:
        for record in records:
            assert_no_secrets(record)
        target = self._named(name)
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        atomic_write(target, body)
        self._raw[name] = target
        return target

    def adopt(self, name: str, source: Path) -> Path:
        """Record a file produced by something else — a `/usr/bin/time` file,
        a captured stdout — so the manifest checksums it too.

        Adopted rather than copied: the controller owns those files and the
        manifest's job is to bind them, not to make a second copy that could
        disagree with the first.
        """
        if not source.is_file():
            msg = f"cannot adopt missing file for {name!r}: {source}"
            raise EvidenceError(msg)
        self._raw[name] = source
        return source

    def _named(self, name: str) -> Path:
        if self._finalized:
            msg = f"run {self.run_id!r} is finalized; {name!r} cannot be added"
            raise EvidenceError(msg)
        if name == _MANIFEST_NAME:
            msg = "the manifest is written by finalize(), not as a raw file"
            raise EvidenceError(msg)
        return self._directory.path / name

    def finalize(self, *, exit_status: int, summary: Mapping[str, Any]) -> dict[str, Any]:
        """Seal the run: checksum every raw file, then write the manifest last.

        Manifest-last is the ordering the whole scheme depends on. Its presence
        is the signal that the run completed, so writing it before the files it
        names would advertise a complete run over an incomplete one.
        """
        if self._finalized:
            msg = f"run {self.run_id!r} is already finalized"
            raise EvidenceError(msg)
        assert_no_secrets(summary)

        checksums = {name: sha256_file(path) for name, path in sorted(self._raw.items())}
        manifest = {
            "run_id": self.run_id,
            "exit_status": exit_status,
            "raw_files": {name: str(path.name) for name, path in sorted(self._raw.items())},
            "checksums": checksums,
            "summary": dict(summary),
        }
        atomic_write(self._directory.manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self._finalized = True
        return manifest


@dataclass(frozen=True)
class ExternalTiming:
    """`real`, `user`, and `sys` as the outer layer alone may record them.

    Only the outer controller writes these. A child reporting its own external
    time is reporting the interval it chose to measure, which excludes whatever
    happened before its first line of Python ran — process spawn, Make's own
    startup, interpreter import — and that excluded part is exactly where a
    regression hides from an inner clock.

    `real` is the boundary that matters. `user + sys` is a different quantity
    that happens to be near it on an idle machine and diverges from it under
    load or across cores, so the internal total is never derived from the pair.
    """

    real: float
    user: float
    sys: float

    def as_evidence(self) -> dict[str, float]:
        return {
            "external_real_seconds": round(self.real, 6),
            "external_user_seconds": round(self.user, 6),
            "external_sys_seconds": round(self.sys, 6),
        }


def parse_time_file(path: Path) -> ExternalTiming:
    """Read a completed `/usr/bin/time -p` file.

    Called only after the child has exited. `/usr/bin/time` writes this file
    when the process it wrapped terminates, so parsing it while the child still
    runs reads either nothing or a truncated line — and a truncated `real` that
    happens to parse is a measurement that is simply wrong rather than missing.
    """
    if not path.is_file():
        msg = f"no timing file at {path}; the child produced no external measurement"
        raise EvidenceError(msg)
    values: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in {"real", "user", "sys"}:
            try:
                values[parts[0]] = float(parts[1])
            except ValueError:
                continue
    missing = sorted({"real", "user", "sys"} - set(values))
    if missing:
        msg = f"timing file {path} is incomplete: no {', '.join(missing)}. The child may not have exited."
        raise EvidenceError(msg)
    return ExternalTiming(real=values["real"], user=values["user"], sys=values["sys"])


def read_manifest(directory: Path) -> dict[str, Any]:
    """Load a sealed manifest, refusing an unfinalized run."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        msg = f"no sealed manifest at {manifest_path}; the run did not finalize"
        raise EvidenceError(msg)
    loaded: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return loaded


def verify_manifest(directory: Path) -> dict[str, str]:
    """Re-checksum every file a manifest names. Returns the mismatches.

    Empty means intact. The verifier calls this rather than trusting the
    recorded checksums, because a manifest that vouches for itself proves only
    that whoever edited the file could also edit the manifest.
    """
    manifest = read_manifest(directory)
    recorded: Mapping[str, str] = manifest.get("checksums", {})
    files: Mapping[str, str] = manifest.get("raw_files", {})
    mismatches: dict[str, str] = {}
    for name, expected in sorted(recorded.items()):
        relative = files.get(name)
        if relative is None:
            mismatches[name] = "named in checksums but not in raw_files"
            continue
        path = directory / relative
        if not path.is_file():
            mismatches[name] = f"missing file {relative}"
            continue
        observed = sha256_file(path)
        if observed != expected:
            mismatches[name] = f"checksum {observed} != recorded {expected}"
    return mismatches
