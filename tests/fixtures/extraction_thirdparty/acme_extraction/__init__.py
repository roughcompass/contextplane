"""A third-party extraction adapter, as somebody outside this repo would write one.

This is the worked example as much as it is a fixture. It is deliberately the
smallest thing that satisfies the contract, so what the contract actually costs
an implementer is visible: a builder callable, a `provider_id` matching the
selector, and an `extract` that returns candidates whose usage accounting is
honest.

It calls nothing over the network. A fixture that needed an endpoint would make
this a test of the endpoint, and discovery is what is under test here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from registry.extraction.provider import (
    USAGE_UNKNOWN,
    CandidateClaim,
    ExtractionResult,
    TokenUsage,
)

if TYPE_CHECKING:
    from registry.config import Settings
    from registry.extraction.provider import ExtractionRequest

#: Matches the selector this package's entry point is registered under. The
#: registry refuses a provider whose declared id disagrees: the selector names
#: the metric label and `provider_id` is a persisted calibration key, so a
#: disagreement files a deployment's metrics and its calibration rows under
#: different names with nothing to join them by afterwards.
PROVIDER_ID = "acme"

#: Every adapter declares the wire model it uses when a strategy pins none.
DEFAULT_MODEL = "acme-extract-v1"

# One deliberately dull rule. The point is not extraction quality -- it is that
# a real adapter can be written against this contract without importing
# anything from the host application beyond the types it returns.
_TIMEOUT = re.compile(r"times out after (\d+) seconds", re.IGNORECASE)


class AcmeExtractionProvider:
    """Rule-based, network-free, and just enough to be a real provider."""

    provider_id = PROVIDER_ID
    default_model_id = DEFAULT_MODEL

    def __init__(self, settings: Settings | None = None) -> None:
        # The settings object is accepted and ignored. A third party would read
        # its endpoint and credential from here; taking the argument and not
        # using it keeps the builder signature honest without inventing config
        # this fixture has no use for.
        self._settings = settings

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        claims: list[CandidateClaim] = []
        for event in request.events:
            match = _TIMEOUT.search(event.body)
            if match is None:
                continue
            claims.append(
                CandidateClaim(
                    subject_reference=str(event.session_id),
                    predicate="request_timeout_seconds",
                    value=int(match.group(1)),
                    evidence_event_ids=(str(event.event_id),),
                    excerpt=match.group(0),
                    provider_confidence=None,
                )
            )

        # Unknown rather than zero. This adapter counts no tokens because it
        # calls no model, and saying "zero" would make it indistinguishable from
        # a metered provider that happened to be free.
        return ExtractionResult(
            claims=tuple(claims),
            usage=TokenUsage(
                prompt_tokens=None,
                completion_tokens=None,
                cached_prompt_tokens=None,
                source=USAGE_UNKNOWN,
            ),
            model_id=DEFAULT_MODEL,
            duration_ms=0,
        )


def build(settings: Any = None) -> AcmeExtractionProvider:
    """The entry point. One argument, returns the provider."""
    return AcmeExtractionProvider(settings)
