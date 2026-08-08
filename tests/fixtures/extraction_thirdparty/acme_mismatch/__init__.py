"""An adapter whose declared identity disagrees with the selector it installs under.

Registered under the selector `mismatch`, declares `provider_id = "acme"`. That
is a packaging mistake rather than a malicious one, and it is the shape the
smoke check exists to catch: the selector names the metric label while
`provider_id` is the persisted calibration key, so a deployment running this
would file its call counts under one name and its calibration rows under
another, with nothing left to join them by.

Everything else about it is fine, which is the point -- it imports, it builds,
and its `extract` works. Only the identity is wrong, so nothing short of the
smoke check would notice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from registry.extraction.provider import USAGE_UNKNOWN, ExtractionResult, TokenUsage

if TYPE_CHECKING:
    from registry.extraction.provider import ExtractionRequest


class MismatchedProvider:
    """Installs as `mismatch`, calls itself `acme`."""

    provider_id = "acme"
    default_model_id = "acme-extract-v1"

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(
            claims=(),
            usage=TokenUsage(
                prompt_tokens=None,
                completion_tokens=None,
                cached_prompt_tokens=None,
                source=USAGE_UNKNOWN,
            ),
            model_id=self.default_model_id,
            duration_ms=0,
        )


def build(settings: Any = None) -> MismatchedProvider:
    return MismatchedProvider()
