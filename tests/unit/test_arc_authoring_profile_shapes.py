"""Unit tests for the authoring-surface profile *shapes* module
(`contextplane/arc/schemas/authoring_profile_shapes.py`): the small composable
schema-builder functions and the closed schemas they combine into.

This module carries no validation or canonicalization logic of its own --
`authoring_profiles.py` is the one that walks these dicts. Every schema in
this file is built at module import time (nothing here is constructed
lazily), so merely importing the module already exercises every statement;
that is exactly why the tests below are not import-only. They check that
each builder function actually produces the shape its name promises, and
that the two structural invariants `authoring_profiles.py`'s own docstring
states -- every declared property is required, and every array is labeled
`set` or `ordered` -- hold recursively for every profile and every
nested shape reachable from them, not just for one hand-picked example.

`tests/conformance/test_arc_authoring_vectors.py` and
`test_arc_authoring_schemas.py` check this module's *output* against a
fixture manifest and a wire-contract snapshot respectively; neither calls
these builder functions directly, and neither asserts the closed-field-set
or array-labeling invariant generically the way the tests below do -- this
is deliberately not a second copy of either.
"""

from __future__ import annotations

from typing import Any

from contextplane.arc.schemas import authoring_profile_shapes as shapes

# ---------------------------------------------------------------------------
# Scalar builders.
# ---------------------------------------------------------------------------


def test_const_schema_pins_the_exact_literal() -> None:
    assert shapes._const("arc_actor_separation_v1") == {"type": "string", "const": "arc_actor_separation_v1"}


def test_string_schema_has_no_constraint() -> None:
    assert shapes._string() == {"type": "string"}


def test_number_and_boolean_schemas() -> None:
    assert shapes._number() == {"type": "number"}
    assert shapes._boolean() == {"type": "boolean"}


def test_enum_schema_carries_the_exact_value_tuple() -> None:
    assert shapes._enum("a", "b", "c") == {"type": "string", "enum": ("a", "b", "c")}


def test_uuid_digest_and_timestamp_schemas_embed_their_named_pattern() -> None:
    assert shapes._uuid() == {"type": "string", "pattern": shapes._UUID_PATTERN}
    assert shapes._digest() == {"type": "string", "pattern": shapes._DIGEST_PATTERN}
    assert shapes._timestamp() == {"type": "string", "pattern": shapes._TIMESTAMP_PATTERN}


def test_uuid_pattern_matches_a_canonical_uuid_and_rejects_a_malformed_one() -> None:
    assert shapes._UUID_PATTERN.fullmatch("11111111-1111-1111-1111-111111111111") is not None
    assert shapes._UUID_PATTERN.fullmatch("11111111-1111-1111-1111-11111111111") is None  # one hex digit short
    assert shapes._UUID_PATTERN.fullmatch("11111111-1111-1111-1111-111111111111Z") is None  # trailing garbage


def test_digest_pattern_matches_sixty_four_lowercase_hex_and_rejects_uppercase_or_short() -> None:
    assert shapes._DIGEST_PATTERN.fullmatch("a" * 64) is not None
    assert shapes._DIGEST_PATTERN.fullmatch("A" * 64) is None
    assert shapes._DIGEST_PATTERN.fullmatch("a" * 63) is None


def test_timestamp_pattern_matches_utc_z_suffixed_and_rejects_a_numeric_offset() -> None:
    assert shapes._TIMESTAMP_PATTERN.fullmatch("2024-01-01T00:00:00Z") is not None
    assert shapes._TIMESTAMP_PATTERN.fullmatch("2024-01-01T00:00:00.123456Z") is not None
    assert shapes._TIMESTAMP_PATTERN.fullmatch("2024-01-01T00:00:00+00:00") is None


# ---------------------------------------------------------------------------
# Composite builders.
# ---------------------------------------------------------------------------


def test_nullable_widens_a_scalar_type_to_a_two_element_list() -> None:
    assert shapes._nullable(shapes._string()) == {"type": ["string", "null"]}


def test_nullable_preserves_the_wrapped_schemas_other_keys() -> None:
    widened = shapes._nullable(shapes._enum("x", "y"))
    assert widened["type"] == ["string", "null"]
    assert widened["enum"] == ("x", "y")


def test_array_records_kind_and_omits_absent_optional_keys() -> None:
    bare = shapes._array(shapes._string(), kind="set")
    assert bare == {"type": "array", "items": {"type": "string"}, "x-array-kind": "set"}
    assert "x-order-key" not in bare
    assert "minItems" not in bare


def test_array_records_order_key_and_min_items_when_given() -> None:
    full = shapes._array(shapes._string(), kind="ordered", order_key="item_id", min_items=1)
    assert full["x-order-key"] == "item_id"
    assert full["minItems"] == 1


def test_object_requires_every_declared_property_and_forbids_nothing_else() -> None:
    obj = shapes._object({"a": shapes._string(), "b": shapes._number()})
    assert obj["type"] == "object"
    assert obj["required"] == ("a", "b")
    assert set(obj["properties"]) == {"a", "b"}


def test_profile_prefixes_a_const_profile_field_onto_the_given_fields() -> None:
    schema = shapes._profile("arc_widget_v1", {"name": shapes._string()})
    assert schema["properties"]["profile"] == {"type": "string", "const": "arc_widget_v1"}
    assert set(schema["properties"]) == {"profile", "name"}
    assert schema["required"] == ("profile", "name")


# ---------------------------------------------------------------------------
# The published vocabularies these schemas draw their enum values from.
# ---------------------------------------------------------------------------


def test_risk_classifications_and_delta_codes_are_the_exact_published_tuples() -> None:
    """Both vocabularies spelled out, because the point of a published tuple
    is that changing it is visible. The two differ in exactly their last two
    members -- the scope ladder's narrowest rung -- and everything above
    that rung is identical, which is the property that makes the reducer's
    f"{scope}_{mandatory}" construction work under either."""
    assert shapes.RISK_CLASSIFICATIONS_V1 == (
        "global_mandatory",
        "global_non_mandatory",
        "tenant_mandatory",
        "tenant_non_mandatory",
        "domain_mandatory",
        "domain_non_mandatory",
        "entity_mandatory",
        "entity_non_mandatory",
        "task_mandatory",
        "task_non_mandatory",
    )
    assert shapes.RISK_CLASSIFICATIONS_V2 == (
        "global_mandatory",
        "global_non_mandatory",
        "tenant_mandatory",
        "tenant_non_mandatory",
        "domain_mandatory",
        "domain_non_mandatory",
        "entity_mandatory",
        "entity_non_mandatory",
        "intent_mandatory",
        "intent_non_mandatory",
    )
    # The unsuffixed name is the active one, and this is what says so.
    assert shapes.RISK_CLASSIFICATIONS == shapes.RISK_CLASSIFICATIONS_V2
    assert shapes.RISK_CLASSIFICATIONS_V1[:8] == shapes.RISK_CLASSIFICATIONS_V2[:8]
    assert shapes.DELTA_CODES == (
        "newly_selected",
        "no_longer_selected",
        "conflict_changed",
        "mandatory_block_added",
        "mandatory_block_removed",
    )


# ---------------------------------------------------------------------------
# Cross-registry invariants over every profile at once.
# ---------------------------------------------------------------------------

#: Spelled as strings, not built from `shapes`' own constants. A pin whose
#: members are read from the module under test follows that module wherever
#: it goes -- rebinding `ARTIFACT_SEMANTICS_PROFILE` from the v1 literal to
#: the v2 one would have moved this set silently and the assertion below
#: would still have passed. Written out, the same rebinding fails here,
#: which is the entire reason to have a published inventory.
_PROFILE_LITERALS = frozenset(
    {
        "arc_source_approval_claim_v1",
        "arc_source_verifier_attestation_v1",
        "arc_source_approval_evidence_v1",
        "arc_field_provenance_v1",
        "arc_approval_verifier_enrollment_v1",
        "arc_approval_provider_assertion_v1",
        "arc_operational_event_v1",
        "arc_observation_qualification_v1",
        "arc_observation_replay_corpus_v1",
        # The seven families the Intent rename split, both halves each.
        "arc_observation_class_predicate_v1",
        "arc_observation_class_predicate_v2",
        "arc_expected_impact_envelope_v1",
        "arc_expected_impact_envelope_v2",
        "arc_artifact_semantics_v1",
        "arc_artifact_semantics_v2",
        "arc_approval_review_package_v1",
        "arc_approval_review_package_v2",
        "arc_artifact_revision_v1",
        "arc_artifact_revision_v2",
        "arc_actor_separation_v1",
        "arc_actor_separation_v2",
        "arc_observation_cohort_v1",
        "arc_observation_cohort_v2",
    }
)

#: Which of them a new write may use. `arc_observation_qualification_v1` is
#: deliberately here: it carries the renamed classification inline but
#: nothing canonicalizes one, so it never gained a v2.
_ACTIVE_PROFILE_LITERALS = _PROFILE_LITERALS - {
    "arc_observation_class_predicate_v1",
    "arc_expected_impact_envelope_v1",
    "arc_artifact_semantics_v1",
    "arc_approval_review_package_v1",
    "arc_artifact_revision_v1",
    "arc_actor_separation_v1",
    "arc_observation_cohort_v1",
}


def test_authoring_profiles_and_schema_by_profile_agree_on_every_literal() -> None:
    assert len(_PROFILE_LITERALS) == 23
    assert shapes.AUTHORING_PROFILES == _PROFILE_LITERALS
    assert set(shapes.SCHEMA_BY_PROFILE) == _PROFILE_LITERALS


def test_only_the_active_half_of_a_split_family_is_writable() -> None:
    """Verifiable and writable are different sets, and this is what says by
    how much: seven frozen literals verify but cannot be authored."""
    assert shapes.ACTIVE_AUTHORING_PROFILES == _ACTIVE_PROFILE_LITERALS
    assert len(shapes.AUTHORING_PROFILES - shapes.ACTIVE_AUTHORING_PROFILES) == 7


def test_each_unsuffixed_alias_names_the_active_version() -> None:
    """The convention this module relies on, asserted rather than assumed:
    a call site saying `ARTIFACT_SEMANTICS_PROFILE` means "what we author
    today", so each alias must equal its own _V2."""
    for family in (
        "OBSERVATION_CLASS_PREDICATE",
        "EXPECTED_IMPACT_ENVELOPE",
        "ARTIFACT_SEMANTICS",
        "APPROVAL_REVIEW_PACKAGE",
        "ARTIFACT_REVISION",
        "ACTOR_SEPARATION",
        "OBSERVATION_COHORT",
    ):
        assert getattr(shapes, f"{family}_PROFILE") == getattr(shapes, f"{family}_V2_PROFILE"), family


def _assert_closed_and_every_array_labeled(schema: dict[str, Any], path: str) -> None:
    """Recursively asserts `authoring_profiles.py`'s two stated invariants:
    an object's `required` tuple is exactly its declared property set (never
    a subset), and every array carries a recognized `x-array-kind`. Walking
    this once, from each top-level schema down through every
    nested shape they embed, is what proves the invariant holds for the
    directive/applicability/envelope-item/provenance-summary/semantic-test/
    event-payload/delta-counter shapes too -- not only for the profiles that
    happen to be flat objects.
    """
    schema_type = schema.get("type")
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    if "object" in types:
        assert schema["required"] == tuple(schema["properties"]), path
        for name, prop in schema["properties"].items():
            _assert_closed_and_every_array_labeled(prop, f"{path}.{name}")
    if "array" in types:
        # `_check_and_canonicalize`'s array branch raises "array has no
        # set/ordered label" for any array schema missing this key -- this
        # is what would catch a newly added array field that forgot to
        # declare one before it ever reached that defensive branch at
        # runtime.
        assert schema.get("x-array-kind") in {"set", "ordered"}, path
        _assert_closed_and_every_array_labeled(schema["items"], f"{path}[]")


def test_every_profile_schema_is_closed_and_every_array_is_labeled_recursively() -> None:
    for literal, schema in shapes.SCHEMA_BY_PROFILE.items():
        assert schema["properties"]["profile"] == {"type": "string", "const": literal}, literal
        _assert_closed_and_every_array_labeled(schema, literal)
