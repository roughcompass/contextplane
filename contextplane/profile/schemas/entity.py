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

Scope note: this defines and composes entity and ownership *definitions*. It
publishes nothing, binds no tenant, and validates no instance data against the
schemas it describes; folding those in would put schema authorship and schema
enforcement behind one import.

Provenance of the vocabulary: the concept set below was **not** validated by any
external organization. It is derived from this product's own catalog and its
ownership and interface requirements. Nothing here should be read, cited, or
extended on the belief that an outside party reviewed and accepted it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Self

# --- namespaces and names -----------------------------------------------------

# The one namespace an extension may never publish into. Reserved by name rather
# than by "whatever the core happens to use", so a core that later grew a second
# namespace could not silently open this one to tenants.
CORE_NAMESPACE = "core"

# Deliberately narrow. A name differing from another only by case or by a
# dash/underscore swap reads as one name to a person and two to a lookup, and
# that gap is where a collision slips past the collision check.
_NAME = r"[a-z][a-z0-9_]*"
NAMESPACE_PATTERN = re.compile(rf"^{_NAME}$")
TYPE_NAME_PATTERN = re.compile(rf"^{_NAME}$")
PROPERTY_NAME_PATTERN = re.compile(rf"^{_NAME}$")
QUALIFIED_TYPE_PATTERN = re.compile(rf"^{_NAME}:{_NAME}$")


def qualify(namespace: str, type_name: str) -> str:
    """`<namespace>:<entity_type>`, the only spelling of a type this module accepts.

    A bare type name is never passed between functions here. Two tenants may
    legitimately both define `pipeline`, and an unqualified name that travels
    even one call deep is a name that can arrive somewhere it means the other
    tenant's type.
    """
    return f"{namespace}:{type_name}"


def normalize_text(value: str) -> str:
    """NFC, because two spellings of one identifier defeat every check below.

    Composed and decomposed forms of the same character compare unequal in
    Python while rendering identically. Left alone, an extension could define a
    "different" property whose name is visually the core's, and the collision
    check would agree it was different.
    """
    return unicodedata.normalize("NFC", value)


# --- value types --------------------------------------------------------------

ValueType = Literal["string", "integer", "boolean", "timestamp", "enum", "reference"]

VALUE_TYPES: frozenset[str] = frozenset({"string", "integer", "boolean", "timestamp", "enum", "reference"})

# How much authority a source needs to assert a property. Ordered weakest to
# strongest; an extension may demand more than the core, never less.
Authority = Literal["observed", "derived", "external_authority", "canonical_owner"]

AUTHORITY_ORDER: tuple[Authority, ...] = ("observed", "derived", "external_authority", "canonical_owner")

AUTHORITY_RANK: Mapping[str, int] = {name: rank for rank, name in enumerate(AUTHORITY_ORDER)}


class ProfileDefinitionError(ValueError):
    """A definition document is malformed, independent of any composition."""


@dataclasses.dataclass(frozen=True)
class Conflict:
    """One reason a composition was refused.

    Carries a machine-readable `code` alongside the prose because two audiences
    read this: a publication gate branching on the class of failure, and the
    author who has to fix it. Collapsing them would make one of the two guess.
    """

    code: str
    qualified_type: str
    property_name: str | None
    detail: str

    def __str__(self) -> str:
        where = self.qualified_type if self.property_name is None else f"{self.qualified_type}.{self.property_name}"
        return f"{self.code}: {where} -- {self.detail}"


class ProfileCompositionError(ValueError):
    """Composition failed. Carries every conflict found, in a stable order."""

    def __init__(self, conflicts: Sequence[Conflict]) -> None:
        self.conflicts: tuple[Conflict, ...] = tuple(conflicts)
        joined = "; ".join(str(conflict) for conflict in self.conflicts)
        super().__init__(f"composition refused with {len(self.conflicts)} conflict(s): {joined}")

    @property
    def codes(self) -> tuple[str, ...]:
        """Just the codes, for a gate branching on the class of failure."""
        return tuple(conflict.code for conflict in self.conflicts)


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


# --- property and type definitions --------------------------------------------


@dataclasses.dataclass(frozen=True)
class PropertyDefinition:
    """One property on one entity type.

    Cardinality counts values, so a single-valued required property is `(1, 1)`.
    `required` stays its own flag rather than being inferred from the minimum:
    the two weaken independently, and an author needs to be told which one of
    them they weakened.
    """

    name: str
    value_type: ValueType
    required: bool = False
    min_cardinality: int = 0
    max_cardinality: int | None = 1
    enum_values: tuple[str, ...] = ()
    authority: Authority = "observed"

    def __post_init__(self) -> None:
        if not PROPERTY_NAME_PATTERN.match(self.name):
            raise ProfileDefinitionError(
                f"property name {self.name!r} is lowercase alphanumeric with underscores; a name that varies "
                "by case or separator reads as one name to a person and two to a lookup"
            )
        if self.value_type not in VALUE_TYPES:
            raise ProfileDefinitionError(f"unknown value type {self.value_type!r}; legal: {sorted(VALUE_TYPES)}")
        if self.authority not in AUTHORITY_RANK:
            raise ProfileDefinitionError(f"unknown authority {self.authority!r}; legal: {list(AUTHORITY_ORDER)}")
        if self.min_cardinality < 0:
            raise ProfileDefinitionError(f"{self.name}: minimum cardinality is not negative")
        if self.max_cardinality is not None and self.max_cardinality < 1:
            raise ProfileDefinitionError(f"{self.name}: a maximum cardinality below one forbids the property")
        if self.max_cardinality is not None and self.min_cardinality > self.max_cardinality:
            raise ProfileDefinitionError(
                f"{self.name}: minimum cardinality {self.min_cardinality} exceeds maximum {self.max_cardinality}"
            )
        if self.required and self.min_cardinality < 1:
            raise ProfileDefinitionError(
                f"{self.name}: a required property holds at least one value; required with a zero minimum is a "
                "guarantee that permits its own absence"
            )
        if self.value_type == "enum" and not self.enum_values:
            raise ProfileDefinitionError(f"{self.name}: an enum property names its values")
        if self.value_type != "enum" and self.enum_values:
            raise ProfileDefinitionError(f"{self.name}: only an enum property carries enum values")

    def canonical(self) -> dict[str, Any]:
        """This property reduced to plain, sorted, NFC-normalized data."""
        return {
            "name": normalize_text(self.name),
            "value_type": self.value_type,
            "required": self.required,
            "min_cardinality": self.min_cardinality,
            "max_cardinality": self.max_cardinality,
            "enum_values": sorted(normalize_text(value) for value in self.enum_values),
            "authority": self.authority,
        }


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


# --- canonicalization ---------------------------------------------------------


def canonical_document(definitions: Sequence[EntityTypeDefinition]) -> str:
    """The one byte-sequence a definition set reduces to.

    Sorted by qualified name and by property name within each type, NFC
    normalized, no insignificant whitespace, and non-ASCII left as itself rather
    than escaped -- so the digest is a function of the definitions and not of
    the order somebody happened to write them in or the encoder's defaults.
    """
    ordered = sorted(definitions, key=lambda definition: definition.qualified)
    return json.dumps(
        [definition.canonical() for definition in ordered],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def definition_digest(definitions: Sequence[EntityTypeDefinition]) -> str:
    """SHA-256 over the canonical document, as lowercase hex."""
    return hashlib.sha256(canonical_document(definitions).encode("utf-8")).hexdigest()


CORE_DIGEST_ALGORITHM = "sha256"


# --- composition --------------------------------------------------------------


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
    "AUTHORITY_ORDER",
    "AUTHORITY_RANK",
    "CONFLICT_CODES",
    "CORE_DIGEST_ALGORITHM",
    "CORE_ENTITY_DEFINITIONS",
    "CORE_NAMESPACE",
    "CORE_TYPES_BY_QUALIFIED",
    "CORE_TYPE_NAMES",
    "DERIVED_INITIAL_STATE",
    "OWNERSHIP_STATES",
    "OWNERSHIP_TERMINAL_STATES",
    "OWNERSHIP_TRANSITIONS",
    "VALUE_TYPES",
    "Authority",
    "ComposedProfile",
    "Conflict",
    "EntityTypeDefinition",
    "ExtensionDocument",
    "OwnershipLifecycleError",
    "OwnershipState",
    "ProfileCompositionError",
    "ProfileDefinitionError",
    "PropertyDefinition",
    "ValueType",
    "assert_ownership_transition",
    "canonical_document",
    "definition_digest",
    "compose",
    "normalize_text",
    "qualify",
]
