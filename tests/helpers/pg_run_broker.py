"""Re-export of the Postgres run broker, which lives under ``scripts/``.

The broker leases per-worker databases, plans the worker set, and signs the
control file the workers read. It is shared by the parent-side runner and this
suite's helpers and tests. It lives under ``scripts/`` because ``scripts/`` must
not import from ``tests/`` — the dependency runs the other way, the same
direction as ``tests/helpers/pg_provider.py`` importing ``scripts.devstack``.
This module keeps the test-facing import path stable so callers need not know
where the implementation sits.
"""

from __future__ import annotations

from scripts.pg_run_broker import (
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
