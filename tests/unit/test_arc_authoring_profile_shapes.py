"""Unit tests for the authoring-surface profile *shapes* module
(`registry/arc/schemas/authoring_profile_shapes.py`): the small composable
schema-builder functions and the sixteen closed schemas they combine into.

This module carries no validation or canonicalization logic of its own --
`authoring_profiles.py` is the one that walks these dicts. Every schema in
this file is built at module import time (nothing here is constructed
lazily), so merely importing the module already exercises every statement;
that is exactly why the tests below are not import-only. They check that
each builder function actually produces the shape its name promises, and
that the two structural invariants `authoring_profiles.py`'s own docstring
states -- every declared property is required, and every array is labeled
`set` or `ordered` -- hold recursively for all sixteen profiles and every
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

from registry.arc.schemas import authoring_profile_shapes as shapes

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
    assert shapes.RISK_CLASSIFICATIONS == (
        "global_mandatory",
        "global_non_mandatory",
        "tenant_mandatory",
        "tenant_non_mandatory",
        "domain_mandatory",
        "domain_non_mandatory",
        "capability_mandatory",
        "capability_non_mandatory",
        "task_mandatory",
        "task_non_mandatory",
    )
    assert shapes.DELTA_CODES == (
        "newly_selected",
        "no_longer_selected",
        "conflict_changed",
        "mandatory_block_added",
        "mandatory_block_removed",
    )


# ---------------------------------------------------------------------------
# Cross-registry invariants over all sixteen profiles at once.
# ---------------------------------------------------------------------------

_SIXTEEN_PROFILE_LITERALS = frozenset(
    {
        shapes.SOURCE_APPROVAL_CLAIM_PROFILE,
        shapes.SOURCE_VERIFIER_ATTESTATION_PROFILE,
        shapes.SOURCE_APPROVAL_EVIDENCE_PROFILE,
        shapes.OBSERVATION_CLASS_PREDICATE_PROFILE,
        shapes.EXPECTED_IMPACT_ENVELOPE_PROFILE,
        shapes.FIELD_PROVENANCE_PROFILE,
        shapes.ARTIFACT_SEMANTICS_PROFILE,
        shapes.APPROVAL_REVIEW_PACKAGE_PROFILE,
        shapes.ARTIFACT_REVISION_PROFILE,
        shapes.ACTOR_SEPARATION_PROFILE,
        shapes.APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
        shapes.APPROVAL_PROVIDER_ASSERTION_PROFILE,
        shapes.OPERATIONAL_EVENT_PROFILE,
        shapes.OBSERVATION_COHORT_PROFILE,
        shapes.OBSERVATION_QUALIFICATION_PROFILE,
        shapes.OBSERVATION_REPLAY_CORPUS_PROFILE,
    }
)


def test_authoring_profiles_and_schema_by_profile_agree_on_the_sixteen_literals() -> None:
    assert len(_SIXTEEN_PROFILE_LITERALS) == 16
    assert shapes.AUTHORING_PROFILES == _SIXTEEN_PROFILE_LITERALS
    assert set(shapes.SCHEMA_BY_PROFILE) == _SIXTEEN_PROFILE_LITERALS


def _assert_closed_and_every_array_labeled(schema: dict[str, Any], path: str) -> None:
    """Recursively asserts `authoring_profiles.py`'s two stated invariants:
    an object's `required` tuple is exactly its declared property set (never
    a subset), and every array carries a recognized `x-array-kind`. Walking
    this once, from each of the sixteen top-level schemas down through every
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
