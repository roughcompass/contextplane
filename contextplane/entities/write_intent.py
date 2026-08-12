"""The three ways a generic profile write may arrive, and why the strongest of
them is never the caller's to grant itself.

One generic contract serves every profile type, so "what is this write allowed
to do" can no longer be answered by which endpoint received it. It is answered
here, once, from two things that are deliberately kept apart: an **intent** the
caller states, and an **authority** the server resolved. The caller says what it
is trying to do; the server says what it is standing on. A surface that
collapsed the two would let the request describe its own permissions.

**There is no default intent.** Every default is somebody's write. A body that
arrives without an intent is not obviously an observation -- it is equally a
proposal somebody meant to route for review, and picking either one on the
caller's behalf produces a row that looks exactly like one that was chosen. The
absence is refused rather than filled.

**Stating the approval intent does not confer approval.** An ordinary agent may
name `authorized_approval` in its body all day; what routes the write is the
authority the server resolved for that actor, and an actor that did not pass an
approval control does not have it. This is the whole reason the two are
separate: if the strongest write in the system could be selected by a request
field, then every caller would already hold it, and the control would exist only
for callers that chose to respect it.

**Nothing a caller sends may assert canonical authority.** Beyond the route, a
second refusal covers the fields: trust class, validation outcome, approver,
audit reference and the rest are what the platform *concluded*, not what the
caller *observed*. A caller-set trust class would be indistinguishable from a
platform-derived one the moment it was stored, and no later reader could tell
which it had been.

The three intents are disjoint in effect: an observation stages a claim nobody
has yet stood behind, a request enters the accountable owner's queue, and only a
verified approval reaches canonical validation. Each lands in exactly one place.

This vocabulary is not the agent context-write vocabulary, which covers task
checkpoints and memory events and has four members of its own. The two are
separate because they govern different stores under different authorities;
naming either one "the" write intent would invite a surface to reach for
whichever it imported first.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

# --- intents ------------------------------------------------------------------

ProfileWriteIntent = Literal["observation", "request", "authorized_approval"]

INTENT_OBSERVATION: ProfileWriteIntent = "observation"
INTENT_REQUEST: ProfileWriteIntent = "request"
INTENT_AUTHORIZED_APPROVAL: ProfileWriteIntent = "authorized_approval"

# Order is stable so a refusal lists the alternatives the same way every time,
# and weakest-first so the list does not read as a menu topped by the strongest.
PROFILE_WRITE_INTENTS: tuple[ProfileWriteIntent, ...] = (
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    INTENT_AUTHORIZED_APPROVAL,
)


# --- effects ------------------------------------------------------------------

ProfileWriteEffect = Literal["staged_claim", "owner_review_entry", "canonical_assertion_write"]

EFFECT_STAGED_CLAIM: ProfileWriteEffect = "staged_claim"
EFFECT_OWNER_REVIEW_ENTRY: ProfileWriteEffect = "owner_review_entry"
EFFECT_CANONICAL_ASSERTION_WRITE: ProfileWriteEffect = "canonical_assertion_write"


# --- authority ----------------------------------------------------------------

ProfileWriteAuthorityOrigin = Literal["observed_evidence", "requester_entitlement", "verified_approval"]

AUTHORITY_OBSERVED_EVIDENCE: ProfileWriteAuthorityOrigin = "observed_evidence"
AUTHORITY_REQUESTER_ENTITLEMENT: ProfileWriteAuthorityOrigin = "requester_entitlement"
AUTHORITY_VERIFIED_APPROVAL: ProfileWriteAuthorityOrigin = "verified_approval"

PROFILE_WRITE_AUTHORITY_ORIGINS: frozenset[str] = frozenset(
    {
        AUTHORITY_OBSERVED_EVIDENCE,
        AUTHORITY_REQUESTER_ENTITLEMENT,
        AUTHORITY_VERIFIED_APPROVAL,
    }
)

# Field names a request body may never carry, because each one states what the
# platform concluded rather than what the caller saw. Stored, a caller-supplied
# value here is byte-identical to a derived one, so the distinction has to be
# enforced at the door or it stops existing at all. `approval_reference` is
# pointedly absent: a caller may *name* the approval it believes it holds, and
# the service re-resolves that name rather than trusting it.
RESERVED_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "approved_at",
        "approved_by",
        "assertion_id",
        "audit_reference",
        "authority",
        "canonical",
        "extension_digest",
        "freshness_state",
        "ingested_at",
        "provenance_id",
        "revocation_ref",
        "revoked_at",
        "trust_class",
        "validated_at",
        "validating_profile_revision",
        "validation_result",
    }
)


class RefusedProfileWrite(ValueError):
    """A generic profile write was refused before anything was stored.

    Refusal is the only outcome besides routing. There is no repair step: a
    write corrected on the caller's behalf still lands as a row somebody else
    decided the shape of, and nothing downstream can see that it was corrected.
    """


@dataclasses.dataclass(frozen=True)
class ProfileWriteAuthority:
    """What the server resolved about the caller, never what the caller claimed.

    Constructed from authenticated identity and, for the approval route, from
    approval evidence the service verified itself. It is a separate object from
    the request body precisely so that no code path can build one out of the
    body it is meant to be judging.
    """

    actor_id: str
    origin: ProfileWriteAuthorityOrigin
    # The approval this actor actually passed, for the one route that requires
    # passing one. Absent everywhere else: an approval id resolved for a caller
    # taking the observation route means something upstream resolved the wrong
    # thing.
    approval_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise RefusedProfileWrite(
                "a profile write needs an actor; an unattributed assertion cannot be reviewed, "
                "superseded, or revoked by anyone afterwards"
            )
        if self.origin not in PROFILE_WRITE_AUTHORITY_ORIGINS:
            raise RefusedProfileWrite(
                f"unknown authority origin {self.origin!r}; the server resolves one of "
                f"{sorted(PROFILE_WRITE_AUTHORITY_ORIGINS)}"
            )
        if self.origin == AUTHORITY_VERIFIED_APPROVAL:
            if self.approval_reference is None or not self.approval_reference.strip():
                raise RefusedProfileWrite(
                    "a verified approval names the approval it verified; an approval authority with no "
                    "reference is indistinguishable from one nothing checked"
                )
        elif self.approval_reference is not None:
            raise RefusedProfileWrite(
                f"authority origin {self.origin!r} carries no approval, so an approval reference on it is "
                "something upstream resolved for a different route"
            )


# --- routes -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProfileWriteRoute:
    """One intent's whole contract: what it does, and on whose authority."""

    intent: ProfileWriteIntent
    effect: ProfileWriteEffect
    authority: ProfileWriteAuthorityOrigin
    # Whether the caller may name the approval it believes it holds. True for
    # exactly one route, and on that route it is mandatory.
    carries_approval_reference: bool = False


ROUTES: Mapping[str, ProfileWriteRoute] = {
    INTENT_OBSERVATION: ProfileWriteRoute(
        intent=INTENT_OBSERVATION,
        # Staged, not asserted. The writer records what it saw; whether the
        # platform stands behind it is a later decision made by somebody who
        # can be held to it.
        effect=EFFECT_STAGED_CLAIM,
        authority=AUTHORITY_OBSERVED_EVIDENCE,
    ),
    INTENT_REQUEST: ProfileWriteRoute(
        intent=INTENT_REQUEST,
        effect=EFFECT_OWNER_REVIEW_ENTRY,
        authority=AUTHORITY_REQUESTER_ENTITLEMENT,
    ),
    INTENT_AUTHORIZED_APPROVAL: ProfileWriteRoute(
        intent=INTENT_AUTHORIZED_APPROVAL,
        effect=EFFECT_CANONICAL_ASSERTION_WRITE,
        authority=AUTHORITY_VERIFIED_APPROVAL,
        carries_approval_reference=True,
    ),
}


def assert_routes_disjoint(routes: Mapping[str, ProfileWriteRoute]) -> None:
    """Refuse a route table where two intents could reach the same place.

    Run against this module's own table at import, so a crossed route fails the
    process at boot rather than at the first request that takes it. Takes the
    table as an argument rather than reading the module global, because a rule
    nothing can be tested against is a rule nobody knows still holds.
    """
    if tuple(routes) != PROFILE_WRITE_INTENTS:
        raise RefusedProfileWrite(
            f"the generic write routes are exactly {list(PROFILE_WRITE_INTENTS)} in order, got "
            f"{list(routes)}; a fourth route is a fourth thing a caller may do without anyone deciding it may"
        )

    for intent, route in routes.items():
        if route.intent != intent:
            raise RefusedProfileWrite(f"route filed under {intent!r} declares itself {route.intent!r}")
        if route.carries_approval_reference != (route.authority == AUTHORITY_VERIFIED_APPROVAL):
            raise RefusedProfileWrite(
                f"the {intent} route disagrees with itself about approval: an approval reference is carried "
                "by exactly the route that requires verified approval, and by no other"
            )

    dimensions: tuple[tuple[str, list[str]], ...] = (
        ("effect", [str(route.effect) for route in routes.values()]),
        ("authority", [str(route.authority) for route in routes.values()]),
    )
    for label, values in dimensions:
        duplicated = sorted({value for value in values if values.count(value) > 1})
        if duplicated:
            raise RefusedProfileWrite(
                f"two routes share the {label} {duplicated}; the three intents are disjoint, and a shared "
                f"{label} means one of them is reachable by the other's caller"
            )


assert_routes_disjoint(ROUTES)


# --- routing ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoutedProfileWrite:
    """A write that passed routing: what it will do, and who it was resolved for.

    Carries the effect so no caller can pick a different one after the decision
    was made, and the actor so the assertion it produces is attributable without
    a second lookup that could disagree with this one.
    """

    intent: ProfileWriteIntent
    effect: ProfileWriteEffect
    actor_id: str
    approval_reference: str | None = None


def refuse_caller_asserted_authority(body: Mapping[str, Any], *, where: str = "request") -> None:
    """Refuse a body that states what the platform concluded.

    Named separately from the shape validation so it can be applied to nested
    objects that have their own field sets, and so the refusal says *why* the
    field is unwelcome rather than reporting it as merely unrecognised. A field
    rejected as "unknown" invites somebody to add it.
    """
    asserted = sorted(field for field in body if field in RESERVED_AUTHORITY_FIELDS)
    if asserted:
        raise RefusedProfileWrite(
            f"the {where} sets {asserted}, which the platform derives rather than accepts; a caller-supplied "
            "trust class, validation outcome, approver or audit reference is stored as the platform's own "
            "conclusion and no later reader can tell the difference"
        )


def route_profile_write(
    intent: str | None,
    *,
    authority: ProfileWriteAuthority,
    approval_reference: str | None = None,
) -> RoutedProfileWrite:
    """Decide which of the three writes this is, or refuse it.

    `intent` is what the caller asked for and `authority` is what the server
    resolved. They are compared, never merged: the request selects a route, and
    the resolved authority decides whether that route is open to this actor.
    """
    if intent is None or not str(intent).strip():
        raise RefusedProfileWrite(
            f"a generic profile write states its intent, one of {list(PROFILE_WRITE_INTENTS)}; there is no "
            "default, because every default routes somebody's write somewhere they did not choose"
        )

    route = ROUTES.get(intent)
    if route is None:
        raise RefusedProfileWrite(
            f"unknown write intent {intent!r}; a generic profile write is one of "
            f"{list(PROFILE_WRITE_INTENTS)}, and an unrecognised intent has no safe interpretation"
        )

    if authority.origin != route.authority:
        raise RefusedProfileWrite(
            f"the {route.intent} route needs {route.authority!r} authority; this caller resolved to "
            f"{authority.origin!r}. Authority is resolved from authenticated identity and verified approval "
            "evidence, so naming a route in the request body does not open it"
        )

    if not route.carries_approval_reference:
        if approval_reference is not None:
            raise RefusedProfileWrite(
                f"the {route.intent} route passes through no approval, so an approval reference on it is the "
                "caller asserting a review that did not happen"
            )
        return RoutedProfileWrite(intent=route.intent, effect=route.effect, actor_id=authority.actor_id)

    if approval_reference is None or not approval_reference.strip():
        raise RefusedProfileWrite(
            f"the {route.intent} route names the approval it passed; an approval route naming none is one "
            "nothing can be re-resolved against"
        )
    if approval_reference != authority.approval_reference:
        raise RefusedProfileWrite(
            "the approval named by the request is not the approval verified for this caller; the service "
            "re-resolves the reference rather than trusting it, and a mismatch means the caller is pointing "
            "at somebody else's approval"
        )

    return RoutedProfileWrite(
        intent=route.intent,
        effect=route.effect,
        actor_id=authority.actor_id,
        approval_reference=authority.approval_reference,
    )


def effect_of(intent: str) -> ProfileWriteEffect:
    """The one effect an intent produces. Raises on anything else."""
    route = ROUTES.get(intent)
    if route is None:
        raise RefusedProfileWrite(f"unknown write intent {intent!r}; the three are {list(PROFILE_WRITE_INTENTS)}")
    return route.effect


__all__ = [
    "AUTHORITY_OBSERVED_EVIDENCE",
    "AUTHORITY_REQUESTER_ENTITLEMENT",
    "AUTHORITY_VERIFIED_APPROVAL",
    "EFFECT_CANONICAL_ASSERTION_WRITE",
    "EFFECT_OWNER_REVIEW_ENTRY",
    "EFFECT_STAGED_CLAIM",
    "INTENT_AUTHORIZED_APPROVAL",
    "INTENT_OBSERVATION",
    "INTENT_REQUEST",
    "PROFILE_WRITE_AUTHORITY_ORIGINS",
    "PROFILE_WRITE_INTENTS",
    "RESERVED_AUTHORITY_FIELDS",
    "ROUTES",
    "ProfileWriteAuthority",
    "ProfileWriteAuthorityOrigin",
    "ProfileWriteEffect",
    "ProfileWriteIntent",
    "ProfileWriteRoute",
    "RefusedProfileWrite",
    "RoutedProfileWrite",
    "assert_routes_disjoint",
    "effect_of",
    "refuse_caller_asserted_authority",
    "route_profile_write",
]
