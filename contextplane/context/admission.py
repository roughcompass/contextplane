"""Admission: what may enter storage, decided before it gets there.

Every pilot write surface asks this module one question -- may this content be
stored -- and gets an answer plus, on refusal, a record of why. Detection itself
is not decided here: the prohibited classes are the shipped detectors, and this
module adds none. What it decides is the *consequence*, which is the part that
was missing.

**The floor is block, and it lives in code rather than in configuration.**
Detection has always been on; refusal has not. The scanner escalates to blocking
only when a policy row says so, so a deployment with no rows configured detected
a card number, logged it, and stored it. Admission passes an explicit blocking
policy for every prohibited class on every pilot field type, so the floor holds
on a fresh deployment with an empty policy table. A tenant policy can still
raise severity and can never lower it, because the scanner takes the maximum --
which is the only direction a floor may be adjusted in.

**A refusal is recorded, and the record never carries the offending value.** The
whole point of refusing is that the content is prohibited; copying it into an
audit row would put it in the one place guaranteed to be retained and read. The
record names the class and the field, and that is enough to act on. Offsets are
excluded for the same reason at one remove: an offset is not the value, but it
points at it, and a record that locates a credential inside stored text is a
record that helps somebody find the credential.

**A class is never a trigger.** The trigger says what kind of refusal this was;
the class says which detector fired. Collapsing them would make the trigger
vocabulary grow by one every time a detector is added, and that vocabulary is a
metric label with a closed set.

**Admission answers; it does not store.** It has no session and writes nothing.
A caller that ignores a refusal and stores anyway is a caller this module cannot
stop, which is why the conformance suite checks callers rather than trusting
them.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, cast

from contextplane.security.pii_patterns import BUILT_IN_PATTERNS
from contextplane.security.pii_scanner import PiiPattern, PiiScanner

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextplane.types import PiiScanResponse

#: The refusal code for content carrying a prohibited class. One code, not one
#: per class: this value is a Prometheus label and its set is closed, so a new
#: detector must not widen it. Which detector fired goes in `pii_class`.
TRIGGER_PII_BLOCKED = "pii_blocked"

#: The field types classification attaches to. Classification attaches to a
#: field, not to a module -- two surfaces writing the same field carry the same
#: obligation, and a surface writing none of these has nothing to admit.
FIELD_MEMORY_SESSION_EVENT_BODY = "memory_session_event.body"
FIELD_ARTIFACT_BODY = "artifact.body"
FIELD_CLAIM_VALUE = "claim_value"
FIELD_WORKSPACE_ENTRY_BODY = "workspace_entry.body"
FIELD_WORKSPACE_ENTRY_REFERENCES = "workspace_entry.references"

#: The observation a signal carries, in whichever form it arrives. One field
#: type covers both the canonical serialization of the payload mapping *and* the
#: evidence-handle URI, because they are the same thing to a policy: what the
#: producer says it saw. Splitting them would let a deployment block one and
#: admit the other, and a URI is a real token channel -- a credential in a query
#: string is a credential in storage.
FIELD_EXTERNAL_SIGNAL_PAYLOAD = "external_signal.payload"
#: The normalized references a signal cites, serialized canonically. Separate
#: from the payload because they are separately authored: a producer can get the
#: observation right and still paste a credential into a URI beside it.
FIELD_EXTERNAL_SIGNAL_REFERENCES = "external_signal.references"

PILOT_FIELD_TYPES: frozenset[str] = frozenset(
    {
        FIELD_MEMORY_SESSION_EVENT_BODY,
        FIELD_ARTIFACT_BODY,
        FIELD_CLAIM_VALUE,
        FIELD_WORKSPACE_ENTRY_BODY,
        FIELD_WORKSPACE_ENTRY_REFERENCES,
        FIELD_EXTERNAL_SIGNAL_PAYLOAD,
        FIELD_EXTERNAL_SIGNAL_REFERENCES,
    }
)

#: The shipped detectors, named through the protocol they satisfy. The registry
#: itself is a plain list of singletons whose joined type is `object`, so the
#: annotation is what lets this module read `.name` off them at all.
_BUILT_IN: tuple[PiiPattern, ...] = tuple(cast("list[PiiPattern]", BUILT_IN_PATTERNS))

#: The prohibited classes, read off the shipped detectors rather than restated.
#: A list written here by hand would be a second source of truth, and the two
#: would disagree the first time a detector was added -- in the direction that
#: silently admits the new class.
PROHIBITED_CLASSES: frozenset[str] = frozenset(pattern.name for pattern in _BUILT_IN)


class NotAPilotField(ValueError):
    """Admission was asked about a field type it has no floor for.

    Refused rather than defaulted. Defaulting to "admit" would make a typo in a
    field name silently disable admission for that surface, and defaulting to
    "refuse" would break every write of a field that legitimately carries no
    classification.
    """


@dataclasses.dataclass(frozen=True)
class RefusalRecord:
    """One refusal, in the shape an auditor reads.

    Deliberately carries no offset, no length and no excerpt. The value is
    prohibited; a record locating it inside stored text is a pointer to the
    thing the refusal exists to keep out.
    """

    trigger: str
    #: Which detector fired. Named `pii_class` rather than `pii_category`
    #: because the shipped detectors expose both a fine-grained name (`ssn`) and
    #: a coarse category (`CREDENTIALS`), and an auditor asking "which class was
    #: refused" means the first. The coarse one is carried alongside.
    pii_class: str
    pii_category: str
    detail: str
    tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    target_type: str
    target_id: uuid.UUID | None
    occurred_at: datetime.datetime
    #: Which strategy produced the content, when one did. Nullable because it is
    #: set only where a namespace is present, so attribution is not guaranteed.
    strategy_id: str | None = None

    def as_audit_payload(self) -> dict[str, object]:
        """The record as an audit row's payload.

        Explicit rather than `dataclasses.asdict` so a field added here has to
        be considered before it reaches a durable row -- which is the moment to
        notice that it should not.
        """
        return {
            "trigger": self.trigger,
            "pii_class": self.pii_class,
            "pii_category": self.pii_category,
            "detail": self.detail,
            "strategy_id": self.strategy_id,
            "field_type": self.target_type,
        }


@dataclasses.dataclass(frozen=True)
class AdmissionDecision:
    """Whether content may be stored, and every reason it may not.

    `refusals` is a tuple rather than a first-failure, because content carrying
    both a card number and a secret key has two problems, and a caller told
    about one will fix it and be refused again.
    """

    admitted: bool
    refusals: tuple[RefusalRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.admitted and self.refusals:
            raise ValueError("an admitted decision cannot carry refusals; that reading is what gets ignored")
        if not self.admitted and not self.refusals:
            raise ValueError("a refusal must say why; an unexplained refusal is indistinguishable from a bug")

    @property
    def classes(self) -> tuple[str, ...]:
        """Every prohibited class found, in detection order."""
        return tuple(refusal.pii_class for refusal in self.refusals)


def blocking_field_policies() -> dict[str, str]:
    """The floor, as the scanner's own policy vocabulary.

    Every prohibited class blocked on every pilot field type. Built rather than
    written out so adding a detector or a field type extends the floor by
    construction; a hand-written table would have to be remembered, and the
    failure mode of forgetting is silent admission.
    """
    return {
        f"{field_type}:{pii_class}": "block"
        for field_type in sorted(PILOT_FIELD_TYPES)
        for pii_class in sorted(PROHIBITED_CLASSES)
    }


def _scanner() -> PiiScanner:
    """A scanner with the built-in detectors and no tenant default.

    The tenant default is left advisory on purpose: the floor is supplied per
    field below, and the scanner takes the maximum of the two, so a tenant that
    has configured nothing still gets blocked. Passing a blocking tenant default
    here instead would block every field type, including ones this document has
    made no classification decision about.
    """
    return PiiScanner(patterns=list(_BUILT_IN), tenant_policy="advisory")


def admit(
    content: str,
    *,
    field_type: str,
    tenant_id: uuid.UUID,
    now: datetime.datetime,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    strategy_id: str | None = None,
) -> AdmissionDecision:
    """Decide whether `content` may be stored in `field_type`.

    Runs before storage, not after. The caller must not write on a refusal --
    this module has no session and cannot enforce that, which is why the
    conformance suite checks the call sites.

    Raises `NotAPilotField` for a field type with no floor, rather than
    admitting it: a mistyped field name must not be the way admission gets
    switched off for a surface.
    """
    if field_type not in PILOT_FIELD_TYPES:
        raise NotAPilotField(
            f"{field_type!r} has no admission floor; the pilot field types are {sorted(PILOT_FIELD_TYPES)}"
        )

    response: PiiScanResponse = _scanner().scan(
        content,
        field_type=field_type,
        field_policies=blocking_field_policies(),
    )
    if response.action_taken != "block":
        return AdmissionDecision(admitted=True)

    seen: set[str] = set()
    refusals: list[RefusalRecord] = []
    for match in response.matched_patterns:
        if match.name in seen or match.name not in PROHIBITED_CLASSES:
            continue
        seen.add(match.name)
        refusals.append(
            RefusalRecord(
                trigger=TRIGGER_PII_BLOCKED,
                pii_class=match.name,
                pii_category=match.category,
                detail=f"content carries a prohibited class ({match.name}) and was refused before storage",
                tenant_id=tenant_id,
                actor_id=actor_id,
                target_type=field_type,
                target_id=target_id,
                occurred_at=now,
                strategy_id=strategy_id,
            )
        )

    if not refusals:
        # The scanner said block and named nothing this module recognises. That
        # is a tenant-configured pattern, and refusing without being able to say
        # which class would produce an unexplained refusal -- so it is reported
        # as the generic code with no class rather than swallowed.
        refusals.append(
            RefusalRecord(
                trigger=TRIGGER_PII_BLOCKED,
                pii_class="unknown",
                pii_category="unknown",
                detail="a configured pattern blocked this content; the class is not one of the built-in detectors",
                tenant_id=tenant_id,
                actor_id=actor_id,
                target_type=field_type,
                target_id=target_id,
                occurred_at=now,
                strategy_id=strategy_id,
            )
        )
    return AdmissionDecision(admitted=False, refusals=tuple(refusals))


__all__ = [
    "PILOT_FIELD_TYPES",
    "PROHIBITED_CLASSES",
    "TRIGGER_PII_BLOCKED",
    "AdmissionDecision",
    "NotAPilotField",
    "RefusalRecord",
    "admit",
    "blocking_field_policies",
]
