"""Conformance gate for the ARC parser sandbox and its closed output contract.

Every isolation claim below is checked by attempting the violation and
asserting it is refused -- a suite where every test would also pass
against a plain, unsandboxed interpreter is not testing a sandbox. Where a
property genuinely cannot be enforced on this platform, that is stated
here as a fact this suite has confirmed, not assumed:

Enforced on this platform (darwin), and proven here, not merely on the
Linux deployment target:

- peer-UID authentication (`ipc.py`) -- real `getpeereid(2)` via `ctypes`;
  the Linux deployment uses `SO_PEERCRED` instead, same authenticated-
  local-channel guarantee;
- every wire-framing bound (oversize/truncated/extra-data/malformed frame)
  -- pure Python, identically portable to Linux;
- the wall-clock deadline -- enforced client-side, identically portable;
- the CPU-time ceiling -- `RLIMIT_CPU`, confirmed on this kernel to raise
  `SIGXCPU`; same mechanism on Linux;
- the output-size ceiling (envelope + transport frame) -- pure Python,
  identically portable;
- no outbound network/DNS -- a process-level guard (`socket.socket` and
  `getaddrinfo` replaced) proven to raise deterministically, not merely
  absent connectivity; the Linux deployment adds its own network
  namespace on top (`AAS-T24`, out of this task's scope);
- no lifecycle/database import -- structural, an AST-checked absence of
  the import itself;
- no filesystem write outside the granted scratch root -- real OS
  permission bits (mode 0500/0400), confirmed to raise `PermissionError`;
  the Linux deployment adds a read-only container root filesystem on top
  (`AAS-T24`).

Environment-limited on this platform -- confirmed, not assumed, and not
claimed as enforced:

- the memory ceiling -- `RLIMIT_AS`/`RLIMIT_DATA` cannot be set to any
  finite value on this darwin/XNU kernel (`ValueError`, matching a plain
  `ulimit -v` failing the same way outside Python); a best-effort,
  non-preemptive RSS watchdog is the only self-defense here, versus a real
  `RLIMIT_AS` ceiling on the Linux deployment;
- CPU core-count allocation ("one CPU") -- no per-process CPU affinity API
  exists on darwin at all (`os.sched_setaffinity` is absent); this is a
  cgroup/container-runtime concern on either platform, out of this task's
  scope regardless;
- read-side filesystem confinement beyond the one granted path -- no
  mount namespace/chroot without root; this process structurally only
  ever opens the one granted content path, but nothing here prevents a
  buggy code path from opening some other world-readable file (closed by
  a container's read-only root filesystem, `AAS-T24`);
- a dedicated OS group for the socket path -- `chmod 0660` is always
  applied and tested; `chown` to a dedicated group is best-effort and
  requires a group this process is not a member of locally.

The closed output contract (`registry.arc.schemas.parser_output`) is
covered separately: schema/snapshot parity, every closedness property
(unknown-field refusal, section/warning count ceilings, ordinal
ordering/uniqueness, envelope byte ceiling), and the API-side binding
check that refuses a schema-valid envelope bound to the wrong source.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess  # noqa: S404 - the sandboxed process is a real separate OS process by design; fixed argv below
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from registry.arc.sandbox import ipc
from registry.arc.sandbox.parser_main import parse_markdown
from registry.arc.schemas import parser_output as po

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "arc_parser" / "typical.md"
SNAPSHOT_PATH = Path(__file__).resolve().parent / "snapshots" / "arc_parser_output_schema.json"
SANDBOX_SCRIPT = REPO_ROOT / "scripts" / "run_parser_sandbox.sh"
SANDBOX_PACKAGE_ROOT = REPO_ROOT / "registry" / "arc" / "sandbox"
PARSER_OUTPUT_MODULE_PATH = REPO_ROOT / "registry" / "arc" / "schemas" / "parser_output.py"


@pytest.fixture
def short_tmp_path() -> Any:
    """A short, real path directly under `/tmp`, for every test below that
    binds an actual `AF_UNIX` socket.

    Pytest's own `tmp_path` nests several directory levels deep
    (`/private/var/folders/.../pytest-of-<user>/pytest-NNN/<test-name>0/...`
    on macOS); combined with a socket filename that easily exceeds the
    kernel's `sun_path` limit (104 bytes on BSD/macOS, 108 on Linux) --
    confirmed empirically while writing this suite, not assumed. Every
    sandboxed deployment has the same constraint, which is why
    `scripts/run_parser_sandbox.sh` also binds its socket under a fresh
    `mktemp -d`, not somewhere deeply nested.
    """
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _minimal_envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_parsed_source_envelope_v1",
        "source_evidence_id": str(uuid.uuid4()),
        "source_content_digest": "0" * 64,
        "media_type": "text/markdown",
        "parser_id": "test-parser",
        "parser_version": "1",
        "title": None,
        "sections": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def _section(
    ordinal: int, *, section_id: str | None = None, source_anchor: str | None = None, text: str = "body"
) -> dict[str, Any]:
    return {
        "section_id": section_id or f"s{ordinal}",
        "source_anchor": source_anchor or f"a{ordinal}",
        "ordinal": ordinal,
        "heading": None,
        "text": text,
        "excerpt_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _section_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "section_id": "s",
        "source_anchor": "a",
        "ordinal": 0,
        "heading": None,
        "text": "body",
        "excerpt_digest": "0" * 64,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Closed output contract: schema/snapshot parity.
# ---------------------------------------------------------------------------


def test_component_schemas_match_snapshot() -> None:
    """Pinned exactly. See `test_snapshot_bites` note in this task's report
    for the manual proof that this assertion is not vacuous -- a temporary
    field change to `ParsedSourceSection` was confirmed to fail this test
    before being reverted.
    """
    assert po.component_schemas() == _load_json(SNAPSHOT_PATH)


def test_snapshot_keys_are_exactly_the_closed_output_contract() -> None:
    assert set(_load_json(SNAPSHOT_PATH).keys()) == {
        "ParsedSourceEnvelope",
        "ParsedSourceSection",
        "ParsedSourceWarning",
        "ParserSuccess",
        "ParserRefusal",
        "ParserResult",
    }


# ---------------------------------------------------------------------------
# Closed output contract: every component actively refuses an unknown field.
# Same non-vacuous proof AAS-T05 established: submitting only a bogus key,
# with every required field still missing, still produces `extra_forbidden`
# for that key alongside the `missing` errors for the rest.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(po.COMPONENTS.keys()))
def test_every_component_rejects_unknown_fields(name: str) -> None:
    model = po.COMPONENTS[name]
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({"__unknown_field_for_conformance__": "x"})
    error_types = {err["type"] for err in exc_info.value.errors()}
    assert "extra_forbidden" in error_types, f"{name} did not refuse an unknown field"


# ---------------------------------------------------------------------------
# Closed output contract: envelope-level closedness (counts, ordering,
# uniqueness, byte ceiling) -- each checked with a refusal that fires and a
# boundary case that succeeds, not merely a refusal in isolation.
# ---------------------------------------------------------------------------


def test_envelope_accepts_exactly_max_sections() -> None:
    sections = [_section(i) for i in range(po.MAX_SECTIONS)]
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))
    assert len(envelope.sections) == po.MAX_SECTIONS


def test_envelope_refuses_one_over_max_sections() -> None:
    sections = [_section(i) for i in range(po.MAX_SECTIONS + 1)]
    with pytest.raises(ValidationError, match="sections"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_one_over_max_warnings() -> None:
    warnings = [{"code": "truncated", "source_anchor": None} for _ in range(po.MAX_WARNINGS + 1)]
    with pytest.raises(ValidationError, match="warnings"):
        po.ParsedSourceEnvelope(**_minimal_envelope(warnings=warnings))


def test_envelope_accepts_exactly_max_warnings() -> None:
    warnings = [{"code": "truncated", "source_anchor": None} for _ in range(po.MAX_WARNINGS)]
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(warnings=warnings))
    assert len(envelope.warnings) == po.MAX_WARNINGS


def test_envelope_refuses_duplicate_ordinal() -> None:
    sections = [_section(0), _section(0, section_id="other", source_anchor="other")]
    with pytest.raises(ValidationError, match="unique"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_unordered_sections() -> None:
    sections = [_section(1), _section(0)]
    with pytest.raises(ValidationError, match="ascending"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_duplicate_section_id() -> None:
    sections = [_section(0, section_id="dup"), _section(1, section_id="dup")]
    with pytest.raises(ValidationError, match="section_id"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_duplicate_source_anchor() -> None:
    sections = [_section(0, source_anchor="dup"), _section(1, source_anchor="dup")]
    with pytest.raises(ValidationError, match="source_anchor"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_byte_ceiling_fires_when_exceeded() -> None:
    big_text = "x" * po.MAX_TEXT_LENGTH
    sections = [_section(i, text=big_text) for i in range(20)]
    with pytest.raises(ValidationError, match="byte ceiling"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_byte_ceiling_allows_content_comfortably_under_it() -> None:
    sections = [_section(i, text="x" * 1000) for i in range(5)]
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))
    assert len(po.canonical_envelope_bytes(envelope)) < po.MAX_ENVELOPE_BYTES


def test_title_over_max_length_refused() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceEnvelope(**_minimal_envelope(title="x" * (po.MAX_TITLE_LENGTH + 1)))


def test_title_at_max_length_accepted() -> None:
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(title="x" * po.MAX_TITLE_LENGTH))
    assert envelope.title is not None
    assert len(envelope.title) == po.MAX_TITLE_LENGTH


def test_section_text_over_max_length_refused() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(**_section_kwargs(text="x" * (po.MAX_TEXT_LENGTH + 1)))


def test_section_digest_must_be_lowercase_hex64() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(**_section_kwargs(excerpt_digest="not-a-digest"))
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(**_section_kwargs(excerpt_digest="A" * 64))  # uppercase refused


# ---------------------------------------------------------------------------
# Closed output contract: the discriminated union round-trips, and refuses
# an unrecognized branch or refusal code rather than passing it through.
# ---------------------------------------------------------------------------


def test_parse_parser_result_success_round_trip() -> None:
    data = {"status": "ok", "envelope": _minimal_envelope(sections=[_section(0)])}
    result = po.parse_parser_result(data)
    assert isinstance(result, po.ParserSuccess)


def test_parse_parser_result_refusal_round_trip() -> None:
    result = po.parse_parser_result({"status": "refused", "refusal_code": "malformed_source"})
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_parse_parser_result_refuses_unknown_status() -> None:
    with pytest.raises(ValidationError):
        po.parse_parser_result({"status": "pending"})


def test_parse_parser_result_refuses_unknown_refusal_code() -> None:
    with pytest.raises(ValidationError):
        po.parse_parser_result({"status": "refused", "refusal_code": "not_a_real_code"})


def test_parse_parser_result_refuses_extra_field_on_success() -> None:
    data = {
        "status": "ok",
        "envelope": _minimal_envelope(sections=[_section(0)]),
        "extra_field_the_sandbox_never_declared": True,
    }
    with pytest.raises(ValidationError):
        po.parse_parser_result(data)


# ---------------------------------------------------------------------------
# API-side binding check: even a schema-valid envelope is untrusted until
# its declared identity matches the admitted row.
# ---------------------------------------------------------------------------


def test_verify_source_binding_accepts_matching() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    po.verify_source_binding(envelope, source_evidence_id=source_evidence_id, source_content_digest="a" * 64)


def test_verify_source_binding_refuses_mismatched_source_evidence_id() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    with pytest.raises(po.ParserBindingError):
        po.verify_source_binding(envelope, source_evidence_id=uuid.uuid4(), source_content_digest="a" * 64)


def test_verify_source_binding_refuses_mismatched_digest() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    with pytest.raises(po.ParserBindingError):
        po.verify_source_binding(envelope, source_evidence_id=source_evidence_id, source_content_digest="b" * 64)


# ---------------------------------------------------------------------------
# Pure parsing logic (`parse_markdown`): no sandbox process needed, since
# the function itself performs no I/O or guard installation.
# ---------------------------------------------------------------------------


def test_typical_fixture_parses_successfully() -> None:
    content = FIXTURE_PATH.read_bytes()
    result = parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert result.envelope.title == "Directive Title"
    assert [s.ordinal for s in result.envelope.sections] == list(range(len(result.envelope.sections)))
    assert result.envelope.warnings == []


def test_unsupported_media_type_refused() -> None:
    result = parse_markdown(b"# hi", media_type="application/pdf", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "unsupported_media_type"


def test_empty_source_refused_as_malformed() -> None:
    result = parse_markdown(b"   \n\n  \n", media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_non_utf8_source_refused_as_malformed() -> None:
    result = parse_markdown(b"\xff\xfe\x00\x01", media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_source_too_complex_refused() -> None:
    content = "\n".join(f"# heading {i}" for i in range(po.MAX_SECTIONS + 5)).encode("utf-8")
    result = parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "source_too_complex"


def test_exactly_max_sections_succeeds() -> None:
    content = "\n".join(f"# heading {i}" for i in range(po.MAX_SECTIONS)).encode("utf-8")
    result = parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert len(result.envelope.sections) == po.MAX_SECTIONS


def test_long_section_text_truncated_with_warning() -> None:
    body = "x" * (po.MAX_TEXT_LENGTH + 100)
    content = f"# Heading\n{body}".encode()
    result = parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert len(result.envelope.sections[0].text) == po.MAX_TEXT_LENGTH
    assert any(w.code == "truncated" for w in result.envelope.warnings)


def test_parsed_envelope_binds_to_the_source_evidence_id_it_was_given() -> None:
    source_evidence_id = uuid.uuid4()
    content = FIXTURE_PATH.read_bytes()
    result = parse_markdown(content, media_type="text/markdown", source_evidence_id=source_evidence_id)
    assert isinstance(result, po.ParserSuccess)
    expected_digest = hashlib.sha256(content).hexdigest()
    po.verify_source_binding(
        result.envelope, source_evidence_id=source_evidence_id, source_content_digest=expected_digest
    )


# ---------------------------------------------------------------------------
# Structural proof: no lifecycle, service, session, or database import
# anywhere in the sandbox package or the schema it shares with it. An
# import-graph walk, not a runtime check that something could still bypass.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_PREFIXES = (
    "registry.storage",
    "registry.arc.service",
    "registry.wiring",
    "registry.config",
    "sqlalchemy",
    "asyncpg",
    "psycopg2",
)


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _forbidden_imports(path: Path) -> set[str]:
    imported = _imported_module_names(path)
    return {
        name
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _FORBIDDEN_IMPORT_PREFIXES)
    }


_SANDBOX_MODULE_PATHS = sorted(SANDBOX_PACKAGE_ROOT.glob("*.py")) + [PARSER_OUTPUT_MODULE_PATH]


@pytest.mark.parametrize("path", _SANDBOX_MODULE_PATHS, ids=lambda p: p.name)
def test_sandbox_modules_have_no_lifecycle_or_database_imports(path: Path) -> None:
    forbidden = _forbidden_imports(path)
    assert not forbidden, f"{path.name} imports {sorted(forbidden)}, giving the sandbox database/lifecycle reach"


def test_forbidden_import_walker_is_not_vacuous(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("import registry.storage.pg\nfrom registry.arc.service import artifact\n")
    assert _forbidden_imports(planted) == {"registry.storage.pg", "registry.arc.service"}


# ---------------------------------------------------------------------------
# Real filesystem permission bits: the granted content root refuses any
# write (existing-file overwrite and new-file creation both refused); the
# scratch root, by contrast, genuinely allows writes -- proving the
# distinction is the permission bits, not an accident of `tmp_path`.
# ---------------------------------------------------------------------------


def test_read_only_content_root_denies_overwrite_and_new_file(tmp_path: Path) -> None:
    read_root = tmp_path / "read_root"
    read_root.mkdir()
    content_path = read_root / "content"
    content_path.write_bytes(b"admitted bytes")
    content_path.chmod(0o400)
    read_root.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            content_path.write_text("hostile overwrite attempt")
        with pytest.raises(PermissionError):
            (read_root / "new_file").write_text("hostile new-file attempt")
    finally:
        read_root.chmod(0o700)  # restore so pytest's own tmp_path cleanup can remove it


def test_scratch_root_allows_writes(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    scratch_root.chmod(0o700)
    (scratch_root / "work.tmp").write_text("scratch space is writable")
    assert (scratch_root / "work.tmp").read_text() == "scratch space is writable"


# ---------------------------------------------------------------------------
# IPC transport: server-side helpers for raw, deliberately misbehaving
# peers -- these bypass `ipc.serve_one`/`ipc.request_json` on purpose, since
# the point is to construct exactly the malformed bytes the real
# implementation never would.
# ---------------------------------------------------------------------------


def _bind_raw_server(sock_path: Path) -> socket.socket:
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    return server


def _serve_once_in_background(server: socket.socket, script: Callable[[socket.socket], None]) -> threading.Thread:
    def _run() -> None:
        conn, _peer_address = server.accept()
        try:
            script(conn)
        except (ipc.SandboxRefusal, OSError):
            # Several scripts here deliberately provoke the client into
            # aborting before this side finishes reading or writing (a
            # peer-uid mismatch, a short-deadline timeout) -- the resulting
            # truncated-frame/connection-reset error on this side is the
            # anticipated other half of that same scenario, not a bug in
            # the test double. The test's own assertion is against the
            # client's observed `SandboxRefusal`, not this side's.
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _wait_for_path(path: Path, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared within {timeout}s")


def _run_capturing_exception(fn: Callable[..., object], *args: object, **kwargs: object) -> threading.Thread:
    box: list[object] = []

    def _target() -> None:
        try:
            box.append(fn(*args, **kwargs))
        except BaseException as exc:
            box.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.result_box = box  # type: ignore[attr-defined]
    return thread


def test_client_refuses_oversize_response_frame(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        conn.sendall(struct.pack("!I", ipc.DEFAULT_MAX_FRAME_BYTES + 1))  # lie about the length; never send a body

    thread = _serve_once_in_background(server, _script)
    try:
        start = time.monotonic()
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        elapsed = time.monotonic() - start
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_TOO_LARGE
        # Refused on the declared length alone, not by waiting to receive
        # (and never being sent) an oversize body.
        assert elapsed < 1.0
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_truncated_response_frame(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        conn.sendall(struct.pack("!I", 100))
        conn.sendall(b"only ten b")  # far short of the promised 100 bytes, then the `with` closes it

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_TRUNCATED
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_malformed_json_response(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = b"{not valid json"
        conn.sendall(struct.pack("!I", len(body)) + body)

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_MALFORMED
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_non_object_json_response(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = json.dumps([1, 2, 3]).encode("utf-8")
        conn.sendall(struct.pack("!I", len(body)) + body)

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_MALFORMED
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_extra_trailing_frame(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = json.dumps({"status": "ok"}).encode("utf-8")
        conn.sendall(struct.pack("!I", len(body)) + body)
        extra = b"1"
        conn.sendall(struct.pack("!I", len(extra)) + extra)
        time.sleep(0.2)  # keep the connection open long enough for the client's trailing-data check to see it

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_EXTRA_DATA
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_when_deadline_exceeded(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        time.sleep(2.0)  # never respond before the client's short deadline

    thread = _serve_once_in_background(server, _script)
    try:
        start = time.monotonic()
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=0.3)
        elapsed = time.monotonic() - start
        assert exc_info.value.code == ipc.SandboxRefusalCode.DEADLINE_EXCEEDED
        assert elapsed < 1.5
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_when_nothing_listening(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "nobody-here.sock"
    with pytest.raises(ipc.SandboxRefusal) as exc_info:
        ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=2.0)
    assert exc_info.value.code == ipc.SandboxRefusalCode.PROCESS_UNAVAILABLE


def test_client_refuses_connection_loss_before_any_response(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        # Exit without responding at all -- the sandboxed process dying
        # mid-request.

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_TRUNCATED
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_refuses_server_with_wrong_peer_uid(short_tmp_path: Path) -> None:
    """A real kernel-verified peer-UID check, not a mock: the server is a
    genuine, well-behaved peer that would otherwise succeed -- proves the
    client's authentication step fires on its own, not because the
    connection was doomed for some unrelated reason.
    """
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = json.dumps({"status": "ok"}).encode("utf-8")
        conn.sendall(struct.pack("!I", len(body)) + body)

    thread = _serve_once_in_background(server, _script)
    try:
        wrong_uid = os.getuid() + 999_999
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=wrong_uid, deadline_seconds=3.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.PEER_MISMATCH
    finally:
        thread.join(timeout=3)
        server.close()


def test_client_accepts_server_with_correct_peer_uid(short_tmp_path: Path) -> None:
    """Positive control for the mismatch test above: PEER_MISMATCH is not
    simply always raised regardless of the supplied uid.
    """
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = json.dumps({"status": "ok"}).encode("utf-8")
        conn.sendall(struct.pack("!I", len(body)) + body)

    thread = _serve_once_in_background(server, _script)
    try:
        response = ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert response == {"status": "ok"}
    finally:
        thread.join(timeout=3)
        server.close()


def test_server_refuses_client_with_wrong_peer_uid(short_tmp_path: Path) -> None:
    """The other authentication direction, using the real `ipc.serve_one`
    (not a raw socket double): the server refuses a client it was told to
    expect a different uid from, even though the client is a normal,
    well-behaved peer.
    """
    sock_path = short_tmp_path / "s.sock"
    wrong_uid = os.getuid() + 999_999

    def _handle(_request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    thread = _run_capturing_exception(
        ipc.serve_one, sock_path, _handle, expected_peer_uid=wrong_uid, deadline_seconds=3.0
    )
    _wait_for_path(sock_path)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3.0)
    try:
        client.connect(str(sock_path))
        ipc.write_frame(client, b"{}")
        try:
            client.recv(1)
        except OSError:
            pass
    finally:
        client.close()

    thread.join(timeout=5)
    box = thread.result_box  # type: ignore[attr-defined]
    assert box and isinstance(box[0], ipc.SandboxRefusal)
    assert box[0].code == ipc.SandboxRefusalCode.PEER_MISMATCH


def test_serve_one_applies_socket_mode(short_tmp_path: Path) -> None:
    sock_path = short_tmp_path / "s.sock"

    def _handle(_request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    thread = threading.Thread(
        target=ipc.serve_one,
        args=(sock_path, _handle),
        kwargs={"expected_peer_uid": os.getuid(), "deadline_seconds": 3.0},
        daemon=True,
    )
    thread.start()
    _wait_for_path(sock_path)
    mode = stat.S_IMODE(sock_path.stat().st_mode)
    assert mode == ipc.SOCKET_MODE
    response = ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
    assert response == {"status": "ok"}
    thread.join(timeout=3)


def test_install_group_ownership_reports_failure_honestly(short_tmp_path: Path) -> None:
    """Without root, `chown` to a gid this process is not a member of
    fails. Confirms the function reports that accurately rather than
    silently claiming success.
    """
    sock_path = short_tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    try:
        implausible_gid = 999_999
        assert ipc.install_group_ownership(sock_path, implausible_gid) is False
    finally:
        server.close()


def test_request_json_wraps_validate_response_failure_as_binding_mismatch(short_tmp_path: Path) -> None:
    """The generic hook `ipc.py` exposes for exactly this purpose, tested
    independently of `parser_output.verify_source_binding` -- the two are
    covered together by the end-to-end test below.
    """
    sock_path = short_tmp_path / "s.sock"
    server = _bind_raw_server(sock_path)

    def _script(conn: socket.socket) -> None:
        ipc.read_frame(conn)
        body = json.dumps({"status": "ok", "actual": "wrong-thing"}).encode("utf-8")
        conn.sendall(struct.pack("!I", len(body)) + body)

    def _validate(response: dict[str, Any]) -> None:
        if response.get("actual") != "expected-thing":
            raise ValueError("response bound to the wrong thing")

    thread = _serve_once_in_background(server, _script)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(
                sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0, validate_response=_validate
            )
        assert exc_info.value.code == ipc.SandboxRefusalCode.BINDING_MISMATCH
    finally:
        thread.join(timeout=3)
        server.close()


# ---------------------------------------------------------------------------
# The real sandboxed process, spawned as a genuine separate OS process --
# not a test double -- to prove the properties that only a real process
# boundary can demonstrate: resource termination, network denial, and
# credential absence.
# ---------------------------------------------------------------------------


def _spawn_parser(
    short_tmp_path: Path,
    content: bytes,
    *,
    media_type: str = "text/markdown",
    cpu_seconds: int = 30,
    memory_bytes: int = 512 * 1024 * 1024,
    deadline_seconds: float = 10.0,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], Path, uuid.UUID, str]:
    read_root = short_tmp_path / "read_root"
    scratch_root = short_tmp_path / "scratch"
    read_root.mkdir()
    scratch_root.mkdir()
    content_path = read_root / "content"
    content_path.write_bytes(content)
    content_path.chmod(0o400)
    read_root.chmod(0o500)
    scratch_root.chmod(0o700)

    sock_path = short_tmp_path / "parser.sock"
    source_evidence_id = uuid.uuid4()
    digest = hashlib.sha256(content).hexdigest()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "registry.arc.sandbox.parser_main",
            "--content-path",
            str(content_path),
            "--sock-path",
            str(sock_path),
            "--media-type",
            media_type,
            "--source-evidence-id",
            str(source_evidence_id),
            "--expected-peer-uid",
            str(os.getuid()),
            "--scratch-dir",
            str(scratch_root),
            "--deadline-seconds",
            str(deadline_seconds),
            "--cpu-seconds",
            str(cpu_seconds),
            "--memory-bytes",
            str(memory_bytes),
        ],
        cwd=str(REPO_ROOT),
        env=dict(os.environ) if env is None else env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_process_socket(proc, sock_path)
    return proc, sock_path, source_evidence_id, digest


def _wait_for_process_socket(proc: subprocess.Popen[bytes], sock_path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            return
        if proc.poll() is not None:
            _out, err = proc.communicate(timeout=1)
            message = err.decode(errors="replace")
            raise AssertionError(f"parser process exited early (code {proc.returncode}): {message}")
        time.sleep(0.02)
    raise AssertionError("parser process never started listening")


def _binding_validator(source_evidence_id: uuid.UUID, digest: str) -> Callable[[dict[str, Any]], None]:
    """A `validate_response` hook for `ipc.request_json` that only checks
    the binding on a success response, leaving a refusal response (which
    carries no envelope to bind) alone.
    """

    def _validate(response: dict[str, Any]) -> None:
        result = po.parse_parser_result(response)
        if isinstance(result, po.ParserSuccess):
            po.verify_source_binding(
                result.envelope, source_evidence_id=source_evidence_id, source_content_digest=digest
            )

    return _validate


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_end_to_end_typical_fixture_success(short_tmp_path: Path) -> None:
    content = FIXTURE_PATH.read_bytes()
    proc, sock_path, source_evidence_id, digest = _spawn_parser(short_tmp_path, content)
    try:
        response = ipc.request_json(
            sock_path,
            {},
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
            validate_response=_binding_validator(source_evidence_id, digest),
        )
        result = po.parse_parser_result(response)
        assert isinstance(result, po.ParserSuccess)
        assert result.envelope.title == "Directive Title"
    finally:
        _terminate(proc)


def test_end_to_end_unsupported_media_type_refused(short_tmp_path: Path) -> None:
    proc, sock_path, _source_evidence_id, _digest = _spawn_parser(
        short_tmp_path, b"<html></html>", media_type="text/html"
    )
    try:
        response = ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=10.0)
        result = po.parse_parser_result(response)
        assert isinstance(result, po.ParserRefusal)
        assert result.refusal_code == "unsupported_media_type"
    finally:
        _terminate(proc)


def test_end_to_end_binding_mismatch_refused(short_tmp_path: Path) -> None:
    proc, sock_path, _source_evidence_id, digest = _spawn_parser(short_tmp_path, FIXTURE_PATH.read_bytes())
    try:
        wrong_source_evidence_id = uuid.uuid4()
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(
                sock_path,
                {},
                expected_peer_uid=os.getuid(),
                deadline_seconds=10.0,
                validate_response=_binding_validator(wrong_source_evidence_id, digest),
            )
        assert exc_info.value.code == ipc.SandboxRefusalCode.BINDING_MISMATCH
    finally:
        _terminate(proc)


def test_end_to_end_peer_uid_mismatch_against_real_process(short_tmp_path: Path) -> None:
    proc, sock_path, _source_evidence_id, _digest = _spawn_parser(short_tmp_path, FIXTURE_PATH.read_bytes())
    try:
        wrong_uid = os.getuid() + 999_999
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=wrong_uid, deadline_seconds=10.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.PEER_MISMATCH
    finally:
        _terminate(proc)


def test_parser_process_needs_no_database_credential(short_tmp_path: Path) -> None:
    """Empirical proof to go with the structural (AST) one above: even with
    a poisoned `DATABASE_URL` present and almost nothing else in the
    environment, the sandboxed process parses successfully -- it never
    reads the variable, structurally or otherwise.
    """
    # `home_fallback` is a default for this test's own scrubbed subprocess
    # environment, not a real temp-file path -- bandit's S108 flags the
    # literal "/tmp" text either way, so it is named to make that clear.
    home_fallback = "/tmp"  # noqa: S108
    minimal_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", home_fallback),
        "DATABASE_URL": "postgresql://poison:poison@example.invalid/poison",
    }
    proc, sock_path, source_evidence_id, digest = _spawn_parser(
        short_tmp_path, FIXTURE_PATH.read_bytes(), env=minimal_env
    )
    try:
        response = ipc.request_json(
            sock_path,
            {},
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
            validate_response=_binding_validator(source_evidence_id, digest),
        )
        result = po.parse_parser_result(response)
        assert isinstance(result, po.ParserSuccess)
    finally:
        _terminate(proc)


# ---------------------------------------------------------------------------
# Resource termination and network denial: each of these runs the guard or
# limit function in a throwaway subprocess, never in this test process
# itself -- calling `apply_resource_limits`/`install_network_guard` directly
# here would apply a real CPU/memory ceiling or a permanent socket-module
# patch to the pytest worker running every other test in this session.
# ---------------------------------------------------------------------------


def _run_script(script: str, *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def test_cpu_limit_terminates_a_runaway_process() -> None:
    script = (
        "from registry.arc.sandbox.parser_main import apply_resource_limits\n"
        "apply_resource_limits(cpu_seconds=1, memory_bytes=512 * 1024 * 1024)\n"
        "x = 0\n"
        "while True:\n"
        "    x += 1\n"
    )
    start = time.monotonic()
    result = _run_script(script, timeout=15.0)
    elapsed = time.monotonic() - start
    # The guarantee is that a runaway process is *terminated at the ceiling*,
    # not that a particular signal delivers the termination. `RLIMIT_CPU` is
    # set with soft == hard, and the two kernels differ in what that means:
    # darwin reports `SIGXCPU`, Linux reports `SIGKILL`, because exceeding the
    # *hard* limit is an unconditional kill there and the soft-limit
    # `SIGXCPU` is not observable when the two coincide. Asserting the exact
    # signal pinned the test to the development platform and failed on the
    # actual deployment target while the security property held perfectly.
    assert result.returncode in (-signal.SIGXCPU, -signal.SIGKILL), (
        result.returncode,
        result.stdout,
        result.stderr,
    )
    # Killed well within the 1-second CPU-time ceiling plus scheduling
    # slack, not merely before the 15-second subprocess timeout that would
    # otherwise have masked a limit that never fired.
    assert elapsed < 10.0


def test_memory_limit_report_reflects_confirmed_platform_reality() -> None:
    script = (
        "import json\n"
        "from registry.arc.sandbox.parser_main import apply_resource_limits\n"
        "report = apply_resource_limits(cpu_seconds=30, memory_bytes=512 * 1024 * 1024)\n"
        "print(json.dumps({'memory_limit_applied': report.memory_limit_applied}))\n"
    )
    result = _run_script(script, timeout=10.0)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if sys.platform.startswith("linux"):
        assert payload["memory_limit_applied"] is True
    else:
        # Confirmed directly against this darwin kernel (see the module
        # docstring): RLIMIT_AS cannot be set to any finite value here.
        assert payload["memory_limit_applied"] is False


def test_network_guard_blocks_af_inet_socket_construction() -> None:
    script = (
        "import socket\n"
        "from registry.arc.sandbox.parser_main import install_network_guard\n"
        "install_network_guard()\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    print('NOT_BLOCKED')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BLOCKED"


def test_network_guard_blocks_create_connection_and_dns() -> None:
    script = (
        "import socket\n"
        "from registry.arc.sandbox.parser_main import install_network_guard\n"
        "install_network_guard()\n"
        "results = []\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 80), timeout=2)\n"
        "    results.append('create_connection:NOT_BLOCKED')\n"
        "except PermissionError:\n"
        "    results.append('create_connection:BLOCKED')\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80)\n"
        "    results.append('getaddrinfo:NOT_BLOCKED')\n"
        "except PermissionError:\n"
        "    results.append('getaddrinfo:BLOCKED')\n"
        "print(' '.join(results))\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "create_connection:BLOCKED getaddrinfo:BLOCKED"


def test_network_guard_still_allows_af_unix_sockets() -> None:
    """The guard is scoped to non-Unix-domain families; the sandbox's own
    IPC channel must keep working once the guard is installed.
    """
    script = (
        "import socket\n"
        "from registry.arc.sandbox.parser_main import install_network_guard\n"
        "install_network_guard()\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.close()\n"
        "print('ALLOWED')\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ALLOWED"


def test_without_the_guard_af_inet_socket_construction_succeeds() -> None:
    """Control for the tests above: proves the `BLOCKED` results come from
    the guard, not from this environment lacking permission to even
    construct a raw socket in the first place (it does have that
    permission; only a real route to the internet is unavailable, which
    the guard's approach deliberately does not rely on).
    """
    script = "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_STREAM)\nprint('CONSTRUCTED')\n"
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CONSTRUCTED"


# ---------------------------------------------------------------------------
# The shipped shell wrapper, actually executed end to end.
# ---------------------------------------------------------------------------


def test_run_parser_sandbox_script_end_to_end(tmp_path: Path) -> None:
    output_path = tmp_path / "output.json"
    result = subprocess.run(
        ["bash", str(SANDBOX_SCRIPT), str(FIXTURE_PATH), str(output_path)],
        cwd=str(REPO_ROOT),
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output_path.exists()
    assert output_path.stat().st_size > 0
    data = json.loads(output_path.read_text())
    assert data["status"] == "ok"
    assert data["envelope"]["title"] == "Directive Title"
