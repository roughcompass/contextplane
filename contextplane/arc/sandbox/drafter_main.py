"""The sandboxed drafter entrypoint.

Run via ``python -m contextplane.arc.sandbox.drafter_main`` -- one process per
drafting attempt, serving exactly one connection on ``ipc.serve_one`` before
exiting. Its own service identity and socket path are distinct from the
parser's (``contextplane.arc.sandbox.parser_main``): the two are separate
sandboxes with separate isolation guarantees, and nothing here lets one
authenticate as the other -- see ``tests/conformance/test_arc_drafter_sandbox.py``
for the proof that a peer authorized for one socket is refused by the other.

This module never imports ``contextplane.storage``, ``contextplane.arc.service``,
``contextplane.wiring``, or ``contextplane.config`` -- no database credential, no
service token, no lifecycle authority, structurally rather than by a
runtime check that something could bypass. Same isolation shape as
``parser_main.py``, applied to this process independently: a sandboxed
process's own defenses cannot depend on a sibling sandbox having already
applied its own, because nothing here trusts that the other process ran
first, ran at all, or ran honestly.

- **CPU time / memory / network guard.** Identical mechanism and identical
  platform caveats to ``parser_main.py`` -- see that module's docstring for
  the full darwin-vs-Linux account of what each control actually enforces.
  Duplicated here rather than imported from a shared module: this file is
  outside the path scope of the task that most recently touched
  ``parser_main.py``, and that module already shipped through a red-CI
  fix (a watchdog thread that could not start once ``RLIMIT_AS`` applied,
  and a shell ``ulimit -v`` that could not cover interpreter startup) --
  refactoring it now to extract a shared helper would touch already-proven
  code for a DRY gain this task does not need to take on. A future task
  that owns both files is a reasonable place to do that extraction.
- **Content handle.** Read-only, granted via ``--content-path``, exactly
  like the parser's. The drafter re-derives the content's own sha256 and
  compares it against the caller-supplied ``--source-content-digest``
  *and* the parsed envelope's own declared digest before drafting anything
  -- a defense-in-depth binding check inside the sandbox itself, on top of
  (not instead of) the API-side adapter's own ``parser_output.
  verify_source_binding`` call before it ever reaches this process.
- **Parsed envelope.** Arrives over the one authenticated request, not via
  a file: it is already-validated structured data (a
  ``contextplane.arc.schemas.parser_output.ParsedSourceEnvelope``), produced by
  a prior call to the parser sandbox, not raw hostile bytes -- so handing it
  over the wire rather than through a second granted file does not reopen
  the "never parse hostile content in-process" boundary either sandbox
  exists to hold.
- **No model artifact.** ``ARC_DRAFTER_MODEL_ARTIFACT_PATH`` is never read
  here. The committed decision (``outcome: human_only``) means no accepted
  model exists to load; ``draft_from_envelope`` below is the deterministic,
  citation-only placeholder decision function this fact leaves in place of
  one, documented in full in ``contextplane.arc.schemas.drafter_output``'s own
  module docstring. When an accepted model exists, only this function's
  body changes -- the sandbox process, the wire protocol, and the output
  contract it must satisfy do not.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import resource
import socket
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from contextplane.arc.sandbox import ipc
from contextplane.arc.schemas.drafter_output import MAX_DECLINED_FIELD_PATHS, DrafterRefusal, DrafterSuccess
from contextplane.arc.schemas.parser_output import ParsedSourceEnvelope
from contextplane.types import JSONValue

log = logging.getLogger(__name__)

DRAFTER_ID = "arc_placeholder_drafter"
DRAFTER_VERSION = "1"

DEFAULT_CPU_SECONDS = 30
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# Resource limits -- identical mechanism to `parser_main.py`; see that
# module's docstring for the full platform account. Duplicated rather than
# shared; see this module's own docstring for why.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimitReport:
    cpu_seconds: int
    memory_bytes_requested: int
    memory_limit_applied: bool
    memory_limit_note: str


def apply_resource_limits(
    *,
    cpu_seconds: int = DEFAULT_CPU_SECONDS,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
) -> ResourceLimitReport:
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
                "setrlimit raises for any finite value); relying on the best-effort RSS "
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
    return usage * 1024 if sys.platform.startswith("linux") else usage


def start_memory_watchdog(memory_bytes: int, *, poll_interval: float = 0.2) -> threading.Event:
    """Cooperative, non-preemptive fallback -- started only when
    `RLIMIT_AS` could not be applied. See `parser_main.py`'s own docstring
    for exactly why starting this thread unconditionally is the mistake
    that broke that sandbox on Linux: a new thread cannot map its stack
    inside a virtual-address-space cap the kernel is actually enforcing.
    """
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(poll_interval):
            if _current_rss_bytes() > memory_bytes:
                log.error("sandboxed drafter exceeded its memory ceiling; self-terminating")
                os._exit(137)

    thread = threading.Thread(target=_watch, name="drafter-sandbox-memory-watchdog", daemon=True)
    thread.start()
    return stop


# ---------------------------------------------------------------------------
# Network guard -- identical mechanism to `parser_main.py`.
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
        raise PermissionError(f"sandboxed drafter process refused to open a socket of family {family!r}")
    return _real_socket_ctor(family, socket_type, proto, fileno)


def _deny_getaddrinfo(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("sandboxed drafter process refused DNS resolution")


def install_network_guard() -> None:
    socket.socket = _guarded_socket_ctor  # type: ignore[assignment,misc]
    socket.getaddrinfo = _deny_getaddrinfo  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Drafting -- pure, no I/O, no guard state. Unit-testable directly, exactly
# like `parser_main.parse_markdown`.
# ---------------------------------------------------------------------------


def draft_from_envelope(
    content: bytes,
    envelope: ParsedSourceEnvelope,
    *,
    source_content_digest: str,
    target_field_paths: Sequence[str],
) -> DrafterSuccess | DrafterRefusal:
    """Decide what, if anything, to propose for each of `target_field_paths`.

    See this module's own docstring and `contextplane.arc.schemas.
    drafter_output`'s docstring for why every branch below either refuses
    outright or declines every requested field -- there is no branch that
    populates one, because no accepted model exists to make the
    classification judgments several `ArtifactSemantics.directives[]`
    sibling fields require and no citation can supply.

    The one substantive check this function performs is the binding one:
    the content this process was actually handed must hash to exactly the
    digest the caller asserts, and the envelope it was handed must declare
    that same digest -- both checked here, inside the sandbox, in addition
    to (not instead of) the API-side adapter's own binding check before
    this process is ever invoked.
    """
    if len(target_field_paths) > MAX_DECLINED_FIELD_PATHS:
        return DrafterRefusal(status="refused", refusal_code="output_limit_exceeded")

    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != source_content_digest or envelope.source_content_digest != source_content_digest:
        return DrafterRefusal(status="refused", refusal_code="envelope_binding_mismatch")

    # De-duplicated, order-preserved: a caller that names the same field path
    # twice gets it declined once, not twice.
    declined = list(dict.fromkeys(target_field_paths))
    return DrafterSuccess(status="ok", patch={}, citations=[], declined_field_paths=declined)


# ---------------------------------------------------------------------------
# Entrypoint.
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARC sandboxed drafter")
    parser.add_argument("--content-path", required=True, help="path to the admitted, read-only content file")
    parser.add_argument("--sock-path", required=True, help="Unix-domain socket path to serve on")
    parser.add_argument("--source-content-digest", required=True, help="sha256 hex digest the content must match")
    parser.add_argument("--expected-peer-uid", required=True, type=int)
    parser.add_argument("--scratch-dir", required=True, help="writable isolated scratch directory")
    parser.add_argument("--deadline-seconds", type=float, default=ipc.DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--cpu-seconds", type=int, default=DEFAULT_CPU_SECONDS)
    parser.add_argument("--memory-bytes", type=int, default=DEFAULT_MEMORY_BYTES)
    return parser.parse_args(argv)


def _malformed_request(reason: str) -> dict[str, JSONValue]:
    log.warning("sandboxed drafter refused a malformed request: %s", reason)
    return DrafterRefusal(status="refused", refusal_code="malformed_request").model_dump(mode="json")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)

    report = apply_resource_limits(cpu_seconds=args.cpu_seconds, memory_bytes=args.memory_bytes)
    # See `parser_main.py::main` for why this is conditional, not
    # belt-and-braces: starting the watchdog thread when `RLIMIT_AS` did
    # apply aborts the process at startup trying to map its own stack
    # inside the cap it is meant to police.
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

    # Isolated bounded temporary storage, matching `parser_main.py`.
    os.chdir(args.scratch_dir)

    def _handle(request: dict[str, JSONValue]) -> dict[str, JSONValue]:
        raw_target_field_paths = request.get("target_field_paths")
        if not isinstance(raw_target_field_paths, list) or not all(isinstance(p, str) for p in raw_target_field_paths):
            return _malformed_request("target_field_paths must be a JSON array of strings")
        # The `isinstance` check above already proved every element is a
        # `str`; this just gives mypy the narrower type it cannot infer
        # through a generator-based `all()` check.
        target_field_paths: list[str] = [p for p in raw_target_field_paths if isinstance(p, str)]

        raw_envelope = request.get("envelope")
        if not isinstance(raw_envelope, dict):
            return _malformed_request("envelope must be a JSON object")
        try:
            envelope = ParsedSourceEnvelope.model_validate(raw_envelope)
        except ValidationError as exc:
            return _malformed_request(f"envelope failed schema validation: {exc.error_count()} error(s)")

        result = draft_from_envelope(
            content,
            envelope,
            source_content_digest=args.source_content_digest,
            target_field_paths=target_field_paths,
        )
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
        log.warning("sandboxed drafter refused the connection: %s", refusal.code.value)
        return 1
    finally:
        if stop_watchdog is not None:
            stop_watchdog.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
