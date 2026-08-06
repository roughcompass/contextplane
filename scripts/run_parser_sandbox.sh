#!/usr/bin/env bash
#
# ARC parser sandbox -- local/manual invocation wrapper.
#
# Usage: scripts/run_parser_sandbox.sh <content-path> <output-path>
#
# Provisions a throwaway, per-invocation sandbox: a read-only content root
# (mode 0500, containing the admitted content at mode 0400 -- a genuine
# OS-permission write refusal, not a Python-level approximation of one), a
# separate writable scratch root, and a dedicated Unix-domain socket path.
# Spawns `registry.arc.sandbox.parser_main` under a best-effort CPU/memory
# ulimit, waits for it to start listening, sends the one IPC request as the
# expected caller (this same OS user -- a real, separate, unprivileged
# service identity is a deployment-time concern; see parser_main.py's own
# module docstring for exactly what is and is not enforceable here),
# verifies the response is bound to the content this script actually
# hashed, and writes the raw JSON `ParserResult` to <output-path>.
#
# This wraps the exact mechanism `tests/conformance/test_arc_parser_sandbox.py`
# exercises directly -- not a second, undertested code path.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <content-path> <output-path>" >&2
  exit 2
fi

CONTENT_PATH_ARG="$1"
OUTPUT_PATH="$2"

if [[ ! -f "$CONTENT_PATH_ARG" ]]; then
  echo "content path not found: $CONTENT_PATH_ARG" >&2
  exit 2
fi

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "python interpreter not found or not executable: $PYTHON (set PYTHON=... to override)" >&2
  exit 2
fi

CONTENT_PATH="$("$PYTHON" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$CONTENT_PATH_ARG")"

WORKDIR="$(mktemp -d)"
PARSER_PID=""

cleanup() {
  if [[ -n "$PARSER_PID" ]] && kill -0 "$PARSER_PID" 2>/dev/null; then
    kill "$PARSER_PID" 2>/dev/null || true
    wait "$PARSER_PID" 2>/dev/null || true
  fi
  chmod -R u+rwx "$WORKDIR" 2>/dev/null || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

READ_ROOT="$WORKDIR/read_root"
SCRATCH_ROOT="$WORKDIR/scratch"
SOCK_PATH="$WORKDIR/parser.sock"
mkdir -p "$READ_ROOT" "$SCRATCH_ROOT"

cp "$CONTENT_PATH" "$READ_ROOT/content"
chmod 0400 "$READ_ROOT/content"
chmod 0500 "$READ_ROOT"
chmod 0700 "$SCRATCH_ROOT"

MEDIA_TYPE="text/markdown"
SOURCE_EVIDENCE_ID="$("$PYTHON" -c 'import uuid; print(uuid.uuid4())')"
EXPECTED_DIGEST="$("$PYTHON" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$CONTENT_PATH")"
CALLER_UID="$(id -u)"

(
  ulimit -t 30
  if ! ulimit -v $((512 * 1024)) 2>/dev/null; then
    echo "note: virtual-memory ulimit is not settable on this platform (environment-limited -- confirmed on darwin/XNU, where a plain 'ulimit -v' fails the same way outside this script); relying on parser_main.py's own RLIMIT_AS attempt, its RSS watchdog, and the output-size ceiling instead" >&2
  fi
  exec "$PYTHON" -m registry.arc.sandbox.parser_main \
    --content-path "$READ_ROOT/content" \
    --sock-path "$SOCK_PATH" \
    --media-type "$MEDIA_TYPE" \
    --source-evidence-id "$SOURCE_EVIDENCE_ID" \
    --expected-peer-uid "$CALLER_UID" \
    --scratch-dir "$SCRATCH_ROOT" \
    --deadline-seconds 30
) &
PARSER_PID=$!

ATTEMPTS=0
until [[ -S "$SOCK_PATH" ]]; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [[ $ATTEMPTS -gt 200 ]]; then
    echo "sandboxed parser never started listening on $SOCK_PATH" >&2
    exit 1
  fi
  if ! kill -0 "$PARSER_PID" 2>/dev/null; then
    echo "sandboxed parser process exited before it started listening" >&2
    exit 1
  fi
  sleep 0.05
done

"$PYTHON" - "$SOCK_PATH" "$SOURCE_EVIDENCE_ID" "$EXPECTED_DIGEST" "$CALLER_UID" "$OUTPUT_PATH" <<'PYCLIENT'
import json
import sys
import uuid

from registry.arc.sandbox import ipc
from registry.arc.schemas import parser_output

sock_path, source_evidence_id, expected_digest, caller_uid, output_path = sys.argv[1:6]


def _validate(response: dict) -> None:
    result = parser_output.parse_parser_result(response)
    if isinstance(result, parser_output.ParserSuccess):
        parser_output.verify_source_binding(
            result.envelope,
            source_evidence_id=uuid.UUID(source_evidence_id),
            source_content_digest=expected_digest,
        )


try:
    response = ipc.request_json(
        sock_path,
        {},
        expected_peer_uid=int(caller_uid),
        deadline_seconds=30.0,
        validate_response=_validate,
    )
except ipc.SandboxRefusal as refusal:
    response = {"status": "sandbox_transport_refused", "refusal_code": refusal.code.value}

with open(output_path, "w", encoding="utf-8") as fh:
    json.dump(response, fh)
    fh.write("\n")
PYCLIENT

wait "$PARSER_PID" 2>/dev/null || true
echo "wrote $OUTPUT_PATH" >&2
