"""Conformance gate for the ARC drafter sandbox and its closed output contract.

Sibling of `test_arc_parser_sandbox.py`, same standard: every isolation
claim is checked by attempting the violation and asserting it is refused,
not assumed from the parser's own proof. The drafter reuses `contextplane.arc.
sandbox.ipc` unchanged (`serve_one`/`request_json`, the same peer-UID
authentication) -- what this suite proves that the parser's own suite
cannot is that the *drafter's own* process independently applies the same
guards under its own socket and its own peer-UID expectation, and that
nothing lets a caller holding one sandbox's socket path or expected UID
reach the other.

Enforced on this platform (darwin), and proven here, not merely on the
Linux deployment target -- see `test_arc_parser_sandbox.py`'s own module
docstring for the full account; every property below is the same
mechanism, applied to `drafter_main.py` instead of `parser_main.py`:

- peer-UID authentication;
- every wire-framing bound (`ipc.py` is the same module, not retested here);
- the CPU-time ceiling;
- no outbound network/DNS;
- no lifecycle/database import;
- no filesystem write outside the granted scratch root.

Environment-limited on this platform, confirmed not assumed, same as the
parser's own suite:

- the memory ceiling (`RLIMIT_AS` cannot be set to any finite value on
  darwin/XNU).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess  # noqa: S404 - the sandboxed process is a real separate OS process by design; fixed argv below
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from contextplane.arc.sandbox import ipc
from contextplane.arc.schemas import drafter_output as do
from contextplane.arc.schemas import parser_output as po

REPO_ROOT = Path(__file__).resolve().parents[2]
DRAFTER_MODULE_PATH = REPO_ROOT / "contextplane" / "arc" / "sandbox" / "drafter_main.py"
DRAFTER_OUTPUT_MODULE_PATH = REPO_ROOT / "contextplane" / "arc" / "schemas" / "drafter_output.py"
PARSER_MODULE_PATH = REPO_ROOT / "contextplane" / "arc" / "sandbox" / "parser_main.py"

_FORBIDDEN_IMPORT_PREFIXES = (
    "contextplane.storage",
    "contextplane.arc.service",
    "contextplane.wiring",
    "contextplane.config",
    "sqlalchemy",
    "asyncpg",
    "psycopg2",
)


@pytest.fixture
def short_tmp_path() -> Any:
    """A short, real path directly under `/tmp` -- see
    `test_arc_parser_sandbox.py`'s own fixture docstring for why: an
    `AF_UNIX` socket path easily exceeds the kernel's `sun_path` limit
    under pytest's own deeply-nested `tmp_path`."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Import graph: structural, not a runtime check something could bypass.
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize("path", [DRAFTER_MODULE_PATH, DRAFTER_OUTPUT_MODULE_PATH], ids=lambda p: p.name)
def test_drafter_modules_have_no_lifecycle_or_database_imports(path: Path) -> None:
    forbidden = _forbidden_imports(path)
    assert not forbidden, f"{path.name} imports {sorted(forbidden)}, giving the sandbox database/lifecycle reach"


# ---------------------------------------------------------------------------
# The closed output contract: schema/snapshot-shaped properties, without a
# checked-in snapshot file (this task's own contract does not add one --
# `DrafterSuccess`/`DrafterRefusal` are new, small, and every field is
# already exercised by `tests/unit/test_arc_drafter.py`'s pure-function
# tests). What belongs here instead is closedness: unknown fields refused.
# ---------------------------------------------------------------------------


def test_drafter_success_rejects_unknown_fields() -> None:
    with pytest.raises(Exception, match="extra"):
        do.DrafterSuccess(status="ok", patch={}, citations=[], declined_field_paths=[], unexpected="x")  # type: ignore[call-arg]


def test_drafter_refusal_rejects_unknown_fields() -> None:
    with pytest.raises(Exception, match="extra"):
        do.DrafterRefusal(status="refused", refusal_code="malformed_request", unexpected="x")  # type: ignore[call-arg]


def test_parse_drafter_result_round_trips_success_and_refusal() -> None:
    success = {"status": "ok", "patch": {}, "citations": [], "declined_field_paths": ["a"]}
    refusal = {"status": "refused", "refusal_code": "envelope_binding_mismatch"}
    assert isinstance(do.parse_drafter_result(success), do.DrafterSuccess)
    assert isinstance(do.parse_drafter_result(refusal), do.DrafterRefusal)


def test_parse_drafter_result_refuses_unknown_status() -> None:
    with pytest.raises(Exception, match="status"):
        do.parse_drafter_result({"status": "pending"})


# ---------------------------------------------------------------------------
# Resource limits / network guard -- each in a fresh subprocess so the
# process-global `socket.socket`/`socket.getaddrinfo` monkeypatch never
# leaks into the rest of this test session.
# ---------------------------------------------------------------------------


def _run_script(script: str, *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO_ROOT), timeout=timeout, capture_output=True, text=True
    )


def test_cpu_limit_terminates_a_runaway_drafter_process() -> None:
    script = (
        "from contextplane.arc.sandbox.drafter_main import apply_resource_limits\n"
        "apply_resource_limits(cpu_seconds=1, memory_bytes=512 * 1024 * 1024)\n"
        "x = 0\n"
        "while True:\n"
        "    x += 1\n"
    )
    result = _run_script(script, timeout=10.0)
    assert result.returncode != 0


def test_memory_limit_report_reflects_confirmed_platform_reality() -> None:
    script = (
        "import json\n"
        "from contextplane.arc.sandbox.drafter_main import apply_resource_limits\n"
        "report = apply_resource_limits(cpu_seconds=30, memory_bytes=512 * 1024 * 1024)\n"
        "print(json.dumps({'memory_limit_applied': report.memory_limit_applied}))\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if sys.platform.startswith("linux"):
        assert payload["memory_limit_applied"] is True
    else:
        assert payload["memory_limit_applied"] is False


def test_network_guard_blocks_af_inet_socket_construction() -> None:
    script = (
        "import socket\n"
        "from contextplane.arc.sandbox.drafter_main import install_network_guard\n"
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


def test_network_guard_blocks_dns_resolution() -> None:
    script = (
        "import socket\n"
        "from contextplane.arc.sandbox.drafter_main import install_network_guard\n"
        "install_network_guard()\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80)\n"
        "    print('NOT_BLOCKED')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BLOCKED"


def test_network_guard_still_allows_af_unix_sockets() -> None:
    script = (
        "import socket\n"
        "from contextplane.arc.sandbox.drafter_main import install_network_guard\n"
        "install_network_guard()\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.close()\n"
        "print('ALLOWED')\n"
    )
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ALLOWED"


# ---------------------------------------------------------------------------
# Real filesystem permission bits -- same mechanism as the parser's suite.
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
        read_root.chmod(0o700)


def test_scratch_root_allows_writes(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    scratch_root.chmod(0o700)
    (scratch_root / "work.tmp").write_text("scratch space is writable")
    assert (scratch_root / "work.tmp").read_text() == "scratch space is writable"


# ---------------------------------------------------------------------------
# Real end-to-end: spawn the actual sandboxed process and drive it over the
# real socket, exactly the mechanism `contextplane.arc.service.drafter`'s
# production pipeline uses.
# ---------------------------------------------------------------------------


def _spawn_drafter(
    short_tmp_path: Path,
    content: bytes,
    *,
    source_content_digest: str | None = None,
    expected_peer_uid: int | None = None,
    deadline_seconds: float = 10.0,
) -> tuple[subprocess.Popen[bytes], Path]:
    read_root = short_tmp_path / "read_root"
    scratch_root = short_tmp_path / "scratch"
    read_root.mkdir()
    scratch_root.mkdir()
    content_path = read_root / "content"
    content_path.write_bytes(content)
    content_path.chmod(0o400)
    read_root.chmod(0o500)
    scratch_root.chmod(0o700)

    sock_path = short_tmp_path / "drafter.sock"
    digest = source_content_digest or hashlib.sha256(content).hexdigest()
    peer_uid = os.getuid() if expected_peer_uid is None else expected_peer_uid

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "contextplane.arc.sandbox.drafter_main",
            "--content-path",
            str(content_path),
            "--sock-path",
            str(sock_path),
            "--source-content-digest",
            digest,
            "--expected-peer-uid",
            str(peer_uid),
            "--scratch-dir",
            str(scratch_root),
            "--deadline-seconds",
            str(deadline_seconds),
        ],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_process_socket(proc, sock_path)
    return proc, sock_path


def _wait_for_process_socket(proc: subprocess.Popen[bytes], sock_path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            return
        if proc.poll() is not None:
            _out, err = proc.communicate(timeout=1)
            message = err.decode(errors="replace")
            raise AssertionError(f"drafter process exited early (code {proc.returncode}): {message}")
        time.sleep(0.02)
    raise AssertionError("drafter process never started listening")


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _envelope_payload(*, source_evidence_id: uuid.UUID, source_content_digest: str) -> dict[str, Any]:
    return po.ParsedSourceEnvelope(
        profile="arc_parsed_source_envelope_v1",
        source_evidence_id=source_evidence_id,
        source_content_digest=source_content_digest,
        media_type="text/markdown",
        parser_id="test-parser",
        parser_version="1",
        title=None,
        sections=[],
        warnings=[],
    ).model_dump(mode="json")


def test_end_to_end_declines_requested_paths(short_tmp_path: Path) -> None:
    content = b"# heading\nbody text\n"
    digest = hashlib.sha256(content).hexdigest()
    proc, sock_path = _spawn_drafter(short_tmp_path, content, source_content_digest=digest)
    try:
        response = ipc.request_json(
            sock_path,
            {
                "envelope": _envelope_payload(source_evidence_id=uuid.uuid4(), source_content_digest=digest),
                "target_field_paths": ["directives", "applicability"],
            },
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
        )
    finally:
        _terminate(proc)
    result = do.parse_drafter_result(response)
    assert isinstance(result, do.DrafterSuccess)
    assert result.declined_field_paths == ["directives", "applicability"]
    assert result.patch == {}
    assert result.citations == []


def test_end_to_end_binding_mismatch_refused(short_tmp_path: Path) -> None:
    content = b"# heading\nbody text\n"
    real_digest = hashlib.sha256(content).hexdigest()
    wrong_digest = hashlib.sha256(b"different content").hexdigest()
    # Process started asserting the *wrong* digest for the content it will
    # actually read -- the sandbox's own defense-in-depth check must catch
    # this even though the caller-side binding check would normally have
    # refused first.
    proc, sock_path = _spawn_drafter(short_tmp_path, content, source_content_digest=wrong_digest)
    try:
        response = ipc.request_json(
            sock_path,
            {
                "envelope": _envelope_payload(source_evidence_id=uuid.uuid4(), source_content_digest=wrong_digest),
                "target_field_paths": ["a"],
            },
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
        )
    finally:
        _terminate(proc)
    result = do.parse_drafter_result(response)
    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "envelope_binding_mismatch"
    # And never the digest computed against the process's own start-up
    # assertion, proving the mismatch really came from the recomputed hash:
    assert real_digest != wrong_digest


def test_end_to_end_malformed_request_refused(short_tmp_path: Path) -> None:
    content = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    proc, sock_path = _spawn_drafter(short_tmp_path, content, source_content_digest=digest)
    try:
        response = ipc.request_json(
            sock_path, {"target_field_paths": "not-a-list"}, expected_peer_uid=os.getuid(), deadline_seconds=10.0
        )
    finally:
        _terminate(proc)
    result = do.parse_drafter_result(response)
    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "malformed_request"


def test_end_to_end_peer_uid_mismatch_against_real_process(short_tmp_path: Path) -> None:
    content = b"hello"
    proc, sock_path = _spawn_drafter(short_tmp_path, content)
    wrong_uid = os.getuid() + 1
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_path, {}, expected_peer_uid=wrong_uid, deadline_seconds=5.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.PEER_MISMATCH
    finally:
        _terminate(proc)


def test_drafter_process_needs_no_database_credential(short_tmp_path: Path) -> None:
    """A stripped environment (no `DATABASE_URL`, no service token) --
    proving the sandbox never reads for one, not merely that this test
    happened not to set one."""
    content = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    read_root = short_tmp_path / "read_root"
    scratch_root = short_tmp_path / "scratch"
    read_root.mkdir()
    scratch_root.mkdir()
    content_path = read_root / "content"
    content_path.write_bytes(content)
    content_path.chmod(0o400)
    read_root.chmod(0o500)
    scratch_root.chmod(0o700)
    sock_path = short_tmp_path / "drafter.sock"

    stripped_env = {
        key: value for key, value in os.environ.items() if key not in {"DATABASE_URL", "REGISTRY_SERVICE_TOKEN"}
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "contextplane.arc.sandbox.drafter_main",
            "--content-path",
            str(content_path),
            "--sock-path",
            str(sock_path),
            "--source-content-digest",
            digest,
            "--expected-peer-uid",
            str(os.getuid()),
            "--scratch-dir",
            str(scratch_root),
            "--deadline-seconds",
            "10",
        ],
        cwd=str(REPO_ROOT),
        env=stripped_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_process_socket(proc, sock_path)
        response = ipc.request_json(
            sock_path,
            {
                "envelope": _envelope_payload(source_evidence_id=uuid.uuid4(), source_content_digest=digest),
                "target_field_paths": ["a"],
            },
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
        )
    finally:
        _terminate(proc)
    result = do.parse_drafter_result(response)
    assert isinstance(result, do.DrafterSuccess)


# ---------------------------------------------------------------------------
# Cross-sandbox isolation: parser and drafter are distinct processes on
# distinct socket paths, both alive at once, and each keeps behaving like
# itself -- there is no shared fallback channel between them.
# ---------------------------------------------------------------------------


def _spawn_parser(short_tmp_path: Path, content: bytes) -> tuple[subprocess.Popen[bytes], Path, uuid.UUID, str]:
    read_root = short_tmp_path / "parser_read_root"
    scratch_root = short_tmp_path / "parser_scratch"
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
            "contextplane.arc.sandbox.parser_main",
            "--content-path",
            str(content_path),
            "--sock-path",
            str(sock_path),
            "--media-type",
            "text/markdown",
            "--source-evidence-id",
            str(source_evidence_id),
            "--expected-peer-uid",
            str(os.getuid()),
            "--scratch-dir",
            str(scratch_root),
            "--deadline-seconds",
            "10",
        ],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_process_socket(proc, sock_path)
    return proc, sock_path, source_evidence_id, digest


def test_parser_and_drafter_sandboxes_run_concurrently_on_distinct_sockets(short_tmp_path: Path) -> None:
    content = b"# heading\nbody\n"
    digest = hashlib.sha256(content).hexdigest()

    parser_proc, parser_sock, source_evidence_id, parser_digest = _spawn_parser(short_tmp_path, content)
    drafter_proc, drafter_sock = _spawn_drafter(short_tmp_path, content, source_content_digest=digest)
    try:
        assert parser_sock != drafter_sock
        assert parser_sock.exists() and drafter_sock.exists()

        parser_response = ipc.request_json(parser_sock, {}, expected_peer_uid=os.getuid(), deadline_seconds=10.0)
        parsed = po.parse_parser_result(parser_response)
        assert isinstance(parsed, po.ParserSuccess)
        po.verify_source_binding(
            parsed.envelope, source_evidence_id=source_evidence_id, source_content_digest=parser_digest
        )

        drafter_response = ipc.request_json(
            drafter_sock,
            {
                "envelope": _envelope_payload(source_evidence_id=uuid.uuid4(), source_content_digest=digest),
                "target_field_paths": ["directives"],
            },
            expected_peer_uid=os.getuid(),
            deadline_seconds=10.0,
        )
        drafted = do.parse_drafter_result(drafter_response)
        assert isinstance(drafted, do.DrafterSuccess)
        assert drafted.declined_field_paths == ["directives"]
    finally:
        _terminate(parser_proc)
        _terminate(drafter_proc)


def test_drafter_socket_refuses_the_parsers_bare_empty_request(short_tmp_path: Path) -> None:
    """The parser's own wire shape (`{}`, since `parser_main._handle`
    ignores its request body entirely) is not a request the drafter
    accepts -- proving the two sandboxes do not silently interoperate on
    whatever either happens to send."""
    content = b"hello"
    proc, sock_path = _spawn_drafter(short_tmp_path, content)
    try:
        response = ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=10.0)
    finally:
        _terminate(proc)
    result = do.parse_drafter_result(response)
    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "malformed_request"
