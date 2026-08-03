"""The local provider: extraction with no key, no network, and no model.

`make dev-up` should demonstrate the whole pipeline — event lands, outbox
enqueues, provider runs, conformance gate validates, write path stages a claim —
on a laptop with no credentials and no internet. That is what this is for. It is
the default for the local dev stack, and the reason a developer never *needs* an
API key to work on anything downstream of extraction.

**It is rules, and it says so.** `provider_id` is `local-rules`, the model id is
`local-rules-v1`, and token usage is reported as estimated rather than
provider-reported. Nothing here should ever be mistaken for a measurement of
extraction quality: the patterns below find the handful of phrasings a demo
script uses, and real transcripts do not talk like that. A benchmark run against
this provider measures the regexes.

**Why not a recorded-fixture replay instead.** Fixtures would give more realistic
output, but they only answer for the transcripts somebody recorded. A demo that
ingests a sentence a developer just typed has to produce something, and a replay
would return the previous session's claims for it. Rules degrade to "found
nothing", which is an honest and common answer.

**It exercises refusal, not just success.** A demo that only ever shows the happy
path teaches the wrong thing about a pipeline whose main job is refusing. Bodies
containing directive text flow through the same containment checks as a real
model's output and get refused there, because this provider extracts them
faithfully — which is exactly what a real model does with a hostile input.
"""

from __future__ import annotations

import re
from typing import Any

from registry.extraction.provider import (
    USAGE_ESTIMATED,
    CandidateClaim,
    ExtractionRequest,
    ExtractionResult,
    TokenUsage,
)
from registry.extraction.strategies import (
    STRATEGY_OBSERVATION,
    STRATEGY_PREFERENCE,
    STRATEGY_SUMMARY,
)
from registry.service.memory import SessionEvent

# Rough characters-per-token. Named and labelled `estimated` rather than passed
# off as a count: a heuristic averaged into provider-reported totals produces a
# spend figure that is neither measured nor estimated.
_CHARS_PER_TOKEN = 4
TOKENIZER_ID = "heuristic-chars-v1"

MODEL_ID = "local-rules-v1"


def _estimate_tokens_from_chars(chars: int) -> int:
    """Characters to a token estimate. Takes a count, not a string.

    Taking a length rather than the text avoids building a throwaway string just
    to measure it, which matters because the prompt plus a full event batch can
    be tens of kilobytes.
    """
    return max(1, chars // _CHARS_PER_TOKEN)


# An entity reference the demo can actually produce: a UUID, or the
# `system:identifier` external form. Never a bare name -- resolving "the auth
# service" to an entity is guessing, and the write path's whole posture is that
# an unresolvable subject lands unlinked rather than being guessed at.
_SUBJECT_RE = re.compile(
    r"\b(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
    r"|(?P<ext>[a-z][a-z0-9_-]{1,30}:[A-Za-z0-9][\w./-]{1,80})",
    re.IGNORECASE,
)


class _Rule:
    """One phrasing this provider recognizes, and the claim it produces."""

    def __init__(self, predicate: str, pattern: str, *, cast: str = "str") -> None:
        self.predicate = predicate
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.cast = cast

    def value_from(self, match: re.Match[str]) -> Any:
        raw = match.group("value").strip()
        if self.cast == "int":
            return int(raw)
        if self.cast == "bool":
            return raw.lower() in {"true", "yes", "enabled", "public"}
        return raw


# Deliberately few. Each one exists because a demo transcript or an integration
# test says that sentence; this is not an attempt at coverage.
_OBSERVATION_RULES: tuple[_Rule, ...] = (
    _Rule(
        "request_timeout_seconds",
        r"(?:times?\s+out|timeout)(?:\s+\w+){0,3}?\s+(?:after\s+|of\s+|is\s+)?(?P<value>\d{1,7})\s*s(?:ec|econds?)?\b",
        cast="int",
    ),
    _Rule(
        "recovery_time_objective_seconds",
        r"(?:rto|recovery\s+time(?:\s+objective)?)\D{0,20}?(?P<value>\d{1,7})\s*s(?:ec|econds?)?\b",
        cast="int",
    ),
    _Rule(
        "max_request_bytes",
        r"(?:max(?:imum)?\s+(?:request|body|payload))\D{0,20}?(?P<value>\d{1,12})\s*bytes?\b",
        cast="int",
    ),
    _Rule("owned_by_team", r"owned\s+by\s+(?:the\s+)?(?P<value>[\w][\w .'-]{1,60}?)\s*(?:team\b|[.,;]|$)"),
    _Rule("on_call_rotation", r"on[- ]call\s+(?:rotation\s+)?(?:is\s+)?(?P<value>[\w][\w .'-]{1,60}?)\s*(?:[.,;]|$)"),
    _Rule("deployment_environment", r"deployed\s+(?:in|to)\s+(?P<value>[\w-]{2,40})\b"),
    _Rule("runbook_url", r"runbook\s+(?:is\s+)?(?:at\s+)?(?P<value>https?://\S{4,200}?)(?=[.,;)\]]?(?:\s|$))"),
    _Rule(
        "interface_specification_url",
        r"(?:openapi|spec(?:ification)?)\s+(?:is\s+)?(?:at\s+)?(?P<value>https?://\S{4,200}?)(?=[.,;)\]]?(?:\s|$))",
    ),
    _Rule("is_publicly_callable", r"(?:is\s+)?(?P<value>publicly\s+callable|public|internal\s+only)\b", cast="bool"),
    _Rule("target_availability", r"availability\s+(?:target\s+)?(?:of\s+|is\s+)?(?P<value>0\.\d{1,6})\b"),
)

_PREFERENCE_RULES: tuple[_Rule, ...] = (
    _Rule("deployment_environment", r"i\s+(?:usually|always|prefer\s+to)\s+work\s+in\s+(?P<value>[\w-]{2,40})\b"),
    _Rule("owned_by_team", r"(?:i'?m|i\s+am)\s+on\s+(?:the\s+)?(?P<value>[\w][\w .'-]{1,60}?)\s*(?:team\b|[.,;]|$)"),
)


class LocalRulesProvider:
    """Pattern-matching extraction for local development and demos.

    Deterministic: the same events produce the same candidates, every run. That
    is what makes it usable in tests as well as demos — no recorded cassettes to
    refresh, no network flakiness, no key.
    """

    provider_id = "local-rules"

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        if request.strategy_id == STRATEGY_SUMMARY:
            claims = self._summarize(request.events)
        elif request.strategy_id == STRATEGY_PREFERENCE:
            claims = self._apply(request, _PREFERENCE_RULES, subject_is_actor=True)
        elif request.strategy_id == STRATEGY_OBSERVATION:
            claims = self._apply(request, _OBSERVATION_RULES, subject_is_actor=False)
        else:
            # An unknown strategy finds nothing rather than raising. A strategy
            # this provider has no rules for is a normal state in local mode --
            # the alternative is a dev stack that errors on a feature the
            # developer is not working on.
            claims = ()

        prompt_chars = len(request.system_prompt) + sum(len(e.body) for e in request.events)
        completion_chars = sum(len(str(c.value)) for c in claims)

        return ExtractionResult(
            claims=claims,
            usage=TokenUsage(
                prompt_tokens=_estimate_tokens_from_chars(prompt_chars),
                completion_tokens=_estimate_tokens_from_chars(completion_chars),
                # No prompt caching without a provider that has a cache. A real
                # zero, not an unknown.
                cached_prompt_tokens=0,
                source=USAGE_ESTIMATED,
            ),
            model_id=MODEL_ID,
            duration_ms=0,
        )

    def _apply(
        self,
        request: ExtractionRequest,
        rules: tuple[_Rule, ...],
        *,
        subject_is_actor: bool,
    ) -> tuple[CandidateClaim, ...]:
        permitted = frozenset(request.permitted_predicates)
        out: list[CandidateClaim] = []

        for event in request.events:
            subject = "actor:self" if subject_is_actor else _find_subject(event.body)
            if subject is None:
                # No resolvable reference in this body. Emitting a guessed
                # subject would produce a claim attached to the wrong entity,
                # which looks corroborated by something unrelated.
                continue
            for rule in rules:
                if rule.predicate not in permitted:
                    continue
                match = rule.pattern.search(event.body)
                if match is None:
                    continue
                try:
                    value = rule.value_from(match)
                except ValueError:
                    # A number that did not parse. The conformance gate would
                    # refuse it anyway; not emitting it keeps the demo's
                    # rejection counts about real rejections.
                    continue
                out.append(
                    CandidateClaim(
                        subject_reference=subject,
                        predicate=rule.predicate,
                        value=value,
                        evidence_event_ids=(str(event.event_id),),
                        excerpt=_excerpt(event.body, match),
                        # No calibrated confidence to report. A rules match is
                        # not more or less certain than another rules match, and
                        # inventing a spread would imply a judgement.
                        provider_confidence=None,
                    )
                )
        return tuple(out)

    def _summarize(self, events: tuple[SessionEvent, ...]) -> tuple[CandidateClaim, ...]:
        """A mechanical session summary: counts and first words, not narrative.

        Deliberately not an attempt at prose. A generated-looking summary would
        invite someone to judge summary quality from the local provider, and the
        answer would be about string slicing.
        """
        if not events:
            return ()
        kinds: dict[str, int] = {}
        for event in events:
            kinds[event.kind] = kinds.get(event.kind, 0) + 1
        breakdown = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
        opening = events[0].body.strip().replace("\n", " ")[:160]
        value = (
            f"Session of {len(events)} event(s) ({breakdown}). "
            f"Opened with: {opening!r}. "
            "Summary generated by the local rules provider; not a model summary."
        )
        return (
            CandidateClaim(
                subject_reference=f"session:{events[0].session_id}",
                predicate="session_summary",
                value=value,
                evidence_event_ids=tuple(str(e.event_id) for e in events),
                excerpt=None,
                provider_confidence=None,
            ),
        )


def _find_subject(body: str) -> str | None:
    """The first entity reference in the body, or None.

    None rather than a guess. The write path stores an unresolvable subject as
    `unlinked` for a human, but that only helps if the reference is something a
    human can act on -- a hallucinated one is worse than an absent one.
    """
    match = _SUBJECT_RE.search(body)
    if match is None:
        return None
    return match.group("uuid") or match.group("ext")


def _excerpt(body: str, match: re.Match[str], *, window: int = 80) -> str:
    start = max(0, match.start() - window)
    end = min(len(body), match.end() + window)
    return body[start:end].strip()


__all__ = ["MODEL_ID", "TOKENIZER_ID", "LocalRulesProvider"]
