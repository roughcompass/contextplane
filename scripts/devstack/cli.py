"""Command line for the dev stack: up, down, status, reset, logs.

Wrapped by `make dev-up` and friends. Everything it prints is aimed at
answering the question a developer actually has at that moment — what is
running, on which ports, and what to type next.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .cluster import Cluster, ClusterError
from .config import Ports, database_url, render_env_file
from .pg_provider import (
    DEFAULT_DATABASE,
    ExternalPostgres,
    LocalPostgres,
    PostgresUnavailableError,
    resolve,
    resolve_local,
)
from .supervisor import PortConflict, Supervisor, check_ports, port_in_use, services

REPO_ROOT = Path(__file__).resolve().parents[2]


def _devstack_dir() -> Path:
    return REPO_ROOT / ".devstack"


def _cluster(source: LocalPostgres, ports: Ports) -> Cluster:
    return Cluster(source, _devstack_dir() / "pgdata", port=ports.postgres)


def _resolve_or_exit() -> ExternalPostgres | LocalPostgres:
    try:
        return resolve()
    except PostgresUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _run_migrations(env: dict[str, str]) -> None:
    print("  applying migrations ...", end=" ", flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print("failed")
        print(completed.stdout, file=sys.stderr)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit(1)
    print("ok")


def cmd_up(args: argparse.Namespace) -> int:
    ports = Ports.from_env()
    source = _resolve_or_exit()
    supervisor = Supervisor(REPO_ROOT, ports)

    # A stale stack from a previous run holds its ports too; clear it out
    # before the conflict check so `dev-up` is safely repeatable.
    if supervisor.recorded_pids():
        supervisor.stop_all()
        supervisor.clear_state()

    service_list = services(ports)
    # Port keys, not service names: the obs sink owns two of them.
    managed = ["oidc", "entitlements", "api", "otlp", "viewer"]
    if isinstance(source, LocalPostgres):
        managed.append("postgres")
    try:
        check_ports(ports, include=managed)
    except PortConflict as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Postgres source: {source.label}")

    if isinstance(source, LocalPostgres):
        if source.major != 16:
            print(
                f"  note: PostgreSQL {source.version}; CI and the container image "
                "run 16. Differences are usually harmless but not guaranteed."
            )
        cluster = _cluster(source, ports)
        print("  starting cluster ...", end=" ", flush=True)
        try:
            cluster.start()
            cluster.create_database(DEFAULT_DATABASE)
        except ClusterError as exc:
            print("failed")
            print(str(exc), file=sys.stderr)
            return 1
        print(f"ok (port {ports.postgres})")
        env = supervisor.env()
    else:
        # Somebody else's database: connect, migrate, but never manage.
        env = {**supervisor.env(), "DATABASE_URL": source.url}
        env["PGBOUNCER_URL"] = source.url
        env["SCHEDULER_JOBSTORE_URL"] = source.url

    _run_migrations(env)

    for service in service_list:
        print(f"  starting {service.name} ...", end=" ", flush=True)
        pid = supervisor.start(service, env)
        supervisor.write_state({**supervisor.recorded_pids(), service.name: pid})
        try:
            supervisor.wait_healthy(service)
        except RuntimeError as exc:
            print("failed")
            print(str(exc), file=sys.stderr)
            return 1
        print(f"ok (pid {pid}, port {service.port})")

    env_path = _devstack_dir() / "env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(render_env_file(ports), encoding="utf-8")

    _print_summary(ports, source, env_path)
    return 0


def _print_summary(ports: Ports, source: ExternalPostgres | LocalPostgres, env_path: Path) -> None:
    database = f"localhost:{ports.postgres}" if isinstance(source, LocalPostgres) else "external"
    print()
    print("  Service                URL")
    print("  ---------------------  ------------------------------")
    print(f"  registry API           http://localhost:{ports.api}")
    print(f"  API docs               http://localhost:{ports.api}/docs")
    print(f"  Postgres               {database}")
    print(f"  mock OIDC provider     http://localhost:{ports.oidc}/default")
    print(f"  mock entitlements      http://localhost:{ports.entitlements}")
    print(f"  observability          http://localhost:{ports.viewer}")
    print()
    print(f"  Environment written to {env_path.relative_to(REPO_ROOT)}")
    print()
    print("  Next:")
    print("    make dev-token          # seed a dev tenant + client credentials")
    print("    export TOKEN=$(make dev-jwt)")
    print('    curl -H "Authorization: Bearer $TOKEN" ' f"http://localhost:{ports.api}/v1/whoami")
    print()
    print("    make dev-logs SVC=api   # tail a service")
    print("    make dev-down           # stop everything")


def cmd_down(args: argparse.Namespace) -> int:
    ports = Ports.from_env()
    supervisor = Supervisor(REPO_ROOT, ports)

    stopped = supervisor.stop_all()
    supervisor.clear_state()
    for name in stopped:
        print(f"  stopped {name}")

    local: LocalPostgres | None
    try:
        local = resolve_local()
    except PostgresUnavailableError:
        local = None
    if local is not None:
        cluster = _cluster(local, ports)
        if cluster.is_running():
            cluster.stop()
            print("  stopped postgres")

    if not stopped and local is None:
        print("  nothing was running")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ports = Ports.from_env()
    supervisor = Supervisor(REPO_ROOT, ports)

    try:
        source: ExternalPostgres | LocalPostgres | None = resolve()
    except PostgresUnavailableError:
        source = None

    print(f"Postgres source: {source.label if source else 'unavailable'}")

    if isinstance(source, LocalPostgres):
        cluster = _cluster(source, ports)
        running = cluster.is_running()
        state = "running" if running else "stopped"
        print(f"  {'postgres':<14} {state:<9} port {ports.postgres}")
    elif isinstance(source, ExternalPostgres):
        reachable = "reachable" if port_in_use(ports.postgres) else "external"
        print(f"  {'postgres':<14} {reachable:<9} {source.label}")

    pids = supervisor.recorded_pids()
    for service in services(ports):
        healthy, detail = supervisor.health(service)
        state = "healthy" if healthy else "down"
        pid = pids.get(service.name)
        suffix = f"pid {pid}" if pid and healthy else detail
        print(f"  {service.name:<14} {state:<9} port {service.port:<6} {suffix}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    ports = Ports.from_env()
    supervisor = Supervisor(REPO_ROOT, ports)
    supervisor.stop_all()
    supervisor.clear_state()

    try:
        source = resolve_local()
    except PostgresUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cluster = _cluster(source, ports)
    print("  destroying cluster ...", end=" ", flush=True)
    cluster.destroy()
    print("ok")
    return cmd_up(args)


def cmd_logs(args: argparse.Namespace) -> int:
    log_dir = _devstack_dir() / "logs"
    if not log_dir.is_dir():
        print("no logs yet — run `make dev-up` first", file=sys.stderr)
        return 1

    if args.service:
        path = log_dir / f"{args.service}.log"
        if not path.is_file():
            available = ", ".join(sorted(p.stem for p in log_dir.glob("*.log")))
            print(f"no log for {args.service!r}; available: {available}", file=sys.stderr)
            return 1
        paths = [path]
    else:
        paths = sorted(log_dir.glob("*.log"))

    argv = ["tail", "-n", str(args.lines)]
    if args.follow:
        argv.append("-f")
    argv.extend(str(p) for p in paths)
    return subprocess.call(argv)


def cmd_url(args: argparse.Namespace) -> int:
    """Print the database URL. Useful for one-off psql/alembic invocations."""
    ports = Ports.from_env()
    try:
        source = resolve()
    except PostgresUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(source.url if isinstance(source, ExternalPostgres) else database_url(ports))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.devstack",
        description="Run the local development stack without a container runtime.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("up", help="start every service and apply migrations")
    sub.add_parser("down", help="stop every service")
    sub.add_parser("status", help="show what is running")
    sub.add_parser("reset", help="destroy the database and start clean")
    sub.add_parser("url", help="print the database URL")

    logs = sub.add_parser("logs", help="tail service logs")
    logs.add_argument("service", nargs="?", help="one of: oidc entitlements obs api")
    logs.add_argument("-n", "--lines", type=int, default=50)
    logs.add_argument("-f", "--follow", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "up": cmd_up,
        "down": cmd_down,
        "status": cmd_status,
        "reset": cmd_reset,
        "logs": cmd_logs,
        "url": cmd_url,
    }
    return handlers[args.command](args)
