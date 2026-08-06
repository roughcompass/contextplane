"""The one Unix-domain-socket transport every ARC sandbox subprocess speaks.

A sandboxed process (today: `parser_main.py`; a future model-backed drafter
sandbox reuses this module unchanged) never accepts a bearer token or any
other reusable credential -- the wire protocol authenticates each
connection by asking the kernel who the peer actually is
(`get_peer_uid()`), and that is the whole authentication story. TCP, HTTP,
and stdin/stdout are deliberately not alternatives here: a Unix-domain
socket is the only transport the kernel can attach a peer identity to
without either side presenting anything.

The wire format is one request and one response per connection, each
framed by a four-byte unsigned big-endian length prefix followed by UTF-8
JSON. `DEFAULT_MAX_FRAME_BYTES` bounds a single frame; the length prefix is
checked against that ceiling *before* any body byte is read, so a frame
that lies about its own size cannot make either side buffer an unbounded
amount of memory. `DEFAULT_DEADLINE_SECONDS` bounds the whole round trip on
the client side: past the deadline, the client closes the socket and
raises rather than continuing to wait, discarding anything partially read.

Every failure this module can raise is one closed `SandboxRefusalCode` --
authentication mismatch, an oversize/truncated/malformed frame, unexpected
trailing data, a deadline, the sandboxed process never having been
reachable at all, or (via the caller-supplied `validate_response` hook) a
schema-valid response bound to the wrong thing. A refusal here carries only
that code and a short, bounded, operator-facing message -- never a
fragment of whatever bytes were actually on the wire, and never the
content either endpoint was handling.

Platform note. The ADR's normative deployment authenticates the peer via
`SO_PEERCRED` (Linux only). This module also implements the BSD/macOS
equivalent, `getpeereid(2)` via `ctypes` -- not as a lesser fallback, but
so the *same* authentication code path (`get_peer_uid`, `require_peer_uid`)
is genuinely exercised in local development and CI on either platform,
rather than only ever running on whichever platform CI happens to use.
Both return a real, kernel-verified peer UID; neither is a mock.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import enum
import json
import os
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

from registry.exceptions import RegistryError
from registry.types import JSONValue

_LENGTH_PREFIX_FORMAT = "!I"
_LENGTH_PREFIX_SIZE = struct.calcsize(_LENGTH_PREFIX_FORMAT)

#: The transport frame ceiling from the wire protocol. Distinct from
#: `registry.arc.schemas.parser_output.MAX_ENVELOPE_BYTES` (1 MiB): that
#: bounds the *validated* envelope inside a success response; this bounds
#: the raw frame carrying it, request or response, before any JSON parsing
#: or schema validation happens at all.
DEFAULT_MAX_FRAME_BYTES = 2 * 1024 * 1024  # 2 MiB

#: The default request/response deadline the client enforces.
DEFAULT_DEADLINE_SECONDS = 30.0

#: The mode every sandbox socket is created with. A dedicated OS group
#: (so only the API process and the one sandbox identity can even attempt
#: to open the path) is a deployment-time provisioning concern --
#: `install_group_ownership` below applies it best-effort when a group is
#: supplied, but creating that group in the first place is out of this
#: module's scope. The peer-UID check this module performs on every
#: connection is what "authenticated" actually means here regardless of
#: whether group provisioning is present.
SOCKET_MODE = 0o660


class SandboxRefusalCode(enum.StrEnum):
    """Transport-level refusals. Distinct from
    `registry.arc.schemas.parser_output.ParserRefusalCode`: that vocabulary
    is the sandboxed parser's own reasoning about the content it was asked
    to parse, decided only once a request has already been authenticated
    and read successfully. This one is the wire protocol's reasoning about
    the connection itself, and every value here can fire before any
    payload is ever handed to a parser.
    """

    PEER_MISMATCH = "sandbox_peer_mismatch"
    FRAME_TOO_LARGE = "sandbox_frame_too_large"
    FRAME_TRUNCATED = "sandbox_frame_truncated"
    FRAME_EXTRA_DATA = "sandbox_frame_extra_data"
    FRAME_MALFORMED = "sandbox_frame_malformed"
    DEADLINE_EXCEEDED = "sandbox_deadline_exceeded"
    PROCESS_UNAVAILABLE = "sandbox_process_unavailable"
    BINDING_MISMATCH = "sandbox_binding_mismatch"


class SandboxRefusal(RegistryError):
    """Raised by every function in this module that refuses a connection,
    a frame, or a response binding. Carries only `code` (one of
    `SandboxRefusalCode`) and a short, bounded message -- never source
    bytes, parser diagnostic text, or any fragment of what was actually
    received.
    """

    def __init__(self, code: SandboxRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Framing.
# ---------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise SandboxRefusal(
                SandboxRefusalCode.FRAME_TRUNCATED,
                "connection closed before the frame completed",
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> bytes:
    """Read exactly one length-prefixed frame from `sock`.

    The declared length is checked against `max_frame_bytes` before any
    body byte is read -- an oversize claim is refused immediately rather
    than by attempting to buffer it.
    """
    header = _recv_exact(sock, _LENGTH_PREFIX_SIZE)
    (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
    if length > max_frame_bytes:
        raise SandboxRefusal(
            SandboxRefusalCode.FRAME_TOO_LARGE,
            f"frame declares {length} bytes, exceeding the {max_frame_bytes} byte ceiling",
        )
    return _recv_exact(sock, length)


def write_frame(sock: socket.socket, payload: bytes, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
    """Write exactly one length-prefixed frame to `sock`."""
    if len(payload) > max_frame_bytes:
        # A local encoding bug, not a wire-protocol violation from a peer --
        # this side is the one about to emit an oversize frame.
        raise ValueError(f"refusing to send a {len(payload)}-byte frame over the {max_frame_bytes} byte ceiling")
    sock.sendall(struct.pack(_LENGTH_PREFIX_FORMAT, len(payload)) + payload)


def assert_no_trailing_data(sock: socket.socket) -> None:
    """Raise `SandboxRefusal(FRAME_EXTRA_DATA)` if `sock` has more bytes
    available beyond the one frame already read. The wire protocol is one
    request and one response per connection; anything else is refused
    rather than silently read and discarded.
    """
    previous_timeout = sock.gettimeout()
    sock.settimeout(0.05)
    try:
        extra = sock.recv(1)
    except (TimeoutError, OSError):
        return
    finally:
        sock.settimeout(previous_timeout)
    if extra:
        raise SandboxRefusal(SandboxRefusalCode.FRAME_EXTRA_DATA, "unexpected data after the one framed message")


# ---------------------------------------------------------------------------
# Peer-UID authentication.
# ---------------------------------------------------------------------------

_SO_PEERCRED_STRUCT_FORMAT = "3i"  # pid_t, uid_t, gid_t
_libc: ctypes.CDLL | None = None


def _load_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        library_path = ctypes.util.find_library("c") or "libc.so.6"
        _libc = ctypes.CDLL(library_path, use_errno=True)
    return _libc


def _peer_uid_linux(sock: socket.socket) -> int:
    # `SO_PEERCRED` is Linux-only; typeshed's `socket` stub does not declare
    # it unconditionally, so it is looked up dynamically rather than
    # referenced as a static attribute. The caller (`get_peer_uid`) only
    # reaches this function when `sys.platform.startswith("linux")`.
    so_peercred = getattr(socket, "SO_PEERCRED")  # noqa: B009 - conditionally-defined platform constant
    raw = sock.getsockopt(
        socket.SOL_SOCKET,
        so_peercred,
        struct.calcsize(_SO_PEERCRED_STRUCT_FORMAT),
    )
    _pid, uid, _gid = struct.unpack(_SO_PEERCRED_STRUCT_FORMAT, raw)
    return int(uid)


def _peer_uid_bsd(sock: socket.socket) -> int:
    libc = _load_libc()
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    rc = libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid))
    if rc != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(uid.value)


def get_peer_uid(sock: socket.socket) -> int:
    """The real, kernel-verified UID of whatever process is on the other
    end of `sock`. `SO_PEERCRED` on Linux, `getpeereid(2)` everywhere else
    POSIX (exercised in local development and CI on macOS) -- see the
    module docstring's platform note.
    """
    if sys.platform.startswith("linux"):
        return _peer_uid_linux(sock)
    return _peer_uid_bsd(sock)


def require_peer_uid(sock: socket.socket, expected_uid: int) -> None:
    """Raise `SandboxRefusal(PEER_MISMATCH)` unless the real peer UID of
    `sock` is exactly `expected_uid`.
    """
    actual_uid = get_peer_uid(sock)
    if actual_uid != expected_uid:
        raise SandboxRefusal(
            SandboxRefusalCode.PEER_MISMATCH,
            f"peer uid {actual_uid} does not match the required uid {expected_uid}",
        )


def install_group_ownership(sock_path: str | Path, group_gid: int) -> bool:
    """Best-effort `chown` of `sock_path` to `group_gid`, leaving the owning
    user unchanged. Returns whether it succeeded. Creating the dedicated
    group itself is a deployment-time concern (outside this module); a
    caller with no such group configured simply never calls this, and
    `SOCKET_MODE` plus the peer-UID check above are what actually
    authenticate a connection either way.
    """
    try:
        os.chown(str(sock_path), -1, group_gid)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Server side (used by parser_main.py; a future drafter sandbox reuses it).
# ---------------------------------------------------------------------------


def serve_one(
    sock_path: str | Path,
    handler: Callable[[dict[str, JSONValue]], dict[str, JSONValue]],
    *,
    expected_peer_uid: int,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    group_gid: int | None = None,
) -> None:
    """Bind `sock_path`, accept exactly one connection, authenticate its
    peer, read exactly one request frame, call `handler` with the parsed
    JSON object, write exactly one response frame, and return.

    Any transport-level failure before `handler` is reached (peer mismatch,
    oversize/truncated/malformed frame, accept timeout) closes the
    connection without a response -- the client's own read then observes
    that as its own transport-level refusal (truncation or a deadline),
    which is itself a bounded, correct outcome, not a missing case.
    """
    sock_path = Path(sock_path)
    if sock_path.exists():
        sock_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        sock_path.chmod(SOCKET_MODE)
        if group_gid is not None:
            install_group_ownership(sock_path, group_gid)
        server.listen(1)
        server.settimeout(deadline_seconds)
        try:
            conn, _peer_address = server.accept()
        except TimeoutError:
            raise SandboxRefusal(
                SandboxRefusalCode.DEADLINE_EXCEEDED,
                "no connection arrived before the deadline",
            ) from None
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()

    with conn:
        conn.settimeout(deadline_seconds)
        require_peer_uid(conn, expected_peer_uid)
        request_bytes = read_frame(conn, max_frame_bytes=max_frame_bytes)
        request = _decode_json_object(request_bytes)
        assert_no_trailing_data(conn)
        response = handler(request)
        response_bytes = json.dumps(response, ensure_ascii=False).encode("utf-8")
        write_frame(conn, response_bytes, max_frame_bytes=max_frame_bytes)


# ---------------------------------------------------------------------------
# Client side (used by whatever API-side code calls into a sandbox).
# ---------------------------------------------------------------------------


def request_json(
    sock_path: str | Path,
    payload: dict[str, JSONValue],
    *,
    expected_peer_uid: int,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    validate_response: Callable[[dict[str, JSONValue]], None] | None = None,
) -> dict[str, JSONValue]:
    """Connect to `sock_path`, authenticate its peer, send `payload` as the
    one request frame, and return the one response frame as a parsed JSON
    object.

    `validate_response`, if given, runs against the parsed response before
    it is returned; any exception it raises is wrapped into
    `SandboxRefusal(BINDING_MISMATCH)` -- the hook a caller uses to bind a
    schema-valid response to whatever it actually asked the sandbox about
    (see `registry.arc.schemas.parser_output.verify_source_binding`).
    """
    deadline = time.monotonic() + deadline_seconds
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(deadline_seconds)
        try:
            sock.connect(str(sock_path))
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as exc:
            raise SandboxRefusal(
                SandboxRefusalCode.PROCESS_UNAVAILABLE,
                "the sandboxed process is not accepting connections",
            ) from exc

        require_peer_uid(sock, expected_peer_uid)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        write_frame(sock, body, max_frame_bytes=max_frame_bytes)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxRefusal(SandboxRefusalCode.DEADLINE_EXCEEDED, "deadline exceeded before reading a response")
        sock.settimeout(remaining)
        try:
            response_bytes = read_frame(sock, max_frame_bytes=max_frame_bytes)
        except TimeoutError as exc:
            raise SandboxRefusal(
                SandboxRefusalCode.DEADLINE_EXCEEDED,
                "the sandboxed process did not respond before the deadline",
            ) from exc

        assert_no_trailing_data(sock)
        response = _decode_json_object(response_bytes)

        if validate_response is not None:
            try:
                validate_response(response)
            except SandboxRefusal:
                raise
            except Exception as exc:
                # exception shape (its own domain error, e.g. `ParserBindingError`); every
                # one is re-raised as a bounded `SandboxRefusal`, never swallowed.
                raise SandboxRefusal(SandboxRefusalCode.BINDING_MISMATCH, str(exc)) from exc

        return response
    finally:
        sock.close()


def _decode_json_object(raw: bytes) -> dict[str, JSONValue]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxRefusal(SandboxRefusalCode.FRAME_MALFORMED, "frame body is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise SandboxRefusal(SandboxRefusalCode.FRAME_MALFORMED, "frame body is not a JSON object")
    return decoded
