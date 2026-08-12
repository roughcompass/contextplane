"""Conformance gate for the frozen core relationship definitions.

The relationship vocabulary is a contract other organizations write extensions
against, and it is the harder half of the profile to share: narrowing an entity
property hurts readers of that type, while widening a relationship endpoint or
opening its cross-organization policy changes what every traversal over the graph
can return.

Four properties carry the weight here.

**The corpus is the gate's input, and the gate proves it read it.** A fixture
suite that silently finds zero files passes every assertion it makes about them.
So the corpus is counted against a floor, every file must parse, and every
conflict code must be covered by at least one fixture -- a refusal with no
fixture behind it is one nobody has ever watched fire.

**Each refusal must fire on its own.** A fixture that expects two codes proves
neither in isolation, because the composition would still refuse if only one
rule survived. Every code therefore has at least one fixture that names it
alone, and that is asserted rather than assumed: an earlier draft of this module
had eight rules that could *only* fire alongside `unnamespaced_definition`,
which made them unreachable code sitting behind a guard while a full fixture
corpus read as proof they worked.

**Narrowing is allowed and is tested as carefully as weakening is refused.** A
gate that only proves refusals cannot tell a correct rule from one that refuses
everything, which is the failure a corpus of negatives is least able to see.

**The core digest is pinned.** It is the identity extensions declare against, so
it moving is a compatibility event rather than an implementation detail. The pin
is a literal, recomputed by hand when the core deliberately changes; deriving it
from the module under test would make the assertion agree with any core at all.

The vocabulary this gate pins was not validated by any external organization. It
is derived from this product's own catalog and its ownership and interface
requirements, and nothing here should be read as evidence that an outside party
reviewed it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from contextplane.profile.schemas.common import (
    ProfileCompositionError,
    ProfileDefinitionError,
    PropertyDefinition,
    shadowed_conflict_codes,
)
from contextplane.profile.schemas.entity import CORE_TYPES_BY_QUALIFIED
from contextplane.profile.schemas.relationship import (
    CARDINALITY_SCOPES,
    CORE_NAMESPACE,
    CORE_RELATIONSHIP_DEFINITIONS,
    CORE_RELATIONSHIPS_BY_QUALIFIED,
    CROSS_ORG_POLICIES,
    RELATIONSHIP_CONFLICT_CODES,
    RelationshipTypeDefinition,
    canonical_relationship_document,
    relationship_digest,
)
from contextplane.profile.schemas.relationship_composition import (
    ComposedRelationships,
    RelationshipExtensionDocument,
    compose,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform_profile" / "relationship" / "negative"
_COMPOSITION_DIR = _FIXTURES / "composition"

# Recomputed by hand when the core deliberately changes; see the module docstring
# for why this is a literal rather than a derivation.
CORE_DIGEST = "1c6b0928e810300d723a9ba1cc1a2cf770384244fe445491460ef02a999bb489"

# The sentinel a fixture uses to mean "the current core", so a fixture testing a
# collision does not also fail on a stale digest and pass for the wrong reason.
_CURRENT_CORE = "@core"

# A floor rather than an exact count: adding a fixture is routine, and a gate that
# failed on every addition would be edited reflexively until it meant nothing.
# Losing the corpus entirely is the failure this catches.
_MINIMUM_FIXTURES = 15


def _load(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    return sorted((path.name, json.loads(path.read_text(encoding="utf-8"))) for path in directory.glob("*.json"))


def _property(raw: dict[str, Any]) -> PropertyDefinition:
    return PropertyDefinition(
        name=raw["name"],
        value_type=raw["value_type"],
        required=raw.get("required", False),
        min_cardinality=raw.get("min_cardinality", 0),
        max_cardinality=raw.get("max_cardinality", 1),
        enum_values=tuple(raw.get("enum_values", ())),
        authority=raw.get("authority", "observed"),
    )


def _relationship(raw: dict[str, Any]) -> RelationshipTypeDefinition:
    return RelationshipTypeDefinition(
        namespace=raw["namespace"],
        type_name=raw["type_name"],
        source_type=raw["source_type"],
        destination_type=raw["destination_type"],
        direction=raw["direction"],
        cardinality_scope=raw["cardinality_scope"],
        authority=raw["authority"],
        cross_org_policy=raw["cross_org_policy"],
        min_cardinality=raw.get("min_cardinality", 0),
        max_cardinality=raw.get("max_cardinality"),
        duplicate_policy=raw.get("duplicate_policy", "reject"),
        symmetry=raw.get("symmetry", "asymmetric"),
        inverse_view=raw.get("inverse_view", "read_only"),
        properties=tuple(_property(prop) for prop in raw.get("properties", ())),
        extension_points=tuple(raw.get("extension_points", ())),
        allows_self_reference=raw.get("allows_self_reference", False),
    )


def _extension(raw: dict[str, Any]) -> RelationshipExtensionDocument:
    target = raw["target_core_digest"]
    return RelationshipExtensionDocument(
        namespace=raw["namespace"],
        target_core_digest=(relationship_digest(CORE_RELATIONSHIP_DEFINITIONS) if target == _CURRENT_CORE else target),
        definitions=tuple(_relationship(definition) for definition in raw.get("definitions", ())),
        added_properties={
            qualified: tuple(_property(prop) for prop in props)
            for qualified, props in raw.get("added_properties", {}).items()
        },
        restatements={
            qualified: _relationship(definition) for qualified, definition in raw.get("restatements", {}).items()
        },
    )


COMPOSITION_FIXTURES = _load(_COMPOSITION_DIR)


# --- the corpus is real ----------------------------------------------------------


def test_the_fixture_corpus_is_present() -> None:
    """A suite that finds zero fixtures passes every assertion it makes about them."""
    assert len(COMPOSITION_FIXTURES) >= _MINIMUM_FIXTURES, (
        f"the composition corpus holds {len(COMPOSITION_FIXTURES)} fixture(s), below the floor of "
        f"{_MINIMUM_FIXTURES}. Report short rather than topping up with an invented fixture."
    )


def test_every_fixture_declares_what_it_expects() -> None:
    """A fixture with no expectation would pass by raising for any reason at all."""
    for name, raw in COMPOSITION_FIXTURES:
        assert raw.get("expect_conflict_codes"), f"{name} names no expected conflict codes"
        assert raw.get("why"), f"{name} does not say why it must be refused"
        unknown = set(raw["expect_conflict_codes"]) - RELATIONSHIP_CONFLICT_CODES
        assert not unknown, f"{name} expects codes the module cannot emit: {sorted(unknown)}"


def test_every_conflict_code_has_a_fixture() -> None:
    """A refusal with no fixture behind it is one nobody has ever watched fire."""
    covered = {code for _, raw in COMPOSITION_FIXTURES for code in raw["expect_conflict_codes"]}
    assert RELATIONSHIP_CONFLICT_CODES <= covered, f"uncovered: {sorted(RELATIONSHIP_CONFLICT_CODES - covered)}"


#: The modules that may emit this family's conflict codes. Both are read, not
#: just the composition module: a code emitted from the definitions module would
#: be invisible to a single-file scan, and "which file emits it" is not a
#: property the closed set promises.
_EMITTING_SOURCES: tuple[str, ...] = (
    "contextplane/profile/schemas/relationship.py",
    "contextplane/profile/schemas/relationship_composition.py",
)


def test_every_declared_code_is_actually_emitted_somewhere() -> None:
    """A code in the closed set with no producer cannot fail for any reason at all.

    This is a weaker failure than a shadowed code and a harder one to see. A
    shadowed code fires, just never alone; an unemitted one never fires, and
    **a declared code with no emitter reads exactly like a declared code with an
    emitter** -- there is nothing about the vocabulary to notice. The interface
    family shipped one: a code declared, fixture-able in principle, and produced
    by nothing.

    The fixture-coverage check above cannot catch it, because a fixture can name
    a code the module never emits and simply fail for some other reason. This
    asks the source directly.
    """
    root = Path(__file__).resolve().parents[2]
    source = "\n".join((root / name).read_text(encoding="utf-8") for name in _EMITTING_SOURCES)
    emitted = {code for code in RELATIONSHIP_CONFLICT_CODES if f'code="{code}"' in source}
    missing = sorted(set(RELATIONSHIP_CONFLICT_CODES) - emitted)
    assert emitted == set(RELATIONSHIP_CONFLICT_CODES), f"declared but never emitted: {missing}"


#: Codes that provably cannot fire alone, each with the invariant that shadows it.
#: An entry here is a claim about the *vocabulary*, not an excuse for a fixture --
#: the test below fails both when a new code becomes shadowed and when an entry
#: stops being true, so a stale exemption cannot sit here unnoticed.
_STRUCTURALLY_SHADOWED: dict[str, str] = {
    "changed_symmetry": (
        "a symmetric relationship must be undirected, and every core relationship is directed, so any restatement "
        "reaching this rule has already changed the direction. It fires with `changed_direction` and adds the "
        "precision that the restatement also re-read the edge both ways."
    ),
}


def test_every_conflict_code_can_fire_on_its_own() -> None:
    """The property an earlier draft of this module failed.

    Eight rules could only ever fire alongside `unnamespaced_definition`, because
    the only way to express them was to publish into the core namespace, which is
    refused by a guard that runs first. Each was unreachable code, and a fixture
    naming two codes would have looked like coverage for both.
    """
    shadowed = shadowed_conflict_codes(
        RELATIONSHIP_CONFLICT_CODES,
        [raw["expect_conflict_codes"] for _, raw in COMPOSITION_FIXTURES],
    )
    assert shadowed == set(_STRUCTURALLY_SHADOWED), (
        f"expected exactly {sorted(_STRUCTURALLY_SHADOWED)} to be unprovable alone, got {sorted(shadowed)}. "
        "A newly shadowed code is a rule some other rule is really testing; a code that stopped being shadowed "
        "means its exemption above is stale and the fixture that now proves it should be named."
    )


def test_the_shadowing_invariant_still_holds() -> None:
    """`changed_symmetry`'s exemption rests on every core relationship being
    directed. Add one undirected core relationship and symmetry becomes provable
    on its own -- at which point the exemption above is wrong rather than merely
    unnecessary, and this fails so somebody removes it."""
    assert all(definition.direction == "directed" for definition in CORE_RELATIONSHIP_DEFINITIONS), (
        "a core relationship is now undirected, so `changed_symmetry` can fire alone; "
        "drop its entry from the shadowed table and give it a fixture of its own"
    )


# --- what composition refuses ----------------------------------------------------


@pytest.mark.parametrize(("name", "raw"), COMPOSITION_FIXTURES, ids=[name for name, _ in COMPOSITION_FIXTURES])
def test_composition_refuses_the_fixture_with_exactly_its_codes(name: str, raw: dict[str, Any]) -> None:
    """Codes compared as a set, not merely `raises`.

    A change that broadened one refusal into a catch-all would still raise, and
    would still satisfy a test that only asserted an exception.
    """
    extension = _extension(raw["extension"])
    with pytest.raises(ProfileCompositionError) as caught:
        compose(extension)
    assert set(caught.value.codes) == set(raw["expect_conflict_codes"]), f"{name}: {caught.value}"


def test_conflicts_come_back_collected_rather_than_one_at_a_time() -> None:
    """An author who fixes refusals one round-trip apiece only ever sees the last."""
    restated = dataclasses.replace(
        CORE_RELATIONSHIPS_BY_QUALIFIED["core:depends_on"],
        authority="observed",
        duplicate_policy="allow",
        cross_org_policy="allow_with_grant",
    )
    extension = RelationshipExtensionDocument(
        namespace="northwind",
        target_core_digest=CORE_DIGEST,
        restatements={"core:depends_on": restated},
    )
    with pytest.raises(ProfileCompositionError) as caught:
        compose(extension)
    assert set(caught.value.codes) == {
        "weakened_authority",
        "weakened_duplicate_policy",
        "weakened_cross_org_policy",
    }


# --- what composition allows -----------------------------------------------------


def test_a_tenant_may_narrow_a_core_relationship() -> None:
    """Holding yourself to more than the shared guarantee breaks no reader of it.

    The counterpart to every refusal above. Without it, a rule that refused
    every restatement would pass the whole negative corpus.
    """
    narrowed = dataclasses.replace(
        CORE_RELATIONSHIPS_BY_QUALIFIED["core:depends_on"],
        authority="canonical_owner",
        max_cardinality=5,
    )
    composed = compose(
        RelationshipExtensionDocument(
            namespace="northwind",
            target_core_digest=CORE_DIGEST,
            restatements={"core:depends_on": narrowed},
        )
    )
    resolved = {definition.qualified: definition for definition in composed.definitions}
    assert resolved["core:depends_on"].authority == "canonical_owner"
    assert resolved["core:depends_on"].max_cardinality == 5


def test_a_narrowing_changes_the_digest() -> None:
    """Otherwise the restatement could be accepted and silently dropped, and the
    test above would pass on a composition that kept the core unchanged."""
    narrowed = dataclasses.replace(CORE_RELATIONSHIPS_BY_QUALIFIED["core:depends_on"], authority="canonical_owner")
    composed = compose(
        RelationshipExtensionDocument(
            namespace="northwind",
            target_core_digest=CORE_DIGEST,
            restatements={"core:depends_on": narrowed},
        )
    )
    assert composed.digest != CORE_DIGEST


def test_a_tenant_may_add_a_relationship_in_its_own_namespace() -> None:
    added = RelationshipTypeDefinition(
        namespace="northwind",
        type_name="escalates_to",
        source_type="core:capability",
        destination_type="core:capability",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )
    composed = compose(
        RelationshipExtensionDocument(namespace="northwind", target_core_digest=CORE_DIGEST, definitions=(added,))
    )
    assert "northwind:escalates_to" in {definition.qualified for definition in composed.definitions}


def test_an_endpoint_may_name_a_type_the_extension_itself_defines() -> None:
    """Otherwise an extension could only ever connect core types, and a tenant
    could never relate two of its own."""
    first = RelationshipTypeDefinition(
        namespace="northwind",
        type_name="escalates_to",
        source_type="core:capability",
        destination_type="northwind:workstream",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )
    second = RelationshipTypeDefinition(
        namespace="northwind",
        type_name="workstream",
        source_type="northwind:workstream",
        destination_type="core:capability",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )
    composed = compose(
        RelationshipExtensionDocument(
            namespace="northwind", target_core_digest=CORE_DIGEST, definitions=(first, second)
        )
    )
    assert len(composed.definitions) == len(CORE_RELATIONSHIP_DEFINITIONS) + 2


# --- the frozen core -------------------------------------------------------------


def test_the_core_digest_is_pinned() -> None:
    """Extensions declare against this identity, so it moving is a compatibility
    event rather than an implementation detail."""
    assert relationship_digest(CORE_RELATIONSHIP_DEFINITIONS) == CORE_DIGEST


def test_the_digest_is_stable_across_input_order() -> None:
    """Canonicalization is what makes the digest a function of the definitions
    rather than of the order somebody wrote them in."""
    shuffled = tuple(reversed(CORE_RELATIONSHIP_DEFINITIONS))
    assert relationship_digest(shuffled) == CORE_DIGEST
    assert canonical_relationship_document(shuffled) == canonical_relationship_document(CORE_RELATIONSHIP_DEFINITIONS)


def test_composing_the_same_extension_twice_is_identical() -> None:
    """Determinism, stated as a property rather than trusted to the implementation."""
    extension = RelationshipExtensionDocument(namespace="northwind", target_core_digest=CORE_DIGEST)
    assert compose(extension).digest == compose(extension).digest


def test_every_core_endpoint_resolves_to_a_core_entity_type() -> None:
    """A dangling endpoint in the shared vocabulary is a relationship no writer
    could ever satisfy, and it would only surface at the first write."""
    for definition in CORE_RELATIONSHIP_DEFINITIONS:
        for endpoint in (definition.source_type, definition.destination_type):
            assert endpoint in CORE_TYPES_BY_QUALIFIED, f"{definition.qualified} dangles at {endpoint}"


def test_every_core_relationship_states_a_cross_organization_policy() -> None:
    """Omitted policy is denial, so the value is stated rather than defaulted."""
    for definition in CORE_RELATIONSHIP_DEFINITIONS:
        assert definition.cross_org_policy in CROSS_ORG_POLICIES
        assert definition.cardinality_scope in CARDINALITY_SCOPES


def test_the_core_lives_entirely_in_the_core_namespace() -> None:
    for definition in CORE_RELATIONSHIP_DEFINITIONS:
        assert definition.namespace == CORE_NAMESPACE


# --- definition-level rules ------------------------------------------------------


def _definition(**overrides: Any) -> RelationshipTypeDefinition:
    base: dict[str, Any] = {
        "namespace": "northwind",
        "type_name": "escalates_to",
        "source_type": "core:capability",
        "destination_type": "core:capability",
        "direction": "directed",
        "cardinality_scope": "per_source",
        "authority": "observed",
        "cross_org_policy": "deny",
    }
    return RelationshipTypeDefinition(**{**base, **overrides})


def test_a_symmetric_relationship_must_be_undirected() -> None:
    """Declaring a direction and then asserting both ways describes two different
    relationships, and a reader cannot tell which one is stored."""
    with pytest.raises(ProfileDefinitionError, match="undirected"):
        _definition(symmetry="symmetric")


def test_symmetry_requires_identical_endpoints() -> None:
    """The reverse of an edge between different types is a claim this vocabulary
    cannot make -- an interface does not expose a component."""
    with pytest.raises(ProfileDefinitionError, match="identical endpoints"):
        _definition(direction="undirected", symmetry="symmetric", destination_type="core:component")


def test_an_undirected_relationship_has_no_separately_assertable_inverse() -> None:
    """One edge already carries both readings, so a second would be a duplicate
    row for a fact already stored."""
    with pytest.raises(ProfileDefinitionError, match="no separate inverse"):
        _definition(direction="undirected", inverse_view="independently_asserted")


def test_an_unqualified_endpoint_is_refused() -> None:
    """An unqualified name that travels even one call deep can arrive somewhere
    it means another tenant's type."""
    with pytest.raises(ProfileDefinitionError, match="qualified"):
        _definition(source_type="capability")


def test_a_maximum_below_one_is_refused() -> None:
    """It forbids the relationship outright; remove the type rather than defining
    one no edge may satisfy."""
    with pytest.raises(ProfileDefinitionError, match="forbids the relationship"):
        _definition(max_cardinality=0)


def test_a_minimum_above_the_maximum_is_refused() -> None:
    with pytest.raises(ProfileDefinitionError, match="exceeds maximum"):
        _definition(min_cardinality=3, max_cardinality=2)


def test_an_unknown_cross_organization_policy_is_refused() -> None:
    with pytest.raises(ProfileDefinitionError, match="cross-organization policy"):
        _definition(cross_org_policy="ask_someone")


def test_an_extension_point_may_not_name_an_existing_property() -> None:
    """A point that names an existing property is an invitation to redefine it."""
    with pytest.raises(ProfileDefinitionError, match="both a core property and an extension point"):
        _definition(
            properties=(PropertyDefinition(name="criticality", value_type="string"),),
            extension_points=("criticality",),
        )


def test_an_extension_may_not_publish_into_the_core_namespace() -> None:
    with pytest.raises(ProfileDefinitionError, match="does not publish into"):
        RelationshipExtensionDocument(namespace=CORE_NAMESPACE, target_core_digest=CORE_DIGEST)


def test_composed_relationships_order_canonically() -> None:
    """`of` sorts and digests in one step, so two callers cannot disagree about
    which of the two they did."""
    composed = ComposedRelationships.of(tuple(reversed(CORE_RELATIONSHIP_DEFINITIONS)))
    assert [definition.qualified for definition in composed.definitions] == sorted(
        definition.qualified for definition in CORE_RELATIONSHIP_DEFINITIONS
    )
    assert composed.digest == CORE_DIGEST
