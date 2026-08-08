"""Operator surface: is anything wrong right now.

Answers a different question from the metrics endpoint, and is shaped for a
different reader. `/metrics` serves a scraper and is credentialed for one; this
serves a person in a console and is gated on a role.

**Why the console cannot get this anywhere else.** The Prometheus exposition is
per-replica and cumulative, so a browser reading it renders one arbitrary
process while presenting it as the service. A dashboard tool would answer it,
but that tool is optional deployment infrastructure — a console that depends on
one is a blank page wherever it was not installed. So the service answers for
itself.

**On the gate.** This is `admin`, which in this API means *tenant* administrator
rather than service operator; there is no service-operator identity in the REST
surface at all, by design. The response is service-global — queue depths and
parse-failure counts — so a tenant admin does learn something about the shared
deployment. That is a deliberate, narrow disclosure: no tenant is named, no
actor, no entity, and no row content. The alternative is that the operational
console is reachable by nobody, since the people who run the service do not hold
a REST identity.

**Every number carries its own provenance.** A queue depth is counted from the
table and is correct under any replica count; a data-quality counter is this
process's, cumulative since it started. Rendered side by side they look
identical, so `scope` and `kind` are required fields on every reading rather
than optional annotations.
"""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.routers._admin_common import _admin_required
from contextplane.service.operations.health import Reading, collect_operational_health

router = APIRouter(prefix="/v1/admin")


class ReadingOut(BaseModel):
    """One number and everything needed to read it correctly.

    `scope` and `kind` have no defaults and are not optional. A response type
    that could omit them would eventually omit them, and the result is a figure
    that looks like a service-wide total and is one replica's count since it
    last restarted.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: float | None = Field(
        description=(
            "Null when the value could not be read. Deliberately not zero: an "
            "unreadable table is not an empty queue, and reporting one as the "
            "other shows the healthiest possible state at the worst moment."
        )
    )
    scope: Literal["cluster", "process"] = Field(
        description=(
            "'cluster' is counted from the database at read time and is correct "
            "under any number of replicas. 'process' is this replica's counter — "
            "a load-balanced read reaches an arbitrary process, so zero here "
            "does not prove zero everywhere."
        )
    )
    kind: Literal["gauge", "counter"] = Field(
        description=(
            "'gauge' is current state and can fall. 'counter' is cumulative "
            "since the process started and resets on deploy; a single reading "
            "of one is not a rate."
        )
    )
    instance: str | None = Field(
        default=None,
        description="Which replica answered. Set only for process-scoped readings.",
    )
    actionable: str | None = Field(
        default=None,
        description="Why a non-zero value matters. Present when it is actionable on sight.",
    )


class OperationalHealthOut(BaseModel):
    """Response body for GET /operational-health: metrics an operator should see on demand rather than scrape."""

    model_config = ConfigDict(extra="forbid")

    observed_at: datetime.datetime
    queues: list[ReadingOut]
    data_quality: list[ReadingOut]


def _to_out(reading: Reading) -> ReadingOut:
    return ReadingOut(
        key=reading.key,
        label=reading.label,
        value=reading.value,
        scope=reading.scope,
        kind=reading.kind,
        instance=reading.instance,
        actionable=reading.actionable,
    )


@router.get(
    "/operational-health",
    response_model=OperationalHealthOut,
    tags=["admin: operations"],
    summary="Operational conditions an operator should meet rather than search for",
    dependencies=[Depends(_admin_required)],
)
async def get_operational_health(request: Request) -> OperationalHealthOut:
    """Collect queue depths, data-quality checks, and other actionable operator metrics from the current instant."""
    health = await collect_operational_health(
        request.app.state.session_factory,
        now=datetime.datetime.now(tz=datetime.UTC),
    )
    return OperationalHealthOut(
        observed_at=health.observed_at,
        queues=[_to_out(r) for r in health.queues],
        data_quality=[_to_out(r) for r in health.data_quality],
    )
