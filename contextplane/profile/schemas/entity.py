"""The closed core entity vocabulary, and the rules an extension cannot bend.

A tenant may extend this vocabulary. It may not change it. Everything below
exists to make the second sentence checkable rather than merely stated: a
vocabulary one tenant can quietly narrow is not shared vocabulary, only a set of
local dialects that spell things the same way.

Composition is additive or it fails -- no first-wins, no last-wins, no implicit
override, since a merged-with-a-warning result is indistinguishable from one
somebody designed on purpose. Refusals come back collected and ordered, because
an author who fixes them one round-trip apiece only ever sees the shape of the
last. Weakening is named separately from collision for the same reason it is
tempting: dropping a required property, widening a maximum, or lowering a
minimum costs the extension asking nothing, and costs every reader relying on
the core guarantee, none of whom are in the room. Canonicalization makes the
result reproducible -- sorting and normalizing once, here, so the same inputs
digest identically anywhere. Each rule is argued at its definition site below.

Scope note: this defines entity and ownership *definitions*. Composing an
extension onto them is the sibling module's job, and the primitives both share --
conflicts, properties, authority ranking, canonicalization -- live in `common`.
This module publishes nothing, binds no tenant, and validates no instance data
against the schemas it describes.

Provenance of the vocabulary: the concept set below was **not** validated by any
external organization. It is derived from this product's own catalog and its
ownership and interface requirements. Nothing here should be read, cited, or
extended on the belief that an outside party reviewed and accepted it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from contextplane.profile.schemas.common import (
    CORE_NAMESPACE,
    NAMESPACE_PATTERN,
    PROPERTY_NAME_PATTERN,
    TYPE_NAME_PATTERN,
    ProfileDefinitionError,
    PropertyDefinition,
    normalize_text,
    qualify,
)

# The complete set of reasons publication may refuse. Named as a closed set so a
# gate can assert it is covered by fixtures; a code that exists with no fixture
# behind it is a refusal nobody has ever seen fire.
CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "namespace_collision",
        "core_redefinition",
        "changed_value_type",
        "weakened_required",
        "weakened_cardinality",
        "weakened_authority",
        "undeclared_extension_point",
        "unnamespaced_definition",
        "incompatible_target_revision",
    }
)


# --- the entity type ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EntityTypeDefinition:
    """One entity type: its properties, where it may be extended, and when it is ready.

    `extension_points` is the whole of a tenant's permission to touch this type.
    An empty tuple -- the default -- means the type is closed, which is the safe
    direction to be wrong in: opening a point later is a compatible change, and
    closing one that tenants already extended is not.
    """

    namespace: str
    type_name: str
    properties: tuple[PropertyDefinition, ...] = ()
    # Property names an extension may add. Naming the *points* rather than
    # allowing "any new property" is what makes an unexpected property a
    # refusal instead of a silent addition to a shared type.
    extension_points: tuple[str, ...] = ()
    # Properties that must be present before an instance counts as ready.
    readiness_required: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"namespace {self.namespace!r} is lowercase alphanumeric with underscores")
        if not TYPE_NAME_PATTERN.match(self.type_name):
            raise ProfileDefinitionError(f"type name {self.type_name!r} is lowercase alphanumeric with underscores")

        seen: set[str] = set()
        for prop in self.properties:
            if prop.name in seen:
                raise ProfileDefinitionError(f"{self.qualified}: property {prop.name!r} is defined twice")
            seen.add(prop.name)

        for point in self.extension_points:
            if not PROPERTY_NAME_PATTERN.match(point):
                raise ProfileDefinitionError(f"{self.qualified}: extension point {point!r} is not a property name")
            if point in seen:
                raise ProfileDefinitionError(
                    f"{self.qualified}: {point!r} is both a core property and an extension point; a point that "
                    "names an existing property is an invitation to redefine it"
                )

        for name in self.readiness_required:
            if name not in seen:
                raise ProfileDefinitionError(
                    f"{self.qualified}: readiness requires {name!r}, which the type does not define"
                )

    @property
    def qualified(self) -> str:
        """`<namespace>:<type_name>`, this type's only unambiguous name."""
        return qualify(self.namespace, self.type_name)

    @property
    def by_name(self) -> Mapping[str, PropertyDefinition]:
        """Properties keyed by name, for the collision and weakening checks."""
        return {prop.name: prop for prop in self.properties}

    def canonical(self) -> dict[str, Any]:
        """This type reduced to plain data, with every sequence in sorted order."""
        return {
            "namespace": normalize_text(self.namespace),
            "type_name": normalize_text(self.type_name),
            "properties": [prop.canonical() for prop in sorted(self.properties, key=lambda p: p.name)],
            "extension_points": sorted(normalize_text(point) for point in self.extension_points),
            "readiness_required": sorted(normalize_text(name) for name in self.readiness_required),
        }


# --- the ownership lifecycle --------------------------------------------------

OwnershipState = Literal["draft", "proposed", "validated", "superseded", "revoked"]

OWNERSHIP_STATES: tuple[OwnershipState, ...] = ("draft", "proposed", "validated", "superseded", "revoked")

# Terminal in the sense that nothing follows them. `superseded` points at a
# replacement and `revoked` records a loss of standing; both stay readable
# forever, which is the point of ending here rather than deleting the row.
OWNERSHIP_TERMINAL_STATES: frozenset[str] = frozenset({"superseded", "revoked"})

# The only legal moves. Absence from this table is a refusal, not an unknown.
OWNERSHIP_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "draft": frozenset({"proposed", "revoked"}),
    "proposed": frozenset({"validated", "revoked"}),
    "validated": frozenset({"superseded", "revoked"}),
    "superseded": frozenset(),
    "revoked": frozenset(),
}

# An assignment nobody asserted by hand starts here: it has been computed, not
# agreed, and entering as `draft` would let it look like somebody's unfinished
# work rather than a machine's proposal awaiting an owner.
DERIVED_INITIAL_STATE: OwnershipState = "proposed"


class OwnershipLifecycleError(ValueError):
    """An ownership assignment was asked to make a move it does not have."""


def assert_ownership_transition(
    from_state: str,
    to_state: str,
    *,
    validated_by_subject_owner: bool = False,
) -> None:
    """Refuse an illegal ownership move, and say what was legal instead.

    `validated_by_subject_owner` is resolved by the caller from authenticated
    identity, never taken from a request body. Validation is the transition that
    turns a proposal into the platform's answer to "who owns this", so the one
    party whose agreement it represents has to be the one who gave it.
    """
    if from_state not in OWNERSHIP_TRANSITIONS:
        raise OwnershipLifecycleError(f"unknown ownership state {from_state!r}; the five are {list(OWNERSHIP_STATES)}")
    if to_state not in OWNERSHIP_TRANSITIONS:
        raise OwnershipLifecycleError(f"unknown ownership state {to_state!r}; the five are {list(OWNERSHIP_STATES)}")

    if from_state in OWNERSHIP_TERMINAL_STATES:
        raise OwnershipLifecycleError(
            f"{from_state!r} is terminal, so it does not move to {to_state!r}; a superseded or revoked assignment "
            "stays readable as what it was, and reviving it in place would erase the record of the change"
        )

    allowed = OWNERSHIP_TRANSITIONS[from_state]
    if to_state not in allowed:
        raise OwnershipLifecycleError(
            f"ownership does not move {from_state!r} -> {to_state!r}; from {from_state!r} the legal moves are "
            f"{sorted(allowed)}"
        )

    if to_state == "validated" and not validated_by_subject_owner:
        raise OwnershipLifecycleError(
            "only the subject owner validates an ownership assignment; validation by anyone else would record "
            "their agreement as the owner's, which is the one thing this state is read as meaning"
        )


# --- the closed core vocabulary -----------------------------------------------

# The core entity types, frozen. Derived from this product's own catalog spine
# and from the ownership and interface requirements it has to satisfy -- not
# from any external organization's vocabulary, and not reviewed by one.
CORE_ENTITY_DEFINITIONS: tuple[EntityTypeDefinition, ...] = (
    EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="capability",
        properties=(
            PropertyDefinition(name="name", value_type="string", required=True, min_cardinality=1, max_cardinality=1),
            PropertyDefinition(name="description", value_type="string"),
            PropertyDefinition(
                name="lifecycle_state",
                value_type="enum",
                required=True,
                min_cardinality=1,
                max_cardinality=1,
                enum_values=("proposed", "active", "deprecated", "retired"),
            ),
        ),
        extension_points=("classification", "external_reference"),
        readiness_required=("name", "lifecycle_state"),
    ),
    EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="component",
        properties=(
            PropertyDefinition(name="name", value_type="string", required=True, min_cardinality=1, max_cardinality=1),
            PropertyDefinition(name="description", value_type="string"),
        ),
        extension_points=("classification", "external_reference"),
        readiness_required=("name",),
    ),
    EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="interface",
        properties=(
            PropertyDefinition(name="name", value_type="string", required=True, min_cardinality=1, max_cardinality=1),
            PropertyDefinition(name="description", value_type="string"),
        ),
        extension_points=("classification",),
        readiness_required=("name",),
    ),
    EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="interface_version",
        properties=(
            PropertyDefinition(
                name="version", value_type="string", required=True, min_cardinality=1, max_cardinality=1
            ),
            PropertyDefinition(
                name="lifecycle_state",
                value_type="enum",
                required=True,
                min_cardinality=1,
                max_cardinality=1,
                enum_values=("draft", "published", "deprecated", "retired"),
            ),
        ),
        extension_points=("classification",),
        readiness_required=("version", "lifecycle_state"),
    ),
    EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="ownership_assignment",
        properties=(
            PropertyDefinition(
                name="role",
                value_type="enum",
                required=True,
                min_cardinality=1,
                max_cardinality=1,
                enum_values=("accountable_owner", "maintainer", "steward"),
            ),
            PropertyDefinition(name="scope", value_type="string", required=True, min_cardinality=1, max_cardinality=1),
            PropertyDefinition(
                name="validation_state",
                value_type="enum",
                required=True,
                min_cardinality=1,
                max_cardinality=1,
                enum_values=OWNERSHIP_STATES,
                # The state an assignment is in is the platform's conclusion
                # about who agreed to what, so it is not an observed value.
                authority="canonical_owner",
            ),
            PropertyDefinition(name="derivation_method", value_type="string"),
            # Only meaningful on a derived assignment, mirroring the rule that a
            # confidence score on a directly-asserted value quantifies nothing.
            PropertyDefinition(name="confidence", value_type="string"),
        ),
        # Closed. Ownership is the type whose meaning a tenant most wants to
        # adjust and least may: a locally-added field here would change what
        # "validated" is taken to mean, which is exactly the shared guarantee.
        extension_points=(),
        readiness_required=("role", "scope", "validation_state"),
    ),
)

CORE_TYPES_BY_QUALIFIED: Mapping[str, EntityTypeDefinition] = {
    definition.qualified: definition for definition in CORE_ENTITY_DEFINITIONS
}

CORE_TYPE_NAMES: frozenset[str] = frozenset(definition.type_name for definition in CORE_ENTITY_DEFINITIONS)


def _assert_core_is_well_formed(definitions: Iterable[EntityTypeDefinition]) -> None:
    """Refuse a core that contradicts itself, at import rather than at publication."""
    seen: set[str] = set()
    for definition in definitions:
        if definition.namespace != CORE_NAMESPACE:
            raise ProfileDefinitionError(
                f"core definition {definition.qualified} is not in the {CORE_NAMESPACE!r} namespace"
            )
        if definition.qualified in seen:
            raise ProfileDefinitionError(f"core defines {definition.qualified} twice")
        seen.add(definition.qualified)


_assert_core_is_well_formed(CORE_ENTITY_DEFINITIONS)


__all__ = [
    "CONFLICT_CODES",
    "CORE_ENTITY_DEFINITIONS",
    "CORE_TYPES_BY_QUALIFIED",
    "CORE_TYPE_NAMES",
    "DERIVED_INITIAL_STATE",
    "OWNERSHIP_STATES",
    "OWNERSHIP_TERMINAL_STATES",
    "OWNERSHIP_TRANSITIONS",
    "EntityTypeDefinition",
    "OwnershipLifecycleError",
    "OwnershipState",
    "assert_ownership_transition",
]
