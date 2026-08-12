"""Compile the three profile-schema families into one profile, or refuse it.

Each family already freezes its own vocabulary, composes its own extensions and
digests its own definitions. What none of them can do is look at another: a
relationship holds its endpoints as qualified *names*, an interface holds its
type context as a qualified *name*, and a name resolves against a vocabulary no
single family owns. Every check here is one that becomes possible only once all
three sets are in the same room, and nothing here re-decides a question a family
has already answered about its own members.

Three properties carry the weight.

**The profile digest is per family, combined under fixed keys — never one list.**
`canonical_document()` orders by qualified name alone, so flattening the three
families into a single list makes the digest depend on input order exactly when
two families share a qualified name. That is the collision this module exists to
detect, so the naive combination would be order-dependent precisely where it
matters most, and it would still pass a repeat-compile assertion. Each family is
canonicalized on its own and the three documents are combined under the keys
below, which is order-independent in the collision case as well.

**Canonicalization is borrowed, not rebuilt.** Every definition reduces through
the one `canonical()`/`canonical_document()` path the families share. A second
canonicalizer is how one document acquires two digests, which is the failure a
determinism gate is least able to tolerate. The only thing computed here is the
hash of the combined document, and it names its algorithm from the same shared
constant the family digests use so the two cannot drift apart.

**Interfaces and interface versions may legitimately share a qualified name.**
Their canonical forms carry a `kind` discriminator that keeps them apart, so the
collision check treats the interface family as one namespace and never reports a
version colliding with its own interface.

The vocabulary compiled here was not validated by any external organization. It
is derived from this product's own catalog, ownership and interface
requirements.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Self

from contextplane.profile.schemas.common import (
    CORE_DIGEST_ALGORITHM,
    Conflict,
    ProfileCompositionError,
    canonical_document,
    definition_digest,
)
from contextplane.profile.schemas.entity import CORE_ENTITY_DEFINITIONS, EntityTypeDefinition
from contextplane.profile.schemas.entity_composition import ComposedProfile
from contextplane.profile.schemas.interface import (
    CORE_INTERFACE_DEFINITIONS,
    CORE_INTERFACE_VERSIONS,
    ComposedInterfaces,
    InterfaceFamilyDefinition,
    InterfaceVersionDefinition,
)
from contextplane.profile.schemas.relationship import CORE_RELATIONSHIP_DEFINITIONS, RelationshipTypeDefinition
from contextplane.profile.schemas.relationship_composition import ComposedRelationships

# --- the families -------------------------------------------------------------

ENTITY_FAMILY = "entity"
INTERFACE_FAMILY = "interface"
RELATIONSHIP_FAMILY = "relationship"

#: The keys the combined document is written under. Fixed rather than derived
#: from whatever was passed, so a family arriving empty still occupies its key
#: and the document keeps the same shape. A family silently missing from the
#: document would change the digest without changing any definition.
FAMILY_KEYS: tuple[str, ...] = (ENTITY_FAMILY, INTERFACE_FAMILY, RELATIONSHIP_FAMILY)

#: Recorded alongside every compile, because the same inputs through a different
#: compiler may legitimately produce a different output digest. Without the
#: version, that difference cannot be told apart from corruption.
COMPILER_VERSION = "profile-compiler-1"


# --- what a compile refuses -----------------------------------------------------

#: The closed set of reasons a compile is refused. Every code here is a
#: cross-family question: a family that could answer it alone answers it in its
#: own composition instead, and repeating the refusal here would give one rule
#: two places to drift.
COMPILER_CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "cross_family_collision",
        "unknown_entity_endpoint",
        "unresolved_type_context",
        "retired_reference",
    }
)


# --- canonicalization -----------------------------------------------------------


def canonical_profile_document(
    entities: Sequence[EntityTypeDefinition],
    relationships: Sequence[RelationshipTypeDefinition],
    interfaces: Sequence[InterfaceFamilyDefinition],
) -> str:
    """The one byte-sequence a three-family profile reduces to.

    Each family reduces through the shared canonicalizer on its own, and the
    three results are combined under fixed keys. Combining the *definitions*
    instead would order them by qualified name across family boundaries, where
    two families sharing a name have no defined order between them -- so the
    document would depend on which family the caller happened to list first.
    """
    return json.dumps(
        {
            ENTITY_FAMILY: canonical_document(entities),
            INTERFACE_FAMILY: canonical_document(interfaces),
            RELATIONSHIP_FAMILY: canonical_document(relationships),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def profile_digest(
    entities: Sequence[EntityTypeDefinition],
    relationships: Sequence[RelationshipTypeDefinition],
    interfaces: Sequence[InterfaceFamilyDefinition],
) -> str:
    """Hash over the combined canonical document, as lowercase hex.

    Named from the same algorithm constant the per-family digests use: a profile
    digest and the family digests it is built over must be the same kind of
    number, and two literals would let one of them be changed alone.
    """
    document = canonical_profile_document(entities, relationships, interfaces)
    return hashlib.new(CORE_DIGEST_ALGORITHM, document.encode("utf-8")).hexdigest()


# --- the compiled profile --------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CompiledProfile:
    """A profile that compiled, its inputs' digests, and its own.

    `input_digests` is kept per family rather than collapsed into the output
    digest alone. A compile that produces an unexpected output has to be
    attributable to an input that moved, and one combined number cannot say
    which of the three it was.
    """

    entities: tuple[EntityTypeDefinition, ...]
    relationships: tuple[RelationshipTypeDefinition, ...]
    interfaces: tuple[InterfaceFamilyDefinition, ...]
    input_digests: Mapping[str, str]
    output_digest: str
    compiler_version: str = COMPILER_VERSION

    @classmethod
    def of(
        cls,
        entities: Sequence[EntityTypeDefinition],
        relationships: Sequence[RelationshipTypeDefinition],
        interfaces: Sequence[InterfaceFamilyDefinition],
    ) -> Self:
        """Order each family canonically and digest it, in one step.

        One step rather than two so no caller can order without digesting, or
        digest something other than what it ordered.
        """
        ordered_entities = tuple(sorted(entities, key=_by_qualified))
        ordered_relationships = tuple(sorted(relationships, key=_by_qualified))
        ordered_interfaces = tuple(sorted(interfaces, key=_by_qualified))
        return cls(
            entities=ordered_entities,
            relationships=ordered_relationships,
            interfaces=ordered_interfaces,
            input_digests={
                ENTITY_FAMILY: definition_digest(ordered_entities),
                INTERFACE_FAMILY: definition_digest(ordered_interfaces),
                RELATIONSHIP_FAMILY: definition_digest(ordered_relationships),
            },
            output_digest=profile_digest(ordered_entities, ordered_relationships, ordered_interfaces),
        )

    @property
    def document(self) -> str:
        """The canonical document this profile's output digest was taken over."""
        return canonical_profile_document(self.entities, self.relationships, self.interfaces)


def _by_qualified(definition: EntityTypeDefinition | RelationshipTypeDefinition | InterfaceFamilyDefinition) -> str:
    return definition.qualified


# --- the cross-family checks ------------------------------------------------------


def _collision_conflicts(
    entities: Sequence[EntityTypeDefinition],
    relationships: Sequence[RelationshipTypeDefinition],
    interfaces: Sequence[InterfaceFamilyDefinition],
) -> list[Conflict]:
    """Qualified names claimed by more than one family.

    Within the interface family an interface and one of its versions may share a
    name -- their canonical forms carry a `kind`, so they never collapse -- which
    is why that family contributes a *set* of names here rather than a list. The
    collision this reports is between families, where no discriminator exists and
    a single qualified name would mean two different things to two readers.
    """
    by_family: Mapping[str, set[str]] = {
        ENTITY_FAMILY: {definition.qualified for definition in entities},
        INTERFACE_FAMILY: {definition.qualified for definition in interfaces},
        RELATIONSHIP_FAMILY: {definition.qualified for definition in relationships},
    }
    conflicts: list[Conflict] = []
    for qualified in sorted(set().union(*by_family.values())):
        claimed = sorted(family for family in FAMILY_KEYS if qualified in by_family[family])
        if len(claimed) > 1:
            conflicts.append(
                Conflict(
                    code="cross_family_collision",
                    qualified_type=qualified,
                    property_name=None,
                    detail=(
                        f"{qualified!r} is defined by more than one family ({', '.join(claimed)}); across families "
                        "nothing distinguishes them, so one name would resolve to two definitions and every "
                        "reference to it would be answered by whichever was looked up first"
                    ),
                )
            )
    return conflicts


def _endpoint_conflicts(
    relationships: Sequence[RelationshipTypeDefinition],
    entity_names: frozenset[str],
) -> list[Conflict]:
    """Relationship endpoints naming no entity type in the compiled profile.

    The relationship family checks its endpoints against whatever vocabulary its
    caller hands it, defaulting to the core. Only here is the vocabulary the
    profile actually compiled, so an endpoint pointing at a tenant type that the
    entity family never accepted is caught at the one place both sets exist.
    """
    conflicts: list[Conflict] = []
    for definition in sorted(relationships, key=_by_qualified):
        for role, endpoint in (("source", definition.source_type), ("destination", definition.destination_type)):
            if endpoint not in entity_names:
                conflicts.append(
                    Conflict(
                        code="unknown_entity_endpoint",
                        qualified_type=definition.qualified,
                        property_name=None,
                        detail=(
                            f"{role} endpoint {endpoint!r} names no entity type in this profile; an edge whose "
                            "endpoint type does not exist can never be validated, and the row would be written "
                            "against a type nothing else in the profile agrees about"
                        ),
                    )
                )
    return conflicts


def _type_context_conflicts(
    interfaces: Sequence[InterfaceFamilyDefinition],
    entity_names: frozenset[str],
) -> list[Conflict]:
    """Interfaces whose `applies_to_type` names no entity type in the profile.

    An interface states what it applies to as a qualified name. Unresolved, the
    contract describes a shape belonging to nothing, and a compatibility verdict
    that cannot say which pair it compared has compared nothing.
    """
    conflicts: list[Conflict] = []
    for definition in sorted(interfaces, key=_by_qualified):
        if isinstance(definition, InterfaceVersionDefinition):
            continue
        if definition.applies_to_type not in entity_names:
            conflicts.append(
                Conflict(
                    code="unresolved_type_context",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"applies_to_type {definition.applies_to_type!r} names no entity type in this profile; an "
                        "interface whose type context does not resolve cannot be compared against anything"
                    ),
                )
            )
    return conflicts


def _retired_reference_conflicts(interfaces: Sequence[InterfaceFamilyDefinition]) -> list[Conflict]:
    """Versions whose `version_of` resolves to a retired interface version.

    Nothing else in this vocabulary carries a lifecycle. Entity and relationship
    definitions have no lifecycle field at all, and the only `retired` state a
    definition can be in is an interface version's -- so a reference to something
    retired is, in this profile, a reference landing on a retired version.

    `version_of` is the only site whose target is an interface-family definition;
    endpoints and type contexts name entity types and are unresolved rather than
    retired when they miss. Resolution is over the whole compiled interface
    family, which is why this is a compile question: within one extension
    document the target may not be present at all.
    """
    retired = {
        definition.qualified
        for definition in interfaces
        if isinstance(definition, InterfaceVersionDefinition) and definition.lifecycle_state == "retired"
    }
    conflicts: list[Conflict] = []
    for definition in sorted(interfaces, key=_by_qualified):
        if not isinstance(definition, InterfaceVersionDefinition):
            continue
        if definition.version_of in retired:
            conflicts.append(
                Conflict(
                    code="retired_reference",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"version_of {definition.version_of!r} resolves to a retired interface version; a contract "
                        "built on one that has been withdrawn inherits a promise nobody is still keeping"
                    ),
                )
            )
    return conflicts


# --- the compile ------------------------------------------------------------------


def compile_profile(
    *,
    entities: Sequence[EntityTypeDefinition] = CORE_ENTITY_DEFINITIONS,
    relationships: Sequence[RelationshipTypeDefinition] = CORE_RELATIONSHIP_DEFINITIONS,
    interfaces: Sequence[InterfaceFamilyDefinition] = (*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS),
) -> CompiledProfile:
    """Compile the three families into one profile, or refuse with every conflict.

    Deterministic: the same three definition sets produce the same definitions,
    in the same order, with the same digests, on any machine and on any repeat.

    Conflicts come back collected and ordered rather than one at a time, because
    an author who fixes them one round-trip apiece only ever sees the shape of
    the last one.
    """
    entity_names = frozenset(definition.qualified for definition in entities)

    conflicts: list[Conflict] = [
        *_collision_conflicts(entities, relationships, interfaces),
        *_endpoint_conflicts(relationships, entity_names),
        *_type_context_conflicts(interfaces, entity_names),
        *_retired_reference_conflicts(interfaces),
    ]

    if conflicts:
        raise ProfileCompositionError(
            sorted(conflicts, key=lambda c: (c.qualified_type, c.code, c.property_name or ""))
        )

    return CompiledProfile.of(entities, relationships, interfaces)


def compile_composed(
    entities: ComposedProfile,
    relationships: ComposedRelationships,
    interfaces: ComposedInterfaces,
) -> CompiledProfile:
    """Compile what the three family compositions produced.

    The ordinary path: each family composes its own extension and refuses its own
    conflicts first, and what survives arrives here for the questions that need
    all three. Written as an adapter rather than as a second compile so there is
    one implementation of the cross-family rules, not one per entry point.
    """
    return compile_profile(
        entities=entities.definitions,
        relationships=relationships.definitions,
        interfaces=interfaces.definitions,
    )


__all__ = [
    "COMPILER_CONFLICT_CODES",
    "COMPILER_VERSION",
    "ENTITY_FAMILY",
    "FAMILY_KEYS",
    "INTERFACE_FAMILY",
    "RELATIONSHIP_FAMILY",
    "CompiledProfile",
    "canonical_profile_document",
    "compile_composed",
    "compile_profile",
    "profile_digest",
]
