"""The closed core relationship vocabulary, and the rules an extension cannot bend.

An entity type says what a thing is. A relationship type says what may be
asserted *between* things, and that is the harder contract to share: a tenant
narrowing an entity property hurts readers of that type, while a tenant widening
a relationship endpoint changes what every traversal over the graph can return.

The generic composition vocabulary -- conflicts, properties, authority ranking,
canonicalization -- is imported from the entity module rather than restated.
Restating it would produce a second `Conflict` whose codes drift from the first,
and a composition gate cannot branch on a class of failure that means two things.

Five rules are specific to relationships and are argued at their definition sites
below:

**One physical direction.** A directed relationship is stored once, from source
to destination. The inverse is a read-only view derived from it, never a second
stored edge, because two edges representing one fact disagree the moment one is
written and the other is not. A profile may declare the inverse independently
assertable; the default is that it may not.

**Endpoints narrow, never widen.** An extension may constrain an endpoint to a
subtype it also defines. It may not point an endpoint at a broader type, and it
may not repoint it at an unrelated one: both silently change what an existing
traversal returns, and the readers relying on the old endpoint are not in the
room.

**Cardinality carries a scope.** "At most one" is unanswerable without saying
per what -- per source, per destination, or per ordered pair. A bound with no
scope is not checkable, so the scope is required rather than defaulted.

**Symmetry is a property of the type, not of the data.** A symmetric
relationship asserts both directions from one stored edge. Changing symmetry
after publication reinterprets every edge already stored, which is why it is a
conflict rather than a compatible change.

**Cross-organization policy defaults to denial.** An omitted policy is a denial,
so the field is required and its vocabulary is closed. A policy that could be
absent would be read as permission by whichever reader assumed most.

Scope note: this defines and composes relationship *definitions*. It stores no
edge, validates no instance against the schemas it describes, and enforces no
cardinality at write time -- the transactional side of cardinality belongs with
the code that takes the write lock, not with the code that describes the rule.

Provenance of the vocabulary: the relationship set below was **not** validated by
any external organization. It is derived from this product's own catalog and its
ownership and interface requirements. Nothing here should be read, cited, or
extended on the belief that an outside party reviewed and accepted it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from contextplane.profile.schemas.entity import (
    AUTHORITY_RANK,
    CORE_NAMESPACE,
    CORE_TYPES_BY_QUALIFIED,
    NAMESPACE_PATTERN,
    QUALIFIED_TYPE_PATTERN,
    TYPE_NAME_PATTERN,
    Authority,
    ProfileDefinitionError,
    PropertyDefinition,
    normalize_text,
    qualify,
)

# --- the dimensions a relationship type is defined over -------------------------

#: Whether the relationship means the same thing read backwards. `undirected`
#: still stores one physical edge; it changes what a traversal may infer from it,
#: not how many rows exist.
Direction = Literal["directed", "undirected"]

DIRECTIONS: tuple[Direction, ...] = ("directed", "undirected")

#: Whether a second identical edge between the same endpoints is a duplicate to
#: reject or a distinct assertion to keep. `allow` is meaningful only where the
#: edge carries properties that distinguish the two.
DuplicatePolicy = Literal["reject", "allow"]

DUPLICATE_POLICIES: tuple[DuplicatePolicy, ...] = ("reject", "allow")

#: `symmetric` means one stored edge asserts both directions. It is legal only on
#: an undirected relationship whose endpoints are the same type -- asserting the
#: reverse of `component -> interface` would claim an interface exposes a
#: component, which is not a statement this vocabulary can make.
Symmetry = Literal["asymmetric", "symmetric"]

SYMMETRIES: tuple[Symmetry, ...] = ("asymmetric", "symmetric")

#: Whether the reverse traversal is a derived read-only view of the stored edge,
#: or a direction a writer may assert on its own. `read_only` is the default and
#: the safe direction: promoting a view to independently assertable later is a
#: compatible change, and demoting one that writers already use is not.
InverseView = Literal["read_only", "independently_asserted"]

INVERSE_VIEWS: tuple[InverseView, ...] = ("read_only", "independently_asserted")

#: What a cardinality bound is counted over. Required rather than defaulted: "at
#: most one" is unanswerable without it, and a default would answer it silently.
CardinalityScope = Literal["per_source", "per_destination", "per_pair"]

CARDINALITY_SCOPES: tuple[CardinalityScope, ...] = ("per_source", "per_destination", "per_pair")

#: What a graph read may cross an organization boundary for. Omission is denial,
#: which is why there is no "unset" member: the absent case is spelled `deny`.
CrossOrgPolicy = Literal["deny", "allow_with_grant"]

CROSS_ORG_POLICIES: tuple[CrossOrgPolicy, ...] = ("deny", "allow_with_grant")


#: Every reason a relationship composition may refuse. A closed set so the
#: conformance gate can assert each one has a fixture behind it; a refusal with
#: no fixture is one nobody has ever watched fire.
RELATIONSHIP_CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "namespace_collision",
        "core_redefinition",
        "unnamespaced_definition",
        "undeclared_extension_point",
        "incompatible_target_revision",
        "unknown_endpoint_type",
        "weakened_endpoint",
        "weakened_authority",
        "weakened_cardinality",
        "changed_direction",
        "changed_symmetry",
        "changed_cardinality_scope",
        "weakened_duplicate_policy",
        "weakened_cross_org_policy",
        "weakened_inverse_view",
    }
)


# --- the definition -------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RelationshipTypeDefinition:
    """One relationship type: its endpoints, its shape, and who may assert it.

    Endpoints are *qualified* type names rather than definition objects. An
    extension may legitimately point at a core type it did not define, and a
    reference to the object would make the definition set an object graph that
    cannot be canonicalized independently of load order.

    `min_cardinality` gates readiness rather than validity. A draft is allowed to
    sit below the minimum -- an entity that cannot be saved until it is complete
    cannot be drafted at all -- so the minimum is what readiness checks, and only
    the maximum is a write-time refusal.
    """

    namespace: str
    type_name: str
    source_type: str
    destination_type: str
    direction: Direction
    cardinality_scope: CardinalityScope
    authority: Authority
    cross_org_policy: CrossOrgPolicy
    min_cardinality: int = 0
    max_cardinality: int | None = None
    duplicate_policy: DuplicatePolicy = "reject"
    symmetry: Symmetry = "asymmetric"
    inverse_view: InverseView = "read_only"
    properties: tuple[PropertyDefinition, ...] = ()
    extension_points: tuple[str, ...] = ()
    #: Whether an edge may join an entity to itself. Off by default: a
    #: dependency that depends on itself is the shape that makes a closure walk
    #: non-terminating, and it is never what an author meant to declare.
    allows_self_reference: bool = False

    def __post_init__(self) -> None:
        if not NAMESPACE_PATTERN.match(self.namespace):
            raise ProfileDefinitionError(f"namespace {self.namespace!r} is lowercase alphanumeric with underscores")
        if not TYPE_NAME_PATTERN.match(self.type_name):
            raise ProfileDefinitionError(
                f"relationship type name {self.type_name!r} is lowercase alphanumeric with underscores"
            )
        for role, endpoint in (("source", self.source_type), ("destination", self.destination_type)):
            if not QUALIFIED_TYPE_PATTERN.match(endpoint):
                raise ProfileDefinitionError(
                    f"{self.qualified}: {role} endpoint {endpoint!r} is not a qualified `<namespace>:<type>` name; "
                    "an unqualified endpoint can arrive somewhere it means another tenant's type"
                )

        if self.direction not in DIRECTIONS:
            raise ProfileDefinitionError(f"{self.qualified}: unknown direction {self.direction!r}")
        if self.cardinality_scope not in CARDINALITY_SCOPES:
            raise ProfileDefinitionError(f"{self.qualified}: unknown cardinality scope {self.cardinality_scope!r}")
        if self.duplicate_policy not in DUPLICATE_POLICIES:
            raise ProfileDefinitionError(f"{self.qualified}: unknown duplicate policy {self.duplicate_policy!r}")
        if self.symmetry not in SYMMETRIES:
            raise ProfileDefinitionError(f"{self.qualified}: unknown symmetry {self.symmetry!r}")
        if self.inverse_view not in INVERSE_VIEWS:
            raise ProfileDefinitionError(f"{self.qualified}: unknown inverse view {self.inverse_view!r}")
        if self.cross_org_policy not in CROSS_ORG_POLICIES:
            raise ProfileDefinitionError(
                f"{self.qualified}: unknown cross-organization policy {self.cross_org_policy!r}; an omitted policy "
                "is a denial, so the value is stated rather than left to a reader's assumption"
            )
        if self.authority not in AUTHORITY_RANK:
            raise ProfileDefinitionError(f"{self.qualified}: unknown authority {self.authority!r}")

        if self.min_cardinality < 0:
            raise ProfileDefinitionError(f"{self.qualified}: minimum cardinality is not negative")
        if self.max_cardinality is not None and self.max_cardinality < 1:
            raise ProfileDefinitionError(
                f"{self.qualified}: a maximum cardinality below one forbids the relationship outright; remove the "
                "type rather than defining one no edge may satisfy"
            )
        if self.max_cardinality is not None and self.min_cardinality > self.max_cardinality:
            raise ProfileDefinitionError(
                f"{self.qualified}: minimum cardinality {self.min_cardinality} exceeds maximum {self.max_cardinality}; "
                "no edge count satisfies the window"
            )

        # Symmetry asserts the reverse of a stored edge, so it is only coherent
        # where the reverse is a statement of the same kind. Between different
        # endpoint types it would claim something the vocabulary cannot express.
        if self.symmetry == "symmetric":
            if self.direction != "undirected":
                raise ProfileDefinitionError(
                    f"{self.qualified}: a symmetric relationship is undirected; declaring a direction and then "
                    "asserting both ways describes two different relationships"
                )
            if self.source_type != self.destination_type:
                raise ProfileDefinitionError(
                    f"{self.qualified}: symmetry requires identical endpoints, not "
                    f"{self.source_type!r} and {self.destination_type!r}; the reverse of an edge between different "
                    "types is a claim this vocabulary cannot make"
                )
        # An undirected relationship has no reverse to assert separately -- the
        # single edge already means both ways -- so an independently assertable
        # inverse would be a second row for a fact already stored.
        if self.direction == "undirected" and self.inverse_view == "independently_asserted":
            raise ProfileDefinitionError(
                f"{self.qualified}: an undirected relationship has no separate inverse to assert; one edge already "
                "carries both readings"
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
        """`<namespace>:<type_name>`, this relationship's only unambiguous name."""
        return qualify(self.namespace, self.type_name)

    @property
    def by_name(self) -> Mapping[str, PropertyDefinition]:
        """Properties keyed by name, for the collision and weakening checks."""
        return {prop.name: prop for prop in self.properties}

    def canonical(self) -> dict[str, Any]:
        """This relationship reduced to plain data, every sequence sorted."""
        return {
            "namespace": normalize_text(self.namespace),
            "type_name": normalize_text(self.type_name),
            "source_type": normalize_text(self.source_type),
            "destination_type": normalize_text(self.destination_type),
            "direction": self.direction,
            "cardinality_scope": self.cardinality_scope,
            "authority": self.authority,
            "cross_org_policy": self.cross_org_policy,
            "min_cardinality": self.min_cardinality,
            "max_cardinality": self.max_cardinality,
            "duplicate_policy": self.duplicate_policy,
            "symmetry": self.symmetry,
            "inverse_view": self.inverse_view,
            "allows_self_reference": self.allows_self_reference,
            "properties": [prop.canonical() for prop in sorted(self.properties, key=lambda p: p.name)],
            "extension_points": sorted(normalize_text(point) for point in self.extension_points),
        }


# --- the frozen core ------------------------------------------------------------

_CAPABILITY = qualify(CORE_NAMESPACE, "capability")
_COMPONENT = qualify(CORE_NAMESPACE, "component")
_INTERFACE = qualify(CORE_NAMESPACE, "interface")
_INTERFACE_VERSION = qualify(CORE_NAMESPACE, "interface_version")

#: The closed core relationship vocabulary. Every bound here is deliberate and
#: each is argued in its own comment; a bound with no reason is a bound the next
#: author will assume was arbitrary and relax.
CORE_RELATIONSHIP_DEFINITIONS: tuple[RelationshipTypeDefinition, ...] = (
    # A capability resting on another. Self-reference is refused because a
    # capability depending on itself makes the dependency closure non-terminating
    # and is never a statement anybody meant to make.
    RelationshipTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="depends_on",
        source_type=_CAPABILITY,
        destination_type=_CAPABILITY,
        direction="directed",
        cardinality_scope="per_source",
        authority="derived",
        cross_org_policy="deny",
        duplicate_policy="reject",
        allows_self_reference=False,
        extension_points=("criticality",),
    ),
    # What builds a capability. Unbounded per source: one component may implement
    # several capabilities, and a bound here would be a modelling opinion rather
    # than a shared guarantee.
    RelationshipTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="implements",
        source_type=_COMPONENT,
        destination_type=_CAPABILITY,
        direction="directed",
        cardinality_scope="per_source",
        authority="external_authority",
        cross_org_policy="deny",
        duplicate_policy="reject",
    ),
    # What a component offers. `allow_with_grant` because a provided interface is
    # the one part of this graph another organization has a legitimate reason to
    # read -- and still only with a grant, never by default.
    RelationshipTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="provides",
        source_type=_COMPONENT,
        destination_type=_INTERFACE,
        direction="directed",
        cardinality_scope="per_source",
        authority="external_authority",
        cross_org_policy="allow_with_grant",
        duplicate_policy="reject",
    ),
    # What a component depends on being offered. Deliberately `deny` while
    # `provides` is grantable: what you publish is a different disclosure from
    # what you rely on, and the consumer side reveals the shape of an
    # organization's internals to anyone who can enumerate it.
    RelationshipTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="consumes",
        source_type=_COMPONENT,
        destination_type=_INTERFACE,
        direction="directed",
        cardinality_scope="per_source",
        authority="external_authority",
        cross_org_policy="deny",
        duplicate_policy="reject",
    ),
    # A version belongs to exactly one interface. This is the one core bound that
    # is a hard maximum rather than a modelling preference: a version pointing at
    # two interfaces makes "which interface is this a version of" unanswerable,
    # and every compatibility comparison reads that answer.
    RelationshipTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="version_of",
        source_type=_INTERFACE_VERSION,
        destination_type=_INTERFACE,
        direction="directed",
        cardinality_scope="per_source",
        authority="canonical_owner",
        cross_org_policy="allow_with_grant",
        min_cardinality=1,
        max_cardinality=1,
        duplicate_policy="reject",
    ),
)


def _assert_core_is_well_formed(definitions: Iterable[RelationshipTypeDefinition]) -> None:
    """Refuse a core that contradicts itself, at import rather than at publication."""
    seen: set[str] = set()
    for definition in definitions:
        if definition.namespace != CORE_NAMESPACE:
            raise ProfileDefinitionError(
                f"core relationship {definition.qualified} is not in the {CORE_NAMESPACE!r} namespace"
            )
        if definition.qualified in seen:
            raise ProfileDefinitionError(f"core defines relationship {definition.qualified} twice")
        seen.add(definition.qualified)
        # A core endpoint naming a type the core does not define would be a
        # dangling reference in the shared vocabulary itself.
        for endpoint in (definition.source_type, definition.destination_type):
            if endpoint not in CORE_TYPES_BY_QUALIFIED:
                raise ProfileDefinitionError(
                    f"core relationship {definition.qualified} names endpoint {endpoint!r}, which the core entity "
                    "vocabulary does not define"
                )


_assert_core_is_well_formed(CORE_RELATIONSHIP_DEFINITIONS)

CORE_RELATIONSHIPS_BY_QUALIFIED: Mapping[str, RelationshipTypeDefinition] = {
    definition.qualified: definition for definition in CORE_RELATIONSHIP_DEFINITIONS
}

CORE_RELATIONSHIP_NAMES: frozenset[str] = frozenset(
    definition.type_name for definition in CORE_RELATIONSHIP_DEFINITIONS
)


# --- canonicalization -----------------------------------------------------------


def canonical_relationship_document(definitions: Sequence[RelationshipTypeDefinition]) -> str:
    """The one byte-sequence a relationship definition set reduces to.

    Sorted by qualified name, NFC normalized, no insignificant whitespace, and
    non-ASCII left as itself rather than escaped -- so the digest is a function of
    the definitions and not of the order somebody wrote them in.
    """
    ordered = sorted(definitions, key=lambda definition: definition.qualified)
    return json.dumps(
        [definition.canonical() for definition in ordered],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def relationship_digest(definitions: Sequence[RelationshipTypeDefinition]) -> str:
    """SHA-256 over the canonical document, as lowercase hex."""
    return hashlib.sha256(canonical_relationship_document(definitions).encode("utf-8")).hexdigest()


__all__ = [
    "CARDINALITY_SCOPES",
    "CORE_RELATIONSHIPS_BY_QUALIFIED",
    "CORE_RELATIONSHIP_DEFINITIONS",
    "CORE_RELATIONSHIP_NAMES",
    "CROSS_ORG_POLICIES",
    "DIRECTIONS",
    "DUPLICATE_POLICIES",
    "INVERSE_VIEWS",
    "RELATIONSHIP_CONFLICT_CODES",
    "SYMMETRIES",
    "CardinalityScope",
    "CrossOrgPolicy",
    "Direction",
    "DuplicatePolicy",
    "InverseView",
    "RelationshipTypeDefinition",
    "Symmetry",
    "canonical_relationship_document",
    "relationship_digest",
]
