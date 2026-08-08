"""The extraction provider contract: session events in, candidate claims out.

This is the registry's first LLM dependency, and it is optional. With no
provider configured, extraction pauses: events are still captured and served,
connector-fed claims still land, no session-derived claims are produced, and the
state is logged rather than raised. A deployment that never configures a
provider is a working deployment with one feature switched off, not a broken one.

**Token usage is part of the return type, not an afterthought.** Every
implementation reports prompt, completion, and cached-prompt counts separately.
A contract that returns only claims makes per-tenant LLM spend unknowable, and
adding usage after adapters exist is a breaking change to every one of them.

**An unknown count is not zero.** A provider that cannot report usage says so.
A silent zero is indistinguishable from a free call, which is exactly the
reading that makes a spend dashboard lie.

**A candidate is not a claim.** What comes back from a provider has not been
validated against the ontology, has no resolved subject, and carries no
authority. It is a proposal. The conformance gate and the write path decide
whether any of it becomes a claim, and both are free to refuse all of it.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Literal, Protocol

from registry.exceptions import RegistryError
from registry.extraction.containment import new_boundary
from registry.service.memory.session_events import SessionEvent

# How a usage count was arrived at. The distinction matters for cost
# attribution: an estimate is a number somebody computed from token heuristics,
# and averaging it with provider-reported figures produces a total that is
# neither.
UsageSource = Literal["provider_reported", "estimated", "unknown"]

USAGE_REPORTED: UsageSource = "provider_reported"
USAGE_ESTIMATED: UsageSource = "estimated"
USAGE_UNKNOWN: UsageSource = "unknown"


class ProviderError(RegistryError):
    """A provider call failed.

    `is_retriable` names the class of failure the drain may re-attempt. A field
    rather than an isinstance check, so the contract is explicit at the call
    site and a new error type cannot silently become non-retriable.
    """

    is_retriable: bool = True

    def __init__(self, reason: str, *, is_retriable: bool = True) -> None:
        super().__init__(f"extraction provider failed: {reason}")
        self.reason = reason
        self.is_retriable = is_retriable


class ProviderMalformedError(ProviderError):
    """The provider returned something that is not the agreed schema.

    Never retriable and never repaired. Output is schema-constrained precisely
    so that free-form text is refused rather than parsed — a best-effort parse
    of a model's prose is how instruction text becomes a stored assertion.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason, is_retriable=False)


@dataclasses.dataclass(frozen=True)
class TokenUsage:
    """What one provider call cost, in tokens.

    All three counts are `None` exactly when `source` is unknown. The
    biconditional is asserted rather than assumed: a partially-populated usage
    record would be summed as though the missing fields were zero.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    source: UsageSource

    def __post_init__(self) -> None:
        counts = (self.prompt_tokens, self.completion_tokens, self.cached_prompt_tokens)
        absent = [c is None for c in counts]

        # All three or none of the three. A record with two counts and a gap is
        # the dangerous shape: it looks usable, and summing it treats the gap as
        # zero, so the total is wrong without anything looking wrong.
        if any(absent) and not all(absent):
            msg = (
                "token usage must be entirely present or entirely absent; a partially-filled "
                f"record gets summed as if the gaps were zero (counts={counts!r})"
            )
            raise ValueError(msg)

        # And absence means exactly one thing: nobody could report.
        if all(absent) != (self.source == USAGE_UNKNOWN):
            msg = (
                "token usage is absent exactly when the source is unknown; a known source with "
                "no counts hides a call that happened, and an unknown source with counts claims "
                f"a measurement nobody made (source={self.source!r}, counts={counts!r})"
            )
            raise ValueError(msg)

        if any(c is not None and c < 0 for c in counts):
            msg = f"token counts cannot be negative: {counts!r}"
            raise ValueError(msg)

    @classmethod
    def unknown(cls) -> TokenUsage:
        """For a provider that genuinely cannot report. Not the same as free."""
        return cls(
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            source=USAGE_UNKNOWN,
        )

    @property
    def total_tokens(self) -> int | None:
        """Prompt plus completion, or None if unknown.

        Cached prompt tokens are *part of* the prompt count as providers report
        it, not an addition to it, so adding them here would double-count.
        """
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


@dataclasses.dataclass(frozen=True)
class CandidateClaim:
    """A proposal, not a claim.

    Nothing here has been validated: the predicate may not exist, the value may
    not match its declared type, the subject may resolve to nothing. That is
    the conformance gate's job, and it is meant to reject.

    `evidence_event_ids` is what makes the candidate checkable. A candidate that
    cites no event is refused rather than staged — an extraction nobody can
    trace back to a source is indistinguishable from an invention.
    """

    subject_reference: str
    predicate: str
    value: Any
    evidence_event_ids: tuple[str, ...]
    excerpt: str | None = None
    # The model's own number, on whatever scale it used. Carried through
    # unchanged: calibration is a separate concern, and inventing a scale here
    # would mean moving every stored value once the real one lands.
    provider_confidence: float | None = None


@dataclasses.dataclass(frozen=True)
class ExtractionResult:
    """One provider call's output: what it proposed, and what it cost."""

    claims: tuple[CandidateClaim, ...]
    usage: TokenUsage
    model_id: str
    # Wall-clock for the call itself, for the extraction-lag budget. Measured by
    # the adapter because only it knows where the call started and ended.
    duration_ms: int | None = None


@dataclasses.dataclass(frozen=True)
class ExtractionRequest:
    """Everything a provider needs, and nothing it should decide.

    The strategy names the prompt, the output schema, and the predicates the
    output is allowed to use. The provider chooses none of those: an override
    changes how well claims are found, never what they are allowed to mean.
    """

    events: tuple[SessionEvent, ...]
    strategy_id: str
    system_prompt: str
    output_schema: dict[str, Any]
    model_id: str
    max_output_tokens: int
    # The predicates this strategy may emit. Passed to the provider so the
    # prompt can enumerate them, and re-checked afterwards regardless: a model
    # told which terms are legal still returns illegal ones.
    permitted_predicates: tuple[str, ...]
    requested_at: datetime.datetime
    # The delimiter this request's event bodies are wrapped in, and the one its
    # output is checked against. It lives on the request because both ends must
    # be the same string: an adapter that mints its own and a validator that
    # checks a different one make the forgery check unable to fire at all, which
    # is a hole that looks exactly like a working defence.
    #
    # `default_factory`, never a call expression evaluated at class-definition
    # time -- that would freeze one delimiter for the process lifetime, which is
    # the fixed sentinel this design exists to avoid.
    boundary: str = dataclasses.field(default_factory=new_boundary)


class ExtractionProvider(Protocol):
    """An LLM that turns session events into candidate claims.

    Implementations must treat event bodies as data and never as instructions
    — see `containment.py`. That is not enforceable by a Protocol, so it is
    enforced by every adapter routing its prompt construction through the one
    function that does the delimiting, with `request.boundary` and never a
    delimiter of its own. An adapter that mints one is checked against a string
    it never used.
    """

    provider_id: str

    async def extract(self, request: ExtractionRequest) -> ExtractionResult: ...


class NoOpProvider:
    """The default. Proposes nothing, costs nothing, fails at nothing.

    Not a stub for tests — this is what a deployment with no configured provider
    runs. Extraction pausing has to be an ordinary state rather than an error,
    because the alternative is a scheduler job that logs an exception every tick
    on every deployment that has not bought an LLM.

    Usage is reported as unknown rather than zero. A call that never happened
    has no cost, but saying "zero tokens" would make an unconfigured deployment
    look identical to a configured one that happens to be free.
    """

    provider_id = "noop"

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(
            claims=(),
            usage=TokenUsage.unknown(),
            model_id="noop",
            duration_ms=0,
        )


__all__ = [
    "USAGE_ESTIMATED",
    "USAGE_REPORTED",
    "USAGE_UNKNOWN",
    "CandidateClaim",
    "ExtractionProvider",
    "ExtractionRequest",
    "ExtractionResult",
    "NoOpProvider",
    "ProviderError",
    "ProviderMalformedError",
    "TokenUsage",
    "UsageSource",
]
