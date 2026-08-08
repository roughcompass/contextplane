"""The sandboxed source-parser entrypoint.

Run via ``python -m registry.arc.sandbox.parser_main`` -- one process per
parse job, serving exactly one connection on ``ipc.serve_one`` before
exiting. ``scripts/run_parser_sandbox.sh`` wraps the full local invocation
(provisioning the read-only content root, the writable scratch root, and
the socket path) for manual testing; the shipped deployment descriptors
(Dockerfile, Helm chart) run the same entrypoint under a dedicated,
unprivileged service identity inside a container with its own network and
filesystem isolation -- this module assumes that isolation is present
rather than building it.

This module never imports ``registry.storage``, ``registry.arc.service``,
``registry.wiring``, or ``registry.config`` -- it has no database
credential, no service token, and no lifecycle authority, structurally
rather than by a runtime check that something could bypass. See
``tests/conformance/test_arc_parser_sandbox.py``'s import-graph walk.

What this process applies to itself, and what is honestly out of reach on
every platform it might run on locally:

- **CPU time.** ``apply_resource_limits`` calls ``resource.setrlimit(RLIMIT_CPU,
  ...)`` before any content is read. Confirmed on this platform (darwin):
  the kernel delivers ``SIGXCPU`` and the process dies at the limit,
  every time, not merely in theory.
- **Wall clock.** Enforced by the *caller*, not here: ``ipc.serve_one``'s
  deadline closes the socket and the caller discards partial output. This
  process has no separate wall-clock self-limit because the caller's
  deadline already bounds how long anyone waits on it.
- **Memory.** ``resource.setrlimit(RLIMIT_AS, ...)`` is the Linux
  deployment's real, kernel-enforced ceiling. On darwin it is not merely
  weaker -- ``setrlimit`` refuses to set *any* finite ``RLIMIT_AS`` or
  ``RLIMIT_DATA`` at all (confirmed empirically: ``ValueError: current
  limit exceeds maximum limit`` for every value tried, and ``ulimit -v``
  fails the same way from a plain shell on this machine). This is a real,
  environment-limited gap, not a weaker version of the same guarantee:
  `apply_resource_limits` reports whether it actually took effect, and
  `start_memory_watchdog` below is a cooperative, non-preemptive fallback
  -- it can catch a process that overshoots and self-exit, but it cannot
  prevent an allocation from happening in the first place the way a real
  kernel ceiling would. The output-size ceiling this process's own
  response is subject to (``parser_output.MAX_ENVELOPE_BYTES``, plus the
  transport frame ceiling in ``ipc.py``) is the bound that reliably fires
  on every platform, and is why the sandboxed parser's practical safety
  does not rest on the memory ceiling alone.
- **CPU core count.** The "one CPU" resource limit is a scheduling
  allocation, not a time bound -- normally a container runtime's cgroup
  concern, not something a single process sets on itself.
  ``os.sched_setaffinity`` does not exist on darwin at all (confirmed:
  ``hasattr`` is false), and even on Linux it is unrelated to
  ``RLIMIT_CPU``. This process does not claim to enforce core-count
  allocation; it is out of reach at this layer on either platform.
- **Filesystem.** This process only ever opens the one content path it was
  given, read-only, and never writes outside the scratch root it was
  given. The real, kernel-enforced half of that guarantee is the caller's
  job: ``scripts/run_parser_sandbox.sh`` provisions the content root at
  mode ``0500`` (so a write anywhere under it fails with a genuine
  ``PermissionError`` regardless of what this process's own code does) and
  a separate, writable scratch root. What is *not* enforced at this layer
  on either platform without a container's mount namespace is confinement
  of *reads* to only the granted path -- this process could, by a bug,
  open some other world-readable file on the host. Closing that requires a
  read-only root filesystem at the container layer, which this module does
  not build.
- **Network.** ``install_network_guard`` replaces ``socket.socket`` and
  ``socket.getaddrinfo`` for the remainder of this process's life so that
  constructing any non-Unix-domain socket, or resolving any hostname,
  raises immediately -- proven in the conformance suite by demonstrating
  the *specific*, immediate ``PermissionError`` the guard raises, not by
  relying on this test environment happening to have no route to the
  internet (it does not, but that would prove nothing about this code).
  This is a real, tested, portable control -- but it is a process-level
  guard, not a kernel-level one: a mount-namespace/seccomp-level network
  denial (the Linux deployment's actual boundary, enforced by the
  container runtime rather than this process) does not depend on this
  process's own Python-level state the way this guard necessarily does.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import resource
import socket
import sys
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from registry.arc.sandbox import ipc
from registry.arc.schemas.parser_output import (
    MAX_SECTIONS,
    MAX_TEXT_LENGTH,
    ParsedSourceEnvelope,
    ParsedSourceSection,
    ParsedSourceWarning,
    ParserRefusal,
    ParserSuccess,
)
from registry.types import JSONValue

log = logging.getLogger(__name__)

#: The only media type this parser implementation understands. A later,
#: media-type-specific parser adds its own accepted value here without
#: changing `ParsedSourceEnvelope`'s field set (see the module docstring's
#: non-goal note in the task this module ships under).
SUPPORTED_MEDIA_TYPE = "text/markdown"

PARSER_ID = "arc_markdown_parser"
PARSER_VERSION = "1"

DEFAULT_CPU_SECONDS = 30
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024  # 512 MiB

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


# ---------------------------------------------------------------------------
# Resource limits.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimitReport:
    """What `apply_resource_limits` actually managed to apply. Logged, and
    asserted against directly in the conformance suite, so a platform gap
    is a recorded fact rather than a silent assumption.
    """

    cpu_seconds: int
    memory_bytes_requested: int
    memory_limit_applied: bool
    memory_limit_note: str


def apply_resource_limits(
    *,
    cpu_seconds: int = DEFAULT_CPU_SECONDS,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
) -> ResourceLimitReport:
    """Apply this process's own CPU-time ceiling, and attempt a memory
    ceiling. See the module docstring for why the two have different
    guarantees on this platform.
    """
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError):
        return ResourceLimitReport(
            cpu_seconds=cpu_seconds,
            memory_bytes_requested=memory_bytes,
            memory_limit_applied=False,
            memory_limit_note=(
                "RLIMIT_AS could not be set on this platform (confirmed on darwin/XNU: "
                "setrlimit raises for any finite value, matching a plain `ulimit -v` "
                "failing the same way outside Python); relying on the best-effort RSS "
                "watchdog and the output-size ceiling instead of a kernel memory limit"
            ),
        )
    return ResourceLimitReport(
        cpu_seconds=cpu_seconds,
        memory_bytes_requested=memory_bytes,
        memory_limit_applied=True,
        memory_limit_note="RLIMIT_AS applied",
    )


def _current_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports ru_maxrss in kilobytes; darwin reports it in bytes.
    # Both platforms are handled explicitly rather than guessed at, since
    # a wrong unit here would make the watchdog either fire immediately or
    # never fire at all.
    return usage * 1024 if sys.platform.startswith("linux") else usage


def start_memory_watchdog(memory_bytes: int, *, poll_interval: float = 0.2) -> threading.Event:
    """Start a daemon thread that self-exits this process if its own RSS
    ever exceeds `memory_bytes`. This is cooperative, not preemptive: it
    can only catch an overshoot after the fact, on the next poll, not
    prevent the allocation that caused it. It runs on every platform as a
    backstop; where `apply_resource_limits` reports a real `RLIMIT_AS`
    ceiling (Linux), the kernel is expected to act first.

    Returns the `threading.Event` the caller sets to stop the watchdog
    cleanly at shutdown.
    """
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(poll_interval):
            if _current_rss_bytes() > memory_bytes:
                log.error("sandboxed parser exceeded its memory ceiling; self-terminating")
                os._exit(137)

    thread = threading.Thread(target=_watch, name="parser-sandbox-memory-watchdog", daemon=True)
    thread.start()
    return stop


# ---------------------------------------------------------------------------
# Network guard.
#
# There is no kernel-level network namespace available to a single
# unprivileged process on either target platform without a container
# runtime (the Linux deployment's actual mechanism, out of this module's
# scope). This is the portable, always-on substitute: replace the two
# entry points anything in this process would have to go through to reach
# the network -- constructing a non-Unix-domain socket, or resolving a
# hostname -- with a version that refuses immediately. `ipc.py`'s own
# AF_UNIX sockets are unaffected; only outbound network address families
# are denied.
# ---------------------------------------------------------------------------

_ALLOWED_SOCKET_FAMILIES = frozenset({socket.AF_UNIX})
_real_socket_ctor = socket.socket


def _guarded_socket_ctor(
    family: socket.AddressFamily | int = socket.AF_INET,
    socket_type: socket.SocketKind | int = socket.SOCK_STREAM,
    proto: int = 0,
    fileno: int | None = None,
) -> socket.socket:
    if family not in _ALLOWED_SOCKET_FAMILIES:
        raise PermissionError(f"sandboxed parser process refused to open a socket of family {family!r}")
    return _real_socket_ctor(family, socket_type, proto, fileno)


def _deny_getaddrinfo(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("sandboxed parser process refused DNS resolution")


def install_network_guard() -> None:
    """Deny every outbound-network entry point for the rest of this
    process's life. See the section docstring above for exactly what this
    does and does not close.
    """
    socket.socket = _guarded_socket_ctor  # type: ignore[assignment,misc]
    socket.getaddrinfo = _deny_getaddrinfo  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Markdown parsing -- pure, no I/O, no guard state. Unit-testable directly.
# ---------------------------------------------------------------------------


class _TooManySectionsError(Exception):
    """Internal signal: the document would exceed `MAX_SECTIONS` sections."""


def _split_sections(
    text: str,
) -> tuple[str | None, list[ParsedSourceSection], list[ParsedSourceWarning]]:
    """Split `text` into ordered sections at each Markdown heading line.

    Text preceding the first heading, if non-blank, becomes an anchorless
    leading section. Raises `_TooManySectionsError` the moment a
    (MAX_SECTIONS + 1)-th heading-triggered section would start, without
    processing the rest of the document -- a defensive early exit; the
    model's own `MAX_SECTIONS` check in `ParsedSourceEnvelope` remains the
    authoritative ceiling regardless.
    """
    lines = text.splitlines()
    # Two passes: first split into raw (heading, anchor, body_lines) groups
    # with no bound checks beyond the section-count guard, then build the
    # closed `ParsedSourceSection` list, dropping an empty leading group and
    # applying per-field truncation with its matching warning.
    raw_sections: list[tuple[str | None, str, list[str]]] = []
    title: str | None = None
    heading_count = 0

    for line_number, line in enumerate(lines, start=1):
        match = _HEADING_PATTERN.match(line)
        if match is not None:
            if heading_count + 1 > MAX_SECTIONS:
                raise _TooManySectionsError
            heading_count += 1
            level, heading_text = match.groups()
            if title is None and len(level) == 1:
                title = heading_text[:500]
            raw_sections.append((heading_text, f"line-{line_number}", []))
            continue
        if not raw_sections:
            raw_sections.append((None, "line-1", []))
        raw_sections[-1][2].append(line)

    sections: list[ParsedSourceSection] = []
    warnings: list[ParsedSourceWarning] = []
    ordinal = 0
    for heading, anchor, body_lines in raw_sections:
        body = "\n".join(body_lines).strip("\n")
        if heading is None and not body.strip():
            continue  # nothing precedes the first heading
        if len(body) > MAX_TEXT_LENGTH:
            body = body[:MAX_TEXT_LENGTH]
            warnings.append(ParsedSourceWarning(code="truncated", source_anchor=anchor))
        if heading is not None and len(heading) > 500:
            heading = heading[:500]
            warnings.append(ParsedSourceWarning(code="truncated", source_anchor=anchor))
        sections.append(
            ParsedSourceSection(
                section_id=f"section-{ordinal}",
                source_anchor=anchor,
                ordinal=ordinal,
                heading=heading,
                text=body,
                excerpt_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
        ordinal += 1

    return title, sections, warnings


def parse_markdown(
    content: bytes,
    *,
    media_type: str,
    source_evidence_id: uuid.UUID,
) -> ParserSuccess | ParserRefusal:
    """The parser's own business logic: pure, no filesystem or socket I/O.
    `parser_main.main()` is the only caller that supplies bytes actually
    read from the sandboxed content path; every conformance test below
    calls this function directly with in-memory bytes.
    """
    if media_type != SUPPORTED_MEDIA_TYPE:
        return ParserRefusal(status="refused", refusal_code="unsupported_media_type")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ParserRefusal(status="refused", refusal_code="malformed_source")

    if not text.strip():
        return ParserRefusal(status="refused", refusal_code="malformed_source")

    try:
        title, sections, warnings = _split_sections(text)
    except _TooManySectionsError:
        return ParserRefusal(status="refused", refusal_code="source_too_complex")

    digest = hashlib.sha256(content).hexdigest()
    try:
        envelope = ParsedSourceEnvelope(
            profile="arc_parsed_source_envelope_v1",
            source_evidence_id=source_evidence_id,
            source_content_digest=digest,
            media_type=media_type,
            parser_id=PARSER_ID,
            parser_version=PARSER_VERSION,
            title=title,
            sections=sections,
            warnings=warnings,
        )
    except ValidationError:
        return ParserRefusal(status="refused", refusal_code="output_limit_exceeded")

    return ParserSuccess(status="ok", envelope=envelope)


# ---------------------------------------------------------------------------
# Entrypoint.
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARC sandboxed source parser")
    parser.add_argument("--content-path", required=True, help="path to the admitted, read-only content file")
    parser.add_argument("--sock-path", required=True, help="Unix-domain socket path to serve on")
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--source-evidence-id", required=True)
    parser.add_argument("--expected-peer-uid", required=True, type=int)
    parser.add_argument("--scratch-dir", required=True, help="writable isolated scratch directory")
    parser.add_argument("--deadline-seconds", type=float, default=ipc.DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--cpu-seconds", type=int, default=DEFAULT_CPU_SECONDS)
    parser.add_argument("--memory-bytes", type=int, default=DEFAULT_MEMORY_BYTES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)

    report = apply_resource_limits(cpu_seconds=args.cpu_seconds, memory_bytes=args.memory_bytes)
    # The RSS watchdog is the *fallback* for platforms that refuse a kernel
    # memory ceiling, not a belt-and-braces addition to one. Starting it when
    # `RLIMIT_AS` did apply is worse than redundant: the limit caps virtual
    # address space, a new thread has to map a stack (8 MiB by default) inside
    # that cap, and the allocation fails -- so the watchdog thread aborts the
    # very process it exists to protect, at startup, before any content is
    # read. That is why this is conditional. It was not caught on the
    # development platform for the reason that makes the bug interesting:
    # darwin refuses to set `RLIMIT_AS` at all, so the watchdog only ever ran
    # where nothing constrained it.
    stop_watchdog: threading.Event | None = None
    if report.memory_limit_applied:
        log.info("memory ceiling enforced by the kernel: %s", report.memory_limit_note)
    else:
        log.warning("memory ceiling not kernel-enforced on this platform: %s", report.memory_limit_note)
        stop_watchdog = start_memory_watchdog(args.memory_bytes)
    install_network_guard()

    content_path = Path(args.content_path)
    with content_path.open("rb") as handle:
        content = handle.read()

    # Isolated bounded temporary storage: relative-path writes from here on
    # land in the granted scratch root, not wherever the process happened
    # to start.
    os.chdir(args.scratch_dir)

    source_evidence_id = uuid.UUID(args.source_evidence_id)

    def _handle(_request: dict[str, JSONValue]) -> dict[str, JSONValue]:
        result = parse_markdown(content, media_type=args.media_type, source_evidence_id=source_evidence_id)
        dumped: dict[str, JSONValue] = result.model_dump(mode="json")
        return dumped

    try:
        ipc.serve_one(
            args.sock_path,
            _handle,
            expected_peer_uid=args.expected_peer_uid,
            deadline_seconds=args.deadline_seconds,
        )
    except ipc.SandboxRefusal as refusal:
        log.warning("sandboxed parser refused the connection: %s", refusal.code.value)
        return 1
    finally:
        if stop_watchdog is not None:
            stop_watchdog.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
