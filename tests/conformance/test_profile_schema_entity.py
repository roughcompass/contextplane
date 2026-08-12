"""Conformance gate for the frozen core entity and ownership definitions.

The core vocabulary is a contract other organizations write extensions against,
so the things worth gating are the ones a later edit could break without any
test failing on its own terms: the digest moving, a conflict class quietly
ceasing to fire, or the shipped fixture corpus drifting away from the refusals
the code can actually produce.

Three properties carry that weight here.

**The corpus is the gate's input, and the gate proves it read it.** A fixture
suite that silently finds zero files passes every assertion it makes about
them. So the corpus is counted, every file is required to parse and to be
claimed by a test, and the conflict-code vocabulary is required to be covered
by at least one fixture -- a refusal with no fixture behind it is one nobody has
ever watched fire.

**Composition is checked for what it refuses, not merely that it refuses.**
Each fixture names the exact conflict codes it expects. A change that broadened
one refusal into a catch-all would still raise, and would still pass a test that
only asserted "raises" -- so the codes are compared as a set.

**The core digest is pinned.** It is the identity extensions declare against,
so it moving is a compatibility event rather than an implementation detail. The
pin is a literal here, recomputed by hand from the canonical document when the
core deliberately changes; deriving it from the module under test would make the
assertion agree with any core at all.

The vocabulary this gate pins was not validated by any external organization.
It is derived from this product's own catalog and its ownership and interface
requirements, and nothing here should be read as evidence that an outside party
reviewed it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from contextplane.profile.schemas.common import (
    AUTHORITY_ORDER,
    CORE_NAMESPACE,
    ProfileCompositionError,
    ProfileDefinitionError,
    PropertyDefinition,
    canonical_document,
    definition_digest,
    shadowed_conflict_codes,
)
from contextplane.profile.schemas.entity import (
    CONFLICT_CODES,
    CORE_ENTITY_DEFINITIONS,
    CORE_TYPES_BY_QUALIFIED,
    DERIVED_INITIAL_STATE,
    OWNERSHIP_STATES,
    OWNERSHIP_TERMINAL_STATES,
    OWNERSHIP_TRANSITIONS,
    EntityTypeDefinition,
    OwnershipLifecycleError,
    assert_ownership_transition,
)
from contextplane.profile.schemas.entity_composition import (
    ComposedProfile,
    ExtensionDocument,
    compose,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform_profile" / "entity" / "negative"
_COMPOSITION_DIR = _FIXTURES / "composition"
_OWNERSHIP_DIR = _FIXTURES / "ownership"

# Recomputed by hand when the core deliberately changes; see the module
# docstring for why this is a literal rather than a derivation.
CORE_DIGEST = "d523fc398eb035461cc17d385eb04296616c32aba019407d9d4a4a8375d484c1"

# The sentinel a fixture uses to mean "the current core", so that a fixture
# testing a collision does not also fail on a stale digest and pass for the
# wrong reason.
_CURRENT_CORE = "@core"


def _load(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    return sorted((path.name, json.loads(path.read_text(encoding="utf-8"))) for path in directory.glob("*.json"))


def _property(raw: dict[str, Any]) -> PropertyDefinition:
    return PropertyDefinition(
        name=raw["name"],
        value_type=raw["value_type"],
        required=raw.get("required", False),
        min_cardinality=raw.get("min_cardinality", 0),
        max_cardinality=raw.get("max_cardinality", 1) if "max_cardinality" in raw else 1,
        enum_values=tuple(raw.get("enum_values", ())),
        authority=raw.get("authority", "observed"),
    )


def _entity_type(raw: dict[str, Any]) -> EntityTypeDefinition:
    return EntityTypeDefinition(
        namespace=raw["namespace"],
        type_name=raw["type_name"],
        properties=tuple(_property(prop) for prop in raw.get("properties", ())),
        extension_points=tuple(raw.get("extension_points", ())),
        readiness_required=tuple(raw.get("readiness_required", ())),
    )


def _extension(raw: dict[str, Any]) -> ExtensionDocument:
    target = raw["target_core_digest"]
    return ExtensionDocument(
        namespace=raw["namespace"],
        target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS) if target == _CURRENT_CORE else target,
        definitions=tuple(_entity_type(definition) for definition in raw.get("definitions", ())),
        added_properties={
            qualified: tuple(_property(prop) for prop in props)
            for qualified, props in raw.get("added_properties", {}).items()
        },
    )


COMPOSITION_FIXTURES = _load(_COMPOSITION_DIR)
OWNERSHIP_FIXTURES = _load(_OWNERSHIP_DIR)


# --- the corpus is real -------------------------------------------------------


def test_the_composition_corpus_is_not_empty() -> None:
    """A suite that found no files would pass every other test in this module."""
    assert COMPOSITION_FIXTURES, f"no composition fixtures under {_COMPOSITION_DIR}"


def test_the_ownership_corpus_is_not_empty() -> None:
    assert OWNERSHIP_FIXTURES, f"no ownership fixtures under {_OWNERSHIP_DIR}"


def test_every_fixture_file_is_json_this_gate_can_read() -> None:
    """A file the loader skips is a violation nobody is testing for."""
    on_disk = {path.name for path in _COMPOSITION_DIR.glob("*")} | {path.name for path in _OWNERSHIP_DIR.glob("*")}
    loaded = {name for name, _ in COMPOSITION_FIXTURES} | {name for name, _ in OWNERSHIP_FIXTURES}
    assert on_disk == loaded, f"files present but not loaded: {sorted(on_disk - loaded)}"


def test_every_conflict_code_has_a_fixture_behind_it() -> None:
    """A refusal with no fixture is a refusal nobody has watched fire."""
    covered: set[str] = set()
    for _, fixture in COMPOSITION_FIXTURES:
        covered.update(fixture["expect_conflict_codes"])
    assert covered == set(CONFLICT_CODES), f"conflict codes with no fixture: {sorted(set(CONFLICT_CODES) - covered)}"


#: The modules that may emit this family's conflict codes. Both are read, not
#: just the composition module: a code emitted from the definitions module would
#: be invisible to a single-file scan, and "which file emits it" is not a
#: property the closed set promises.
_EMITTING_SOURCES: tuple[str, ...] = (
    "contextplane/profile/schemas/entity.py",
    "contextplane/profile/schemas/entity_composition.py",
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
    emitted = {code for code in CONFLICT_CODES if f'code="{code}"' in source}
    assert emitted == set(CONFLICT_CODES), f"declared but never emitted: {sorted(set(CONFLICT_CODES) - emitted)}"


#: Entity codes that provably cannot fire alone, each mapped to the invariant that
#: shadows it. Empty, and that is a measured fact rather than an assumption: every
#: one of the nine was checked against `compose()` individually and each fires by
#: itself. The table exists so a future code that *is* shadowed has to be declared
#: here with its reason rather than quietly weakening the gate below.
_STRUCTURALLY_SHADOWED: dict[str, str] = {}

#: Codes whose isolation is proven by a direct composition in this module rather
#: than by a fixture naming them alone.
#:
#: This is evidence, not an exemption. `weakened_required` fires by itself when an
#: extension drops `required` while holding `min_cardinality` at the core's value;
#: the shipped fixture happens to lower the minimum as well, so the corpus pairs
#: it with `weakened_cardinality`. Sharpening that fixture is the better fix and
#: belongs with the corpus rather than here. Proven below, and the test after it
#: fails if a fixture ever makes this entry unnecessary.
_PROVEN_ALONE_IN_MODULE: frozenset[str] = frozenset({"weakened_required"})


def test_every_conflict_code_can_fire_on_its_own() -> None:
    """A fixture naming two codes proves neither in isolation.

    The composition would still refuse if only one of the two rules survived, so
    a corpus is evidence only for the codes some fixture expects alone. Without
    this, a family can ship one fixture per code where every fixture really
    exercises the same earlier guard under a different name -- which is what the
    relationship family did before this gate existed, with eight of fifteen codes
    unreachable behind a namespace check that ran first.

    `weakened_required` is the case worth knowing about here: its shipped fixture
    also lowers the minimum, so it declares two codes. It is *not* shadowed --
    dropping `required` while holding `min_cardinality` at 1 fires it alone -- so
    if this gate ever fails on it, the fix is a sharper fixture, not an exemption.
    """
    proofs = [fixture["expect_conflict_codes"] for _, fixture in COMPOSITION_FIXTURES]
    # An isolation proven directly below is evidence of exactly the same kind as a
    # fixture naming one code, so it is counted as one rather than exempted.
    proofs.extend([code] for code in sorted(_PROVEN_ALONE_IN_MODULE))
    shadowed = shadowed_conflict_codes(CONFLICT_CODES, proofs, exempt=_STRUCTURALLY_SHADOWED)
    assert not shadowed, (
        f"these refusals never fire alone, so nothing proves them: {sorted(shadowed)}. "
        "A rule reachable only alongside another is a rule the other one is really testing; "
        "sharpen the fixture, prove the isolation directly, or declare the shadow with its "
        "reason if it is structural."
    )


def test_dropping_required_alone_is_refused_on_its_own_terms() -> None:
    """The isolation `_PROVEN_ALONE_IN_MODULE` claims, proven rather than asserted.

    Holding `min_cardinality` at the core's value is what separates this from the
    cardinality rule: an extension that merely stops requiring a property has
    weakened the guarantee readers depend on without touching how many values it
    may hold.
    """
    core_type = CORE_TYPES_BY_QUALIFIED["core:capability"]
    required = next(prop for prop in core_type.properties if prop.required)
    relaxed = PropertyDefinition(
        name=required.name,
        value_type=required.value_type,
        required=False,
        min_cardinality=required.min_cardinality,
        max_cardinality=required.max_cardinality,
        enum_values=required.enum_values,
        authority=required.authority,
    )
    with pytest.raises(ProfileCompositionError) as caught:
        compose(
            ExtensionDocument(
                namespace="northwind",
                target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS),
                added_properties={core_type.qualified: (relaxed,)},
            )
        )
    assert set(caught.value.codes) == {"weakened_required"}


def test_no_in_module_isolation_proof_is_redundant() -> None:
    """If a fixture is ever sharpened to name one of these alone, the entry above
    is dead weight and should be removed rather than left to accumulate."""
    from_corpus = {
        fixture["expect_conflict_codes"][0]
        for _, fixture in COMPOSITION_FIXTURES
        if len(fixture["expect_conflict_codes"]) == 1
    }
    redundant = _PROVEN_ALONE_IN_MODULE & from_corpus
    assert not redundant, (
        f"a fixture now proves {sorted(redundant)} alone, so the in-module isolation proof is "
        "no longer needed; drop the entry and the test that backs it"
    )


def test_every_fixture_explains_itself() -> None:
    """The `why` is the part a later reader needs; an unexplained fixture gets deleted."""
    for name, fixture in [*COMPOSITION_FIXTURES, *OWNERSHIP_FIXTURES]:
        assert fixture.get("why", "").strip(), f"{name} does not say what it encodes"


# --- composition refuses what it should, for the reason it should -------------


@pytest.mark.parametrize(("name", "fixture"), COMPOSITION_FIXTURES, ids=[n for n, _ in COMPOSITION_FIXTURES])
def test_a_negative_composition_fixture_is_refused_with_its_stated_codes(name: str, fixture: dict[str, Any]) -> None:
    with pytest.raises(ProfileCompositionError) as caught:
        compose(_extension(fixture["extension"]))
    assert set(caught.value.codes) == set(
        fixture["expect_conflict_codes"]
    ), f"{name}: expected {sorted(fixture['expect_conflict_codes'])}, got {sorted(set(caught.value.codes))}"


def test_a_legal_extension_composes() -> None:
    """The refusals mean nothing if the permitted path is also closed."""
    extension = ExtensionDocument(
        namespace="northwind",
        target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS),
        definitions=(
            EntityTypeDefinition(
                namespace="northwind",
                type_name="shipment",
                properties=(
                    PropertyDefinition(name="tracking_id", value_type="string", required=True, min_cardinality=1),
                ),
            ),
        ),
        added_properties={"core:capability": (PropertyDefinition(name="classification", value_type="string"),)},
    )
    composed = compose(extension)
    qualified = {definition.qualified for definition in composed.definitions}
    assert "northwind:shipment" in qualified
    capability = next(d for d in composed.definitions if d.qualified == "core:capability")
    assert "classification" in capability.by_name
    # The filled point closes; a composed type that still advertised it could
    # not survive its own construction check.
    assert "classification" not in capability.extension_points


def test_composition_reports_every_conflict_rather_than_the_first() -> None:
    """An author fixing one round-trip at a time only ever sees the last one."""
    extension = ExtensionDocument(
        namespace="northwind",
        target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS),
        added_properties={
            "core:capability": (
                PropertyDefinition(name="name", value_type="integer", required=True, min_cardinality=1),
                PropertyDefinition(name="unopened", value_type="string"),
            )
        },
    )
    with pytest.raises(ProfileCompositionError) as caught:
        compose(extension)
    assert {"changed_value_type", "undeclared_extension_point"} <= set(caught.value.codes)


def test_conflicts_come_back_in_a_stable_order() -> None:
    """Two runs that disagree on order make a diff of a failure report useless."""
    extension = ExtensionDocument(
        namespace="northwind",
        target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS),
        added_properties={
            "core:capability": (
                PropertyDefinition(name="zeta", value_type="string"),
                PropertyDefinition(name="alpha", value_type="string"),
            )
        },
    )
    runs = []
    for _ in range(2):
        with pytest.raises(ProfileCompositionError) as caught:
            compose(extension)
        runs.append([str(conflict) for conflict in caught.value.conflicts])
    assert runs[0] == runs[1]


# --- canonicalization is deterministic ----------------------------------------


def test_the_core_digest_is_pinned() -> None:
    """The identity extensions declare against; it moving is a compatibility event."""
    assert definition_digest(CORE_ENTITY_DEFINITIONS) == CORE_DIGEST


def test_canonical_output_is_independent_of_input_order() -> None:
    forward = canonical_document(CORE_ENTITY_DEFINITIONS)
    reversed_ = canonical_document(tuple(reversed(CORE_ENTITY_DEFINITIONS)))
    assert forward == reversed_


def test_canonical_output_is_independent_of_property_order() -> None:
    """Otherwise the digest records how somebody typed the file, not what it says."""
    original = CORE_ENTITY_DEFINITIONS[0]
    shuffled = EntityTypeDefinition(
        namespace=original.namespace,
        type_name=original.type_name,
        properties=tuple(reversed(original.properties)),
        extension_points=tuple(reversed(original.extension_points)),
        readiness_required=tuple(reversed(original.readiness_required)),
    )
    assert canonical_document((original,)) == canonical_document((shuffled,))


def test_a_changed_definition_changes_the_digest() -> None:
    """The pin above is only meaningful if the digest is sensitive at all."""
    widened = EntityTypeDefinition(
        namespace=CORE_NAMESPACE,
        type_name="capability",
        properties=(PropertyDefinition(name="name", value_type="string"),),
    )
    assert definition_digest((widened,)) != definition_digest(CORE_ENTITY_DEFINITIONS)


def test_composition_of_the_same_inputs_produces_the_same_digest() -> None:
    extension = ExtensionDocument(
        namespace="northwind",
        target_core_digest=definition_digest(CORE_ENTITY_DEFINITIONS),
        definitions=(EntityTypeDefinition(namespace="northwind", type_name="shipment", properties=()),),
    )
    assert compose(extension).digest == compose(extension).digest


def test_unicode_spellings_of_one_enum_value_canonicalize_together() -> None:
    """Composed and decomposed forms render identically and compare unequal.

    Type and property names are ASCII-restricted at construction, so the
    normalization that earns its place is on the values a profile carries
    verbatim -- enum members. Two spellings of one member must not yield two
    digests, or the same vocabulary would pass or fail its own compatibility
    check depending on which editor wrote it.
    """
    composed_cafe = "caf\u00e9"  # e-acute as one code point
    decomposed_cafe = "cafe\u0301"  # "e" followed by a combining acute

    assert composed_cafe != decomposed_cafe

    def order(site: str) -> EntityTypeDefinition:
        return EntityTypeDefinition(
            namespace="t",
            type_name="order",
            properties=(PropertyDefinition(name="site", value_type="enum", enum_values=(site,)),),
        )

    assert ComposedProfile.of((order(composed_cafe),)).digest == ComposedProfile.of((order(decomposed_cafe),)).digest


# --- the ownership lifecycle --------------------------------------------------


@pytest.mark.parametrize(("name", "fixture"), OWNERSHIP_FIXTURES, ids=[n for n, _ in OWNERSHIP_FIXTURES])
def test_a_negative_ownership_fixture_is_refused(name: str, fixture: dict[str, Any]) -> None:
    with pytest.raises(OwnershipLifecycleError) as caught:
        assert_ownership_transition(
            fixture["from_state"],
            fixture["to_state"],
            validated_by_subject_owner=fixture["validated_by_subject_owner"],
        )
    assert fixture["expect_error_contains"] in str(caught.value), f"{name}: refusal did not explain itself"


def test_the_intended_lifecycle_runs_end_to_end() -> None:
    """draft -> proposed -> validated -> superseded, the path the states exist for."""
    assert_ownership_transition("draft", "proposed")
    assert_ownership_transition("proposed", "validated", validated_by_subject_owner=True)
    assert_ownership_transition("validated", "superseded")


def test_revocation_is_reachable_from_every_live_state() -> None:
    """An owner can lose standing at any point before the record closes."""
    for state in OWNERSHIP_STATES:
        if state in OWNERSHIP_TERMINAL_STATES:
            continue
        assert_ownership_transition(state, "revoked")


def test_terminal_states_have_no_moves() -> None:
    for state in OWNERSHIP_TERMINAL_STATES:
        assert OWNERSHIP_TRANSITIONS[state] == frozenset()


def test_a_derived_assignment_starts_as_a_proposal() -> None:
    """Computed, not agreed -- entering as draft would read as somebody's unfinished work."""
    assert DERIVED_INITIAL_STATE == "proposed"


def test_the_ownership_type_is_closed_to_extension() -> None:
    """The type whose meaning a tenant most wants to adjust and least may."""
    ownership = next(d for d in CORE_ENTITY_DEFINITIONS if d.type_name == "ownership_assignment")
    assert ownership.extension_points == ()


# --- the core definitions are internally coherent -----------------------------


def test_every_core_type_is_in_the_core_namespace() -> None:
    for definition in CORE_ENTITY_DEFINITIONS:
        assert definition.namespace == CORE_NAMESPACE


def test_core_type_names_are_unique() -> None:
    names = [definition.type_name for definition in CORE_ENTITY_DEFINITIONS]
    assert len(names) == len(set(names))


def test_every_readiness_property_exists_on_its_type() -> None:
    """A readiness rule naming an absent property can never be satisfied."""
    for definition in CORE_ENTITY_DEFINITIONS:
        for name in definition.readiness_required:
            assert name in definition.by_name


def test_no_extension_point_shadows_a_core_property() -> None:
    for definition in CORE_ENTITY_DEFINITIONS:
        assert not set(definition.extension_points) & set(definition.by_name)


def test_a_required_property_cannot_permit_its_own_absence() -> None:
    with pytest.raises(ProfileDefinitionError, match="permits its own absence"):
        PropertyDefinition(name="name", value_type="string", required=True, min_cardinality=0)


def test_an_authority_ladder_is_ordered_weakest_first() -> None:
    """The weakening check compares ranks, so the order is load-bearing."""
    assert AUTHORITY_ORDER[0] == "observed"
    assert AUTHORITY_ORDER[-1] == "canonical_owner"


def test_an_extension_may_not_publish_into_the_core_namespace() -> None:
    with pytest.raises(ProfileDefinitionError, match="extending the vocabulary"):
        ExtensionDocument(namespace=CORE_NAMESPACE, target_core_digest=CORE_DIGEST)
