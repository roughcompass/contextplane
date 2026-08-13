"""Interfaces as profile-governed entities, and the gate that must pass before retiring the old shape.

Interfaces are stored today as two attributes on a capability — a source document
and a canonical one — under fixed keys. That works and says nothing: an attribute
cannot state which capability provides an interface, which consumes it, or that
one version supersedes another. Those are relationships, and expressing them as
relationships is what makes an interface answerable to the same governance
everything else is.

This module states the vocabulary that migration targets: the two entity types,
the three relationship types between them, and the compatibility view derived
from them. It writes nothing. `profile` is the bottom of the profile-domain
layers and may not import `relationships`, so the backfill itself is driven from
above through the transactional relationship writer — the same shape the
definitions projection takes.

**The retirement gate is encoded, not described.** The old surface is not removed
here, and cannot be removed by this code at all: `RetirementGate` requires five
independent conditions, three of which are evidence from outside this repository
(a notice period served, a Product approval, a rollback window agreed). A gate
whose conditions live only in a runbook is a gate somebody skips at the moment it
is most inconvenient, so it is a value that has to be constructed with every field
present and refuses otherwise.

**Equivalence is the condition that cannot be waived.** The new reads must return
what the approved reads return. Not "close enough" and not "modulo formatting" —
the adapter preserves the existing REST surface byte-for-byte, so if equivalence
cannot hold without a schema change, that is a finding to escalate rather than a
detail to absorb.
"""

from __future__ import annotations

import dataclasses
from typing import Final

from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition

NAMESPACE: Final = "core"

INTERFACE_TYPE: Final = f"{NAMESPACE}:interface"
INTERFACE_VERSION_TYPE: Final = f"{NAMESPACE}:interface_version"
CAPABILITY_TYPE: Final = f"{NAMESPACE}:capability"

VERSION_OF: Final = f"{NAMESPACE}:version_of"
PROVIDES: Final = f"{NAMESPACE}:provides"
CONSUMES: Final = f"{NAMESPACE}:consumes"

#: The two entity types an interface family occupies.
#:
#: An interface and its versions are separate types rather than one type with a
#: version attribute, because they have different relationships: a capability
#: provides a *version*, while `version_of` joins a version to its interface. One
#: type would make both edges point at the same thing and lose which is which.
INTERFACE_ENTITY_TYPES: Final[tuple[EntityTypeDefinition, ...]] = (
    EntityTypeDefinition(namespace=NAMESPACE, type_name="interface"),
    EntityTypeDefinition(namespace=NAMESPACE, type_name="interface_version"),
)


def _relationship(
    type_name: str,
    *,
    source_type: str,
    destination_type: str,
    max_cardinality: int | None = None,
) -> RelationshipTypeDefinition:
    return RelationshipTypeDefinition(
        namespace=NAMESPACE,
        type_name=type_name,
        source_type=source_type,
        destination_type=destination_type,
        direction="directed",
        cardinality_scope="per_source",
        authority="canonical_owner",
        cross_org_policy="deny",
        max_cardinality=max_cardinality,
        duplicate_policy="reject",
        symmetry="asymmetric",
        inverse_view="read_only",
    )


#: The three typed relationships an interface family needs.
#:
#: `version_of` has a maximum of one: a version belongs to exactly one interface,
#: and a second would make "which interface is this a version of" ambiguous.
#: `provides` and `consumes` are unbounded — a capability may provide several
#: interface versions and consume many.
INTERFACE_RELATIONSHIP_TYPES: Final[tuple[RelationshipTypeDefinition, ...]] = (
    _relationship(
        "version_of",
        source_type=INTERFACE_VERSION_TYPE,
        destination_type=INTERFACE_TYPE,
        max_cardinality=1,
    ),
    _relationship("provides", source_type=CAPABILITY_TYPE, destination_type=INTERFACE_VERSION_TYPE),
    _relationship("consumes", source_type=CAPABILITY_TYPE, destination_type=INTERFACE_VERSION_TYPE),
)


@dataclasses.dataclass(frozen=True)
class CompatibilityEdge:
    """One provider/consumer pair, derived rather than stored.

    Compatibility is not asserted by anyone: it is what follows from a capability
    providing a version another capability consumes. Storing it would create a
    third fact that can disagree with the two it was computed from.
    """

    provider_entity_id: str
    consumer_entity_id: str
    interface_version_entity_id: str


def compatibility_view(
    *,
    provides: dict[str, set[str]],
    consumes: dict[str, set[str]],
) -> tuple[CompatibilityEdge, ...]:
    """Derive who can talk to whom, from who provides and who consumes what.

    Both arguments map an interface-version id to the capability ids on that side.
    A capability that both provides and consumes the same version is not paired
    with itself: a self-edge here would say a capability is compatible with
    itself, which is true and useless, and it would make every closure walk over
    this view non-terminating.

    Ordered, so two readers of one graph see the same sequence.
    """
    edges: list[CompatibilityEdge] = []
    for version_id in sorted(set(provides) & set(consumes)):
        for provider in sorted(provides[version_id]):
            for consumer in sorted(consumes[version_id]):
                if provider == consumer:
                    continue
                edges.append(
                    CompatibilityEdge(
                        provider_entity_id=provider,
                        consumer_entity_id=consumer,
                        interface_version_entity_id=version_id,
                    )
                )
    return tuple(edges)


class RetirementRefused(RuntimeError):
    """The old interface surface may not be retired yet, and this says which condition failed."""


@dataclasses.dataclass(frozen=True)
class RetirementGate:
    """The five conditions that must all hold before the legacy surface is removed.

    Three of them are evidence from outside this repository. That is the point:
    a gate whose conditions live only in a runbook is one somebody skips at the
    moment it is most inconvenient, so each is a required field and the value
    cannot be constructed without them.

    Deliberately has no "force" or "override" parameter. An override would be
    used, and the audit trail afterwards would show a retirement that passed its
    gate.
    """

    consumer_count: int
    notice_period_served: bool
    product_approval_reference: str | None
    equivalence_proven: bool
    rollback_window_days: int

    def assert_satisfied(self) -> None:
        """Refuse retirement unless every condition holds, naming the first that does not."""
        if self.consumer_count != 0:
            msg = (
                f"{self.consumer_count} consumer(s) still use the legacy interface surface; retiring it now "
                "breaks them, and the inventory is what tells you who they are"
            )
            raise RetirementRefused(msg)
        if not self.notice_period_served:
            msg = "the notice period has not been served; consumers were not told this was coming"
            raise RetirementRefused(msg)
        if not self.product_approval_reference or not self.product_approval_reference.strip():
            msg = "retirement carries a Product approval reference; without one nobody agreed to it"
            raise RetirementRefused(msg)
        if not self.equivalence_proven:
            msg = (
                "the new reads have not been proven equivalent to the approved ones; retiring before that is "
                "removing the only surface that still works"
            )
            raise RetirementRefused(msg)
        if self.rollback_window_days <= 0:
            msg = "a retirement with no rollback window cannot be undone when it turns out to be wrong"
            raise RetirementRefused(msg)

    @property
    def is_satisfied(self) -> bool:
        """Whether retirement may proceed, for a caller that wants to branch."""
        try:
            self.assert_satisfied()
        except RetirementRefused:
            return False
        return True


__all__ = [
    "CAPABILITY_TYPE",
    "CONSUMES",
    "INTERFACE_ENTITY_TYPES",
    "INTERFACE_RELATIONSHIP_TYPES",
    "INTERFACE_TYPE",
    "INTERFACE_VERSION_TYPE",
    "NAMESPACE",
    "PROVIDES",
    "VERSION_OF",
    "CompatibilityEdge",
    "RetirementGate",
    "RetirementRefused",
    "compatibility_view",
]
