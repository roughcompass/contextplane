"""What an orchestrator reports about how one piece of delivery work concluded.

The envelope surface already accepts outcome submissions, and that is the whole
reason this module exists rather than a new route. It validates size,
classification, times and payload exclusivity -- and nothing about outcome
*meaning*. So it will happily accept an observation asserting that a deployment
succeeded while citing no external work at all: stored, admitted, `201`, and
joinable to nothing. This is where that is refused.

**No new source, and no new authority.** The registered seat the outcome arrives
under is the one already approved for it, and its authority comes from that
registration exactly as it does for every other producer. This module introduces
no source system of its own: it is a translation function that takes the
registered seat's identity and returns the shared envelope. Adding an authority
here would be laundering one, which is the line the adapter charter draws.

**An outcome with no reference is refused, not stored.** The receipt-to-outcome
join is mediated entirely by the shared external-reference rows: a receipt binds
the work it was resolved about, an outcome binds the work it concluded, and they
meet because both point at the same reference. An outcome citing nothing cannot
meet anything, and the failure is silent -- it looks exactly like an outcome
that has not arrived yet. Refusing at submission converts a permanent invisible
gap into an error the submitter can fix in the moment.

**Kinds are checked against the closed set, and this is the subtle one.** A
reference's `kind` is part of its collision scope, so an outcome written with a
misspelled kind does not fail: it creates a *second* reference row, binds to it
cleanly, and then never joins to the receipt that cited the correct spelling for
the same external id. Nothing errors, nothing warns, and the outcome reads
downstream as one that was never submitted. The shared normalizer deliberately
applies no kind vocabulary -- it is shared with other subsystems that carry
other kinds legitimately -- so enforcement belongs at the boundaries that must
agree, and both of them enforce from the same constant.

**A requested action is never an outcome.** The conclusion vocabulary has no
value meaning "asked for": an orchestrator that triggered a deployment and has
not learned how it went has nothing to report yet, and letting it say so here
would make "the deployment was requested" indistinguishable from "the deployment
succeeded" to everything downstream. It reports when the work concludes.

**Times are normalized to UTC, and that is a correctness fix rather than
tidiness.** The stored content digest is computed over the ISO rendering of the
parsed instants, so the same moment resubmitted under a different offset digests
differently -- and a true redelivery is then refused as a conflicting reuse of
its own key instead of converging on the row it already wrote. Converting to UTC
here makes a replay byte-stable whatever offset the retry queue happens to hold.

**The occurrence id is namespaced by the source system.** The ledger's
uniqueness is over tenant, producer and occurrence id, with no source column, so
two sources sharing a producer convention could otherwise collide on a bare
object id. Leading with the system makes that structurally impossible rather
than merely unlikely.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any, Final

from contextplane.context.lifecycle import normalize_reference_kind
from contextplane.signals.ingest import SIGNAL_SCHEMA_VERSION, ExternalSignalEnvelopeV1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from contextplane.context.schemas.trust import ExternalReferenceV1

#: How an orchestrator names the thing that concluded. Closed because the
#: occurrence id is built from it, and an open set would let one submitter file
#: `workflow_run` where another files `workflowRun` for the same work.
OUTCOME_OBJECTS: Final[tuple[str, ...]] = ("workflow_run", "deployment", "release")

#: How work is allowed to have ended. Deliberately without a value meaning
#: "requested" or "queued": those are not outcomes, and a vocabulary that could
#: express one would let a triggered action be counted as a completed one.
OUTCOME_CONCLUSIONS: Final[tuple[str, ...]] = (
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "neutral",
    "skipped",
    "stale",
)

#: Everything a stored outcome may carry, and nothing else. An allowlist rather
#: than a filter: a field an orchestrator adds later cannot arrive by default,
#: which is the property that keeps logs and free text out of the ledger.
_PAYLOAD_ALLOWLIST: Final[tuple[str, ...]] = (
    "object",
    "object_id",
    "attempt",
    "conclusion",
    "repository",
    "commit",
    "environment",
    "run_url",
    "started_at",
    "concluded_at",
)

#: Fields the payload must carry for the observation to mean anything. `object`
#: and `object_id` name what concluded; `conclusion` says how. An outcome
#: missing any of the three is not a partial outcome, it is a different kind of
#: message.
_PAYLOAD_REQUIRED: Final[tuple[str, ...]] = ("object", "object_id", "conclusion")


class OutcomeRejected(ValueError):
    """An outcome submission this module will not translate.

    Its own type, distinct from the envelope's validation error, because every
    refusal here is about outcome *meaning* -- unjoinable, unspellable, or not
    actually an outcome -- and the envelope has no opinion on any of them. A
    caller that catches this knows the submission was understood and declined,
    not that the transport failed.
    """


def _require_aware(name: str, moment: datetime.datetime) -> datetime.datetime:
    """One instant, timezone-aware, rendered in UTC.

    Naive instants are refused rather than assumed local: the ambiguity is
    exactly the offset nobody recorded, and it would land in a stored time that
    later comparisons treat as exact. Converting the aware ones to UTC is what
    makes a redelivery digest identically whatever offset it is replayed under.
    """
    if moment.tzinfo is None:
        message = f"{name} has no timezone; an ambiguous instant cannot be stored as one of the recorded times"
        raise OutcomeRejected(message)
    return moment.astimezone(datetime.UTC)


def outcome_payload(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted projection of one reported outcome, or a refusal.

    Built by naming what may pass. The refusals are about whether this is an
    outcome at all: what concluded, and how it ended.
    """
    missing = [field for field in _PAYLOAD_REQUIRED if not str(outcome.get(field) or "").strip()]
    if missing:
        message = f"an outcome must say {sorted(_PAYLOAD_REQUIRED)}; missing {missing}"
        raise OutcomeRejected(message)

    obj = str(outcome["object"]).strip().lower()
    if obj not in OUTCOME_OBJECTS:
        message = f"unknown outcome object {obj!r}; legal objects are {list(OUTCOME_OBJECTS)}"
        raise OutcomeRejected(message)

    conclusion = str(outcome["conclusion"]).strip().lower()
    if conclusion not in OUTCOME_CONCLUSIONS:
        message = (
            f"unknown conclusion {conclusion!r}; legal conclusions are {list(OUTCOME_CONCLUSIONS)}. "
            "A requested or queued action is not an outcome and has no spelling here"
        )
        raise OutcomeRejected(message)

    projected = {key: outcome.get(key) for key in _PAYLOAD_ALLOWLIST}
    projected["object"] = obj
    projected["conclusion"] = conclusion
    return {key: value for key, value in projected.items() if value is not None and str(value).strip()}


def checked_references(references: Sequence[ExternalReferenceV1]) -> tuple[ExternalReferenceV1, ...]:
    """The outcome's references, refused unless every kind is one this joins on.

    Two refusals, and neither is cosmetic. An outcome citing nothing can never
    reach the receipt it belongs to, and an outcome citing a misspelled kind
    binds to a reference row nothing else will ever look at -- both produce an
    outcome that is stored and permanently invisible, which is the one failure
    downstream reads as "it has not arrived".
    """
    if not references:
        message = (
            "an outcome must cite at least one external reference; the receipt it belongs to is "
            "reached only through the work both of them name, so an outcome citing nothing is "
            "stored and permanently unjoinable"
        )
        raise OutcomeRejected(message)
    for reference in references:
        # Raises on an out-of-set kind. Enforced from the same constant the
        # context profile enforces from, because a vocabulary agreed in one of
        # two places that must match is not agreed at all.
        normalize_reference_kind(reference.kind)
    return tuple(references)


def control_plane_outcome_envelope(
    *,
    source_id: uuid.UUID,
    source_system: str,
    producer_id: str,
    outcome: Mapping[str, Any],
    references: Sequence[ExternalReferenceV1],
    concluded_at: datetime.datetime,
    received_at: datetime.datetime,
    attempt: int = 1,
    classification: str = "internal",
    idempotency_key: str | None = None,
    team_key: str | None = None,
    project_key: str | None = None,
) -> ExternalSignalEnvelopeV1:
    """Translate one reported outcome into the shared envelope, or refuse it.

    `source_id` and `source_system` are the registered seat's own, passed in
    rather than declared here: this module admits no source of its own and
    derives no authority: the ledger takes both from the registration, which is
    the only place either is decided.

    The occurrence id is `{source_system}:{object}:{object_id}:{attempt}`.
    Namespaced by the system because the ledger's uniqueness has no source
    column; qualified by attempt because a re-execution is a new occurrence
    rather than a mutation of one something may already cite.

    `concluded_at` is when the work ended and `received_at` is when this
    submitter learned of it. Both are kept: collapsing them destroys the only
    evidence of lag between the two systems, and the lag is a pilot measure.
    """
    if attempt < 1:
        message = f"attempt must be at least 1, got {attempt}; an outcome describes an execution that happened"
        raise OutcomeRejected(message)

    payload = outcome_payload(outcome)
    checked = checked_references(references)
    event_time = _require_aware("concluded_at", concluded_at)
    observed_time = _require_aware("received_at", received_at)

    system = source_system.strip().lower()
    if not system:
        message = "source_system is what namespaces the occurrence id; an empty one cannot make it unique"
        raise OutcomeRejected(message)

    return ExternalSignalEnvelopeV1(
        source_id=source_id,
        source_system=system,
        source_event_id=f"{system}:{payload['object']}:{payload['object_id']}:{attempt}",
        producer_id=producer_id,
        producer_type="external",
        idempotency_key=idempotency_key or f"{system}:{uuid.uuid4()}",
        classification=classification,
        schema_version=SIGNAL_SCHEMA_VERSION,
        event_time=event_time,
        observed_time=observed_time,
        references=checked,
        payload={**payload, "attempt": attempt},
        team_key=team_key,
        project_key=project_key,
    )


__all__ = [
    "OUTCOME_CONCLUSIONS",
    "OUTCOME_OBJECTS",
    "OutcomeRejected",
    "checked_references",
    "control_plane_outcome_envelope",
    "outcome_payload",
]
