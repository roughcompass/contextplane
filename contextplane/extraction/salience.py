"""How much an episode is worth keeping, from what the episode itself shows.

Everything is kept today, which is the assumption that fails first when agents
write at machine speed. Salience is the number that lets a retention decision
be made at all, and because it decides what a system remembers it has to be a
thing a person can argue with: a weighted sum of observable signals, each one
a sentence about the transcript rather than a model's opinion of it.

**Signals are computed here; the weighting is not.** These functions each
return a value in ``[0, 1]`` and know nothing about how much any of them
matters. The weights are a governed magnitude, so changing the balance between
"touched a real system" and "used several tools" is a reviewed change to a
committed artifact rather than an edit to this module.

**Five signals, not six.** Novelty -- one minus the similarity to the nearest
existing episode -- is deliberately absent. It needs the claim's embedding, and
embedding is queued at write and computed by a consumer afterwards, so a
synchronous novelty term would either block the write on a model call or quietly
report zero. It is filled in when the vector lands. What is here depends on
nothing but the event window, which is why it can be computed at write.

**Everything degrades to 0.0 on an empty window** rather than raising or
returning a sentinel. An episode with no events is not salient, and that is an
answer rather than an error.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, not behaviour
    from contextplane.service.memory.session_events import SessionEvent

__all__ = [
    "SIGNAL_NAMES",
    "entity_density",
    "human_engagement",
    "outcome_decisive",
    "signal_vector",
    "state_change",
    "tool_diversity",
]

#: The order the weights artifact declares. Kept as a tuple so a weights entry
#: and a signal vector cannot silently disagree about which number is which.
SIGNAL_NAMES: Final = (
    "state_change",
    "outcome_decisive",
    "human_engagement",
    "entity_density",
    "tool_diversity",
)

#: Tools whose invocation means the world outside the transcript changed. Read
#: against the recorded `tool_name`, never the body: a message *describing* a
#: deployment is not a deployment, and matching prose would score a plan as
#: highly as an act.
_MUTATING_TOOL_HINTS: Final = (
    "write",
    "edit",
    "create",
    "update",
    "delete",
    "deploy",
    "apply",
    "commit",
    "push",
    "merge",
    "migrate",
    "restart",
    "rollback",
)

#: Phrases marking an episode that reached a verdict. Deliberately narrow: the
#: signal is meant to separate "this resolved" from "this trailed off", and a
#: generous list would mark every episode decisive and carry no information.
_DECISIVE_MARKERS: Final = (
    "fixed",
    "resolved",
    "root cause",
    "works now",
    "passing now",
    "confirmed",
    "verified",
    "deployed",
    "merged",
)

#: Phrases marking a human correcting or redirecting the agent, which is the
#: strongest available evidence that a turn mattered to the person in it.
_CORRECTIVE_MARKERS: Final = (
    "no,",
    "not that",
    "actually",
    "instead",
    "wrong",
    "revert",
    "undo",
    "stop",
    "don't",
    "do not",
)

#: A crude proxy for a named thing: dotted paths, snake/kebab identifiers,
#: CamelCase, and quoted strings. Deliberately syntactic. A real resolver runs
#: later against the catalog, and borrowing it here would make a write-time
#: signal depend on catalog state, which is exactly what "computed at write"
#: is supposed to exclude.
_ENTITY_TOKEN = re.compile(
    r"""(?:
        [A-Za-z_][\w.]*\.[A-Za-z_]\w*     # dotted path
      | [a-z]+(?:[_-][a-z0-9]+)+          # snake_case or kebab-case
      | (?:[A-Z][a-z0-9]+){2,}            # CamelCase
      | `[^`]+`                           # backticked
    )""",
    re.VERBOSE,
)

#: Above this many distinct named things per event, the signal saturates. Set
#: at three because the point is to separate a transcript that talks about
#: specific systems from one that does not, and past a few per turn the
#: difference stops meaning more.
_ENTITY_DENSITY_CEILING: Final = 3.0

#: Distinct tools at which diversity saturates, for the same reason.
_TOOL_DIVERSITY_CEILING: Final = 4.0


def _contains_any(haystack: str, needles: Sequence[str]) -> bool:
    lowered = haystack.lower()
    return any(needle in lowered for needle in needles)


def state_change(events: Sequence[SessionEvent]) -> float:
    """Did this episode touch a real system, rather than only discuss one?

    Binary on purpose. A partial credit here would express a confidence about
    *how much* changed that the event stream does not carry -- the record shows
    that a mutating tool ran, not what it did.
    """
    for event in events:
        if event.kind != "tool_invocation":
            continue
        name = (event.tool_name or "").lower()
        if any(hint in name for hint in _MUTATING_TOOL_HINTS):
            return 1.0
    return 0.0


def outcome_decisive(events: Sequence[SessionEvent]) -> float:
    """Did it reach a verdict, or trail off?

    Read from the last few events rather than the whole window: "fixed"
    early in a transcript is usually describing the problem, and the same word
    at the end is usually reporting the result.
    """
    if not events:
        return 0.0
    for event in list(events)[-3:]:
        if _contains_any(event.body, _DECISIVE_MARKERS):
            return 1.0
    return 0.0


def human_engagement(events: Sequence[SessionEvent]) -> float:
    """Did the person steer, or watch?

    A corrective turn scores full: a human troubling to say "no, not that" has
    told you the episode mattered to them. Plain participation scores half --
    present, but weaker evidence than a correction.
    """
    user_events = [e for e in events if e.kind == "user_message"]
    if not user_events:
        return 0.0
    if any(_contains_any(e.body, _CORRECTIVE_MARKERS) for e in user_events):
        return 1.0
    return 0.5


def entity_density(events: Sequence[SessionEvent]) -> float:
    """Distinct named things per event, saturating at the ceiling.

    Distinct rather than total: a transcript repeating one service name forty
    times is about one thing, and counting repeats would score it as though it
    were about forty.
    """
    if not events:
        return 0.0
    seen: set[str] = set()
    for event in events:
        seen.update(match.group(0).strip("`") for match in _ENTITY_TOKEN.finditer(event.body))
    return min(1.0, (len(seen) / len(events)) / _ENTITY_DENSITY_CEILING)


def tool_diversity(events: Sequence[SessionEvent]) -> float:
    """How many distinct tools the episode used, saturating at the ceiling.

    A weak signal held deliberately weak by its weight: breadth of tooling
    correlates with substance, and also with flailing.
    """
    if not events:
        return 0.0
    tools = {e.tool_name for e in events if e.kind == "tool_invocation" and e.tool_name}
    return min(1.0, len(tools) / _TOOL_DIVERSITY_CEILING)


def signal_vector(events: Sequence[SessionEvent]) -> dict[str, float]:
    """Every write-time signal, keyed by the names the weights artifact uses.

    Returns the full set even when the window is empty, so a caller combining
    signals with weights never has to decide what a missing key means.
    """
    return {
        "state_change": state_change(events),
        "outcome_decisive": outcome_decisive(events),
        "human_engagement": human_engagement(events),
        "entity_density": entity_density(events),
        "tool_diversity": tool_diversity(events),
    }
