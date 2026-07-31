"""Start, stop, and health-check the dev stack's processes.

Compose gets process supervision, dependency ordering, and health gating
for free. Without it, this module provides the parts that actually
matter for a dev loop: start things in an order that works, wait until
each is genuinely ready rather than merely spawned, record PIDs so a
later `down` can find them, and refuse to start into a port that is
already taken.

That last one is not defensive padding. Both providers publish the same
ports by design, so a developer with a forgotten `docker compose up`
running would otherwise get a stack that half-works, with requests
landing on whichever listener the OS resolved first. Failing loudly with
the reason is much kinder than that.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Ports, build_env

# How long to wait for a service to answer its health check before
# giving up. The API is the slow one — it imports the app, connects to
# Postgres, and starts the scheduler.
STARTUP_TIMEOUT_S = 90.0
HEALTH_POLL_INTERVAL_S = 0.4

# Grace period between SIGTERM and SIGKILL when stopping.
SHUTDOWN_GRACE_S = 10.0


@dataclass(frozen=True)
class Service:
    """A supervised child process."""

    name: str
    argv: Sequence[str]
    port: int
    health_url: str
    description: str


def services(ports: Ports, python: str | None = None) -> list[Service]:
    """The processes the dev stack runs, in start order."""
    exe = python or sys.executable
    return [
        Service(
            name="oidc",
            argv=[
                exe,
                "-m",
                "uvicorn",
                "tests.mocks.oidc_server.app:app",
                "--host",
                "localhost",
                "--port",
                str(ports.oidc),
                "--log-level",
                "warning",
            ],
            port=ports.oidc,
            health_url=f"http://localhost:{ports.oidc}/healthz",
            description="mock OIDC provider",
        ),
        Service(
            name="entitlements",
            argv=[
                exe,
                "-m",
                "uvicorn",
                "tests.mocks.entitlement_service.app:app",
                "--host",
                "localhost",
                "--port",
                str(ports.entitlements),
                "--log-level",
                "warning",
            ],
            port=ports.entitlements,
            health_url=f"http://localhost:{ports.entitlements}/healthz",
            description="mock entitlement service",
        ),
        Service(
            name="obs",
            argv=[
                exe,
                "-m",
                "scripts.devstack.obs_sink",
                "--port",
                str(ports.otlp),
                "--viewer-port",
                str(ports.viewer),
                "--metrics-url",
                f"http://localhost:{ports.api}/metrics",
            ],
            port=ports.viewer,
            health_url=f"http://localhost:{ports.viewer}/healthz",
            description="traces + metrics sink",
        ),
        Service(
            name="api",
            argv=[
                exe,
                "-m",
                "uvicorn",
                "registry.main:create_app",
                "--factory",
                "--reload",
                "--host",
                "localhost",
                "--port",
                str(ports.api),
                "--timeout-keep-alive",
                "5",
            ],
            port=ports.api,
            health_url=f"http://localhost:{ports.api}/healthz",
            description="registry API",
        ),
    ]


def port_in_use(port: int) -> bool:
    """True if anything accepts a connection on *port* over v4 or v6.

    Connecting rather than binding is deliberate. A wildcard listener on
    one address family (what a published container port looks like) can
    coexist with a successful bind on the other, so a bind test reports
    the port free while requests still reach the other process.
    """
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.35)
                if sock.connect_ex((address, port)) == 0:
                    return True
        except OSError:
            continue
    return False


class PortConflict(RuntimeError):
    """Something is already listening on a port the dev stack needs."""


def check_ports(ports: Ports, *, include: Sequence[str] | None = None) -> None:
    """Raise PortConflict naming every occupied port and the likely cause.

    *include* names keys of `Ports`, not service names — one service can
    own two ports (the sink listens for OTLP on one and serves the viewer
    on another). Unknown keys raise rather than being ignored, because a
    silently-skipped key is a port that never gets checked.
    """
    wanted = ports.as_dict()
    if include is not None:
        unknown = sorted(set(include) - set(wanted))
        if unknown:
            raise ValueError(f"unknown port keys {unknown}; expected any of {sorted(wanted)}")
        wanted = {k: v for k, v in wanted.items() if k in include}

    taken = {name: port for name, port in wanted.items() if port_in_use(port)}
    if not taken:
        return

    listed = "\n".join(f"  {name:<14} port {port}" for name, port in sorted(taken.items()))
    raise PortConflict(
        "Something is already listening on ports the dev stack needs:\n\n"
        f"{listed}\n\n"
        "Two likely causes:\n\n"
        "  1. A previous dev stack is still up, or did not shut down cleanly:\n"
        "       make dev-down\n\n"
        "  2. The compose stack is running. Both providers publish the same\n"
        "     ports on purpose, so they cannot run at once:\n"
        "       docker compose down\n\n"
        "Or move this stack out of the way:\n\n"
        "  DEVSTACK_PORT_OFFSET=100 make dev-up\n"
    )


class Supervisor:
    """Owns the child processes and the state file that survives between runs."""

    def __init__(self, root: Path, ports: Ports) -> None:
        self.root = root
        self.ports = ports
        self.devstack_dir = root / ".devstack"
        self.log_dir = self.devstack_dir / "logs"
        self.state_path = self.devstack_dir / "state.json"

    # -- state ------------------------------------------------------------

    def read_state(self) -> dict[str, object]:
        if not self.state_path.is_file():
            return {}
        try:
            loaded: dict[str, object] = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded

    def write_state(self, pids: dict[str, int]) -> None:
        self.devstack_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {"pids": pids, "ports": self.ports.as_dict(), "started_at": time.time()},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def clear_state(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def recorded_pids(self) -> dict[str, int]:
        raw = self.read_state().get("pids")
        if not isinstance(raw, dict):
            return {}
        return {str(name): int(pid) for name, pid in raw.items()}

    # -- lifecycle --------------------------------------------------------

    def start(self, service: Service, env: dict[str, str]) -> int:
        """Spawn *service*, returning its pid. Output goes to its log file."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{service.name}.log"
        handle = log_path.open("a", encoding="utf-8")
        handle.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        handle.flush()

        merged = {**os.environ, **env}
        # The mock services and the obs sink are imported as
        # `tests.mocks...` / `scripts.devstack...`, so the repo root has
        # to be importable regardless of where make was invoked from.
        existing = merged.get("PYTHONPATH")
        merged["PYTHONPATH"] = f"{self.root}{os.pathsep}{existing}" if existing else str(self.root)

        process = subprocess.Popen(  # noqa: S603 - argv is built here, not user input
            list(service.argv),
            cwd=self.root,
            env=merged,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return process.pid

    def wait_healthy(self, service: Service, *, timeout: float = STARTUP_TIMEOUT_S) -> None:
        """Poll until *service* answers its health check, or raise."""
        deadline = time.monotonic() + timeout
        last_error = "no response"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(service.health_url, timeout=3.0)
                if response.status_code < 400:
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(HEALTH_POLL_INTERVAL_S)

        log_path = self.log_dir / f"{service.name}.log"
        tail = ""
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8").splitlines()[-30:]
            tail = "\n".join(lines)
        raise RuntimeError(
            f"{service.name} ({service.description}) did not become healthy within "
            f"{timeout:.0f}s — last attempt: {last_error}\n"
            f"--- {log_path} ---\n{tail}"
        )

    def health(self, service: Service) -> tuple[bool, str]:
        """One-shot health probe, for `dev-status`."""
        try:
            response = httpx.get(service.health_url, timeout=2.0)
        except httpx.HTTPError as exc:
            return False, type(exc).__name__
        return response.status_code < 400, f"HTTP {response.status_code}"

    def stop_all(self) -> list[str]:
        """SIGTERM every recorded pid, escalating to SIGKILL. Returns what was stopped."""
        stopped: list[str] = []
        pids = self.recorded_pids()

        for name, pid in pids.items():
            if not _pid_alive(pid):
                continue
            try:
                # The children were started in their own session, so
                # signalling the group also catches uvicorn's reloader
                # subprocess.
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                with _suppress_lookup():
                    os.kill(pid, signal.SIGTERM)
            stopped.append(name)

        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        while time.monotonic() < deadline:
            if not any(_pid_alive(pid) for pid in pids.values()):
                break
            time.sleep(0.2)

        for pid in pids.values():
            if _pid_alive(pid):
                with _suppress_lookup():
                    os.killpg(os.getpgid(pid), signal.SIGKILL)

        return stopped

    def env(self) -> dict[str, str]:
        return build_env(self.ports)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _suppress_lookup:
    """Context manager swallowing the races inherent in signalling exiting pids."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, ProcessLookupError | PermissionError)
