"""Deployment-level conformance gate for the ARC parser/drafter sandboxes.

The parser/drafter sandbox test suites (`test_arc_parser_sandbox.py`,
`test_arc_drafter_sandbox.py`) proved every isolation property they could
enforce *on darwin*, and were honest about four they structurally could
not: a kernel memory ceiling (`RLIMIT_AS`/`RLIMIT_DATA` refuse any finite
value on darwin/XNU), per-process CPU-core affinity (no such API exists on
darwin), read-side filesystem confinement beyond the one granted content
path (needs a mount namespace or a read-only root filesystem), and a
dedicated OS group for the socket (group creation needs root). Each was
confirmed empirically, not assumed, and each names this module as the
place a *container* closes it for real.

This suite does not re-derive any of that -- it runs the actual, shipped
`Dockerfile` as a real container on the real Linux deployment target (Docker
Desktop's backend on this development machine is itself a Linux VM; CI's
`ubuntu-latest` runners are Linux natively) and observes the four properties
either firing or not, the same discipline the rest of this program uses
everywhere else: a claim is proven by attempting the thing and watching what
happens, never by reading a compose file's text.

What each property below is now proven to do, concretely:

- **Memory.** The pod-level memory limit (mirrored here via `docker run
  --memory`) is the real, kernel-enforced outer ceiling -- proven by
  deliberately exceeding it and reading `docker inspect`'s own `OOMKilled`
  flag, not merely a nonzero exit code. Independently, and *inside* that
  outer ceiling, the sandbox's own `RLIMIT_AS` (applied after Python has
  finished importing -- see `parser_main.py`'s own account of why that
  ordering is load-bearing) is confirmed to actually apply on Linux, and the
  sandbox is confirmed to actually start and successfully parse under it --
  the exact regression class fixed at `6cdb58e` (a memory limit CPython
  cannot start under looks identical to a working one until something tries
  to boot under it). The RSS watchdog is confirmed to stay conditional: its
  own log line never appears when the kernel ceiling applied.
- **CPU.** `docker run --cpuset-cpus` is a real cgroup `cpuset` affinity
  mask, not a scheduling quota -- proven by reading back
  `os.sched_getaffinity(0)` from inside the constrained container and from
  an unconstrained one and observing they differ, with the constrained one
  reading exactly the pinned set.
- **Filesystem.** `docker run --read-only` plus a writable `tmpfs` at `/tmp`
  (the exact shape both `deploy/helm/values.yaml` and the sandbox's own
  `tempfile.TemporaryDirectory(dir="/tmp")` already use) is proven to make a
  write anywhere else raise a real, kernel-level `OSError: Read-only file
  system` -- not the permission-bit `PermissionError` the granted content
  root already relied on before this task. This closes write-side
  confinement for real. It does **not** close read-side confinement to
  exactly the one granted path -- that would need a per-invocation mount
  namespace, and this suite confirms (see
  `test_unprivileged_mount_namespaces_are_unavailable_to_this_deployment_target`)
  that creating one is refused by the container runtime's own default
  security policy short of `--privileged`, which would remove far more
  isolation than it would add. That residual is recorded here as
  environment-limited on the actual deployment target, the same honesty
  the parser/drafter sandbox suites applied to darwin -- not silently
  claimed as closed.
- **Dedicated group.** The `Dockerfile` now creates `arc-sandbox` as
  `registry`'s only group (real, because building an image runs as root),
  and this suite runs the real sandbox subprocess inside a real container and
  `stat()`s the socket it created, confirming it is group-owned by that
  dedicated GID at mode `0660` -- not merely that the Dockerfile *contains*
  a `groupadd` line.

Two more properties are re-proven here specifically because running inside
a real Linux container exercises a code path the darwin-hosted suites never
do: `registry.arc.sandbox.ipc.get_peer_uid` takes the `SO_PEERCRED` branch
on Linux (darwin always takes the `getpeereid(2)` branch), and the
network guard's refusal has never previously been observed to fire inside
the actual deployed namespace rather than a developer's own machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # noqa: S404 - every invocation below is a fixed `docker` argv; no caller input reaches it
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
# These tests build the shipped Dockerfile and run real containers, which a
# cold `docker build` makes far slower than `make test-conformance`'s
# per-test timeout allows -- it passed locally only because an earlier
# `make test-airgap` had already warmed the layer cache. They are gated the
# same way the compose-dependent tests are, and CI runs them in the `image`
# job, which has already built the image and is the one job where paying for
# it is free. Skipping silently in the default suite would lose the coverage,
# so the gate is an explicit opt-in that CI sets rather than a bare skipif on
# docker availability.
pytestmark = [
    pytest.mark.arc_sandbox_container,
    pytest.mark.skipif(
        os.environ.get("ARC_SANDBOX_CONFORMANCE")
        != "1",  # config: intentional - test-suite opt-in, not application configuration
        reason="container sandbox conformance is opt-in; set ARC_SANDBOX_CONFORMANCE=1 (CI runs it in the image job)",
    ),
]


IMAGE_TAG = "registry-arc-sandbox-conformance:test"

#: The dedicated socket group the `Dockerfile` now creates at build time
#: (root-only operation) and makes `registry`'s sole group. Asserted by
#: value, not merely "not root/not the API's historical GID", so a future
#: accidental renumbering that happens to dodge 0 and 999 still gets caught.
DEDICATED_GROUP_GID = 1500

_DOCKER_BUILD_TIMEOUT_S = 300
_DOCKER_RUN_TIMEOUT_S = 60


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """Build the *real, shipped* `Dockerfile` once per test session and
    return its tag. Every test below runs a real container from this exact
    image -- not a stand-in image, and not a fresh build per test, since
    layer caching already makes a single build's cost amortize across the
    whole file.

    Skips (does not fail) when Docker is unavailable, the same way
    `make test-airgap` is its own opt-in gate rather than folded into
    `make all` -- this suite has no such opt-in boundary (it is plain
    `pytest tests/conformance`), so the honest choice for a machine with no
    container runtime at all is a clear skip, not a silent pass or a hard
    failure unrelated to anything this task changed.
    """
    if not _docker_available():
        pytest.skip("docker is not available in this environment; sandbox container isolation cannot be verified")
    build = subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE_TAG, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=_DOCKER_BUILD_TIMEOUT_S,
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(f"docker build of the shipped Dockerfile failed:\n{build.stderr}")
    return IMAGE_TAG


@pytest.fixture
def host_script_path() -> Any:
    """A real path under `/tmp` to write a helper script to and bind-mount
    read-only into a container -- mirrors the sibling darwin suites'
    `short_tmp_path` fixture, though the reason here is simpler: this is
    just where the script this test hands the container lives on the host
    side, with no `AF_UNIX` path-length constraint involved."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _write_script(directory: Path, content: str) -> Path:
    path = directory / f"{uuid.uuid4().hex}.py"
    path.write_text(textwrap.dedent(content))
    return path


def _docker_run_json(
    image: str,
    script_path: Path,
    *extra_flags: str,
    args: tuple[str, ...] = (),
    timeout: float = _DOCKER_RUN_TIMEOUT_S,
) -> dict[str, Any]:
    """Run `script_path` inside a real, throwaway container from `image`
    under an explicit non-root user and whatever `extra_flags` this test
    supplies, parse its stdout as JSON, and return it alongside the
    process's own exit code and stderr. Every test below reads this dict's
    fields rather than the container's text output, so an assertion failure
    names exactly which field diverged.
    """
    container_script = "/tmp/_conformance_script.py"  # noqa: S108 - a path inside the throwaway container, not this host
    argv = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{script_path}:{container_script}:ro",
        *extra_flags,
        "--entrypoint",
        "python",
        image,
        container_script,
        *args,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "json": payload,
    }


# ---------------------------------------------------------------------------
# Shared script bodies. Every one is a real, standalone Python program run
# via `python /tmp/_conformance_script.py` inside a real container -- not a
# unit test double for one.
# ---------------------------------------------------------------------------

_RUN_PARSER_SCRIPT = """\
    import hashlib, json, os, subprocess, sys, tempfile, time, uuid

    memory_bytes = int(sys.argv[1]) if len(sys.argv) > 1 else 512 * 1024 * 1024
    workdir = tempfile.mkdtemp(dir="/tmp")
    read_root = os.path.join(workdir, "read_root")
    scratch = os.path.join(workdir, "scratch")
    os.makedirs(read_root)
    os.makedirs(scratch)
    content = b"# Title\\n\\nSome body text.\\n"
    content_path = os.path.join(read_root, "content")
    with open(content_path, "wb") as f:
        f.write(content)
    os.chmod(content_path, 0o400)
    os.chmod(read_root, 0o500)
    sock_path = os.path.join(workdir, "parser.sock")
    caller_uid = os.getuid()

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "registry.arc.sandbox.parser_main",
            "--content-path", content_path,
            "--sock-path", sock_path,
            "--media-type", "text/markdown",
            "--source-evidence-id", str(uuid.uuid4()),
            "--expected-peer-uid", str(caller_uid),
            "--scratch-dir", scratch,
            "--deadline-seconds", "20",
            "--cpu-seconds", "20",
            "--memory-bytes", str(memory_bytes),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not os.path.exists(sock_path):
        if proc.poll() is not None:
            break
        time.sleep(0.02)

    result = {"socket_existed": os.path.exists(sock_path), "affinity": sorted(os.sched_getaffinity(0))}
    if result["socket_existed"]:
        import stat as statmod
        st = os.stat(sock_path)
        result["socket_uid"] = st.st_uid
        result["socket_gid"] = st.st_gid
        result["socket_mode"] = statmod.S_IMODE(st.st_mode)

    from registry.arc.sandbox import ipc
    try:
        response = ipc.request_json(sock_path, {}, expected_peer_uid=caller_uid, deadline_seconds=10.0)
        result["response_status"] = response.get("status")
    except ipc.SandboxRefusal as e:
        result["client_refusal"] = e.code.value

    proc.wait(timeout=10)
    result["exit_code"] = proc.returncode
    result["stderr"] = proc.stderr.read().decode()
    print(json.dumps(result))
"""

_MEM_HOG_SCRIPT = """\
    import sys
    size_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    chunks = []
    for _ in range(size_mb):
        chunks.append(bytearray(1024 * 1024))
    print("allocated without being killed")
"""

_CPU_AFFINITY_SCRIPT = """\
    import json, os
    print(json.dumps({"affinity": sorted(os.sched_getaffinity(0))}))
"""

_ROOTFS_WRITE_SCRIPT = """\
    import json
    result = {}
    try:
        with open("/tmp/_write_probe", "w") as f:
            f.write("ok")
        result["tmp_write_succeeded"] = True
    except OSError as e:
        result["tmp_write_succeeded"] = False
        result["tmp_write_error"] = repr(e)

    try:
        with open("/app/_write_probe", "w") as f:
            f.write("ok")
        result["outside_write_succeeded"] = True
    except OSError as e:
        result["outside_write_succeeded"] = False
        result["outside_write_errno"] = e.errno
    print(json.dumps(result))
"""

_FULL_PIPELINE_SCRIPT = """\
    import hashlib, json, os, subprocess, sys, tempfile, time, uuid

    workdir = tempfile.mkdtemp(dir="/tmp")
    read_root = os.path.join(workdir, "read_root")
    parser_scratch = os.path.join(workdir, "parser_scratch")
    drafter_scratch = os.path.join(workdir, "drafter_scratch")
    for d in (read_root, parser_scratch, drafter_scratch):
        os.makedirs(d)
    content = b"# Title\\n\\nSome body text about widgets.\\n"
    content_path = os.path.join(read_root, "content")
    with open(content_path, "wb") as f:
        f.write(content)
    os.chmod(content_path, 0o400)
    os.chmod(read_root, 0o500)
    digest = hashlib.sha256(content).hexdigest()
    caller_uid = os.getuid()
    source_evidence_id = str(uuid.uuid4())

    parser_sock = os.path.join(workdir, "parser.sock")
    parser_proc = subprocess.Popen(
        [
            sys.executable, "-m", "registry.arc.sandbox.parser_main",
            "--content-path", content_path, "--sock-path", parser_sock,
            "--media-type", "text/markdown", "--source-evidence-id", source_evidence_id,
            "--expected-peer-uid", str(caller_uid), "--scratch-dir", parser_scratch,
            "--deadline-seconds", "20", "--cpu-seconds", "20", "--memory-bytes", str(512 * 1024 * 1024),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not os.path.exists(parser_sock):
        if parser_proc.poll() is not None:
            break
        time.sleep(0.02)

    from registry.arc.sandbox import ipc
    from registry.arc.schemas import parser_output as po
    from registry.arc.schemas import drafter_output as do

    parser_response = ipc.request_json(parser_sock, {}, expected_peer_uid=caller_uid, deadline_seconds=10.0)
    parser_proc.terminate()
    parser_proc.wait(timeout=5)
    parsed = po.parse_parser_result(parser_response)

    result = {"parser_ok": isinstance(parsed, po.ParserSuccess), "affinity": sorted(os.sched_getaffinity(0))}

    if result["parser_ok"]:
        drafter_sock = os.path.join(workdir, "drafter.sock")
        drafter_proc = subprocess.Popen(
            [
                sys.executable, "-m", "registry.arc.sandbox.drafter_main",
                "--content-path", content_path, "--sock-path", drafter_sock,
                "--source-content-digest", digest,
                "--expected-peer-uid", str(caller_uid), "--scratch-dir", drafter_scratch,
                "--deadline-seconds", "20", "--cpu-seconds", "20", "--memory-bytes", str(512 * 1024 * 1024),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not os.path.exists(drafter_sock):
            if drafter_proc.poll() is not None:
                break
            time.sleep(0.02)
        drafter_request = {
            "envelope": parsed.envelope.model_dump(mode="json"),
            "target_field_paths": ["directives[0].text"],
        }
        drafter_response = ipc.request_json(
            drafter_sock, drafter_request, expected_peer_uid=caller_uid, deadline_seconds=10.0
        )
        drafter_proc.terminate()
        drafter_proc.wait(timeout=5)
        drafted = do.parse_drafter_result(drafter_response)
        result["drafter_ok"] = isinstance(drafted, do.DrafterSuccess)

    print(json.dumps(result))
"""

_NETWORK_GUARD_SCRIPT = """\
    import json, socket
    from registry.arc.sandbox.parser_main import install_network_guard
    install_network_guard()
    result = {}
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result["outbound_socket_construction_succeeded"] = True
    except PermissionError as e:
        result["outbound_socket_construction_succeeded"] = False
        result["refusal"] = str(e)
    try:
        socket.getaddrinfo("example.invalid", 443)
        result["dns_resolution_succeeded"] = True
    except PermissionError:
        result["dns_resolution_succeeded"] = False
    print(json.dumps(result))
"""

_PEER_MISMATCH_SCRIPT = """\
    import json, os, subprocess, sys, tempfile, time, uuid

    workdir = tempfile.mkdtemp(dir="/tmp")
    read_root = os.path.join(workdir, "read_root")
    scratch = os.path.join(workdir, "scratch")
    os.makedirs(read_root)
    os.makedirs(scratch)
    content_path = os.path.join(read_root, "content")
    with open(content_path, "wb") as f:
        f.write(b"# Title\\n\\nbody\\n")
    os.chmod(content_path, 0o400)
    os.chmod(read_root, 0o500)
    sock_path = os.path.join(workdir, "parser.sock")

    real_uid = os.getuid()
    wrong_uid = real_uid + 999_999

    # The *server* is deliberately told to expect a peer UID that is not
    # this real caller's -- the same shape as `test_arc_parser_sandbox.py`'s
    # own `test_server_refuses_client_with_wrong_peer_uid`, run here against
    # the real `parser_main` subprocess instead of a raw-socket test double,
    # so the refusal actually goes through `get_peer_uid`'s Linux branch
    # (`SO_PEERCRED`) rather than the darwin `getpeereid(2)` branch every
    # prior run of that sibling test exercises.
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "registry.arc.sandbox.parser_main",
            "--content-path", content_path,
            "--sock-path", sock_path,
            "--media-type", "text/markdown",
            "--source-evidence-id", str(uuid.uuid4()),
            "--expected-peer-uid", str(wrong_uid),
            "--scratch-dir", scratch,
            "--deadline-seconds", "10",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not os.path.exists(sock_path):
        if proc.poll() is not None:
            break
        time.sleep(0.02)

    from registry.arc.sandbox import ipc
    client_refusal = None
    try:
        # The client's own view of the server's identity is correct (the
        # server really does run as `real_uid`) -- only the server's view of
        # the client is wrong, so this exercises the server-side check, not
        # the client-side one. Depending on exactly how fast the server
        # closes the connection after its own rejection, the client observes
        # either `ipc.py`'s own bounded `SandboxRefusal` (a clean read of
        # zero bytes) or a raw `OSError` from the socket layer (the write
        # landing after the server has already closed) -- both are the
        # client-side symptom of the same server-side refusal, and either
        # is proof enough here; the server's own stderr below is the
        # authoritative signal this test actually asserts on.
        ipc.request_json(sock_path, {}, expected_peer_uid=real_uid, deadline_seconds=8.0)
    except ipc.SandboxRefusal as e:
        client_refusal = e.code.value
    except OSError as e:
        client_refusal = type(e).__name__

    proc.wait(timeout=15)
    print(json.dumps({
        "platform": sys.platform,
        "exit_code": proc.returncode,
        "client_refusal": client_refusal,
        "stderr": proc.stderr.read().decode(),
    }))
"""

_OVERSIZE_FRAME_SCRIPT = """\
    import json, os, socket, struct, subprocess, sys, tempfile, time, uuid

    workdir = tempfile.mkdtemp(dir="/tmp")
    read_root = os.path.join(workdir, "read_root")
    scratch = os.path.join(workdir, "scratch")
    os.makedirs(read_root)
    os.makedirs(scratch)
    content_path = os.path.join(read_root, "content")
    with open(content_path, "wb") as f:
        f.write(b"# Title\\n\\nbody\\n")
    os.chmod(content_path, 0o400)
    os.chmod(read_root, 0o500)
    sock_path = os.path.join(workdir, "parser.sock")
    caller_uid = os.getuid()

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "registry.arc.sandbox.parser_main",
            "--content-path", content_path,
            "--sock-path", sock_path,
            "--media-type", "text/markdown",
            "--source-evidence-id", str(uuid.uuid4()),
            "--expected-peer-uid", str(caller_uid),
            "--scratch-dir", scratch,
            "--deadline-seconds", "10",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not os.path.exists(sock_path):
        if proc.poll() is not None:
            break
        time.sleep(0.02)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(sock_path)
    # A length prefix declaring 5 MB, with only two real body bytes sent --
    # `ipc.read_frame` must refuse on the declared length alone, before
    # trying to buffer anything close to that much.
    client.sendall(struct.pack("!I", 5_000_000) + b"{}")
    try:
        client.recv(1)
        client_saw_close = False
    except OSError:
        client_saw_close = True
    client.close()

    proc.wait(timeout=10)
    print(json.dumps({
        "client_saw_close": client_saw_close,
        "exit_code": proc.returncode,
        "stderr": proc.stderr.read().decode(),
    }))
"""


# ---------------------------------------------------------------------------
# Property 4: a dedicated OS group for the socket.
# ---------------------------------------------------------------------------


def test_dockerfile_creates_a_dedicated_non_root_socket_group(sandbox_image: str, host_script_path: Path) -> None:
    """`registry`'s only group in the shipped image is `arc-sandbox`
    (GID 1500) -- not GID 0 (root, the previous default) and not the same
    numeric value as its own UID. Creating a group at all needs root; a
    container build has root, every platform this code runs on locally
    (darwin included) does not."""
    script = _write_script(
        host_script_path,
        """
        import json, os
        print(json.dumps({"uid": os.getuid(), "gid": os.getgid()}))
        """,
    )
    outcome = _docker_run_json(sandbox_image, script)
    assert outcome["exit_code"] == 0, outcome["stderr"]
    assert outcome["json"]["gid"] == DEDICATED_GROUP_GID
    assert outcome["json"]["gid"] != 0
    assert outcome["json"]["gid"] != outcome["json"]["uid"]


def test_sandbox_socket_is_group_owned_by_the_dedicated_group_in_a_real_container(
    sandbox_image: str, host_script_path: Path
) -> None:
    """Not "the Dockerfile contains a `groupadd` line" -- the socket the
    real sandboxed parser subprocess actually creates, inside a real
    container, `stat()`s back with that exact GID and mode 0660."""
    script = _write_script(host_script_path, _RUN_PARSER_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script)
    body = outcome["json"]
    assert body.get("socket_existed") is True, outcome["stderr"]
    assert body["socket_gid"] == DEDICATED_GROUP_GID
    assert body["socket_mode"] == 0o660
    assert body["response_status"] == "ok"


# ---------------------------------------------------------------------------
# Property 1: the memory ceiling.
# ---------------------------------------------------------------------------


def test_sandbox_starts_and_parses_successfully_under_a_real_container_memory_limit(
    sandbox_image: str, host_script_path: Path
) -> None:
    """The exact regression class fixed at `6cdb58e`: a memory limit
    CPython cannot even start under is indistinguishable from a working one
    until something actually tries to boot under it. `--memory=256m` here
    is generous for the base interpreter and the sandbox's own 64 MiB
    `RLIMIT_AS` request -- the point is that both apply and the parse still
    completes, not merely that the numbers look sensible on paper."""
    script = _write_script(host_script_path, _RUN_PARSER_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script, "--memory=256m", args=(str(64 * 1024 * 1024),))
    assert outcome["exit_code"] == 0, outcome["stderr"]
    assert outcome["json"].get("response_status") == "ok"
    assert "RLIMIT_AS applied" in outcome["stderr"] or "RLIMIT_AS applied" in outcome["json"].get("stderr", "")


def test_memory_watchdog_stays_conditional_when_the_kernel_ceiling_applies(
    sandbox_image: str, host_script_path: Path
) -> None:
    """The regression `6cdb58e` also fixed: the RSS watchdog thread must
    never start when `RLIMIT_AS` actually applied, because a fresh thread's
    stack cannot map inside a cap the kernel is genuinely enforcing. On
    Linux, inside a real container, `RLIMIT_AS` does apply -- so the
    watchdog's own "not kernel-enforced" log line must be absent."""
    script = _write_script(host_script_path, _RUN_PARSER_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script, args=(str(512 * 1024 * 1024),))
    stderr = outcome["stderr"] + outcome["json"].get("stderr", "")
    assert outcome["exit_code"] == 0, stderr
    assert "memory ceiling not kernel-enforced" not in stderr
    assert "memory ceiling enforced by the kernel" in stderr


def test_container_memory_limit_is_the_real_outer_ceiling_and_actually_fires(sandbox_image: str) -> None:
    """The parser/drafter sandbox suites recorded that a container memory
    limit is "the real control" the darwin RSS watchdog stands in for.
    Proven here by reading
    `docker inspect`'s own `OOMKilled` flag after deliberately exceeding a
    128 MiB container limit by a wide margin -- not merely a nonzero exit
    code, which could mean anything.
    """
    container_name = f"arc-sandbox-oom-check-{uuid.uuid4().hex[:12]}"
    script_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        script = _write_script(script_dir, _MEM_HOG_SCRIPT)
        run = subprocess.run(
            [
                "docker",
                "run",
                "--name",
                container_name,
                "--memory=128m",
                "-v",
                f"{script}:/tmp/_hog.py:ro",
                "--entrypoint",
                "python",
                IMAGE_TAG,
                "/tmp/_hog.py",  # noqa: S108 - a path inside the throwaway container, not this host
                "400",
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_RUN_TIMEOUT_S,
            check=False,
        )
        assert run.returncode == 137, run.stdout + run.stderr
        inspect = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{.State.OOMKilled}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert inspect.stdout.strip() == "true", inspect.stdout + inspect.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10, check=False)
        shutil.rmtree(script_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2: CPU-core allocation.
# ---------------------------------------------------------------------------


def test_cpu_core_allocation_is_a_real_cgroup_affinity_mask_not_a_static_value(
    sandbox_image: str, host_script_path: Path
) -> None:
    """`os.sched_setaffinity` does not exist on darwin at all -- there was
    nothing to test there. On Linux, `docker run --cpuset-cpus` is a real
    cgroup `cpuset` mask: this container's own
    `os.sched_getaffinity(0)` reads back exactly the pinned set, and an
    unconstrained sibling container reads back a *different*, larger set --
    proving the flag drives the difference rather than this just being
    whatever a fixed, unconstrained value happens to be.
    """
    script = _write_script(host_script_path, _CPU_AFFINITY_SCRIPT)
    unconstrained = _docker_run_json(sandbox_image, script)
    pinned = _docker_run_json(sandbox_image, script, "--cpuset-cpus=0")
    assert unconstrained["exit_code"] == 0, unconstrained["stderr"]
    assert pinned["exit_code"] == 0, pinned["stderr"]
    assert pinned["json"]["affinity"] == [0]
    assert set(pinned["json"]["affinity"]) != set(unconstrained["json"]["affinity"])
    assert len(unconstrained["json"]["affinity"]) > 1


# ---------------------------------------------------------------------------
# Property 3: filesystem confinement.
# ---------------------------------------------------------------------------


def test_read_only_root_filesystem_confines_writes_to_the_granted_scratch_root(
    sandbox_image: str, host_script_path: Path
) -> None:
    """A kernel-level `OSError: Read-only file system` for a write attempt
    outside `/tmp`, not the `PermissionError` mode bits alone would give --
    the difference matters because mode bits can be bypassed by anything
    running as root inside the mount, and a read-only bind mount cannot be,
    short of remounting it (which needs `CAP_SYS_ADMIN`, dropped here).
    Writing inside the granted `/tmp` tmpfs -- the same directory
    `tempfile.TemporaryDirectory(dir="/tmp")` already uses -- still
    succeeds, so this control does not also break the thing it protects.
    """
    script = _write_script(host_script_path, _ROOTFS_WRITE_SCRIPT)
    outcome = _docker_run_json(
        sandbox_image,
        script,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m,mode=1777",  # noqa: S108 - a `docker run --tmpfs` mount spec inside the container, not this host
    )
    assert outcome["exit_code"] == 0, outcome["stderr"]
    body = outcome["json"]
    assert body["tmp_write_succeeded"] is True
    assert body["outside_write_succeeded"] is False
    assert body["outside_write_errno"] == 30  # EROFS


def test_unprivileged_mount_namespaces_are_unavailable_under_the_shipped_capability_set(
    host_script_path: Path,
) -> None:
    """Records, rather than assumes, why read-side confinement to exactly
    the one granted content path is not attempted here. A per-invocation
    mount namespace (bind-mounting only the granted content file) is the
    mechanism that would close it.

    Measured directly (not assumed from documentation): the deciding factor
    is `CAP_SYS_ADMIN` specifically, not root vs. non-root and not
    `--privileged` -- `unshare --user --mount --map-root-user` fails
    identically as root, as this image's own uid 999, and as an arbitrary
    uid 1000, all *without* that one capability, and succeeds under all
    three *with* it. The shipped `Dockerfile`/Helm profile drops every
    capability and adds none back (`capabilities.drop: [ALL]`, no
    `cap_add`), which is exactly the "without" case this test pins down --
    so this deployment genuinely cannot create a mount namespace today, and
    the reason this task does not add `CAP_SYS_ADMIN` to close it is a
    deliberate tradeoff, not a technical wall: that capability is broad
    enough (`mount`/`umount` of arbitrary filesystems, among other things)
    that granting it to close one read-confinement gap would hand a
    compromised sandbox process -- or the API process itself, since
    capabilities are not selectively droppable per-subprocess without
    further code this task's path scope does not include -- meaningfully
    more power than the gap it would close. Uses the stock
    `python:3.12-slim` base directly (the same base the shipped image
    builds from) rather than the built sandbox image, since this is a
    property of the container runtime's capability/security policy, not of
    anything this task's own image adds.
    """
    if not _docker_available():
        pytest.skip("docker is not available in this environment")
    probe = ["unshare", "--user", "--mount", "--map-root-user", "echo", "probe-ok"]
    without_cap = subprocess.run(
        ["docker", "run", "--rm", "--user", "999:999", "python:3.12-slim", *probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    with_cap = subprocess.run(
        ["docker", "run", "--rm", "--user", "999:999", "--cap-add=SYS_ADMIN", "python:3.12-slim", *probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert without_cap.returncode != 0
    assert "probe-ok" not in without_cap.stdout
    assert with_cap.returncode == 0
    assert "probe-ok" in with_cap.stdout


# ---------------------------------------------------------------------------
# Combined: everything the shipped Helm chart actually asks for, at once,
# against the real sandbox pipeline (both hops).
# ---------------------------------------------------------------------------


def test_full_sandbox_pipeline_succeeds_under_the_combined_hardened_deployment_profile(
    sandbox_image: str, host_script_path: Path
) -> None:
    """The parser-then-drafter pipeline `registry.arc.service.drafter`
    actually drives in production, run end to end inside one container
    configured with every control `deploy/helm/values.yaml` ships
    (`readOnlyRootFilesystem`, `capabilities.drop: [ALL]`,
    `allowPrivilegeEscalation: false`, non-root uid/gid, a memory ceiling)
    plus the `cpuset` pinning Kubernetes cannot force without node-level
    cooperation but plain Docker can. Every earlier test in this file
    proves one property in isolation; this is the "none of them broke each
    other, or the thing they are all protecting" check.
    """
    script = _write_script(host_script_path, _FULL_PIPELINE_SCRIPT)
    outcome = _docker_run_json(
        sandbox_image,
        script,
        "--memory=1536m",
        "--cpuset-cpus=0",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m,mode=1777",  # noqa: S108 - a `docker run --tmpfs` mount spec inside the container, not this host
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        "999:1500",
    )
    assert outcome["exit_code"] == 0, outcome["stderr"]
    assert outcome["json"].get("parser_ok") is True
    assert outcome["json"].get("drafter_ok") is True
    assert outcome["json"].get("affinity") == [0]


# ---------------------------------------------------------------------------
# Re-proven inside the deployed container specifically: peer-mismatch and
# malformed-frame refusals are proven "in the deployed namespaces" here, not
# because the logic differs, but because Linux takes a different
# authentication code path (`SO_PEERCRED`) than every darwin-hosted run of
# the sibling suites ever exercises (`getpeereid(2)`).
# ---------------------------------------------------------------------------


def test_peer_uid_authentication_uses_so_peercred_on_linux_and_refuses_a_mismatched_peer(
    sandbox_image: str, host_script_path: Path
) -> None:
    script = _write_script(host_script_path, _PEER_MISMATCH_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script)
    body = outcome["json"]
    assert body.get("platform", "").startswith("linux"), body
    assert body.get("exit_code") == 1
    assert "sandbox_peer_mismatch" in body.get("stderr", "")
    assert body.get("client_refusal") is not None


def test_oversize_frame_is_refused_as_a_bounded_transport_error_in_the_deployed_container(
    sandbox_image: str, host_script_path: Path
) -> None:
    script = _write_script(host_script_path, _OVERSIZE_FRAME_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script)
    body = outcome["json"]
    assert body.get("client_saw_close") is True
    assert body.get("exit_code") == 1
    assert "sandbox_frame_too_large" in body.get("stderr", "")


def test_outbound_network_connection_is_refused_inside_the_deployed_container(
    sandbox_image: str, host_script_path: Path
) -> None:
    """The network guard is process-level Python state (replaced
    `socket.socket`/`getaddrinfo`), so it is not inherently platform
    dependent -- but it has never before been observed to fire inside the
    actual container image and namespace the deployment target runs, only
    on a developer's own machine."""
    script = _write_script(host_script_path, _NETWORK_GUARD_SCRIPT)
    outcome = _docker_run_json(sandbox_image, script)
    body = outcome["json"]
    assert outcome["exit_code"] == 0, outcome["stderr"]
    assert body["outbound_socket_construction_succeeded"] is False
    assert body["dns_resolution_succeeded"] is False
