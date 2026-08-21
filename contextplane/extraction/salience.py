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

from contextplane import ranking

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, not behaviour
    from contextplane.service.memory.session_events import SessionEvent

__all__ = [
    "SIGNAL_NAMES",
    "WEIGHTS_MODEL_ID",
    "combine",
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

#: Above this many distinct named things per event, the signal saturates.
#:
#: Governed rather than held here, and registered by the first review of ordering
#: sites: the salience weights were already in the registry and these were not,
#: which governed half an arithmetic. The weights are applied to a value these
#: normalise, so moving a ceiling reorders every episode while every weight stays
#: put -- the registry entry carries why each holds its value.
_ENTITY_DENSITY_CEILING: Final = ranking.threshold("salience-entity-density-ceiling@1")

#: Distinct tools at which diversity saturates. Higher than the entity ceiling
#: because tool invocations are sparser than named entities in the same
#: transcript; see the registry entry.
_TOOL_DIVERSITY_CEILING: Final = ranking.threshold("salience-tool-diversity-ceiling@1")


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


#: The governed magnitude this module's output is weighted by. Its values, and
#: the reason each holds the value it does, live in the committed registry.
WEIGHTS_MODEL_ID: Final = "salience-weights@1"

#: The one signal that cannot be computed here. Named rather than inferred from
#: "whichever weight has no matching function", because that inference would
#: silently absorb a typo in a new signal's name as a deliberate omission.
NOVELTY: Final = "novelty"


def combine(signals: dict[str, float], *, weights: dict[str, float], novelty: float | None = None) -> float:
    """The weighted sum, in `[0, 1]`, of everything known about an episode.

    `weights` is required and has no default, which is the point. This function
    used to read `ranking.weights(...)` itself and so scored every tenant on the
    deployment's core values, silently ignoring an override the tenant had
    published, validated and activated. A default here would put that path back
    the first time somebody found the parameter inconvenient; requiring it means
    a caller with no tenant to resolve for cannot call this at all.

    An absent novelty contributes **zero**, and its weight is not redistributed
    across the other five. Redistributing would mean a claim's salience *falls*
    when its embedding lands and novelty turns out low, which is the opposite of
    what a reader expects from a number that is being filled in. Contributing
    zero makes the later arrival monotone: salience can only rise, by at most the
    novelty weight, and until then the score is honestly missing that much.

    Raises on a signal the weights do not name, and on a weight no signal
    supplies. Either is a rename that half-landed, and the failure mode without
    this is a silently lower score that still looks like a score.
    """
    supplied = dict(signals)
    if novelty is not None:
        supplied[NOVELTY] = novelty

    unknown = sorted(set(supplied) - set(weights))
    if unknown:
        msg = f"{unknown} have no weight in {WEIGHTS_MODEL_ID}; a signal nobody weights contributes nothing silently"
        raise ranking.UngovernedMagnitude(msg)

    # Novelty is the only weight allowed to have no value yet.
    missing = sorted(set(weights) - set(supplied) - {NOVELTY})
    if missing:
        msg = f"{WEIGHTS_MODEL_ID} weights {missing}, which nothing supplied; a dropped signal lowers every score"
        raise ranking.UngovernedMagnitude(msg)

    total = sum(weights[name] * value for name, value in supplied.items())
    # Clamped rather than trusted. Weights summing to one over signals in [0,1]
    # cannot exceed one, so a value outside the range means the artifact and this
    # function disagree -- and the database CHECK would then reject the write
    # with a constraint name instead of a reason.
    return round(min(1.0, max(0.0, total)), 3)
