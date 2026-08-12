"""Conformance gate for compiling the three profile-schema families into one profile.

Each family freezes and gates its own vocabulary in its own module. This gate
owns only what needs all three sets present at once: that a compile is
deterministic across every family together, and that the three cross-family
refusals fire.

Four properties carry the weight, and three of them are the ones the family
gates are held to — deliberately, because a gate that differs per family is one
a reader has to re-learn each time.

**Each refusal must fire on its own.** A fixture naming two codes proves neither
in isolation. All four codes here were probed alone before a single fixture was
written, which is the order that finds an unreachable code rather than
documenting one.

**Every declared code must have a producer.** A code in the closed set that no
code path emits cannot fail for any reason at all, and nothing about the
vocabulary looks wrong when that is true.

**The digests are pinned.** The three family digests are the identities
extensions declare against, and the profile digest is the identity the compiled
profile is published under. All four are hand-recomputed literals: deriving one
from the module under test would make the assertion agree with any output at all.

**The combination strategy is proven against its own failure.** The profile
digest combines three per-family documents under fixed keys rather than
digesting one flattened list. The flattened form is order-dependent exactly when
two families share a qualified name -- the collision this gate exists to detect
-- and it would still pass a repeat-compile assertion. So the flattened form is
built here and watched being order-dependent, next to the real one being stable
on the same input. An assertion satisfied by both the working and the broken
combination would guard nothing.

There is deliberately no minimum-fixture-count assertion. The family gates carry
one because their corpora are globbed off disk and a suite that silently finds
zero files passes every assertion it makes about them. This corpus is a literal
in this module: it cannot arrive empty without the diff saying so, and a floor
over a literal list is a check that cannot fail for its stated reason.

The vocabulary compiled here was not validated by any external organization. It
is derived from this product's own catalog, ownership and interface
requirements.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from contextplane.profile.compiler import (
    COMPILER_CONFLICT_CODES,
    COMPILER_VERSION,
    ENTITY_FAMILY,
    FAMILY_KEYS,
    INTERFACE_FAMILY,
    RELATIONSHIP_FAMILY,
    CompiledProfile,
    canonical_profile_document,
    compile_composed,
    compile_profile,
    profile_digest,
)
from contextplane.profile.schemas.common import (
    ProfileCompositionError,
    canonical_document,
    definition_digest,
    shadowed_conflict_codes,
)
from contextplane.profile.schemas.entity import CORE_ENTITY_DEFINITIONS, EntityTypeDefinition
from contextplane.profile.schemas.entity_composition import ExtensionDocument
from contextplane.profile.schemas.entity_composition import compose as compose_entities
from contextplane.profile.schemas.interface import (
    CORE_INTERFACE_DEFINITIONS,
    CORE_INTERFACE_VERSIONS,
    InterfaceDefinition,
    InterfaceExtensionDocument,
    InterfaceFamilyDefinition,
    InterfaceVersionDefinition,
)
from contextplane.profile.schemas.interface import compose as compose_interfaces
from contextplane.profile.schemas.relationship import (
    CORE_RELATIONSHIP_DEFINITIONS,
    RelationshipTypeDefinition,
)
from contextplane.profile.schemas.relationship_composition import RelationshipExtensionDocument
from contextplane.profile.schemas.relationship_composition import compose as compose_relationships

# Recomputed by hand when a core deliberately changes. The three family digests
# are restated here rather than imported from the family gates: a compile that
# re-digested a family differently from the family itself is exactly what this
# module is here to catch, and reading the number from the family would make the
# assertion agree with whatever the compiler did to it.
ENTITY_CORE_DIGEST = "d523fc398eb035461cc17d385eb04296616c32aba019407d9d4a4a8375d484c1"
RELATIONSHIP_CORE_DIGEST = "1c6b0928e810300d723a9ba1cc1a2cf770384244fe445491460ef02a999bb489"
INTERFACE_CORE_DIGEST = "1af79b5a746a4bc7941bf6018c4e4358efa73af1bd84384fd69b6837afb80120"

#: The identity the compiled core profile is published under. It moves when any
#: of the three family cores moves, which is a compatibility event rather than an
#: implementation detail.
PROFILE_CORE_DIGEST = "dd95d804865fa0efeb98768340d0b1b1528afcfb2243bfe06d0a22e80a55f127"

CORE_INTERFACE_FAMILY: tuple[InterfaceFamilyDefinition, ...] = (
    *CORE_INTERFACE_DEFINITIONS,
    *CORE_INTERFACE_VERSIONS,
)

_TENANT = "northwind"


# --- fixture builders ---------------------------------------------------------------


def _entity(type_name: str, *, namespace: str = _TENANT) -> EntityTypeDefinition:
    return EntityTypeDefinition(namespace=namespace, type_name=type_name)


def _relationship(
    type_name: str,
    *,
    source_type: str = "core:capability",
    destination_type: str = "core:component",
    namespace: str = _TENANT,
) -> RelationshipTypeDefinition:
    return RelationshipTypeDefinition(
        namespace=namespace,
        type_name=type_name,
        source_type=source_type,
        destination_type=destination_type,
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )


def _interface(name: str, *, applies_to_type: str = "core:capability") -> InterfaceDefinition:
    return InterfaceDefinition(
        namespace=_TENANT,
        name=name,
        applies_to_type=applies_to_type,
        profile_revision="core-1",
    )


def _version(
    name: str,
    *,
    version_of: str,
    lifecycle_state: str = "draft",
    compatibility: str = "backward_compatible",
) -> InterfaceVersionDefinition:
    return InterfaceVersionDefinition(
        namespace=_TENANT,
        name=name,
        version="1.0.0",
        version_of=version_of,
        compatibility=compatibility,  # type: ignore[arg-type]
        lifecycle_state=lifecycle_state,  # type: ignore[arg-type]
    )


# --- the negative corpus -------------------------------------------------------------

Families = tuple[
    Sequence[EntityTypeDefinition],
    Sequence[RelationshipTypeDefinition],
    Sequence[InterfaceFamilyDefinition],
]


def _collision_case() -> Families:
    """One qualified name claimed by the entity family and the relationship family."""
    return (
        (*CORE_ENTITY_DEFINITIONS, _entity("thing")),
        (*CORE_RELATIONSHIP_DEFINITIONS, _relationship("thing")),
        CORE_INTERFACE_FAMILY,
    )


def _unknown_endpoint_case() -> Families:
    """A relationship endpoint naming an entity type the profile never defines."""
    return (
        CORE_ENTITY_DEFINITIONS,
        (*CORE_RELATIONSHIP_DEFINITIONS, _relationship("touches", source_type="northwind:order")),
        CORE_INTERFACE_FAMILY,
    )


def _unresolved_type_context_case() -> Families:
    """An interface whose `applies_to_type` resolves to nothing in the profile."""
    return (
        CORE_ENTITY_DEFINITIONS,
        CORE_RELATIONSHIP_DEFINITIONS,
        (*CORE_INTERFACE_FAMILY, _interface("orders_read", applies_to_type="northwind:order")),
    )


def _retired_reference_case() -> Families:
    """A version built on a version that has been withdrawn.

    The contract clause behind this fixture says "an interface referencing a
    retired type". No definition in this vocabulary has a type lifecycle: entity
    and relationship definitions carry no lifecycle field of any kind, and the
    only `retired` state a definition can be in is an interface version's. The
    remaining `retired` vocabulary in the package describes property values on
    instances, which a definition set cannot see at all.

    So "type" there is a drafting imprecision rather than a concept, and the only
    referent the definitions support is a reference landing on an interface
    version in `retired` lifecycle state. The interpretation is recorded here, in
    the fixture, so a later reader sees that the clause was read rather than
    believing the contract said this.
    """
    withdrawn = _version("orders_read_v1", version_of=f"{_TENANT}:orders_read", lifecycle_state="retired")
    successor = _version("orders_read_v2", version_of=withdrawn.qualified)
    return (
        CORE_ENTITY_DEFINITIONS,
        CORE_RELATIONSHIP_DEFINITIONS,
        (*CORE_INTERFACE_FAMILY, _interface("orders_read"), withdrawn, successor),
    )


#: Every cross-family refusal, each proved by a case that expects it alone.
#: `expect` is a list so a case naming two codes stays expressible; a case that
#: named two would prove neither, and the shadowing gate below is what says so.
NEGATIVES: tuple[tuple[str, Callable[[], Families], list[str], str], ...] = (
    (
        "one_name_in_two_families",
        _collision_case,
        ["cross_family_collision"],
        "across families nothing distinguishes two definitions sharing a qualified name, so every reference to "
        "it resolves to whichever was looked up first",
    ),
    (
        "endpoint_names_no_entity_type",
        _unknown_endpoint_case,
        ["unknown_entity_endpoint"],
        "an edge whose endpoint type does not exist can never be validated, and only a compile sees both the "
        "entity set and the relationship set",
    ),
    (
        "interface_type_context_resolves_to_nothing",
        _unresolved_type_context_case,
        ["unresolved_type_context"],
        "an interface whose type context does not resolve describes a shape belonging to nothing",
    ),
    (
        "version_built_on_a_retired_version",
        _retired_reference_case,
        ["retired_reference"],
        "a contract built on a version that has been withdrawn inherits a promise nobody is still keeping",
    ),
)


# --- the gate reads its own corpus -----------------------------------------------------


def test_every_case_declares_what_it_expects() -> None:
    for name, _, expect, why in NEGATIVES:
        assert expect, f"{name} names no expected conflict codes"
        assert why, f"{name} does not say why it must be refused"
        unknown = set(expect) - COMPILER_CONFLICT_CODES
        assert not unknown, f"{name} expects codes the compiler cannot emit: {sorted(unknown)}"


def test_every_conflict_code_has_a_case() -> None:
    covered = {code for _, _, expect, _ in NEGATIVES for code in expect}
    assert COMPILER_CONFLICT_CODES <= covered, f"uncovered: {sorted(COMPILER_CONFLICT_CODES - covered)}"


def test_every_declared_code_is_actually_emitted_somewhere() -> None:
    """A code in the closed set with no producer cannot fail for any reason at all.

    It is a weaker failure than a shadowed code and a harder one to see, because
    the vocabulary reads as complete either way.
    """
    source = (Path(__file__).resolve().parents[2] / "contextplane/profile/compiler.py").read_text(encoding="utf-8")
    emitted = {code for code in COMPILER_CONFLICT_CODES if f'code="{code}"' in source}
    assert emitted == set(
        COMPILER_CONFLICT_CODES
    ), f"declared but never emitted: {sorted(set(COMPILER_CONFLICT_CODES) - emitted)}"


def test_every_conflict_code_can_fire_on_its_own() -> None:
    """All four, proven alone. No exemptions in this vocabulary."""
    shadowed = shadowed_conflict_codes(
        COMPILER_CONFLICT_CODES,
        [expect for _, _, expect, _ in NEGATIVES],
    )
    assert not shadowed, (
        f"these refusals never fire alone, so nothing proves them: {sorted(shadowed)}. "
        "A rule reachable only alongside another is a rule the other one is really testing."
    )


# --- what a compile refuses -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "build", "expect"),
    [(name, build, expect) for name, build, expect, _ in NEGATIVES],
    ids=[name for name, _, _, _ in NEGATIVES],
)
def test_compiling_refuses_the_case_with_exactly_its_codes(
    name: str,
    build: Callable[[], Families],
    expect: list[str],
) -> None:
    """Codes compared as a set: a refusal broadened into a catch-all would still
    raise, and would still satisfy a test that only asserted an exception."""
    entities, relationships, interfaces = build()
    with pytest.raises(ProfileCompositionError) as caught:
        compile_profile(entities=entities, relationships=relationships, interfaces=interfaces)
    assert set(caught.value.codes) == set(expect), f"{name}: {caught.value}"


def test_conflicts_come_back_in_a_stable_order() -> None:
    """An author works through them in the order they are printed, so two runs
    that disagree about that order disagree about the report."""
    entities = (*CORE_ENTITY_DEFINITIONS, _entity("thing"), _entity("other"))
    relationships = (*CORE_RELATIONSHIP_DEFINITIONS, _relationship("thing"), _relationship("other"))

    with pytest.raises(ProfileCompositionError) as caught:
        compile_profile(entities=entities, relationships=relationships, interfaces=CORE_INTERFACE_FAMILY)
    forwards = caught.value.conflicts

    with pytest.raises(ProfileCompositionError) as caught:
        compile_profile(
            entities=tuple(reversed(entities)),
            relationships=tuple(reversed(relationships)),
            interfaces=tuple(reversed(CORE_INTERFACE_FAMILY)),
        )
    backwards = caught.value.conflicts

    assert len(forwards) == 2, f"expected both collisions reported, got {forwards}"
    assert forwards == backwards


# --- what a compile allows ----------------------------------------------------------------


def test_the_three_cores_compile_together() -> None:
    """The counterpart to every refusal above: a compiler that refused everything
    would pass the whole negative corpus."""
    compiled = compile_profile()
    assert len(compiled.entities) == len(CORE_ENTITY_DEFINITIONS)
    assert len(compiled.relationships) == len(CORE_RELATIONSHIP_DEFINITIONS)
    assert len(compiled.interfaces) == len(CORE_INTERFACE_FAMILY)
    assert compiled.compiler_version == COMPILER_VERSION


def test_an_interface_and_a_version_sharing_a_name_are_not_a_collision() -> None:
    """Both are the interface family, and their canonical forms carry a `kind`
    that keeps them apart. Reporting them as a collision would refuse a profile
    the interface family itself accepts."""
    interface = _interface("same")
    version = _version("same", version_of=f"{_TENANT}:same")
    compiled = compile_profile(interfaces=(*CORE_INTERFACE_FAMILY, interface, version))
    assert interface.canonical() != version.canonical()
    assert definition_digest([interface]) != definition_digest([version])
    assert compiled.output_digest != PROFILE_CORE_DIGEST


def test_a_tenant_may_point_a_relationship_at_its_own_entity_type() -> None:
    """An endpoint resolves against the compiled entity set, not just the core --
    which is the whole reason this check lives at the compile and not in the
    relationship family."""
    compiled = compile_profile(
        entities=(*CORE_ENTITY_DEFINITIONS, _entity("order")),
        relationships=(*CORE_RELATIONSHIP_DEFINITIONS, _relationship("touches", source_type="northwind:order")),
    )
    assert "northwind:touches" in {definition.qualified for definition in compiled.relationships}


def test_a_retired_version_is_legal_until_something_references_it() -> None:
    """Withdrawing a version is a normal act. Only building on the withdrawn one
    is refused -- a rule that refused the retirement itself would make the
    lifecycle state unusable."""
    withdrawn = _version("orders_read_v1", version_of=f"{_TENANT}:orders_read", lifecycle_state="retired")
    compiled = compile_profile(interfaces=(*CORE_INTERFACE_FAMILY, _interface("orders_read"), withdrawn))
    assert withdrawn.qualified in {definition.qualified for definition in compiled.interfaces}


# --- determinism ------------------------------------------------------------------------


def test_compiling_twice_produces_the_same_profile() -> None:
    """The property this module exists for: a repeat compile of one input is the
    same profile, digest and document included."""
    first = compile_profile()
    second = compile_profile()
    assert first == second
    assert first.output_digest == second.output_digest
    assert first.document == second.document
    assert first.entities == second.entities
    assert first.relationships == second.relationships
    assert first.interfaces == second.interfaces


def test_the_digest_is_stable_across_input_order() -> None:
    reversed_profile = compile_profile(
        entities=tuple(reversed(CORE_ENTITY_DEFINITIONS)),
        relationships=tuple(reversed(CORE_RELATIONSHIP_DEFINITIONS)),
        interfaces=tuple(reversed(CORE_INTERFACE_FAMILY)),
    )
    assert reversed_profile.output_digest == compile_profile().output_digest
    assert reversed_profile.document == compile_profile().document


def test_each_family_section_is_that_family_s_own_document_byte_for_byte() -> None:
    """The combination is three family documents under fixed keys, and nothing else.

    This is the assertion that fails if the three families are ever flattened
    into one list: a flattened document has no family sections to compare, so it
    cannot satisfy this by accident. The digest assertions alone would not do the
    job -- a flattened combination produces a different but perfectly stable
    number, and every determinism assertion above would still pass on it.
    """
    entities = (*CORE_ENTITY_DEFINITIONS, _entity("thing"))
    relationships = (*CORE_RELATIONSHIP_DEFINITIONS, _relationship("other"))
    document = json.loads(canonical_profile_document(entities, relationships, CORE_INTERFACE_FAMILY))

    assert document[ENTITY_FAMILY] == canonical_document(sorted(entities, key=lambda d: d.qualified))
    assert document[RELATIONSHIP_FAMILY] == canonical_document(sorted(relationships, key=lambda d: d.qualified))
    assert document[INTERFACE_FAMILY] == canonical_document(sorted(CORE_INTERFACE_FAMILY, key=lambda d: d.qualified))


def test_a_name_shared_across_families_digests_the_same_either_way() -> None:
    """Why the combination is per family rather than one flattened list.

    `canonical_document()` orders by qualified name alone and Python's sort is
    stable, so a flattened list keeps two definitions sharing a qualified name in
    whatever order they were passed -- order-dependence in exactly the collision
    case a cross-family compile exists to detect, and one a repeat-compile
    assertion would never see. The flattened form is built here and watched being
    order-dependent, so the reason this module combines per family stays
    re-derivable rather than remembered.

    The profile digest is then shown stable on the same colliding pair. It cannot
    be reached through a compile -- that collision is refused -- so the pure
    function is what carries the property.
    """
    entities = (_entity("thing"),)
    relationships = (_relationship("thing"),)

    assert definition_digest([*entities, *relationships]) != definition_digest([*relationships, *entities]), (
        "a flattened combination is expected to be order-dependent when two families share a qualified name; "
        "if it no longer is, the reason this module combines per family has changed and needs re-deriving"
    )

    assert profile_digest(entities, relationships, CORE_INTERFACE_FAMILY) == profile_digest(
        tuple(entities), tuple(relationships), tuple(reversed(CORE_INTERFACE_FAMILY))
    )


def test_the_document_carries_exactly_the_three_family_keys() -> None:
    """A family silently missing from the document would move the digest without
    any definition changing."""
    document = json.loads(canonical_profile_document(CORE_ENTITY_DEFINITIONS, (), ()))
    assert set(document) == set(FAMILY_KEYS) == {ENTITY_FAMILY, INTERFACE_FAMILY, RELATIONSHIP_FAMILY}
    assert document[RELATIONSHIP_FAMILY] == "[]"


def test_the_output_digest_is_taken_over_the_document_it_reports() -> None:
    compiled = compile_profile()
    assert hashlib.sha256(compiled.document.encode("utf-8")).hexdigest() == compiled.output_digest


# --- the pinned identities -----------------------------------------------------------------


def test_the_compiled_core_profile_digest_is_pinned() -> None:
    assert compile_profile().output_digest == PROFILE_CORE_DIGEST


def test_the_compile_carries_each_family_digest_through_unchanged() -> None:
    """A compile that re-digested a family would give one definition set two
    identities -- the one its extensions declare against, and the one the profile
    recorded."""
    compiled = compile_profile()
    assert compiled.input_digests == {
        ENTITY_FAMILY: ENTITY_CORE_DIGEST,
        INTERFACE_FAMILY: INTERFACE_CORE_DIGEST,
        RELATIONSHIP_FAMILY: RELATIONSHIP_CORE_DIGEST,
    }


def test_the_profile_digest_is_not_any_family_digest() -> None:
    """Two numbers that could be confused for each other are two numbers a caller
    can store in the wrong column."""
    compiled = compile_profile()
    assert compiled.output_digest not in set(compiled.input_digests.values())


# --- composing each family, then compiling ---------------------------------------------------


def test_what_the_three_compositions_produce_compiles() -> None:
    """The ordinary path end to end: each family composes its own extension and
    refuses its own conflicts, and what survives is compiled together.

    The relationship extension points at a type the entity composition just
    produced, which is why the composed entity set is handed to it rather than
    the core -- and why the endpoint check has to run again over the compiled
    profile, where both sets are finally present.
    """
    entities = compose_entities(
        ExtensionDocument(
            namespace=_TENANT,
            target_core_digest=ENTITY_CORE_DIGEST,
            definitions=(_entity("order"),),
        )
    )
    relationships = compose_relationships(
        RelationshipExtensionDocument(
            namespace=_TENANT,
            target_core_digest=RELATIONSHIP_CORE_DIGEST,
            definitions=(_relationship("places", source_type="northwind:order"),),
        ),
        known_types={definition.qualified: definition for definition in entities.definitions},
    )
    interfaces = compose_interfaces(
        InterfaceExtensionDocument(
            namespace=_TENANT,
            target_core_digest=INTERFACE_CORE_DIGEST,
            interfaces=(_interface("orders_read"),),
        )
    )

    compiled = compile_composed(entities, relationships, interfaces)

    assert compiled.input_digests[ENTITY_FAMILY] == entities.digest
    assert compiled.input_digests[RELATIONSHIP_FAMILY] == relationships.digest
    assert compiled.input_digests[INTERFACE_FAMILY] == interfaces.digest
    assert compiled.output_digest != PROFILE_CORE_DIGEST
    assert compile_composed(entities, relationships, interfaces) == compiled


def test_composed_and_direct_compiles_agree_on_the_core() -> None:
    """The adapter is an entry point, not a second implementation."""
    composed = compile_composed(
        compose_entities(ExtensionDocument(namespace=_TENANT, target_core_digest=ENTITY_CORE_DIGEST)),
        compose_relationships(
            RelationshipExtensionDocument(namespace=_TENANT, target_core_digest=RELATIONSHIP_CORE_DIGEST)
        ),
        compose_interfaces(InterfaceExtensionDocument(namespace=_TENANT, target_core_digest=INTERFACE_CORE_DIGEST)),
    )
    assert composed.output_digest == PROFILE_CORE_DIGEST


def test_compiled_profiles_order_canonically() -> None:
    compiled = CompiledProfile.of(
        tuple(reversed(CORE_ENTITY_DEFINITIONS)),
        tuple(reversed(CORE_RELATIONSHIP_DEFINITIONS)),
        tuple(reversed(CORE_INTERFACE_FAMILY)),
    )
    assert compiled.output_digest == PROFILE_CORE_DIGEST
    assert [definition.qualified for definition in compiled.entities] == sorted(
        definition.qualified for definition in CORE_ENTITY_DEFINITIONS
    )
