"""Primitives every profile-schema family shares.

One family says what an entity is, another what a relationship is, and each will
say what an interface is. What none of them may do is disagree about a
`Conflict`, an authority ranking or a canonical form -- a second `Conflict` class
whose codes drift from the first makes a publication gate unable to branch on a
class of failure that means two things, and a second canonicalizer makes two
digests for one document.

So the shared vocabulary lives here rather than inside whichever family happened
to be written first. Before this module existed, `entity.py` held it and every
other family imported the generic parts from the entity module -- which read as a
dependency on entities that was really a dependency on the vocabulary.

Nothing here knows what an entity or a relationship is. A family contributes its
own definition type, its own closed set of conflict codes, and its own core; this
module supplies only what is true of all of them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

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


# --- property definitions -------------------------------------------------------


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


# --- canonicalization -----------------------------------------------------------


@runtime_checkable
class CanonicalDefinition(Protocol):
    """What canonicalization needs from a definition, and nothing more.

    Stated as a protocol rather than a base class so a family's definition stays
    a plain frozen dataclass. Inheritance here would put a shared mutable surface
    under every family for the sake of two attribute lookups.
    """

    @property
    def qualified(self) -> str:
        """`<namespace>:<name>`, the definition's only unambiguous name."""

    def canonical(self) -> dict[str, Any]:
        """This definition reduced to plain, sorted, NFC-normalized data."""


def canonical_document(definitions: Sequence[CanonicalDefinition]) -> str:
    """The one byte-sequence a definition set reduces to.

    Sorted by qualified name, NFC normalized, no insignificant whitespace, and
    non-ASCII left as itself rather than escaped -- so the digest is a function of
    the definitions and not of the order somebody happened to write them in or the
    encoder's defaults.
    """
    ordered = sorted(definitions, key=lambda definition: definition.qualified)
    return json.dumps(
        [definition.canonical() for definition in ordered],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def definition_digest(definitions: Sequence[CanonicalDefinition]) -> str:
    """SHA-256 over the canonical document, as lowercase hex."""
    return hashlib.sha256(canonical_document(definitions).encode("utf-8")).hexdigest()


CORE_DIGEST_ALGORITHM = "sha256"


# --- reachability of a family's refusals -----------------------------------------


def shadowed_conflict_codes(
    codes: frozenset[str],
    expectations: Sequence[Sequence[str]],
    *,
    exempt: Mapping[str, str] | None = None,
) -> set[str]:
    """Codes in `codes` that no fixture proves on its own.

    A fixture naming two codes proves neither in isolation: the composition would
    still refuse if only one of the two rules survived. So a family's corpus is
    only evidence for the codes some fixture expects *alone*.

    This exists because a whole family once shipped where eight of fifteen codes
    could not fire except alongside another -- the only way to express those
    refusals reached a guard that ran first, leaving every one of them as
    unreachable code behind it. A corpus with one fixture per code passed a gate
    asserting every code had a fixture, while really exercising the guard fifteen
    times under fifteen names.

    `exempt` names codes that provably cannot fire alone, mapped to the reason.
    An entry is a claim about the vocabulary, not an excuse for a missing fixture,
    and a caller is expected to pair it with an assertion that fails when the
    reason stops being true -- an exemption that cannot become false is a
    permanent hole.
    """
    alone = {tuple(expected)[0] for expected in expectations if len(expected) == 1}
    return set(codes) - alone - set(exempt or {})


__all__ = [
    "AUTHORITY_ORDER",
    "AUTHORITY_RANK",
    "CORE_DIGEST_ALGORITHM",
    "CORE_NAMESPACE",
    "NAMESPACE_PATTERN",
    "PROPERTY_NAME_PATTERN",
    "QUALIFIED_TYPE_PATTERN",
    "TYPE_NAME_PATTERN",
    "VALUE_TYPES",
    "Authority",
    "CanonicalDefinition",
    "Conflict",
    "ProfileCompositionError",
    "ProfileDefinitionError",
    "PropertyDefinition",
    "ValueType",
    "canonical_document",
    "definition_digest",
    "normalize_text",
    "qualify",
    "shadowed_conflict_codes",
]
