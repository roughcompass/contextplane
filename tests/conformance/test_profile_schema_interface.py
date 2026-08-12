"""Conformance gate for the frozen core interface definitions.

An interface is a promise about a shape other systems build against, which makes
this the family where a weakening is least visible when it happens and most
expensive afterwards. The consumers who relied on the guarantee are not in the
room when it is relaxed, and they find out at runtime.

Four properties carry the weight here, and they are the same four the entity and
relationship families are held to — deliberately, because a gate that differs per
family is one a reader has to re-learn each time.

**The corpus is the gate's input, and the gate proves it read it.** A fixture
suite that silently finds zero files passes every assertion it makes about them.

**Each refusal must fire on its own.** A fixture naming two codes proves neither
in isolation. Two of this family's nine codes were unreachable when first
written — `unknown_field` had no producer at all, and `weakened_compatibility`
could only fire against a core version nothing declared — and both were found by
probing every code before a single fixture existed rather than after.

**Refusals are checked at the stage that can still see them.** `unknown_field`
is refused while parsing a raw document, because once a document has become a
typed definition the question cannot be asked: a dataclass has exactly the fields
it declares. A gate that constructed the type first and validated afterwards
would have discarded the evidence.

**The core digest is pinned.** It is the identity extensions declare against, so
it moving is a compatibility event rather than an implementation detail.

The vocabulary this gate pins was not validated by any external organization. It
is derived from this product's own catalog and its ownership and interface
requirements.
"""

from __future__ import annotations

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
from contextplane.profile.schemas.interface import (
    COMPATIBILITIES,
    CORE_INTERFACE_DEFINITIONS,
    CORE_INTERFACE_VERSIONS,
    INTERFACE_CONFLICT_CODES,
    INTERFACE_FIELDS,
    ComposedInterfaces,
    InterfaceDefinition,
    InterfaceExtensionDocument,
    InterfaceVersionDefinition,
    canonical_interface_document,
    compose,
    interface_digest,
    parse_interface,
    parse_interface_version,
    unknown_fields,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "platform_profile" / "interface" / "negative"
_COMPOSITION_DIR = _FIXTURES / "composition"
_DOCUMENT_DIR = _FIXTURES / "document"

# Recomputed by hand when the core deliberately changes; deriving it from the
# module under test would make the assertion agree with any core at all.
CORE_DIGEST = "1af79b5a746a4bc7941bf6018c4e4358efa73af1bd84384fd69b6837afb80120"

_CURRENT_CORE = "@core"

# A floor rather than an exact count: adding a fixture is routine, and a gate
# failing on every addition would be edited reflexively until it meant nothing.
_MINIMUM_FIXTURES = 10


def _load(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    return sorted((path.name, json.loads(path.read_text(encoding="utf-8"))) for path in directory.glob("*.json"))


COMPOSITION_FIXTURES = _load(_COMPOSITION_DIR)
DOCUMENT_FIXTURES = _load(_DOCUMENT_DIR)
ALL_FIXTURES = COMPOSITION_FIXTURES + DOCUMENT_FIXTURES


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


def _interface(raw: dict[str, Any]) -> InterfaceDefinition:
    return InterfaceDefinition(
        namespace=raw["namespace"],
        name=raw["name"],
        applies_to_type=raw["applies_to_type"],
        profile_revision=raw["profile_revision"],
        properties=tuple(_property(prop) for prop in raw.get("properties", ())),
        extension_points=tuple(raw.get("extension_points", ())),
    )


def _version(raw: dict[str, Any]) -> InterfaceVersionDefinition:
    return InterfaceVersionDefinition(
        namespace=raw["namespace"],
        name=raw["name"],
        version=raw["version"],
        version_of=raw["version_of"],
        compatibility=raw["compatibility"],
        lifecycle_state=raw.get("lifecycle_state", "draft"),
        properties=tuple(_property(prop) for prop in raw.get("properties", ())),
    )


def _extension(raw: dict[str, Any]) -> InterfaceExtensionDocument:
    target = raw["target_core_digest"]
    return InterfaceExtensionDocument(
        namespace=raw["namespace"],
        target_core_digest=(
            interface_digest([*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS])
            if target == _CURRENT_CORE
            else target
        ),
        interfaces=tuple(_interface(item) for item in raw.get("interfaces", ())),
        versions=tuple(_version(item) for item in raw.get("versions", ())),
        added_properties={
            qualified: tuple(_property(prop) for prop in props)
            for qualified, props in raw.get("added_properties", {}).items()
        },
    )


# --- the corpus is real -----------------------------------------------------------


def test_the_fixture_corpus_is_present() -> None:
    """A suite that finds zero fixtures passes every assertion it makes about them."""
    assert len(ALL_FIXTURES) >= _MINIMUM_FIXTURES, (
        f"the corpus holds {len(ALL_FIXTURES)} fixture(s), below the floor of {_MINIMUM_FIXTURES}. "
        "Report short rather than topping up with an invented fixture."
    )


def test_every_fixture_declares_what_it_expects() -> None:
    for name, raw in ALL_FIXTURES:
        assert raw.get("expect_conflict_codes"), f"{name} names no expected conflict codes"
        assert raw.get("why"), f"{name} does not say why it must be refused"
        unknown = set(raw["expect_conflict_codes"]) - INTERFACE_CONFLICT_CODES
        assert not unknown, f"{name} expects codes the module cannot emit: {sorted(unknown)}"


def test_every_conflict_code_has_a_fixture() -> None:
    covered = {code for _, raw in ALL_FIXTURES for code in raw["expect_conflict_codes"]}
    assert INTERFACE_CONFLICT_CODES <= covered, f"uncovered: {sorted(INTERFACE_CONFLICT_CODES - covered)}"


def test_every_declared_code_is_actually_emitted_somewhere() -> None:
    """A code in the closed set with no producer cannot fail for any reason at all.

    `unknown_field` was exactly that when this module was first written: declared,
    fixture-able in principle, and emitted by no code path. It is a weaker failure
    than a shadowed code and a harder one to see, because nothing about the
    vocabulary looks wrong.
    """
    source = (Path(__file__).resolve().parents[2] / "contextplane/profile/schemas/interface.py").read_text()
    emitted = {code for code in INTERFACE_CONFLICT_CODES if f'code="{code}"' in source}
    assert emitted == set(
        INTERFACE_CONFLICT_CODES
    ), f"declared but never emitted: {sorted(set(INTERFACE_CONFLICT_CODES) - emitted)}"


def test_every_conflict_code_can_fire_on_its_own() -> None:
    """Every one of the nine, proven alone. No exemptions in this family."""
    shadowed = shadowed_conflict_codes(
        INTERFACE_CONFLICT_CODES,
        [raw["expect_conflict_codes"] for _, raw in ALL_FIXTURES],
    )
    assert not shadowed, (
        f"these refusals never fire alone, so nothing proves them: {sorted(shadowed)}. "
        "A rule reachable only alongside another is a rule the other one is really testing."
    )


# --- what composition refuses ------------------------------------------------------


@pytest.mark.parametrize(("name", "raw"), COMPOSITION_FIXTURES, ids=[name for name, _ in COMPOSITION_FIXTURES])
def test_composition_refuses_the_fixture_with_exactly_its_codes(name: str, raw: dict[str, Any]) -> None:
    """Codes compared as a set: a refusal broadened into a catch-all would still
    raise, and would still satisfy a test that only asserted an exception."""
    with pytest.raises(ProfileCompositionError) as caught:
        compose(_extension(raw["extension"]))
    assert set(caught.value.codes) == set(raw["expect_conflict_codes"]), f"{name}: {caught.value}"


@pytest.mark.parametrize(("name", "raw"), DOCUMENT_FIXTURES, ids=[name for name, _ in DOCUMENT_FIXTURES])
def test_parsing_refuses_the_document_with_exactly_its_codes(name: str, raw: dict[str, Any]) -> None:
    """Refused at the stage that can still see the problem, not after the type
    has discarded it."""
    parser = parse_interface if raw["parse"] == "interface" else parse_interface_version
    with pytest.raises(ProfileCompositionError) as caught:
        parser(raw["document"])
    assert set(caught.value.codes) == set(raw["expect_conflict_codes"]), f"{name}: {caught.value}"


# --- what composition allows -------------------------------------------------------


def test_a_tenant_may_publish_an_interface_in_its_own_namespace() -> None:
    """The counterpart to every refusal above: a rule that refused everything
    would pass the whole negative corpus."""
    composed = compose(
        InterfaceExtensionDocument(
            namespace="northwind",
            target_core_digest=CORE_DIGEST,
            interfaces=(
                InterfaceDefinition(
                    namespace="northwind",
                    name="orders_read",
                    applies_to_type="core:capability",
                    profile_revision="core-1",
                ),
            ),
        )
    )
    assert "northwind:orders_read" in {definition.qualified for definition in composed.definitions}


def test_a_version_may_strengthen_its_compatibility_claim() -> None:
    """Calling a release breaking when its predecessor was compatible costs
    consumers a review they did not need; the reverse hands them a break they
    were told would not happen. Only the second is refused."""
    composed = compose(
        InterfaceExtensionDocument(
            namespace="northwind",
            target_core_digest=CORE_DIGEST,
            interfaces=(
                InterfaceDefinition(
                    namespace="northwind",
                    name="orders_read",
                    applies_to_type="core:capability",
                    profile_revision="core-1",
                ),
            ),
            versions=(
                InterfaceVersionDefinition(
                    namespace="northwind",
                    name="orders_read_v1",
                    version="1.0.0",
                    version_of="northwind:orders_read",
                    compatibility="backward_compatible",
                ),
                InterfaceVersionDefinition(
                    namespace="northwind",
                    name="orders_read_v2",
                    version="2.0.0",
                    version_of="northwind:orders_read",
                    compatibility="breaking",
                ),
            ),
        )
    )
    assert composed.digest != CORE_DIGEST


def test_a_known_field_set_parses() -> None:
    parsed = parse_interface(
        {
            "namespace": "northwind",
            "name": "orders_read",
            "applies_to_type": "core:capability",
            "profile_revision": "core-1",
        }
    )
    assert parsed.qualified == "northwind:orders_read"
    assert unknown_fields({"namespace": "x"}, allowed=INTERFACE_FIELDS) == []


# --- the frozen core ---------------------------------------------------------------


def test_the_core_digest_is_pinned() -> None:
    assert interface_digest([*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS]) == CORE_DIGEST


def test_the_digest_is_stable_across_input_order() -> None:
    shuffled = tuple(reversed([*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS]))
    assert interface_digest(shuffled) == CORE_DIGEST
    assert canonical_interface_document(shuffled) == canonical_interface_document(
        [*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS]
    )


def test_an_interface_and_a_version_sharing_a_name_do_not_collapse() -> None:
    """Both families order by qualified name, so each canonical form carries a
    `kind`. Without it a version named like its interface would digest as the
    same document."""
    interface = InterfaceDefinition(
        namespace="northwind", name="same", applies_to_type="core:capability", profile_revision="core-1"
    )
    version = InterfaceVersionDefinition(
        namespace="northwind",
        name="same",
        version="1.0.0",
        version_of="northwind:same",
        compatibility="breaking",
    )
    assert interface.canonical() != version.canonical()
    assert interface_digest([interface]) != interface_digest([version])


def test_every_core_interface_resolves_its_type_context() -> None:
    for definition in CORE_INTERFACE_DEFINITIONS:
        assert definition.applies_to_type in CORE_TYPES_BY_QUALIFIED


def test_every_core_version_names_an_interface_the_core_defines() -> None:
    published = {definition.qualified for definition in CORE_INTERFACE_DEFINITIONS}
    for version in CORE_INTERFACE_VERSIONS:
        assert version.version_of in published


def test_composed_interfaces_order_canonically() -> None:
    composed = ComposedInterfaces.of(tuple(reversed([*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS])))
    assert composed.digest == CORE_DIGEST


# --- definition-level rules ---------------------------------------------------------


def test_an_interface_without_a_profile_revision_is_refused() -> None:
    """An interface checked against no stated revision is one whose compatibility
    verdict cannot be reproduced."""
    with pytest.raises(ProfileDefinitionError, match="profile revision is required"):
        InterfaceDefinition(
            namespace="northwind", name="orders_read", applies_to_type="core:capability", profile_revision="  "
        )


def test_an_unqualified_type_context_is_refused() -> None:
    with pytest.raises(ProfileDefinitionError, match="qualified"):
        InterfaceDefinition(
            namespace="northwind", name="orders_read", applies_to_type="capability", profile_revision="core-1"
        )


def test_an_unknown_compatibility_is_refused() -> None:
    with pytest.raises(ProfileDefinitionError, match="unknown compatibility"):
        InterfaceVersionDefinition(
            namespace="northwind",
            name="orders_read_v1",
            version="1.0.0",
            version_of="northwind:orders_read",
            compatibility="probably_fine",
        )


def test_the_compatibility_vocabulary_is_closed() -> None:
    assert set(COMPATIBILITIES) == {"backward_compatible", "breaking", "deprecating"}


def test_an_extension_point_may_not_name_an_existing_property() -> None:
    with pytest.raises(ProfileDefinitionError, match="both a core property and an extension point"):
        InterfaceDefinition(
            namespace="northwind",
            name="orders_read",
            applies_to_type="core:capability",
            profile_revision="core-1",
            properties=(PropertyDefinition(name="classification", value_type="string"),),
            extension_points=("classification",),
        )
