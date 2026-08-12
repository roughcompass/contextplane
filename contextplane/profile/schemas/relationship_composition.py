"""Composing a tenant's relationship extension onto the frozen core.

Split from the definitions module rather than living beside them, because the two
answer different questions and only one of them is a policy decision. The
definitions module says what a relationship type *is*; this one decides whether
two documents may be merged and what to refuse when they may not. Keeping the
refusal vocabulary here means a change to what is forbidden touches one file, and
a change to what a relationship can express touches the other.

The rules themselves are argued at their definition sites below. Two shape the
whole module:

**Refusals come back collected and ordered.** An author who fixes them one
round-trip apiece only ever sees the shape of the last one, and a composition
that reported the first failure would make a document with five problems take
five publications to diagnose.

**Weakening has to be expressible before it can be refused.** A tenant may
restate a core relationship to *narrow* it -- demand higher authority, a tighter
maximum, denial where the core grants -- because holding yourself to more than the
shared guarantee breaks no reader of it. That path exists so the weakening rules
have something to fire on: without it the only way to change a relationship's
shape would be to publish into the core namespace, which is refused by a guard
that runs first, leaving every weakening rule below as unreachable code behind
it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Self

from contextplane.profile.schemas.common import (
    AUTHORITY_RANK,
    CORE_NAMESPACE,
    NAMESPACE_PATTERN,
    Conflict,
    ProfileCompositionError,
    ProfileDefinitionError,
    PropertyDefinition,
    qualify,
)
from contextplane.profile.schemas.entity import CORE_TYPES_BY_QUALIFIED
from contextplane.profile.schemas.relationship import (
    CORE_RELATIONSHIP_DEFINITIONS,
    RelationshipTypeDefinition,
    relationship_digest,
)


@dataclasses.dataclass(frozen=True)
class RelationshipExtensionDocument:
    """A tenant's relationship additions, and the core revision they target.

    The target digest is declared rather than inferred from whatever is current,
    so an extension written against an older core is refused instead of silently
    re-interpreted against one it has never been checked against.
    """

    namespace: str
    target_core_digest: str
    definitions: tuple[RelationshipTypeDefinition, ...] = ()
    #: Additions to existing core relationships, keyed by qualified name. Only
    #: properties may be added; every other dimension is the core's to state.
    added_properties: Mapping[str, tuple[PropertyDefinition, ...]] = dataclasses.field(default_factory=dict)
    #: Core relationships this tenant re-declares for itself, keyed by qualified
    #: core name.
    #:
    #: This is the one legitimate way to change a core relationship's shape, and
    #: it only ever goes one direction: a restatement may *narrow* -- demand
    #: higher authority, a tighter maximum, a higher minimum, denial where the
    #: core grants -- because a tenant holding itself to more than the shared
    #: guarantee breaks no reader of that guarantee. Weakening is refused.
    #:
    #: Without this, weakening could only be expressed by publishing a definition
    #: into the core namespace, which is refused before the shape is ever
    #: compared -- so every weakening rule below would be unreachable code sitting
    #: behind an earlier guard, and no fixture could make one fire on its own.
    restatements: Mapping[str, RelationshipTypeDefinition] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"extension namespace {self.namespace!r} is not a legal namespace")
        if self.namespace == CORE_NAMESPACE:
            raise ProfileDefinitionError(
                f"an extension does not publish into the {CORE_NAMESPACE!r} namespace; that is the whole of the "
                "distinction between extending the vocabulary and editing it"
            )


@dataclasses.dataclass(frozen=True)
class ComposedRelationships:
    """The result of a successful composition, with its own digest."""

    definitions: tuple[RelationshipTypeDefinition, ...]
    digest: str

    @classmethod
    def of(cls, definitions: Sequence[RelationshipTypeDefinition]) -> Self:
        """Order the definitions canonically and digest them in one step."""
        ordered = tuple(sorted(definitions, key=lambda definition: definition.qualified))
        return cls(definitions=ordered, digest=relationship_digest(ordered))


def _endpoint_conflicts(core: RelationshipTypeDefinition, added: RelationshipTypeDefinition) -> list[Conflict]:
    """Endpoint changes an extension may not make.

    Repointing either end is refused outright rather than compared for breadth.
    This vocabulary has no subtype lattice, so "narrower" is not a question it can
    answer -- and a check that guessed would let a repoint through whenever the
    guess said narrower. An extension that needs a different endpoint is
    describing a different relationship and may define one under its own
    namespace.
    """
    conflicts: list[Conflict] = []
    for role, core_endpoint, added_endpoint in (
        ("source", core.source_type, added.source_type),
        ("destination", core.destination_type, added.destination_type),
    ):
        if core_endpoint != added_endpoint:
            conflicts.append(
                Conflict(
                    code="weakened_endpoint",
                    qualified_type=core.qualified,
                    property_name=None,
                    detail=(
                        f"core declares the {role} endpoint as {core_endpoint!r}, extension declares "
                        f"{added_endpoint!r}; every traversal written against this relationship expects the core "
                        "endpoint, and repointing it changes what they return"
                    ),
                )
            )
    return conflicts


def _shape_conflicts(core: RelationshipTypeDefinition, added: RelationshipTypeDefinition) -> list[Conflict]:
    """Dimensions an extension may not restate differently from the core."""
    conflicts: list[Conflict] = []

    if added.direction != core.direction:
        conflicts.append(
            Conflict(
                code="changed_direction",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core declares {core.direction!r}, extension declares {added.direction!r}; direction decides "
                    "what a stored edge means, so changing it reinterprets every edge already written"
                ),
            )
        )
    if added.symmetry != core.symmetry:
        conflicts.append(
            Conflict(
                code="changed_symmetry",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core declares {core.symmetry!r}, extension declares {added.symmetry!r}; symmetry is a property "
                    "of the type rather than of any edge, so it cannot differ per tenant"
                ),
            )
        )
    if added.cardinality_scope != core.cardinality_scope:
        conflicts.append(
            Conflict(
                code="changed_cardinality_scope",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core counts {core.cardinality_scope!r}, extension counts {added.cardinality_scope!r}; the same "
                    "bound over a different scope is a different rule wearing the same number"
                ),
            )
        )
    if core.duplicate_policy == "reject" and added.duplicate_policy == "allow":
        conflicts.append(
            Conflict(
                code="weakened_duplicate_policy",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    "core rejects duplicate edges and the extension admits them; a reader counting edges would "
                    "start counting restatements of one fact"
                ),
            )
        )
    if core.cross_org_policy == "deny" and added.cross_org_policy == "allow_with_grant":
        conflicts.append(
            Conflict(
                code="weakened_cross_org_policy",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    "core denies cross-organization access to this relationship and the extension opens it; the "
                    "organizations whose graph becomes readable are not the ones asking"
                ),
            )
        )
    if core.inverse_view == "read_only" and added.inverse_view == "independently_asserted":
        conflicts.append(
            Conflict(
                code="weakened_inverse_view",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    "core derives the inverse as a read-only view and the extension makes it independently "
                    "assertable; that admits a second stored edge for a fact the first already carries, and the two "
                    "disagree as soon as one is written without the other"
                ),
            )
        )
    if AUTHORITY_RANK[added.authority] < AUTHORITY_RANK[core.authority]:
        conflicts.append(
            Conflict(
                code="weakened_authority",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core requires {core.authority!r} authority to assert this relationship, extension accepts "
                    f"{added.authority!r}; lowering the bar changes what a stored edge means, not merely who writes it"
                ),
            )
        )
    if added.min_cardinality < core.min_cardinality:
        conflicts.append(
            Conflict(
                code="weakened_cardinality",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core requires at least {core.min_cardinality} per {core.cardinality_scope}, extension allows "
                    f"{added.min_cardinality}"
                ),
            )
        )
    if core.max_cardinality is not None and (
        added.max_cardinality is None or added.max_cardinality > core.max_cardinality
    ):
        allowed = "unbounded" if added.max_cardinality is None else str(added.max_cardinality)
        conflicts.append(
            Conflict(
                code="weakened_cardinality",
                qualified_type=core.qualified,
                property_name=None,
                detail=(
                    f"core allows at most {core.max_cardinality} per {core.cardinality_scope}, extension allows "
                    f"{allowed}; a reader written for one edge would silently take the first of several"
                ),
            )
        )
    return conflicts


def _added_property_conflicts(
    core: RelationshipTypeDefinition,
    added: Sequence[PropertyDefinition],
) -> list[Conflict]:
    """Properties an extension adds to a core relationship it did not define."""
    conflicts: list[Conflict] = []
    existing = core.by_name
    points = set(core.extension_points)
    for prop in added:
        if prop.name in existing:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=core.qualified,
                    property_name=prop.name,
                    detail=(
                        "the core already defines this property; an extension adds properties and does not restate "
                        "the ones the core guarantees"
                    ),
                )
            )
        elif prop.name not in points:
            conflicts.append(
                Conflict(
                    code="undeclared_extension_point",
                    qualified_type=core.qualified,
                    property_name=prop.name,
                    detail=(
                        f"{core.qualified} declares extension points {sorted(points)}; adding elsewhere makes an "
                        "unexpected property a silent addition to a shared type rather than a refusal"
                    ),
                )
            )
    return conflicts


def compose(
    extension: RelationshipExtensionDocument,
    *,
    core: Sequence[RelationshipTypeDefinition] = CORE_RELATIONSHIP_DEFINITIONS,
    known_types: Mapping[str, Any] | None = None,
) -> ComposedRelationships:
    """Compose a relationship extension onto the core, or refuse with every conflict.

    Deterministic: the same core and extension produce the same definitions and
    the same digest, in the same order, on any machine. Conflicts come back
    collected and ordered rather than one at a time, because an author who fixes
    them one round-trip apiece only ever sees the shape of the last.
    """
    core_by_qualified = {definition.qualified: definition for definition in core}
    entity_types = CORE_TYPES_BY_QUALIFIED if known_types is None else known_types
    conflicts: list[Conflict] = []

    expected_digest = relationship_digest(core)
    if extension.target_core_digest != expected_digest:
        conflicts.append(
            Conflict(
                code="incompatible_target_revision",
                qualified_type="",
                property_name=None,
                detail=(
                    f"extension targets core digest {extension.target_core_digest!r}, current core is "
                    f"{expected_digest!r}; composing anyway would check it against a core it was never written for"
                ),
            )
        )

    # Endpoint types an extension defines itself are legal targets alongside the
    # core's, so they are gathered before any endpoint is resolved.
    extension_type_names = {qualify(definition.namespace, definition.type_name) for definition in extension.definitions}

    composed: list[RelationshipTypeDefinition] = list(core)
    for definition in extension.definitions:
        if definition.namespace == CORE_NAMESPACE:
            conflicts.append(
                Conflict(
                    code="unnamespaced_definition",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"an extension publishes into its own namespace, not {CORE_NAMESPACE!r}; a definition in the "
                        "core namespace is an edit to the shared vocabulary wearing an addition's clothes"
                    ),
                )
            )
            continue
        if definition.namespace != extension.namespace:
            conflicts.append(
                Conflict(
                    code="namespace_collision",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"extension publishes as {extension.namespace!r} but this definition claims "
                        f"{definition.namespace!r}; a document that writes into two namespaces can place a "
                        "definition somewhere its author is not accountable for"
                    ),
                )
            )
            continue
        if definition.qualified in core_by_qualified:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail="the core already defines this relationship; redefining it is an edit, not an extension",
                )
            )
            continue

        for role, endpoint in (("source", definition.source_type), ("destination", definition.destination_type)):
            if endpoint not in entity_types and endpoint not in extension_type_names:
                conflicts.append(
                    Conflict(
                        code="unknown_endpoint_type",
                        qualified_type=definition.qualified,
                        property_name=None,
                        detail=(
                            f"{role} endpoint {endpoint!r} names no type in the core vocabulary or in this "
                            "extension; an edge whose endpoint type does not exist can never be validated"
                        ),
                    )
                )
        composed.append(definition)

    for qualified, added in sorted(extension.added_properties.items()):
        core_definition = core_by_qualified.get(qualified)
        if core_definition is None:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=qualified,
                    property_name=None,
                    detail=(
                        "properties were added to a relationship the core does not define; there is nothing here to "
                        "extend, and the addition would silently create the type"
                    ),
                )
            )
            continue
        conflicts.extend(_added_property_conflicts(core_definition, added))

    # Restatements are the weakening surface. Every dimension the tenant
    # re-declares is compared against what the core guarantees; a narrowing
    # passes and replaces the core definition for this profile, a weakening
    # refuses. Sorted so the conflict order does not depend on dict insertion.
    narrowed: dict[str, RelationshipTypeDefinition] = {}
    for qualified, restated in sorted(extension.restatements.items()):
        core_definition = core_by_qualified.get(qualified)
        if core_definition is None:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=qualified,
                    property_name=None,
                    detail=(
                        "a relationship the core does not define was restated; there is nothing here to narrow, and "
                        "the restatement would silently create the type under the core's name"
                    ),
                )
            )
            continue
        if restated.qualified != qualified:
            conflicts.append(
                Conflict(
                    code="namespace_collision",
                    qualified_type=qualified,
                    property_name=None,
                    detail=(
                        f"the restatement is filed under {qualified!r} but names itself {restated.qualified!r}; a "
                        "definition reachable by two names is one a reader resolves differently by route"
                    ),
                )
            )
            continue
        found = _endpoint_conflicts(core_definition, restated) + _shape_conflicts(core_definition, restated)
        conflicts.extend(found)
        if not found:
            narrowed[qualified] = restated

    composed = [narrowed.get(definition.qualified, definition) for definition in composed]

    if conflicts:
        ordered = sorted(conflicts, key=lambda c: (c.qualified_type, c.code, c.property_name or ""))
        raise ProfileCompositionError(ordered)

    return ComposedRelationships.of(composed)


__all__ = [
    "ComposedRelationships",
    "RelationshipExtensionDocument",
    "compose",
]
