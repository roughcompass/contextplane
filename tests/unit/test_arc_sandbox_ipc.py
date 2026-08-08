"""Unit tests for the sandbox IPC transport's own mechanics
(`contextplane/arc/sandbox/ipc.py`).

`tests/conformance/test_arc_parser_sandbox.py` owns the exhaustive,
real-process half of this module: every misbehaving-peer scenario against
a genuine `AF_UNIX` socket file, the real sandboxed `parser_main.py`
subprocess, and the shipped shell wrapper. None of that is repeated here.
What this file adds is fast, no-DB coverage of the framing and
peer-authentication primitives in isolation -- using `socket.socketpair()`
(no filesystem socket path, no `sun_path` length limit to worry about, no
subprocess) for everything that does not specifically need a *named*
socket (`serve_one`/`request_json`'s bind/connect path), which gets one
representative round trip each here and its exhaustive edge-case coverage
in conformance.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from contextplane.arc.sandbox import ipc

# ---------------------------------------------------------------------------
# Framing primitives, over a real connected socket pair.
# ---------------------------------------------------------------------------


def test_write_frame_then_read_frame_round_trips() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ipc.write_frame(left, b"hello sandbox")
        assert ipc.read_frame(right) == b"hello sandbox"
    finally:
        left.close()
        right.close()


def test_write_frame_refuses_to_send_past_its_own_ceiling() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValueError, match="ceiling"):
            ipc.write_frame(left, b"x" * 10, max_frame_bytes=5)
    finally:
        left.close()
        right.close()


def test_read_frame_refuses_an_oversize_declared_length() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(struct.pack("!I", ipc.DEFAULT_MAX_FRAME_BYTES + 1))  # never sends a body
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.read_frame(right)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_TOO_LARGE
    finally:
        left.close()
        right.close()


def test_read_frame_refuses_a_truncated_frame() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(struct.pack("!I", 100) + b"short")
        left.close()  # EOF before the promised 100 bytes arrive
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.read_frame(right)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_TRUNCATED
    finally:
        right.close()


def test_assert_no_trailing_data_passes_when_socket_is_quiet() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ipc.assert_no_trailing_data(right)  # nothing sent; must not raise
    finally:
        left.close()
        right.close()


def test_assert_no_trailing_data_refuses_extra_bytes() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(b"unexpected")
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.assert_no_trailing_data(right)
        assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_EXTRA_DATA
    finally:
        left.close()
        right.close()


def test_decode_json_object_refuses_malformed_json() -> None:
    with pytest.raises(ipc.SandboxRefusal) as exc_info:
        ipc._decode_json_object(b"{not valid")
    assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_MALFORMED


def test_decode_json_object_refuses_a_non_object() -> None:
    with pytest.raises(ipc.SandboxRefusal) as exc_info:
        ipc._decode_json_object(json.dumps([1, 2]).encode("utf-8"))
    assert exc_info.value.code == ipc.SandboxRefusalCode.FRAME_MALFORMED


def test_decode_json_object_accepts_a_plain_object() -> None:
    assert ipc._decode_json_object(json.dumps({"a": 1}).encode("utf-8")) == {"a": 1}


# ---------------------------------------------------------------------------
# Peer-UID authentication -- a real kernel-verified UID (via socketpair,
# both ends of which are this same process), not a mock.
# ---------------------------------------------------------------------------


def test_get_peer_uid_returns_this_process_own_uid() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert ipc.get_peer_uid(right) == os.getuid()
    finally:
        left.close()
        right.close()


def test_require_peer_uid_accepts_the_real_uid() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ipc.require_peer_uid(right, os.getuid())
        # Reached only if the call above did not raise -- the behavior under test.
        assert ipc.get_peer_uid(right) == os.getuid()
    finally:
        left.close()
        right.close()


def test_require_peer_uid_refuses_a_wrong_uid() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.require_peer_uid(right, os.getuid() + 999_999)
        assert exc_info.value.code == ipc.SandboxRefusalCode.PEER_MISMATCH
    finally:
        left.close()
        right.close()


def test_sandbox_refusal_carries_its_code_and_message() -> None:
    refusal = ipc.SandboxRefusal(ipc.SandboxRefusalCode.DEADLINE_EXCEEDED, "took too long")
    assert refusal.code == ipc.SandboxRefusalCode.DEADLINE_EXCEEDED
    assert str(refusal) == "took too long"


def test_install_group_ownership_reports_failure_honestly() -> None:
    sock_dir = _short_socket_dir()
    sock_path = sock_dir / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        assert ipc.install_group_ownership(sock_path, 999_999) is False
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# One representative full round trip through the named-socket path
# (`serve_one` + `request_json`) -- the exhaustive edge-case matrix over a
# real bound socket lives in conformance.
# ---------------------------------------------------------------------------


def _short_socket_dir() -> Path:
    return Path(tempfile.mkdtemp(dir="/tmp"))


def test_serve_one_and_request_json_round_trip() -> None:
    sock_dir = _short_socket_dir()
    sock_path = sock_dir / "s.sock"
    try:

        def _handle(request: dict[str, Any]) -> dict[str, Any]:
            assert request == {}
            return {"status": "ok"}

        thread = threading.Thread(
            target=ipc.serve_one,
            args=(sock_path, _handle),
            kwargs={"expected_peer_uid": os.getuid(), "deadline_seconds": 3.0},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not sock_path.exists():
            time.sleep(0.01)
        response = ipc.request_json(sock_path, {}, expected_peer_uid=os.getuid(), deadline_seconds=3.0)
        assert response == {"status": "ok"}
        thread.join(timeout=3)
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_request_json_refuses_when_nothing_listening() -> None:
    sock_dir = _short_socket_dir()
    try:
        with pytest.raises(ipc.SandboxRefusal) as exc_info:
            ipc.request_json(sock_dir / "nobody.sock", {}, expected_peer_uid=os.getuid(), deadline_seconds=1.0)
        assert exc_info.value.code == ipc.SandboxRefusalCode.PROCESS_UNAVAILABLE
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
