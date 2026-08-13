"""Re-export of the Postgres run broker, which lives under ``scripts/``.

The broker leases per-worker databases, plans the worker set, and signs the
control file the workers read. It is shared by the parent-side runner and this
suite's helpers and tests. It lives under ``scripts/`` because ``scripts/`` must
not import from ``tests/`` — the dependency runs the other way, the same
direction as ``tests/helpers/pg_provider.py`` importing ``scripts.devstack``.
This module keeps the test-facing import path stable so callers need not know
where the implementation sits.

**The spelling below is load-bearing: it must match what everything under
``scripts/`` uses, which is the flat one.** ``pythonpath = ["scripts"]`` makes
both ``pg_run_broker`` and ``scripts.pg_run_broker`` importable, and they are
two module objects, not one — so each exception class exists twice under two
identities that no ``except`` clause can bridge. `scripts/pg_provider.py` builds
the broker through the flat spelling, so a shim re-exporting the dotted one
hands tests classes their own fixtures can never raise: `pytest.raises` fails
to match, the exception escapes, and the assertion behind it never runs. Two
provider-lifecycle cases failed exactly that way, and they failed *silently* in
the sense that mattered — the refusal they existed to prove was firing
correctly the whole time.

Aligning here rather than in the tests is deliberate. Every symbol below has
the same latent split; the two that surfaced are only the ones a
``pytest.raises`` made visible. And aligning the other way — moving
``scripts/`` to the dotted spelling — is not available: mypy refuses a source
file reachable under two module names and stops before checking anything.
"""

from __future__ import annotations

from pg_run_broker import (
    AdmissionError,
    BrokerBoundary,
    BrokerError,
    BrokerManifest,
    CapabilityError,
    ControlError,
    ControlPayload,
    Inventory,
    LeaseError,
    ProviderCapabilities,
    RunBroker,
    SequenceLease,
    WorkerPlan,
    _utc_now,  # noqa: F401 - the broker's unit tests freeze this clock through the stable import path
    control_ttl_expiry,
    parse_control,
    plan_workers,
    redacted_digest,
    serialize_control,
    sign_control,
    write_control_file,
)

__all__ = [
    "AdmissionError",
    "BrokerBoundary",
    "BrokerError",
    "BrokerManifest",
    "CapabilityError",
    "ControlError",
    "ControlPayload",
    "Inventory",
    "LeaseError",
    "ProviderCapabilities",
    "RunBroker",
    "SequenceLease",
    "WorkerPlan",
    "control_ttl_expiry",
    "parse_control",
    "plan_workers",
    "redacted_digest",
    "serialize_control",
    "sign_control",
    "write_control_file",
]
