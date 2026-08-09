"""What a human or an agent reports about work they were part of.

The simplest adapter, and the one that proves the envelope is not shaped around
CI. A person saying "this deployment went badly" and a CI system saying "run 42
concluded failure" are different in every respect except the one that matters
here: both are observations somebody made, at a time, with an authority that came
from somewhere other than the observation itself.

**A direct reporter reports as itself.** `producer_id` is the caller's own actor
id, and the ingest service refuses a submission that names anybody else — so this
adapter takes the actor from the caller's context rather than from an argument
that could disagree with it. There is no parameter for "who is reporting" on
purpose: an adapter that accepted one would make impersonation a spelling
question.

**The observation time is the reporter's, the observed time is now.** A person
reporting on Monday what happened on Friday has an event time of Friday and an
observed time of Monday, and flattening those loses the only evidence that the
report was late. When a reporter does not say when it happened, the two are the
same instant — which is a claim about the report, not about the world, and is
recorded as such rather than left NULL.

**Nothing here concludes anything about the work.** A `failed` rating from a human
is an assertion by that human, carrying that human's authority and no more. The
adapter does not upgrade it, and the ledger stores the authority the source was
registered with rather than one derived from how confident the wording sounds.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Final

from contextplane.signals.ingest import SIGNAL_SCHEMA_VERSION, ExternalSignalEnvelopeV1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.types import TenantContext

#: The source system a direct report arrives under. Named rather than left to each
#: caller: two spellings of "a person told us" would sit in the ledger as two
#: different sources and never aggregate.
DIRECT_SOURCE_SYSTEM: Final[str] = "direct"

#: Who may report directly. `external` is excluded deliberately -- an external
#: system reporting through this path would be claiming to be a participant of
#: this deployment, and it has its own adapter.
DIRECT_PRODUCER_TYPES: Final[frozenset[str]] = frozenset({"human", "agent"})


def _occurrence_digest(occurred_at: datetime.datetime, observation: Mapping[str, Any]) -> str:
    """A short, stable discriminator for one observation at one instant.

    Truncated because this is an identity component a human reads in an event id,
    not a security boundary: the full digest is carried separately by the ledger's
    own `content_digest`, which is what replay is actually decided on.
    """
    material = json.dumps(
        {"occurred_at": occurred_at.isoformat(), "observation": dict(observation)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def direct_envelope(
    ctx: TenantContext,
    *,
    source_id: uuid.UUID,
    producer_type: str,
    observation: Mapping[str, Any],
    occurred_at: datetime.datetime,
    observed_at: datetime.datetime | None = None,
    references: Sequence[ExternalReferenceV1] = (),
    classification: str = "internal",
    idempotency_key: str | None = None,
    source_event_id: str | None = None,
    team_key: str | None = None,
    project_key: str | None = None,
) -> ExternalSignalEnvelopeV1:
    """Build the envelope for one direct report.

    `producer_id` is taken from `ctx` and is not a parameter: the ingest service
    refuses a human or agent reporting under another identity, and an adapter that
    accepted the id as an argument would turn that rule into something a caller
    could get wrong.

    A missing `observed_at` means the reporter did not distinguish when it
    happened from when they are telling us, so both instants are the report's own.
    That is a statement about the report rather than about the world, and it is
    recorded rather than inferred later from a NULL.
    """
    if producer_type not in DIRECT_PRODUCER_TYPES:
        message = f"direct reports come from {sorted(DIRECT_PRODUCER_TYPES)}, not {producer_type!r}"
        raise ValueError(message)

    reported_at = observed_at if observed_at is not None else occurred_at
    # Distinct from the idempotency key: this names the occurrence, the key names
    # the submission. A reporter resending the same observation under a fresh key
    # must find the stored row rather than file a second complaint about one event.
    #
    # The observation is part of the identity, not just the actor and the instant.
    # Without it, one reporter saying two different things at the same moment --
    # a person filing two notes at once, an agent reporting a batch -- collides as
    # "the same occurrence with different content" and the second is refused as a
    # conflict. The acceptance case found exactly that. Including the digest keeps
    # the property that matters: the *same* observation resubmitted is still the
    # same occurrence, which is what makes a replay a replay.
    event_id = source_event_id or f"direct:{ctx.actor_id}:{_occurrence_digest(occurred_at, observation)}"
    return ExternalSignalEnvelopeV1(
        source_id=source_id,
        source_system=DIRECT_SOURCE_SYSTEM,
        source_event_id=event_id,
        producer_id=str(ctx.actor_id),
        producer_type=producer_type,
        idempotency_key=idempotency_key or f"direct:{uuid.uuid4()}",
        classification=classification,
        schema_version=SIGNAL_SCHEMA_VERSION,
        event_time=occurred_at,
        observed_time=reported_at,
        references=tuple(references),
        team_key=team_key,
        project_key=project_key,
        payload=dict(observation),
    )


__all__ = ["DIRECT_PRODUCER_TYPES", "DIRECT_SOURCE_SYSTEM", "direct_envelope"]
