"""Session bodies are data. They are never instructions.

An event body is text an agent produced or observed, which means it can contain
anything — including text written to be read by a *later* agent. The registry
serves claims to agents, so a claim that carries instruction text is an injection
delivered with the platform's own authority behind it. Nothing downstream can
undo that: the claim looks ordinary, cites real provenance, and arrives through
the trusted read path.

Containment is three separate things, and all three are needed:

**On the way in — delimiting.** Bodies are wrapped in a boundary and handed to
the model as data, with the instruction to treat them as such. The boundary is
per-request and unguessable, so a body cannot close it and start a new
instruction block. This alone is not sufficient — a model can be talked out of
almost any framing — but it is what makes the other two layers meaningful rather
than sole defences.

**On the way out — schema.** Free-form output is refused, not parsed. A
best-effort parse of a model's prose is exactly how instruction text becomes a
stored value: the parse succeeds, the text lands in a field, and the field is
served.

**On the way out — content.** A candidate whose *value* is directive toward a
future agent is refused staging and routed to curation. This is the layer that
catches the case where delimiting held and the schema held, and the model
faithfully extracted "ignore your previous instructions" as though it were an
observation about a capability. It was a correct extraction of a hostile input.

The detector is deliberately biased toward refusal. A false positive sends one
candidate to a human; a false negative stores an instruction that will be read
by an agent as fact. Those costs are not close to symmetric, and the refusal
path is a queue rather than a deletion, so an over-refusal is recoverable.
"""

from __future__ import annotations

import re
import secrets

from prometheus_client import Counter

from contextplane.exceptions import RegistryError
from contextplane.service.memory.session_events import SessionEvent

# A poisoning attempt that nobody counts is a poisoning attempt that succeeded
# operationally, whatever happened to the individual candidate.
_REFUSED = Counter(
    "registry_extraction_candidate_refused_total",
    "Candidate claims refused by injection containment, by trigger.",
    ["trigger"],
)

TRIGGER_DIRECTIVE = "directive_content"
TRIGGER_ROLE_REDEFINITION = "role_redefinition"
TRIGGER_TOOL_DIRECTIVE = "tool_invocation_directive"
TRIGGER_BOUNDARY_FORGERY = "boundary_forgery"
TRIGGER_NO_EVIDENCE = "no_evidence_cited"

CONTAINMENT_TRIGGERS = frozenset(
    {
        TRIGGER_DIRECTIVE,
        TRIGGER_ROLE_REDEFINITION,
        TRIGGER_TOOL_DIRECTIVE,
        TRIGGER_BOUNDARY_FORGERY,
        TRIGGER_NO_EVIDENCE,
    }
)


class CandidateRefused(RegistryError):
    """A candidate was refused by containment and routed to curation.

    Carries the trigger so the metric and the curation queue agree on why.
    Refused, not dropped: a human needs to see what was attempted, both to
    recover a false positive and to notice a real attempt.
    """

    def __init__(self, trigger: str, detail: str) -> None:
        super().__init__(f"candidate refused by containment ({trigger}): {detail}")
        self.trigger = trigger
        self.detail = detail
        _REFUSED.labels(trigger=trigger).inc()


# --- on the way in: delimiting ----------------------------------------------


def new_boundary() -> str:
    """A fresh, unguessable delimiter for one request.

    Per-request rather than a constant: a fixed sentinel appears in training
    data, in documentation, and eventually in a session body written by someone
    who read the source. A body cannot close a boundary it cannot predict.
    """
    return f"data-{secrets.token_hex(16)}"


def render_events_as_data(events: tuple[SessionEvent, ...], boundary: str) -> str:
    """Wrap event bodies as inert data inside *boundary*.

    Any occurrence of the boundary inside a body is neutralized rather than
    passed through. A body containing the boundary is either an extraordinary
    coincidence or an attempt to close the block early and start issuing
    instructions; both warrant the same handling, and it is not this function's
    place to decide which.

    Metadata is deliberately excluded. It is caller-supplied, structurally
    identical to a body from the model's point of view, and nothing in the
    strategy needs it — so it is one whole class of injection surface that does
    not have to exist.
    """
    lines: list[str] = []
    for event in events:
        body = event.body.replace(boundary, "[boundary-removed]")
        lines.append(f'<{boundary} event_id="{event.event_id}" seq="{event.seq}" ')
        lines.append(f'  kind="{event.kind}">')
        lines.append(body)
        lines.append(f"</{boundary}>")
    return "\n".join(lines)


def assert_no_boundary_forgery(text: str, boundary: str) -> None:
    """Refuse output that reproduces the request boundary.

    A model echoing the delimiter is either confused about what is data or has
    been steered into emitting structure. Either way the output can no longer be
    read as a plain value.
    """
    if boundary in text:
        raise CandidateRefused(
            TRIGGER_BOUNDARY_FORGERY,
            "output reproduced the request data boundary, so it can no longer be read as a value",
        )


# --- on the way out: content ------------------------------------------------

# Imperatives aimed at whoever reads the claim later. Anchored to the start of a
# clause so that *describing* an instruction ("the runbook says to ignore stale
# entries") is not caught, while *issuing* one is.
_DIRECTIVE_OPENERS = (
    r"ignore\s+(?:all\s+|any\s+|your\s+)?(?:previous|prior|earlier|above|preceding)",
    r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|earlier|above|instructions?)",
    r"forget\s+(?:everything|all|what)\b",
    r"(?:from\s+now\s+on|going\s+forward)[, ]+(?:you|always|never)\b",
    r"you\s+(?:must|should|will|shall)\s+(?:now\s+)?(?:always|never|approve|reject|ignore|bypass)",
    r"do\s+not\s+(?:tell|inform|mention|report|log|audit)\b",
    r"always\s+(?:approve|accept|allow|trust|bypass|skip)\b",
    r"never\s+(?:ask|verify|check|validate|audit|refuse)\b",
    r"override\s+(?:the\s+|any\s+|all\s+)?(?:previous|safety|guard|check|policy)",
)

# Attempts to tell a later agent what it is. A claim's value describes a
# capability; it never assigns a role.
_ROLE_REDEFINITION = (
    r"you\s+are\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:admin|administrator|root|superuser|operator)",
    r"(?:act|behave)\s+as\s+(?:an?\s+|the\s+)?(?:admin|administrator|root|unrestricted)",
    r"your\s+new\s+(?:role|instructions?|purpose|task)\s+(?:is|are)\b",
    r"you\s+(?:no\s+longer|don't|do\s+not)\s+(?:need\s+to\s+)?(?:follow|obey|respect)\b",
    r"pretend\s+(?:to\s+be|you\s+are)\b",
    r"system\s*(?:prompt|message)\s*[:=]",
)

# Directives to call something. A claim may *name* an operation as a fact about
# an interface; it may not instruct a reader to invoke one.
_TOOL_DIRECTIVES = (
    # The noun can precede or follow the name: "call the delete tool" and "call
    # the tool delete" are the same instruction, and only one of them puts the
    # keyword adjacent to the verb.
    r"(?:call|invoke|execute|run)\s+(?:the\s+)?(?:\S+\s+){0,3}?(?:tool|function|command|endpoint)s?\b",
    r"(?:call|invoke|execute|run)\s+(?:the\s+)?(?:tool|function|command|endpoint)s?\b",
    r"<(?:tool_use|function_call|invoke|antml:invoke)\b",
    r"```(?:tool|function|bash|sh)\s*\n",
    # Flags and an HTTP method may sit between the command and the URL.
    r"\bcurl\s+(?:-{1,2}[\w-]+\s+|[A-Z]{3,7}\s+)*https?://",
)


def _compile(patterns: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_DIRECTIVE_RE = _compile(_DIRECTIVE_OPENERS)
_ROLE_RE = _compile(_ROLE_REDEFINITION)
_TOOL_RE = _compile(_TOOL_DIRECTIVES)


def assert_not_directive(value: object, *, field: str = "value") -> None:
    """Refuse a value that instructs rather than describes.

    Only string values can carry instructions, so anything else passes straight
    through — a typed integer cannot be an imperative, and pretending to check
    one would suggest a guarantee this does not provide.

    Ordering is most-specific-first so the reported trigger is the informative
    one: role redefinition and tool invocation are narrower findings than a
    generic imperative, and reporting the generic one would lose that.
    """
    if not isinstance(value, str):
        return

    if match := _ROLE_RE.search(value):
        raise CandidateRefused(
            TRIGGER_ROLE_REDEFINITION,
            f"{field} attempts to redefine a reader's role: {match.group(0)!r}",
        )
    if match := _TOOL_RE.search(value):
        raise CandidateRefused(
            TRIGGER_TOOL_DIRECTIVE,
            f"{field} directs a reader to invoke something: {match.group(0)!r}",
        )
    if match := _DIRECTIVE_RE.search(value):
        raise CandidateRefused(
            TRIGGER_DIRECTIVE,
            f"{field} instructs rather than describes: {match.group(0)!r}",
        )


def assert_evidence_cited(event_ids: tuple[str, ...], known: frozenset[str]) -> None:
    """Refuse a candidate that cites no event, or an event not in the batch.

    A fabricated citation is worse than none: it makes an invention look
    checkable. The provider only ever sees the batch, so an id outside it was
    not observed.
    """
    if not event_ids:
        raise CandidateRefused(
            TRIGGER_NO_EVIDENCE,
            "candidate cites no source event, so nothing about it can be checked",
        )
    unknown = [e for e in event_ids if e not in known]
    if unknown:
        raise CandidateRefused(
            TRIGGER_NO_EVIDENCE,
            f"candidate cites events that were not in the batch: {unknown!r}; a fabricated "
            "citation makes an invention look checkable",
        )


__all__ = [
    "CONTAINMENT_TRIGGERS",
    "TRIGGER_BOUNDARY_FORGERY",
    "TRIGGER_DIRECTIVE",
    "TRIGGER_NO_EVIDENCE",
    "TRIGGER_ROLE_REDEFINITION",
    "TRIGGER_TOOL_DIRECTIVE",
    "CandidateRefused",
    "assert_evidence_cited",
    "assert_no_boundary_forgery",
    "assert_not_directive",
    "new_boundary",
    "render_events_as_data",
]
