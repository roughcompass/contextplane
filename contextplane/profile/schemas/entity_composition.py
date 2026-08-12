"""Composing a tenant's entity extension onto the frozen core.

Split from the definitions module so the family is shaped like its siblings: one
module says what an entity type *is*, this one decides whether two documents may
be merged and what to refuse when they may not. A change to what is forbidden
touches one file and a change to what an entity can express touches the other.

Composition is additive or it fails -- no first-wins, no last-wins, no implicit
override, since a merged-with-a-warning result is indistinguishable from one
somebody designed on purpose. Refusals come back collected and ordered, because
an author who fixes them one round-trip apiece only ever sees the shape of the
last. Weakening is named separately from collision for the same reason it is
tempting: dropping a required property, widening a maximum, or lowering a minimum
costs the extension asking nothing, and costs every reader relying on the core
guarantee, none of whom are in the room.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Self

from contextplane.profile.schemas.common import (
    AUTHORITY_RANK,
    CORE_NAMESPACE,
    NAMESPACE_PATTERN,
    Conflict,
    ProfileCompositionError,
    ProfileDefinitionError,
    PropertyDefinition,
    definition_digest,
)
from contextplane.profile.schemas.entity import (
    CORE_ENTITY_DEFINITIONS,
    CORE_TYPE_NAMES,
    EntityTypeDefinition,
)


@dataclasses.dataclass(frozen=True)
class ExtensionDocument:
    """A tenant's additions, and the core revision they were written against.

    The target revision is declared rather than inferred from whatever is
    current, so an extension composed against an older core is refused instead
    of silently re-interpreted against a core it has never been checked against.
    """

    namespace: str
    target_core_digest: str
    definitions: tuple[EntityTypeDefinition, ...] = ()
    # Additions to existing core types, keyed by qualified core type name.
    added_properties: Mapping[str, tuple[PropertyDefinition, ...]] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"extension namespace {self.namespace!r} is not a legal namespace")
        if self.namespace == CORE_NAMESPACE:
            raise ProfileDefinitionError(
                f"an extension does not publish into the {CORE_NAMESPACE!r} namespace; that is the whole of the "
                "distinction between extending the vocabulary and editing it"
            )


@dataclasses.dataclass(frozen=True)
class ComposedProfile:
    """The result of a successful composition, with its own digest."""

    definitions: tuple[EntityTypeDefinition, ...]
    digest: str

    @classmethod
    def of(cls, definitions: Sequence[EntityTypeDefinition]) -> Self:
        """Order the definitions canonically and digest them in one step."""
        ordered = tuple(sorted(definitions, key=lambda definition: definition.qualified))
        return cls(definitions=ordered, digest=definition_digest(ordered))


def _weakening_conflicts(
    qualified: str,
    core_prop: PropertyDefinition,
    added: PropertyDefinition,
) -> list[Conflict]:
    """Every way an addition re-states a core property more weakly than the core.

    Reached only when an extension names a property the core already defines at
    a declared extension point -- which is a redefinition attempt wearing the
    clothes of an addition.
    """
    conflicts: list[Conflict] = []
    if added.value_type != core_prop.value_type:
        conflicts.append(
            Conflict(
                code="changed_value_type",
                qualified_type=qualified,
                property_name=core_prop.name,
                detail=(
                    f"core declares {core_prop.value_type!r}, extension declares {added.value_type!r}; every "
                    "reader of this property was written against the core type"
                ),
            )
        )
    if core_prop.required and not added.required:
        conflicts.append(
            Conflict(
                code="weakened_required",
                qualified_type=qualified,
                property_name=core_prop.name,
                detail=(
                    "core requires this property and the extension makes it optional; the readers relying on "
                    "the guarantee are not the ones asking to drop it"
                ),
            )
        )
    if added.min_cardinality < core_prop.min_cardinality:
        conflicts.append(
            Conflict(
                code="weakened_cardinality",
                qualified_type=qualified,
                property_name=core_prop.name,
                detail=(
                    f"core requires at least {core_prop.min_cardinality}, extension allows " f"{added.min_cardinality}"
                ),
            )
        )
    if core_prop.max_cardinality is not None and (
        added.max_cardinality is None or added.max_cardinality > core_prop.max_cardinality
    ):
        allowed = "unbounded" if added.max_cardinality is None else str(added.max_cardinality)
        conflicts.append(
            Conflict(
                code="weakened_cardinality",
                qualified_type=qualified,
                property_name=core_prop.name,
                detail=(
                    f"core allows at most {core_prop.max_cardinality}, extension allows {allowed}; a reader "
                    "written for one value would silently take the first of several"
                ),
            )
        )
    if AUTHORITY_RANK[added.authority] < AUTHORITY_RANK[core_prop.authority]:
        conflicts.append(
            Conflict(
                code="weakened_authority",
                qualified_type=qualified,
                property_name=core_prop.name,
                detail=(
                    f"core requires {core_prop.authority!r} authority, extension accepts {added.authority!r}; "
                    "lowering the bar changes what the stored value means, not merely who may write it"
                ),
            )
        )
    return conflicts


def compose(
    extension: ExtensionDocument,
    *,
    core: Sequence[EntityTypeDefinition] = CORE_ENTITY_DEFINITIONS,
) -> ComposedProfile:
    """Compose an extension onto the core, or refuse with every conflict found.

    Deterministic: the same core and extension produce the same definitions and
    the same digest, in the same order, on any machine.
    """
    core_by_qualified = {definition.qualified: definition for definition in core}
    conflicts: list[Conflict] = []

    expected_digest = definition_digest(core)
    if extension.target_core_digest != expected_digest:
        conflicts.append(
            Conflict(
                code="incompatible_target_revision",
                qualified_type=f"{extension.namespace}:*",
                property_name=None,
                detail=(
                    f"extension targets core {extension.target_core_digest!r}, this core is {expected_digest!r}; "
                    "an extension checked against one vocabulary tells you nothing about another"
                ),
            )
        )

    # New types the extension brings. Each must sit in the tenant's own
    # namespace and must not shadow a core type name.
    for definition in extension.definitions:
        if definition.namespace == CORE_NAMESPACE:
            conflicts.append(
                Conflict(
                    code="unnamespaced_definition",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail="an extension defines types in its own namespace, never the core's",
                )
            )
            continue
        if definition.namespace != extension.namespace:
            conflicts.append(
                Conflict(
                    code="unnamespaced_definition",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"definition sits in {definition.namespace!r} but the extension publishes "
                        f"{extension.namespace!r}; a tenant does not publish into another tenant's namespace"
                    ),
                )
            )
            continue
        if definition.type_name in CORE_TYPE_NAMES:
            conflicts.append(
                Conflict(
                    code="namespace_collision",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"{definition.type_name!r} is a core type name; reusing it under a tenant namespace "
                        "produces two types that read as one in every unqualified context"
                    ),
                )
            )

    # Additions onto core types. This is the path an extension is meant to take,
    # and therefore the path most of the refusals live on.
    for qualified, properties in sorted(extension.added_properties.items()):
        core_definition = core_by_qualified.get(qualified)
        if core_definition is None:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=qualified,
                    property_name=None,
                    detail="the extension adds properties to a type the core does not define",
                )
            )
            continue

        existing = core_definition.by_name
        for prop in sorted(properties, key=lambda p: p.name):
            if prop.name in existing:
                # Refused either way, but the author is told which thing they
                # did: a weakening names the constraint it loosened, since that
                # and a bare redefinition need different fixes. A restatement
                # weakening nothing is still refused -- it is a second place the
                # core's meaning lives, free to drift from the first.
                weakenings = _weakening_conflicts(qualified, existing[prop.name], prop)
                if weakenings:
                    conflicts.extend(weakenings)
                else:
                    conflicts.append(
                        Conflict(
                            code="core_redefinition",
                            qualified_type=qualified,
                            property_name=prop.name,
                            detail=(
                                "the extension re-declares a core property; core meaning is fixed by the core "
                                "revision, and a second declaration of it is a second thing to keep in step"
                            ),
                        )
                    )
                continue
            if prop.name not in core_definition.extension_points:
                conflicts.append(
                    Conflict(
                        code="undeclared_extension_point",
                        qualified_type=qualified,
                        property_name=prop.name,
                        detail=(
                            f"{qualified} declares extension points "
                            f"{sorted(core_definition.extension_points)}; adding outside them makes a shared "
                            "type mean something different for one tenant"
                        ),
                    )
                )

    if conflicts:
        raise ProfileCompositionError(
            sorted(conflicts, key=lambda c: (c.qualified_type, c.property_name or "", c.code))
        )

    composed: list[EntityTypeDefinition] = []
    for definition in core:
        additions = extension.added_properties.get(definition.qualified, ())
        if additions:
            # A filled point is no longer open. Left listed, the composed type
            # would claim a name is both a property and a place one may still be
            # added -- the contradiction this type refuses at construction, so
            # the composed profile could not be re-validated by its own producer.
            filled = {prop.name for prop in additions}
            composed.append(
                dataclasses.replace(
                    definition,
                    properties=tuple(sorted({*definition.properties, *additions}, key=lambda p: p.name)),
                    extension_points=tuple(point for point in definition.extension_points if point not in filled),
                )
            )
        else:
            composed.append(definition)
    composed.extend(extension.definitions)

    return ComposedProfile.of(composed)


__all__ = [
    "ComposedProfile",
    "ExtensionDocument",
    "compose",
]
