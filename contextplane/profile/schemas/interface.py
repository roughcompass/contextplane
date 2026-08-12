"""The closed core interface vocabulary: contracts, their versions, and what may extend them.

An interface is a promise about a shape other systems build against. That makes
this family the one where a weakening is least visible at the moment it happens
and most expensive afterwards: the consumers who relied on the guarantee are not
the ones asking to relax it, and they usually find out at runtime.

Three rules are specific to interfaces and argued at their definition sites:

**A version declares what it does to its predecessor, and cannot be silent.**
`backward_compatible`, `breaking` and `deprecating` are the three answers a
migration plan can act on. There is no "unspecified" member, because a version
whose compatibility nobody stated is one every consumer must treat as breaking
while its author believed otherwise.

**A definition names the type context it applies to.** An interface declaration
floating free of a profile revision and an entity type is unresolvable: two
tenants may both define `search`, and a compatibility comparison that cannot say
which one it compared has compared nothing. The context is required rather than
defaulted for the same reason an omitted cross-organization policy is a denial.

**Unknown fields are refused rather than ignored.** A published document is
checked once and read forever, so a field nobody recognises is either a typo
that silently did nothing or a newer schema being read by older code. Both are
worth a refusal at publication, and neither is worth discovering when a consumer
asks why its declaration had no effect.

Scope note: this defines and composes interface *definitions*. It publishes
nothing, retires nothing, and does not touch the legacy capability-interface
surface, which remains a derived compatibility view rather than truth.

Provenance of the vocabulary: the interface set below was **not** validated by
any external organization. It is derived from this product's own catalog and its
ownership and interface requirements. Nothing here should be read, cited, or
extended on the belief that an outside party reviewed and accepted it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Self

from contextplane.profile.schemas.common import (
    CORE_NAMESPACE,
    NAMESPACE_PATTERN,
    QUALIFIED_TYPE_PATTERN,
    TYPE_NAME_PATTERN,
    Conflict,
    ProfileCompositionError,
    ProfileDefinitionError,
    PropertyDefinition,
    canonical_document,
    definition_digest,
    normalize_text,
    qualify,
)
from contextplane.profile.schemas.entity import CORE_TYPES_BY_QUALIFIED

# --- the dimensions an interface version is declared over -------------------------

#: What a version does to consumers of its predecessor. Closed, and with no
#: "unspecified" member: a version whose compatibility nobody stated is one every
#: consumer must treat as breaking while its author believed otherwise.
Compatibility = Literal["backward_compatible", "breaking", "deprecating"]

COMPATIBILITIES: tuple[Compatibility, ...] = ("backward_compatible", "breaking", "deprecating")

#: Where a version sits in its own life. `deprecated` still serves; `retired`
#: does not. Retirement is gated elsewhere -- this vocabulary only records which
#: state a version claims to be in.
LifecycleState = Literal["draft", "published", "deprecated", "retired"]

LIFECYCLE_STATES: tuple[LifecycleState, ...] = ("draft", "published", "deprecated", "retired")

#: The fields a definition document may carry. Anything else is refused rather
#: than ignored, so the closed set has to be stated somewhere a reader can find.
INTERFACE_FIELDS: frozenset[str] = frozenset(
    {"namespace", "name", "applies_to_type", "profile_revision", "properties", "extension_points"}
)

INTERFACE_VERSION_FIELDS: frozenset[str] = frozenset(
    {"namespace", "name", "version", "version_of", "compatibility", "lifecycle_state", "properties"}
)

#: Every reason an interface composition may refuse. A closed set so the
#: conformance gate can assert each one is reachable on its own; a refusal that
#: can only fire alongside another is one the other is really testing.
INTERFACE_CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "namespace_collision",
        "core_redefinition",
        "unnamespaced_definition",
        "undeclared_extension_point",
        "incompatible_target_revision",
        "omitted_type_context",
        "unknown_field",
        "unknown_interface_target",
        "weakened_compatibility",
    }
)


# --- definitions -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InterfaceDefinition:
    """One interface: the contract's stable identity and what it applies to.

    `applies_to_type` and `profile_revision` are both required. A declaration
    floating free of either is unresolvable -- two tenants may define `search`
    against different types, and a compatibility comparison that cannot say which
    pair it compared has compared nothing.
    """

    namespace: str
    name: str
    applies_to_type: str
    profile_revision: str
    properties: tuple[PropertyDefinition, ...] = ()
    extension_points: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"namespace {self.namespace!r} is lowercase alphanumeric with underscores")
        if not TYPE_NAME_PATTERN.match(self.name):
            raise ProfileDefinitionError(f"interface name {self.name!r} is lowercase alphanumeric with underscores")
        if not QUALIFIED_TYPE_PATTERN.match(self.applies_to_type):
            raise ProfileDefinitionError(
                f"{self.qualified}: applies_to_type {self.applies_to_type!r} is not a qualified "
                "`<namespace>:<type>` name; an unqualified type can arrive somewhere it means another tenant's"
            )
        if not self.profile_revision.strip():
            raise ProfileDefinitionError(
                f"{self.qualified}: a profile revision is required; an interface checked against no stated revision "
                "is one whose compatibility verdict cannot be reproduced"
            )

        seen: set[str] = set()
        for prop in self.properties:
            if prop.name in seen:
                raise ProfileDefinitionError(f"{self.qualified}: property {prop.name!r} is defined twice")
            seen.add(prop.name)
        for point in self.extension_points:
            if point in seen:
                raise ProfileDefinitionError(
                    f"{self.qualified}: {point!r} is both a core property and an extension point; a point that "
                    "names an existing property is an invitation to redefine it"
                )

    @property
    def qualified(self) -> str:
        """`<namespace>:<name>`, this interface's only unambiguous name."""
        return qualify(self.namespace, self.name)

    def canonical(self) -> dict[str, Any]:
        """This interface reduced to plain data, every sequence sorted."""
        return {
            "kind": "interface",
            "namespace": normalize_text(self.namespace),
            "name": normalize_text(self.name),
            "applies_to_type": normalize_text(self.applies_to_type),
            "profile_revision": normalize_text(self.profile_revision),
            "properties": [prop.canonical() for prop in sorted(self.properties, key=lambda p: p.name)],
            "extension_points": sorted(normalize_text(point) for point in self.extension_points),
        }


@dataclasses.dataclass(frozen=True)
class InterfaceVersionDefinition:
    """One published version of an interface, and what it does to its predecessor.

    `version_of` names the interface by qualified name rather than holding a
    reference, so a definition set canonicalizes independently of load order.
    """

    namespace: str
    name: str
    version: str
    version_of: str
    compatibility: Compatibility
    lifecycle_state: LifecycleState = "draft"
    properties: tuple[PropertyDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"namespace {self.namespace!r} is lowercase alphanumeric with underscores")
        if not TYPE_NAME_PATTERN.match(self.name):
            raise ProfileDefinitionError(f"version name {self.name!r} is lowercase alphanumeric with underscores")
        if not QUALIFIED_TYPE_PATTERN.match(self.version_of):
            raise ProfileDefinitionError(
                f"{self.qualified}: version_of {self.version_of!r} is not a qualified `<namespace>:<name>` interface"
            )
        if not self.version.strip():
            raise ProfileDefinitionError(f"{self.qualified}: a version identifier is required")
        if self.compatibility not in COMPATIBILITIES:
            raise ProfileDefinitionError(
                f"{self.qualified}: unknown compatibility {self.compatibility!r}; a version whose compatibility "
                "nobody stated is one every consumer must treat as breaking"
            )
        if self.lifecycle_state not in LIFECYCLE_STATES:
            raise ProfileDefinitionError(f"{self.qualified}: unknown lifecycle state {self.lifecycle_state!r}")

        seen: set[str] = set()
        for prop in self.properties:
            if prop.name in seen:
                raise ProfileDefinitionError(f"{self.qualified}: property {prop.name!r} is defined twice")
            seen.add(prop.name)

    @property
    def qualified(self) -> str:
        """`<namespace>:<name>`, this version's only unambiguous name."""
        return qualify(self.namespace, self.name)

    def canonical(self) -> dict[str, Any]:
        """This version reduced to plain data, every sequence sorted."""
        return {
            "kind": "interface_version",
            "namespace": normalize_text(self.namespace),
            "name": normalize_text(self.name),
            "version": normalize_text(self.version),
            "version_of": normalize_text(self.version_of),
            "compatibility": self.compatibility,
            "lifecycle_state": self.lifecycle_state,
            "properties": [prop.canonical() for prop in sorted(self.properties, key=lambda p: p.name)],
        }


#: Either member of the family. Both canonicalize, so both digest.
InterfaceFamilyDefinition = InterfaceDefinition | InterfaceVersionDefinition


# --- the frozen core --------------------------------------------------------------

_CORE_PROFILE_REVISION = "core-1"

#: The closed core interface vocabulary. Deliberately small: this family
#: describes contracts the product itself publishes, and every member here is one
#: an external consumer may build against.
CORE_INTERFACE_DEFINITIONS: tuple[InterfaceDefinition, ...] = (
    InterfaceDefinition(
        namespace=CORE_NAMESPACE,
        name="catalog_read",
        applies_to_type=qualify(CORE_NAMESPACE, "interface"),
        profile_revision=_CORE_PROFILE_REVISION,
        extension_points=("classification",),
    ),
    InterfaceDefinition(
        namespace=CORE_NAMESPACE,
        name="capability_read",
        applies_to_type=qualify(CORE_NAMESPACE, "capability"),
        profile_revision=_CORE_PROFILE_REVISION,
        extension_points=("classification",),
    ),
)

CORE_INTERFACE_VERSIONS: tuple[InterfaceVersionDefinition, ...] = (
    InterfaceVersionDefinition(
        namespace=CORE_NAMESPACE,
        name="catalog_read_v1",
        version="1.0.0",
        version_of=qualify(CORE_NAMESPACE, "catalog_read"),
        compatibility="backward_compatible",
        lifecycle_state="published",
    ),
    InterfaceVersionDefinition(
        namespace=CORE_NAMESPACE,
        name="capability_read_v1",
        version="1.0.0",
        version_of=qualify(CORE_NAMESPACE, "capability_read"),
        compatibility="backward_compatible",
        lifecycle_state="published",
    ),
)


def _assert_core_is_well_formed(
    interfaces: Iterable[InterfaceDefinition],
    versions: Iterable[InterfaceVersionDefinition],
) -> None:
    """Refuse a core that contradicts itself, at import rather than at publication."""
    seen: set[str] = set()
    for definition in interfaces:
        if definition.namespace != CORE_NAMESPACE:
            raise ProfileDefinitionError(f"core interface {definition.qualified} is not in {CORE_NAMESPACE!r}")
        if definition.qualified in seen:
            raise ProfileDefinitionError(f"core defines interface {definition.qualified} twice")
        seen.add(definition.qualified)
        if definition.applies_to_type not in CORE_TYPES_BY_QUALIFIED:
            raise ProfileDefinitionError(
                f"core interface {definition.qualified} applies to {definition.applies_to_type!r}, which the core "
                "entity vocabulary does not define"
            )
    for version in versions:
        if version.version_of not in seen:
            raise ProfileDefinitionError(
                f"core version {version.qualified} is a version of {version.version_of!r}, which no core interface "
                "defines; a version of nothing can never be compared against a predecessor"
            )


_assert_core_is_well_formed(CORE_INTERFACE_DEFINITIONS, CORE_INTERFACE_VERSIONS)

CORE_INTERFACES_BY_QUALIFIED: Mapping[str, InterfaceDefinition] = {
    definition.qualified: definition for definition in CORE_INTERFACE_DEFINITIONS
}


def canonical_interface_document(definitions: Sequence[InterfaceFamilyDefinition]) -> str:
    """The one byte-sequence an interface definition set reduces to.

    Delegates rather than reimplements: a second canonicalizer is how one
    document acquires two digests. Interfaces and versions share a namespace for
    ordering, and each `canonical()` carries a `kind` so a name reused across the
    two never collapses.
    """
    return canonical_document(definitions)


def interface_digest(definitions: Sequence[InterfaceFamilyDefinition]) -> str:
    """SHA-256 over the canonical document, as lowercase hex."""
    return definition_digest(definitions)


# --- composition ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InterfaceExtensionDocument:
    """A tenant's interface additions, and the core revision they target."""

    namespace: str
    target_core_digest: str
    interfaces: tuple[InterfaceDefinition, ...] = ()
    versions: tuple[InterfaceVersionDefinition, ...] = ()
    #: Properties added to a core interface at a declared extension point.
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
class ComposedInterfaces:
    """The result of a successful composition, with its own digest."""

    definitions: tuple[InterfaceFamilyDefinition, ...]
    digest: str

    @classmethod
    def of(cls, definitions: Sequence[InterfaceFamilyDefinition]) -> Self:
        """Order the definitions canonically and digest them in one step."""
        ordered = tuple(sorted(definitions, key=lambda definition: definition.qualified))
        return cls(definitions=ordered, digest=interface_digest(ordered))


def unknown_fields(document: Mapping[str, Any], *, allowed: frozenset[str]) -> list[str]:
    """Field names a document carries that the schema does not recognise.

    Exposed rather than inlined because a publication surface reading raw
    documents needs the same answer this module's own checks use, and a second
    implementation would drift into accepting what this one refuses.
    """
    return sorted(set(document) - allowed)


def _refuse_unknown_fields(raw: Mapping[str, Any], *, allowed: frozenset[str], where: str) -> None:
    """Refuse a document carrying fields the schema does not recognise.

    This is the boundary where an unknown field is still visible. Once a document
    has become a typed definition the question cannot be asked -- a dataclass has
    exactly the fields it declares -- so a surface that constructed the type first
    and validated afterwards would have already discarded the evidence.

    A field nobody recognises is either a typo that silently did nothing or a
    newer schema being read by older code. Both deserve a refusal at publication;
    neither is worth discovering when a consumer asks why its declaration had no
    effect.
    """
    unknown = unknown_fields(raw, allowed=allowed)
    if unknown:
        raise ProfileCompositionError(
            [
                Conflict(
                    code="unknown_field",
                    qualified_type=where,
                    property_name=None,
                    detail=(
                        f"document carries unrecognised field(s) {unknown}; legal fields are {sorted(allowed)}. "
                        "An unrecognised field is ignored rather than applied, so accepting it would publish a "
                        "contract that does not say what its author wrote"
                    ),
                )
            ]
        )


def parse_interface(raw: Mapping[str, Any]) -> InterfaceDefinition:
    """Build an interface from a raw document, refusing unrecognised fields."""
    where = f"{raw.get('namespace', '?')}:{raw.get('name', '?')}"
    _refuse_unknown_fields(raw, allowed=INTERFACE_FIELDS, where=where)
    return InterfaceDefinition(
        namespace=raw["namespace"],
        name=raw["name"],
        applies_to_type=raw["applies_to_type"],
        profile_revision=raw["profile_revision"],
        properties=tuple(raw.get("properties", ())),
        extension_points=tuple(raw.get("extension_points", ())),
    )


def parse_interface_version(raw: Mapping[str, Any]) -> InterfaceVersionDefinition:
    """Build a version from a raw document, refusing unrecognised fields."""
    where = f"{raw.get('namespace', '?')}:{raw.get('name', '?')}"
    _refuse_unknown_fields(raw, allowed=INTERFACE_VERSION_FIELDS, where=where)
    return InterfaceVersionDefinition(
        namespace=raw["namespace"],
        name=raw["name"],
        version=raw["version"],
        version_of=raw["version_of"],
        compatibility=raw["compatibility"],
        lifecycle_state=raw.get("lifecycle_state", "draft"),
        properties=tuple(raw.get("properties", ())),
    )


def compose(
    extension: InterfaceExtensionDocument,
    *,
    core: Sequence[InterfaceDefinition] = CORE_INTERFACE_DEFINITIONS,
    core_versions: Sequence[InterfaceVersionDefinition] = CORE_INTERFACE_VERSIONS,
) -> ComposedInterfaces:
    """Compose an interface extension onto the core, or refuse with every conflict.

    Deterministic: the same core and extension produce the same definitions and
    the same digest, in the same order, on any machine. Conflicts come back
    collected and ordered rather than one at a time, because an author who fixes
    them one round-trip apiece only ever sees the shape of the last.
    """
    core_by_qualified = {definition.qualified: definition for definition in core}
    conflicts: list[Conflict] = []

    expected_digest = interface_digest([*core, *core_versions])
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

    composed: list[InterfaceFamilyDefinition] = [*core, *core_versions]
    published = set(core_by_qualified)

    for definition in extension.interfaces:
        if definition.namespace == CORE_NAMESPACE:
            conflicts.append(
                Conflict(
                    code="unnamespaced_definition",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"an extension publishes into its own namespace, not {CORE_NAMESPACE!r}; a definition in "
                        "the core namespace is an edit to a contract other organizations already build against"
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
                    detail="the core already defines this interface; redefining it is an edit, not an extension",
                )
            )
            continue
        if definition.applies_to_type not in CORE_TYPES_BY_QUALIFIED:
            conflicts.append(
                Conflict(
                    code="omitted_type_context",
                    qualified_type=definition.qualified,
                    property_name=None,
                    detail=(
                        f"applies_to_type {definition.applies_to_type!r} names no type in the core vocabulary; an "
                        "interface whose type context does not resolve cannot be compared against anything"
                    ),
                )
            )
            continue
        published.add(definition.qualified)
        composed.append(definition)

    for version in extension.versions:
        if version.namespace == CORE_NAMESPACE:
            conflicts.append(
                Conflict(
                    code="unnamespaced_definition",
                    qualified_type=version.qualified,
                    property_name=None,
                    detail=f"an extension publishes versions into its own namespace, not {CORE_NAMESPACE!r}",
                )
            )
            continue
        if version.version_of not in published:
            conflicts.append(
                Conflict(
                    code="unknown_interface_target",
                    qualified_type=version.qualified,
                    property_name=None,
                    detail=(
                        f"version_of {version.version_of!r} names no interface in the core or in this extension; a "
                        "version of nothing has no predecessor to be compatible with"
                    ),
                )
            )
            continue
        # The predecessor is the newest version already standing for this
        # interface, from the core *or* from earlier in this same document.
        # Looking only at the core would make the rule unreachable for any
        # interface a tenant defines itself, which is most of them -- and a rule
        # that cannot fire on the common case is one nobody is protected by.
        predecessor = next(
            (
                existing
                for existing in reversed(composed)
                if isinstance(existing, InterfaceVersionDefinition) and existing.version_of == version.version_of
            ),
            None,
        )
        # A version may tighten its own claim -- calling a release breaking when
        # the core called it compatible costs consumers a review they did not
        # need. The reverse hands them a break they were told would not happen.
        if (
            predecessor is not None
            and predecessor.compatibility == "breaking"
            and version.compatibility == "backward_compatible"
        ):
            conflicts.append(
                Conflict(
                    code="weakened_compatibility",
                    qualified_type=version.qualified,
                    property_name=None,
                    detail=(
                        f"the core declares {predecessor.qualified} breaking and this version declares itself "
                        "backward compatible; the consumers who would skip a review on that word are not the ones "
                        "asking to change it"
                    ),
                )
            )
            continue
        composed.append(version)

    for qualified, added in sorted(extension.added_properties.items()):
        target = core_by_qualified.get(qualified)
        if target is None:
            conflicts.append(
                Conflict(
                    code="core_redefinition",
                    qualified_type=qualified,
                    property_name=None,
                    detail=(
                        "properties were added to an interface the core does not define; there is nothing here to "
                        "extend, and the addition would silently create the contract"
                    ),
                )
            )
            continue
        points = set(target.extension_points)
        existing = {prop.name for prop in target.properties}
        for prop in added:
            if prop.name in existing:
                conflicts.append(
                    Conflict(
                        code="core_redefinition",
                        qualified_type=qualified,
                        property_name=prop.name,
                        detail="the core already defines this property; an extension adds rather than restates",
                    )
                )
            elif prop.name not in points:
                conflicts.append(
                    Conflict(
                        code="undeclared_extension_point",
                        qualified_type=qualified,
                        property_name=prop.name,
                        detail=(
                            f"{qualified} declares extension points {sorted(points)}; adding elsewhere makes an "
                            "unexpected field a silent addition to a published contract"
                        ),
                    )
                )

    if conflicts:
        ordered = sorted(conflicts, key=lambda c: (c.qualified_type, c.code, c.property_name or ""))
        raise ProfileCompositionError(ordered)

    return ComposedInterfaces.of(composed)


__all__ = [
    "COMPATIBILITIES",
    "CORE_INTERFACES_BY_QUALIFIED",
    "CORE_INTERFACE_DEFINITIONS",
    "CORE_INTERFACE_VERSIONS",
    "INTERFACE_CONFLICT_CODES",
    "INTERFACE_FIELDS",
    "INTERFACE_VERSION_FIELDS",
    "LIFECYCLE_STATES",
    "Compatibility",
    "ComposedInterfaces",
    "InterfaceDefinition",
    "InterfaceExtensionDocument",
    "InterfaceFamilyDefinition",
    "InterfaceVersionDefinition",
    "LifecycleState",
    "canonical_interface_document",
    "compose",
    "interface_digest",
    "unknown_fields",
]
