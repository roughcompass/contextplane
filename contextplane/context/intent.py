"""The four ways an agent may write, and the three effects none of them reach.

An agent has exactly four write paths: it records a checkpoint on a task, it
stages a cited observation, it asks an owner for something, or it submits a
review through a control that already qualified it. The four are disjoint --
one intent, one effect, one table, one authority -- and the decision is made
here, once, rather than re-derived at every surface that accepts a body.

**The paths cannot cross in either direction.** A checkpoint body offered as an
observation is not a nearly-right observation; it is a task note about to be
stored as a cited claim nobody cited. An observation body offered as a
checkpoint is somebody else's assertion about to be stored as a decision the
task made. Both refusals name the route the body actually belongs to, because
whoever crossed them meant one of the two and needs to know which.

**A workspace entry is context, never authority.** It routes nowhere and
authorizes nothing. A working note carried into a canonical review would lend
the registry's authority to a sentence somebody typed while thinking out loud,
and the refusal has to be by name rather than by the accident of a field set
failing to match -- an accident stops holding the moment either shape grows a
field.

**Authority does not carry across routes.** An actor holding a participant
grant may checkpoint that task; the same grant is not a qualified control, and
presenting it for a canonical review is refused rather than quietly accepted at
a lower level. Accepting it would mean the strongest write in the system takes
the weakest evidence in it.

**Three effects are unreachable from any intent**: writing a claim as asserted
truth (an agent stages, a curator promotes), creating a governance artifact
proposal, and mutating canonical rows directly. They are named rather than
merely left out of the vocabulary, so a route table that grows one fails at
import instead of serving.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

from contextplane.context.schemas.reference import WorkspaceContentV1


class DisallowedWrite(ValueError):
    """A write was refused before anything was stored.

    Deliberately not the read side's `InvalidContextItem`. That one drops a
    single item and degrades a block, because a partial answer that says it is
    partial beats no answer. There is no equivalent trade here: a write is
    applied or it is not, and a surface that caught this and continued would be
    storing something nobody authorized.
    """


# --- intents ------------------------------------------------------------------

WriteIntent = Literal["checkpoint", "observation", "request", "canonical_review"]

INTENT_CHECKPOINT: WriteIntent = "checkpoint"
INTENT_OBSERVATION: WriteIntent = "observation"
INTENT_REQUEST: WriteIntent = "request"
INTENT_CANONICAL_REVIEW: WriteIntent = "canonical_review"

# Order is stable so a refusal lists the alternatives the same way every time.
WRITE_INTENTS: tuple[WriteIntent, ...] = (
    INTENT_CHECKPOINT,
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    INTENT_CANONICAL_REVIEW,
)


# --- effects ------------------------------------------------------------------

WriteEffect = Literal[
    "task_checkpoint_append",
    "staged_observation",
    "owner_request",
    "canonical_review_decision",
]

EFFECT_CHECKPOINT_APPEND: WriteEffect = "task_checkpoint_append"
EFFECT_STAGED_OBSERVATION: WriteEffect = "staged_observation"
EFFECT_OWNER_REQUEST: WriteEffect = "owner_request"
EFFECT_CANONICAL_REVIEW_DECISION: WriteEffect = "canonical_review_decision"

# Effects no intent may produce, named so their absence is checked rather than
# assumed. Staging is not asserting: an agent appends a cited observation, and
# it becomes something a curator stands behind only by being drained through
# the one module allowed to create such a row -- never by arriving. A proposal
# is authored through the governance surface that qualifies its author. A
# canonical row changes when a review that passed its control is applied, which
# is a separate act from submitting the review.
UNREACHABLE_EFFECTS: frozenset[str] = frozenset(
    {
        "asserted_claim_write",
        "artifact_proposal",
        "canonical_mutation",
    }
)

# Tables no intent may name as its target. `workspace_entries` is on the list
# for the opposite reason to the others: the workspace surface writes there
# legitimately, and that is exactly why an agent write must not land in the same
# place -- a note and a checkpoint would become indistinguishable rows.
TABLES_NO_INTENT_MAY_TARGET: frozenset[str] = frozenset(
    {
        "entities",
        "attributes",
        "edges",
        "arc_authoring_proposals",
        "workspace_entries",
    }
)


# --- authority ----------------------------------------------------------------

AuthorityOrigin = Literal[
    "participant_grant",
    "cited_evidence",
    "requester_entitlement",
    "qualified_control",
    "workspace_entry",
]

AUTHORITY_PARTICIPANT_GRANT: AuthorityOrigin = "participant_grant"
AUTHORITY_CITED_EVIDENCE: AuthorityOrigin = "cited_evidence"
AUTHORITY_REQUESTER_ENTITLEMENT: AuthorityOrigin = "requester_entitlement"
AUTHORITY_QUALIFIED_CONTROL: AuthorityOrigin = "qualified_control"

# The origin that satisfies nothing. Present in the vocabulary so a caller can
# say where a body came from truthfully and be refused, rather than having to
# omit the provenance to get through.
AUTHORITY_WORKSPACE_ENTRY: AuthorityOrigin = "workspace_entry"

AUTHORITY_ORIGINS: frozenset[str] = frozenset(
    {
        AUTHORITY_PARTICIPANT_GRANT,
        AUTHORITY_CITED_EVIDENCE,
        AUTHORITY_REQUESTER_ENTITLEMENT,
        AUTHORITY_QUALIFIED_CONTROL,
        AUTHORITY_WORKSPACE_ENTRY,
    }
)


@dataclasses.dataclass(frozen=True)
class WriteAuthority:
    """What the caller is standing on, as the server resolved it.

    Never taken from the body. A body that could name its own authority would
    be a body that authorizes itself, and the resulting row is indistinguishable
    from one an authorized actor wrote.
    """

    actor_id: str
    origin: AuthorityOrigin
    # The control this write passed through, for the one route that requires
    # passing through one. Absent everywhere else: a control id attached to a
    # checkpoint says the caller believes it is reviewing something.
    control_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise DisallowedWrite("a write needs an actor; an unattributed write cannot be reviewed afterwards")
        if self.origin not in AUTHORITY_ORIGINS:
            raise DisallowedWrite(
                f"unknown authority origin {self.origin!r}; legal values are {sorted(AUTHORITY_ORIGINS)}"
            )
        if self.control_id is not None and not self.control_id.strip():
            raise DisallowedWrite(
                "a control id is absent or names a control; an empty string reads as 'reviewed' to anyone "
                "checking the field is set"
            )


# --- routes -------------------------------------------------------------------

# Derived from the frozen workspace content shape rather than restated, so the
# two cannot drift into a state where a workspace field is silently routable.
WORKSPACE_BODY_FIELDS: frozenset[str] = frozenset(field.name for field in dataclasses.fields(WorkspaceContentV1))


@dataclasses.dataclass(frozen=True)
class WriteRoute:
    """One intent's whole contract: what it does, where, and on whose authority."""

    intent: WriteIntent
    effect: WriteEffect
    target_table: str
    authority: AuthorityOrigin
    # Closed. An unknown field is refused rather than dropped, because a dropped
    # field is indistinguishable from one the server understood and acted on.
    body_fields: frozenset[str]
    required_fields: tuple[str, ...]
    # Whether the write must name the control it passed through.
    requires_control: bool = False


ROUTES: Mapping[str, WriteRoute] = {
    INTENT_CHECKPOINT: WriteRoute(
        intent=INTENT_CHECKPOINT,
        effect=EFFECT_CHECKPOINT_APPEND,
        target_table="task_checkpoints",
        authority=AUTHORITY_PARTICIPANT_GRANT,
        # The fields a client may supply on a checkpoint. Identity, ordering,
        # attribution and retention are server-derived and are refused here as
        # unknown, the same way the checkpoint schema refuses them.
        body_fields=frozenset(
            {
                "goal",
                "decisions",
                "assumptions",
                "evidence",
                "completed_checks",
                "open_questions",
                "next_action",
            }
        ),
        required_fields=("goal",),
    ),
    INTENT_OBSERVATION: WriteRoute(
        intent=INTENT_OBSERVATION,
        effect=EFFECT_STAGED_OBSERVATION,
        # The agent's own append-only event stream, not the corroborated store
        # downstream of it. An agent records what it saw; turning that into
        # something the registry stands behind is a later, separate decision
        # made by the one module permitted to make it.
        target_table="memory_session_events",
        authority=AUTHORITY_CITED_EVIDENCE,
        body_fields=frozenset({"subject_ref", "predicate", "value", "evidence_event_ids", "excerpt"}),
        required_fields=("subject_ref", "predicate", "evidence_event_ids"),
    ),
    INTENT_REQUEST: WriteRoute(
        intent=INTENT_REQUEST,
        effect=EFFECT_OWNER_REQUEST,
        target_table="memory_capability_request",
        authority=AUTHORITY_REQUESTER_ENTITLEMENT,
        body_fields=frozenset({"subject_ref", "request_kind", "justification"}),
        required_fields=("subject_ref", "request_kind", "justification"),
    ),
    INTENT_CANONICAL_REVIEW: WriteRoute(
        intent=INTENT_CANONICAL_REVIEW,
        effect=EFFECT_CANONICAL_REVIEW_DECISION,
        target_table="memory_promotion_proposal",
        authority=AUTHORITY_QUALIFIED_CONTROL,
        body_fields=frozenset({"entity_id", "review_kind", "justification"}),
        required_fields=("entity_id", "review_kind", "justification"),
        requires_control=True,
    ),
}


def assert_routes_disjoint(routes: Mapping[str, WriteRoute]) -> None:
    """Refuse a route table where two paths could reach the same place.

    Called on this module's own table at import, so a crossed route fails the
    process at boot rather than at the first request that takes it. Takes the
    table as an argument rather than reading the module global, because a rule
    nothing can be tested against is a rule nobody knows still holds.
    """
    if tuple(routes) != WRITE_INTENTS:
        raise DisallowedWrite(
            f"the agent write routes are exactly {list(WRITE_INTENTS)} in order, got {list(routes)}; "
            "a fifth path is a fifth thing an agent can do without anyone deciding it may"
        )

    for intent, route in routes.items():
        if route.intent != intent:
            raise DisallowedWrite(f"route filed under {intent!r} declares itself {route.intent!r}")
        if route.effect in UNREACHABLE_EFFECTS:
            raise DisallowedWrite(
                f"the {intent} route claims effect {route.effect!r}, which no agent write may produce; "
                "staging, proposing and mutating canonical rows are decisions somebody else makes"
            )
        if route.target_table in TABLES_NO_INTENT_MAY_TARGET:
            raise DisallowedWrite(
                f"the {intent} route targets {route.target_table!r}, which no agent write may write directly"
            )
        if route.authority == AUTHORITY_WORKSPACE_ENTRY:
            raise DisallowedWrite(
                f"the {intent} route accepts a workspace entry as authority; a working note is context, "
                "and treating it as authority is how a thought becomes a record"
            )
        crossed = sorted(route.body_fields & WORKSPACE_BODY_FIELDS)
        if crossed:
            raise DisallowedWrite(
                f"the {intent} route accepts workspace field(s) {crossed}; a body that fits both shapes "
                "routes by whichever surface received it rather than by what it is"
            )
        missing = sorted(field for field in route.required_fields if field not in route.body_fields)
        if missing:
            raise DisallowedWrite(f"the {intent} route requires field(s) {missing} it does not accept")

    dimensions: tuple[tuple[str, list[str]], ...] = (
        ("effect", [str(route.effect) for route in routes.values()]),
        ("target table", [route.target_table for route in routes.values()]),
        ("authority", [str(route.authority) for route in routes.values()]),
    )
    for label, values in dimensions:
        duplicated = sorted({value for value in values if values.count(value) > 1})
        if duplicated:
            raise DisallowedWrite(
                f"two routes share the {label} {duplicated}; the four paths are disjoint, and a shared "
                f"{label} means one of them can be reached by the other's caller"
            )


assert_routes_disjoint(ROUTES)


# --- routing ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoutedWrite:
    """A write that passed routing: what it will do and where it will land.

    Carries the target table so the caller cannot pick a different one after
    the decision was made. The body is a copy, because a caller that kept a
    reference to the mapping it passed in could change it afterwards and the
    routed write would follow.
    """

    intent: WriteIntent
    effect: WriteEffect
    target_table: str
    actor_id: str
    body: dict[str, Any]


def _owners_of(field: str) -> list[str]:
    """Which routes accept this field, for a refusal that names the right one."""
    return [intent for intent, route in ROUTES.items() if field in route.body_fields]


def _refuse_workspace_body(intent: str, body: Mapping[str, Any]) -> None:
    present = sorted(set(body) & WORKSPACE_BODY_FIELDS)
    if present:
        raise DisallowedWrite(
            f"the {intent} route was given workspace field(s) {present}; a workspace entry is context, "
            "never authority, and routing one here would store somebody's working note as a durable record"
        )


def _refuse_wrong_authority(route: WriteRoute, authority: WriteAuthority) -> None:
    if authority.origin == AUTHORITY_WORKSPACE_ENTRY:
        raise DisallowedWrite(
            f"a workspace entry authorizes nothing, including the {route.intent} route; it is somebody's "
            "working note, and a note that can authorize a write is a note that has become a decision"
        )
    if authority.origin != route.authority:
        satisfied = [other.intent for other in ROUTES.values() if other.authority == authority.origin]
        belongs = f"satisfies the {satisfied[0]} route" if satisfied else "satisfies no route"
        raise DisallowedWrite(
            f"the {route.intent} route needs {route.authority!r} authority, got {authority.origin!r}, which "
            f"{belongs}; authority does not carry across routes, because an actor trusted for one of these "
            "writes was not thereby trusted for the others"
        )
    if route.requires_control and authority.control_id is None:
        raise DisallowedWrite(
            f"the {route.intent} route goes through a control that already qualified the caller and must name "
            "it; a review naming no control is one nothing checked"
        )
    if not route.requires_control and authority.control_id is not None:
        raise DisallowedWrite(
            f"the {route.intent} route passes through no control, so a control id on it is the caller saying "
            "it believes it is reviewing something; the review route is the one that reviews"
        )


def _refuse_foreign_fields(route: WriteRoute, body: Mapping[str, Any]) -> None:
    for field in sorted(body):
        if field in route.body_fields:
            continue
        owners = _owners_of(field)
        if owners:
            raise DisallowedWrite(
                f"field {field!r} belongs to the {', '.join(owners)} route, not the {route.intent} route; "
                "the four write paths are disjoint in both directions, and a body that crosses them would be "
                "stored as something nobody wrote"
            )
        raise DisallowedWrite(
            f"the {route.intent} route received unknown field {field!r}; the shape is closed, because a "
            "dropped field is indistinguishable from one the server understood and acted on"
        )


def _refuse_incomplete_body(route: WriteRoute, body: Mapping[str, Any]) -> None:
    missing = sorted(field for field in route.required_fields if field not in body)
    if missing:
        raise DisallowedWrite(f"the {route.intent} route is missing required field(s) {missing}")

    for field in route.required_fields:
        value = body[field]
        if isinstance(value, str) and not value.strip():
            raise DisallowedWrite(
                f"the {route.intent} route needs a {field}; a blank one passes an 'is it set' check and "
                "says nothing to whoever reads the row"
            )
        if isinstance(value, list | tuple) and not value:
            if field == "evidence_event_ids":
                raise DisallowedWrite(
                    "an observation cites at least one evidence event; a claim citing nothing is "
                    "indistinguishable from an invention, and staging it puts it where a curator may promote it"
                )
            raise DisallowedWrite(f"the {route.intent} route needs a non-empty {field}")


def _refuse_uncited_observation(route: WriteRoute, body: Mapping[str, Any]) -> None:
    """The observation route stages a claim, so its citation is not optional."""
    if route.intent != INTENT_OBSERVATION:
        return
    cited = body["evidence_event_ids"]
    if isinstance(cited, str) or not isinstance(cited, list | tuple):
        raise DisallowedWrite(
            "evidence_event_ids is a list of event ids; a bare string would be read one character at a time "
            "and every character would look like a citation"
        )
    for entry in cited:
        if not isinstance(entry, str) or not entry.strip():
            raise DisallowedWrite("every cited evidence event is a non-empty id; a blank one cites nothing")


def route_agent_write(intent: str, body: Mapping[str, Any], *, authority: WriteAuthority) -> RoutedWrite:
    """Decide which of the four writes this is, or refuse it.

    Refuses rather than repairs at every step. A repaired write is still the
    wrong write, and it is one nobody can see was repaired: the row it produces
    looks exactly like a row somebody meant to create.
    """
    route = ROUTES.get(intent)
    if route is None:
        raise DisallowedWrite(
            f"unknown write intent {intent!r}; an agent writes in exactly {list(WRITE_INTENTS)} ways, and "
            "an unrecognised intent has no safe default -- every default is somebody's write"
        )

    for key in body:
        if not isinstance(key, str):
            raise DisallowedWrite(f"a write body is keyed by field name, got a {type(key).__name__} key")

    _refuse_workspace_body(route.intent, body)
    _refuse_wrong_authority(route, authority)
    _refuse_foreign_fields(route, body)
    _refuse_incomplete_body(route, body)
    _refuse_uncited_observation(route, body)

    return RoutedWrite(
        intent=route.intent,
        effect=route.effect,
        target_table=route.target_table,
        actor_id=authority.actor_id,
        body=dict(body),
    )


def effect_of(intent: str) -> WriteEffect:
    """The one effect an intent produces. Raises on anything else."""
    route = ROUTES.get(intent)
    if route is None:
        raise DisallowedWrite(f"unknown write intent {intent!r}; the four are {list(WRITE_INTENTS)}")
    return route.effect


def target_table_of(intent: str) -> str:
    """The one table an intent writes. Raises on anything else."""
    route = ROUTES.get(intent)
    if route is None:
        raise DisallowedWrite(f"unknown write intent {intent!r}; the four are {list(WRITE_INTENTS)}")
    return route.target_table


__all__ = [
    "AUTHORITY_CITED_EVIDENCE",
    "AUTHORITY_ORIGINS",
    "AUTHORITY_PARTICIPANT_GRANT",
    "AUTHORITY_QUALIFIED_CONTROL",
    "AUTHORITY_REQUESTER_ENTITLEMENT",
    "AUTHORITY_WORKSPACE_ENTRY",
    "EFFECT_CANONICAL_REVIEW_DECISION",
    "EFFECT_CHECKPOINT_APPEND",
    "EFFECT_OWNER_REQUEST",
    "EFFECT_STAGED_OBSERVATION",
    "INTENT_CANONICAL_REVIEW",
    "INTENT_CHECKPOINT",
    "INTENT_OBSERVATION",
    "INTENT_REQUEST",
    "ROUTES",
    "TABLES_NO_INTENT_MAY_TARGET",
    "UNREACHABLE_EFFECTS",
    "WORKSPACE_BODY_FIELDS",
    "WRITE_INTENTS",
    "AuthorityOrigin",
    "DisallowedWrite",
    "RoutedWrite",
    "WriteAuthority",
    "WriteEffect",
    "WriteIntent",
    "WriteRoute",
    "assert_routes_disjoint",
    "effect_of",
    "route_agent_write",
    "target_table_of",
]
