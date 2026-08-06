"""Byte-for-byte agreement between the two canonicalization engines.

`registry.arc.schemas.canonical` and `registry.arc.schemas.authoring_profiles`
are two independently written canonicalizers living side by side: the first
serves five original ARC profiles, the second serves sixteen authoring
profiles. Both are documented as enforcing the same rules -- NFC-only
strings, no embedded NUL, integral-only numbers, lexicographically sorted
object keys, and identical compact `ensure_ascii=False`, `(",", ":")`-
separated UTF-8 encoding -- because every digest, the `S -> R -> A` chain,
and every signature in this subsystem is a function of canonical bytes. Two
profile families disagreeing about what the same content hashes to would be
invisible everywhere else: the sixteen-profile vector suite only exercises
`authoring_profiles.py`, and nothing before this file compared the two
engines to each other at all.

Each side is reached through its own private primitive rather than through
a public per-profile function, because a public function also enforces one
profile's specific closed field set -- exactly the thing this test does not
want to hold constant. `_canonical_side` calls `canonical._canonical` plus
`canonical._serialize` directly: that engine takes no schema at all, only a
Python value, so it needs no adapter beyond catching its own exception type.
`_authoring_side` calls `authoring_profiles._check_and_canonicalize` plus
`authoring_profiles._serialize`, driven by a schema `_infer_schema` builds
from the corpus value's own shape rather than from any one profile's real
`schema.json` -- the two adapters are deliberately separate call paths, or
comparing them would prove nothing.

One rule in the corpus below does not agree, and this file says so rather
than hiding it: `canonical.py` has no notion of a set-valued array at all,
so it never rejects or reorders a duplicate array entry, while
`authoring_profiles.py`'s schema explicitly labels every array `set` or
`ordered` and deduplicates+sorts a `set`-labelled one. See
`test_set_array_dedup_is_a_documented_asymmetry` below and the note in both
modules' docstrings.
"""

from __future__ import annotations

import unicodedata
from typing import Any

import pytest

from registry.arc.schemas import authoring_profiles, canonical
from registry.types import JSONValue

# ---------------------------------------------------------------------------
# Adapters. Kept intentionally separate -- see the module docstring.
# ---------------------------------------------------------------------------


def _canonical_side(value: JSONValue) -> tuple[str, bytes | None]:
    """Run `value` through `canonical.py`'s shared engine: no schema, no
    per-profile field list, just the exact `_canonical` + `_serialize` pair
    every one of its five profiles builds on."""
    try:
        return "accept", canonical._serialize(canonical._canonical(value))
    except canonical.CanonicalizationError:
        return "refuse", None


def _infer_schema(value: JSONValue, *, array_kind: str = "ordered") -> dict[str, Any]:
    """Build the smallest `authoring_profiles.py` schema that accepts
    exactly the shape of `value`: the same type at every node, every
    object key required, no enum/pattern/const constraint. `canonical.py`
    needs no such schema (it infers behaviour purely from the Python
    value's own type); this stands in for the schema it does not need, so
    the corpus below can drive both engines from the same input value.
    Assumes a list's elements all share one shape, true of every corpus
    entry this file uses.
    """
    if value is None:
        return {"type": ["null"]}
    if isinstance(value, bool):
        return {"type": ["boolean"]}
    if isinstance(value, str):
        return {"type": ["string"]}
    if isinstance(value, int | float):
        return {"type": ["number"]}
    if isinstance(value, list):
        items = _infer_schema(value[0], array_kind=array_kind) if value else {"type": []}
        return {"type": ["array"], "items": items, "x-array-kind": array_kind}
    if isinstance(value, dict):
        properties = {key: _infer_schema(v, array_kind=array_kind) for key, v in value.items()}
        return {"type": ["object"], "properties": properties, "required": tuple(value)}
    raise TypeError(f"corpus value has no JSON-shaped type: {type(value).__name__}")


def _authoring_side(value: JSONValue, *, array_kind: str = "ordered") -> tuple[str, bytes | None]:
    """Run `value` through `authoring_profiles.py`'s shared engine: the
    inferred schema above drives the exact `_check_and_canonicalize` +
    `_serialize` pair every one of its sixteen profiles builds on."""
    schema = _infer_schema(value, array_kind=array_kind)
    try:
        canon = authoring_profiles._check_and_canonicalize(schema, value)
        return "accept", authoring_profiles._serialize(canon)
    except authoring_profiles.AuthoringProfileError:
        return "refuse", None


def _deeply_nested(depth: int) -> JSONValue:
    value: JSONValue = "leaf"
    for level in range(depth):
        value = {f"level_{level}": value}
    return value


# ---------------------------------------------------------------------------
# Corpus. One entry per canonicalization rule both engines document as
# shared -- the set-valued-array rule is deliberately not in this list; see
# `test_set_array_dedup_is_a_documented_asymmetry` below.
# ---------------------------------------------------------------------------

_AGREEMENT_CASES: list[tuple[str, JSONValue, str]] = [
    (
        "non_nfc_text",
        {"note": unicodedata.normalize("NFD", "café")},
        "refuse",
    ),
    (
        "embedded_nul",
        {"note": "abc\x00def"},
        "refuse",
    ),
    (
        "fractional_number",
        {"amount": 1.5},
        "refuse",
    ),
    (
        "unsorted_keys_recursive",
        {"zebra": 1, "apple": {"delta": 2, "bravo": 3}, "middle": "text"},
        "accept",
    ),
    (
        "unicode_above_bmp",
        {"emoji": "rocket \U0001f680 twice \U0001f680"},
        "accept",
    ),
    (
        "deeply_nested_objects",
        _deeply_nested(40),
        "accept",
    ),
    (
        "empty_containers",
        {"list": [], "obj": {}, "maybe": None},
        "accept",
    ),
    (
        "integer_float_boundary",
        {
            "big_int": 9007199254740993,
            "int_like_float": 2.0,
            "neg_zero_float": -0.0,
            "zero": 0,
            "true_flag": True,
            "false_flag": False,
            "sci_notation_float": 1e10,
        },
        "accept",
    ),
]


@pytest.mark.parametrize("case_id,value,expected_decision", _AGREEMENT_CASES, ids=[c[0] for c in _AGREEMENT_CASES])
def test_primitives_agree(case_id: str, value: JSONValue, expected_decision: str) -> None:
    """Both engines reach the documented decision, and when both accept,
    the canonical bytes are identical -- not merely "both valid JSON"."""
    canon_decision, canon_bytes = _canonical_side(value)
    auth_decision, auth_bytes = _authoring_side(value)

    assert canon_decision == expected_decision, f"{case_id}: canonical.py decided {canon_decision!r}"
    assert auth_decision == expected_decision, f"{case_id}: authoring_profiles.py decided {auth_decision!r}"

    if expected_decision == "accept":
        assert canon_bytes == auth_bytes, f"{case_id}: canonical bytes diverge: {canon_bytes!r} != {auth_bytes!r}"


def test_set_array_dedup_is_a_documented_asymmetry() -> None:
    """A genuine, discovered divergence -- reported here, not papered over.

    `canonical.py`'s shared engine has no per-field array-kind concept at
    all: every list it canonicalizes is emitted in whatever order the
    caller supplied, duplicates untouched, because none of its five
    profiles' array fields is canonicalized with set semantics today.
    `authoring_profiles.py`'s schema-driven engine requires every array to
    be labelled `set` or `ordered`, and deduplicates+sorts a `set`-labelled
    one, rejecting a duplicate entry outright.

    Fed the identical raw value, the two engines therefore reach different
    decisions. Teaching `canonical.py`'s generic list handling the same
    set-vs-ordered distinction would change its accepted byte output for
    any existing caller whose array field happens to contain a duplicate
    today -- exactly the kind of behavior change this comparison exists to
    catch, not to make. This test is what keeps the asymmetry from being
    silently forgotten, silently assumed away, or silently "fixed" into a
    different bug; closing it for real is a deliberate follow-up with its
    own migration/compatibility review, not a side effect of this file.
    """
    value: JSONValue = {"items": [1, 1, 2]}

    canon_decision, canon_bytes = _canonical_side(value)
    auth_decision, _ = _authoring_side(value, array_kind="set")

    assert canon_decision == "accept", "canonical.py has no set-array concept; it should accept the duplicate as-is"
    assert canon_bytes == b'{"items":[1,1,2]}'
    assert auth_decision == "refuse", "authoring_profiles.py's set-kind engine should reject the duplicate entry"
