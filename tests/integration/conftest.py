"""Shared integration fixtures: Postgres + pgvector, FakeClock, and respx
cassette transport for connector HTTP mocking.

One database per pytest session, migrated to head before any test runs.
Where that database comes from is chosen by ``CONTEXTPLANE_TEST_PG`` — a
container, a locally managed cluster, or one you point at with
``DATABASE_URL``. See ``tests/helpers/pg_provider.py``; no container
runtime is required.

Note that `db_session` does *not* roll back: it commits like the
application does, so triggers and constraints behave the way they will
in production. Isolation between sessions comes from the database being
created fresh and dropped afterwards, not from transaction rollback.

Cassette infrastructure
-----------------------
``respx_cassette(connector_name)`` is a session-scoped factory fixture.  Call
it inside a test or fixture to get a ``respx.MockRouter`` pre-loaded with all
``cassette_*.json`` files found under
``tests/fixtures/connectors/<connector_name>/``.

Cassette file schema (each file is one HTTP exchange)::

    {
        "request": {
            "method": "GET",
            "url": "https://...",
            "headers": {"Authorization": "Bearer test-token"}
        },
        "response": {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": <string or JSON-serialisable object>
        }
    }

Set ``REFRESH_CASSETTES=1`` to enable pass-through mode (no mocking); real
network calls are made and results can be captured to update the cassette files.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import respx
from httpx import Response

from contextplane.config import Settings
from tests.helpers.pg_provider import test_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "connectors"


def _load_cassette(path: Path) -> dict:
    """Load and parse a single cassette JSON file."""
    with path.open() as fh:
        return json.load(fh)


def _response_from_cassette(entry: dict) -> Response:
    """Build an httpx.Response from the ``response`` block of a cassette entry."""
    resp = entry["response"]
    body = resp["body"]
    if not isinstance(body, str):
        body = json.dumps(body)
    return Response(
        status_code=resp.get("status", 200),
        headers=resp.get("headers", {}),
        text=body,
    )


# ---------------------------------------------------------------------------
# Cassette fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def respx_cassette() -> Callable[[str], respx.MockRouter]:
    """Return a factory that loads cassette files for a named connector.

    Usage::

        def test_something(respx_cassette):
            router = respx_cassette("openapi")
            with router:
                # all httpx calls are intercepted
                ...

    When ``REFRESH_CASSETTES=1`` is set the returned router is in pass-through
    mode — every call is forwarded to the real network.  This is intentionally
    not activated in CI.
    """
    refresh = os.environ.get("REFRESH_CASSETTES", "").strip() == "1"

    def _factory(connector_name: str) -> respx.MockRouter:
        router = respx.MockRouter(assert_all_called=False)

        if refresh:
            # Pass-through: forward to real network.  Callers can capture
            # responses and overwrite the cassette files.
            router.pass_through(lambda request: True)  # type: ignore[arg-type]
            return router

        cassette_dir = _FIXTURES_ROOT / connector_name
        if not cassette_dir.is_dir():
            raise FileNotFoundError(f"No cassette directory for connector '{connector_name}': {cassette_dir}")

        cassette_files = sorted(cassette_dir.glob("cassette_*.json"))
        if not cassette_files:
            raise FileNotFoundError(f"No cassette_*.json files found in {cassette_dir}")

        for cassette_path in cassette_files:
            entry = _load_cassette(cassette_path)
            req = entry["request"]
            method = req["method"].upper()
            url = req["url"]
            response = _response_from_cassette(entry)
            router.route(method=method, url=url).mock(return_value=response)

        return router

    return _factory


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _set_http_methods_mode_for_integration() -> Iterator[None]:
    """Default CONTEXTPLANE_HTTP_METHODS_MODE to "both" for the integration suite.

    Several integration tests POST to alias paths like
    ``/v1/admin/external-systems/{slug}:delete`` and expect 204. Those
    aliases are only registered when the mode is ``both`` or ``post_only``.
    The default mode is ``rest``, which leaves them unregistered.

    Scope this to the integration session so unit tests (which explicitly
    verify the rest-default behavior) are unaffected. Routers register
    routes at module-import time, so we set the env var *before* the first
    integration test imports a router. Tests that need a specific mode
    (``test_http_methods_mode.py``) override the env var and reload the
    affected modules.
    """
    prev = os.environ.get("CONTEXTPLANE_HTTP_METHODS_MODE")
    os.environ["CONTEXTPLANE_HTTP_METHODS_MODE"] = "both"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CONTEXTPLANE_HTTP_METHODS_MODE", None)
        else:
            os.environ["CONTEXTPLANE_HTTP_METHODS_MODE"] = prev


@pytest.fixture(scope="session")
def app_settings(pg_container: str) -> Settings:
    """Overrides the root `app_settings` fixture -- this suite boots a real app.

    Integration tests drive a live FastAPI app through its lifespan, which
    starts the scheduler; the root fixture (used by unit/conformance/perf,
    which never construct a real app) has no reason to carry
    `scheduler_use_memory_jobstore`. That is the one field this version
    adds; everything else matches the root fixture on purpose.
    """
    return Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        # MemoryJobStore avoids the APScheduler-pickles-engine-closure error
        # ("Can't get local object 'create_engine.<locals>.connect'") that
        # SQLAlchemyJobStore hits when a TestClient's lifespan starts the
        # scheduler. Production deploys use SQLAlchemyJobStore.
        scheduler_use_memory_jobstore=True,
        # Settings is a plain dataclass and ignores the environment, so leaving
        # this unset picks up the production default: an in-process model read
        # from a path that only exists inside the container image. create_app()
        # would raise. Tests that want real vectors set it themselves.
        embedding_provider="stub",
    )


# `db_session`, `fake_clock`, and `event_loop` are not redefined here -- the
# root `tests/conftest.py` versions are identical to what this suite needs,
# and pytest resolves fixtures up the conftest tree, so this file inherits
# them without a duplicate definition.


# ---------------------------------------------------------------------------
# Broker manifest URL handoff
# ---------------------------------------------------------------------------

#: Set by the sealed runner on every worker it dispatches, sealed or not, because
#: the reporter is gated on it: a worker with no identity discloses no outcomes
#: and reconciliation then fails every node as undisclosed.
#:
#: So identity answers "which worker is this", never "was this worker assigned a
#: database". It used to answer both, and the two are true on different
#: schedules -- every dispatched worker has an identity, only a sealed one has an
#: assignment. Keying the handoff on identity made an ordinary
#: `make test-integration` error every database-touching test, because a run with
#: no controller still dispatches workers that carry one.
_WORKER_ID_VARIABLE = "CONTEXTPLANE_INTEGRATION_WORKER_ID"

#: Set only when a sealed sequence provisioned databases for this run. This is
#: what the handoff keys on: its presence is the claim "a broker assigned you a
#: database", and it is set in the same place that assignment is made, so it
#: cannot be true while the assignment is absent by construction.
_SEALED_RUN_VARIABLE = "CONTEXTPLANE_INTEGRATION_SEALED_RUN"

#: The database this worker was assigned. Under the runner it is the whole
#: answer: the worker consumes it and never asks a provider for anything.
_ASSIGNED_URL_VARIABLE = "CONTEXTPLANE_TEST_DATABASE_URL"

#: The digest of the broker manifest the assignment came from. Required so a
#: worker cannot be handed a URL by something that is not the broker holding
#: this sequence's lease; an assignment with no manifest behind it is a URL of
#: unknown origin.
_MANIFEST_DIGEST_VARIABLE = "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST"


class BrokerHandoffError(RuntimeError):
    """A worker cannot establish which database it was assigned."""


@dataclass(frozen=True)
class WorkerAssignment:
    """What a runner-worker is allowed to use, and nothing more."""

    worker_id: str
    database_url: str
    manifest_digest: str


def runner_worker_assignment(environ: Mapping[str, str]) -> WorkerAssignment | None:
    """The assignment, or `None` when this is not a runner-worker.

    Fails closed on purpose. Under a sealed sequence, a missing URL or digest
    raises rather than falling through to provisioning: a worker that quietly
    stood up its own server would produce a green run measuring a topology
    nobody chose, and the timing of the run that did it would look like
    everyone else's. The absent-variable case is therefore the dangerous one,
    not the malformed one.

    The fallback is reachable, and that is load-bearing rather than incidental.
    An unsealed run -- what every developer and every other lane invokes --
    dispatches workers with an identity and no assignment, and must reach the
    ordinary provider path. A fallback that exists but can never be entered is
    a worse shape than a plain contradiction, because reading either side alone
    leaves you satisfied.
    """
    if not environ.get(_SEALED_RUN_VARIABLE):
        return None

    worker_id = environ.get(_WORKER_ID_VARIABLE)
    if not worker_id:
        msg = (
            "a sealed run dispatched a worker with no identity. The marker is set only where the "
            f"assignment is made, so {_SEALED_RUN_VARIABLE} without {_WORKER_ID_VARIABLE} means the "
            "child environment was assembled by something other than the runner."
        )
        raise BrokerHandoffError(msg)

    url = environ.get(_ASSIGNED_URL_VARIABLE)
    digest = environ.get(_MANIFEST_DIGEST_VARIABLE)
    missing = [
        name for name, value in ((_ASSIGNED_URL_VARIABLE, url), (_MANIFEST_DIGEST_VARIABLE, digest)) if not value
    ]
    if missing:
        msg = (
            f"worker {worker_id!r} was dispatched without {', '.join(missing)}. A worker consumes the "
            "database the broker assigned it and never provisions, migrates, or selects a provider, so "
            "there is no fallback here -- continuing would stand up a second server inside a measured run."
        )
        raise BrokerHandoffError(msg)
    assert url is not None and digest is not None  # narrowed by the check above
    return WorkerAssignment(worker_id=worker_id, database_url=url, manifest_digest=digest)


@pytest.fixture(scope="session")
def pg_container() -> Iterator[str]:
    """Overrides the root fixture so a runner-worker provisions nothing.

    Outside the runner this is the ordinary path and behaves exactly as the root
    fixture does — a developer running the suite directly still gets a database
    chosen by ``CONTEXTPLANE_TEST_PG``.

    Under the runner it yields the assigned URL and does not enter
    ``test_database()`` at all. That is the point rather than an optimization:
    entering it would let the worker choose a provider, create a database, and
    run migrations, and one server per worker is a different system from the one
    the measurement is about.
    """
    assignment = runner_worker_assignment(os.environ)
    if assignment is None:
        with test_database() as url:
            yield url
        return
    yield assignment.database_url
