"""What GitHub Actions observed about one workflow run, projected to an allowlist.

The one approved external source. It may assert exactly this: workflow run R,
attempt N, on commit S, concluded {success|failure|cancelled|timed_out|...} at
time T. It may not assert that a change was correct or wrong, that a task
succeeded, that code quality moved, or anything about a person. That boundary is
recorded on the source's registration and reaches a derived claim through the
authority the ledger stores; this adapter's part is to carry nothing that would
tempt a later reader past it.

**The raw delivery body is never persisted, and that is a security control rather
than tidiness.** Only the projected allowlist below reaches storage, so logs, step
output and free text — the places a credential actually shows up — have no path
into the ledger at all. The admission floor is the backstop; the projection is
what keeps the floor from being the only thing standing between a CI log and
durable storage.

**A re-run is a new event, not a mutation.** The external id is qualified by
attempt, so attempt 2 of run 42 is its own occurrence and both remain true. The
alternative — treating a re-run as an update to the first — would quietly rewrite
the evidence a derivation already cited.

**Event time is the run's `updated_at` on the completed delivery, and the
approximation is stated rather than hidden.** GitHub publishes no distinct
concluded-at; `updated_at` on a `completed` delivery is the closest honest reading,
and recording it as the event time while the observed time stays the moment of
delivery keeps the gap visible instead of collapsing it.

**Per-job conclusions are out of v1** because the subscribed event does not carry
them. An adapter that inferred one would be asserting something the source never
sent, which is precisely the authority boundary the source decision drew.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any, Final

from contextplane.signals.ingest import SIGNAL_SCHEMA_VERSION, ExternalSignalEnvelopeV1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from contextplane.types import TenantContext

GITHUB_ACTIONS_SOURCE_SYSTEM: Final[str] = "github-actions"

#: The only event consumed in v1. A second event type is a decision about what the
#: source may assert, not a parsing convenience.
SUPPORTED_ACTION: Final[str] = "completed"

#: The projection. Every field a stored signal may carry from a delivery, and
#: nothing else -- the raw body is dropped rather than filtered later, so a field
#: added to the webhook payload upstream cannot arrive here by default.
_PAYLOAD_ALLOWLIST: Final[tuple[str, ...]] = (
    "repository",
    "workflow_name",
    "workflow_path",
    "run_id",
    "run_attempt",
    "head_sha",
    "head_branch",
    "event",
    "status",
    "conclusion",
    "html_url",
    "run_started_at",
    "run_updated_at",
)


class GithubDeliveryRejected(ValueError):
    """A delivery this adapter will not translate.

    Distinct from a validation error on the envelope: this is the adapter saying
    the delivery is not the event it consumes, or is missing a field the external
    identity is built from. Refused rather than best-effort translated, because a
    signal whose identity was guessed cannot be deduplicated against the next one.
    """


def _require(delivery: Mapping[str, Any], key: str) -> object:
    """Return a field the external identity is built from, refusing an absent one.

    Typed `object` rather than `Any`: every value here comes from an external
    system's JSON and nothing downstream should be able to call methods on it
    without narrowing first.
    """
    value = delivery.get(key)
    if value is None or value == "":
        message = f"delivery is missing {key!r}, which the external identity is built from"
        raise GithubDeliveryRejected(message)
    return value


def _parse_moment(name: str, raw: object) -> datetime.datetime:
    """Parse an ISO-8601 instant, refusing a naive one.

    A timezone-naive timestamp from an external system is ambiguous by exactly the
    offset nobody recorded, and storing it would make the three times
    incomparable.
    """
    if isinstance(raw, datetime.datetime):
        moment = raw
    else:
        try:
            moment = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            message = f"{name} is not an ISO-8601 instant: {exc}"
            raise GithubDeliveryRejected(message) from exc
    if moment.tzinfo is None:
        message = f"{name} has no timezone; an ambiguous instant cannot be stored as one of the three times"
        raise GithubDeliveryRejected(message)
    return moment


def projected_payload(workflow_run: Mapping[str, Any], repository: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted projection of one `workflow_run.completed` delivery.

    Built by naming what may pass rather than by removing what may not: a
    deny-list would admit every field GitHub adds later, and the fields GitHub
    adds later are exactly the ones nobody reviewed.
    """
    projection: dict[str, Any] = {
        "repository": repository.get("full_name"),
        "workflow_name": workflow_run.get("name"),
        "workflow_path": workflow_run.get("path"),
        "run_id": workflow_run.get("id"),
        "run_attempt": workflow_run.get("run_attempt"),
        "head_sha": workflow_run.get("head_sha"),
        "head_branch": workflow_run.get("head_branch"),
        "event": workflow_run.get("event"),
        "status": workflow_run.get("status"),
        "conclusion": workflow_run.get("conclusion"),
        "html_url": workflow_run.get("html_url"),
        "run_started_at": workflow_run.get("run_started_at"),
        "run_updated_at": workflow_run.get("updated_at"),
    }
    return {key: projection[key] for key in _PAYLOAD_ALLOWLIST if projection.get(key) is not None}


def github_workflow_run_envelope(
    ctx: TenantContext,
    *,
    source_id: uuid.UUID,
    delivery: Mapping[str, Any],
    delivery_guid: str,
    received_at: datetime.datetime,
    producer_id: str,
    classification: str = "internal",
) -> ExternalSignalEnvelopeV1:
    """Translate one `workflow_run.completed` delivery into the shared envelope.

    `delivery_guid` becomes the submission key: it is the transport's own
    identifier for this delivery, so a redelivery of the same webhook converges
    rather than filing a second observation, and it is what the acceptance fixture
    is cross-checked against so the fixture attests to a capture rather than to
    somebody's memory.

    The external id is `github:workflow_run:{repo}:{run_id}:{run_attempt}` --
    attempt-qualified, so a re-run is a new occurrence rather than a mutation of
    the one a derivation may already cite.
    """
    if delivery.get("action") != SUPPORTED_ACTION:
        message = f"this adapter consumes workflow_run.{SUPPORTED_ACTION} only, not {delivery.get('action')!r}"
        raise GithubDeliveryRejected(message)

    workflow_run = delivery.get("workflow_run") or {}
    repository = delivery.get("repository") or {}
    if not isinstance(workflow_run, dict) or not isinstance(repository, dict):
        message = "delivery is not shaped like a workflow_run event"
        raise GithubDeliveryRejected(message)

    repo_full_name = _require(repository, "full_name")
    run_id = _require(workflow_run, "id")
    run_attempt = _require(workflow_run, "run_attempt")
    # No distinct concluded-at exists upstream; `updated_at` on a completed
    # delivery is the closest honest reading and the module docstring says so.
    event_time = _parse_moment("workflow_run.updated_at", _require(workflow_run, "updated_at"))

    return ExternalSignalEnvelopeV1(
        source_id=source_id,
        source_system=GITHUB_ACTIONS_SOURCE_SYSTEM,
        source_event_id=f"github:workflow_run:{repo_full_name}:{run_id}:{run_attempt}",
        producer_id=producer_id,
        producer_type="external",
        idempotency_key=delivery_guid,
        classification=classification,
        schema_version=SIGNAL_SCHEMA_VERSION,
        event_time=event_time,
        observed_time=received_at,
        payload=projected_payload(workflow_run, repository),
        team_key=None,
        project_key=None,
    )


__all__ = [
    "GITHUB_ACTIONS_SOURCE_SYSTEM",
    "SUPPORTED_ACTION",
    "GithubDeliveryRejected",
    "github_workflow_run_envelope",
    "projected_payload",
]
