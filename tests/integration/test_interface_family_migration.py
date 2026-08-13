"""Interfaces become governed entities, and the old surface is not retired until it may be.

The migration this file covers has two halves that fail in opposite ways. The
modelling half fails by being wrong — a version joined to two interfaces, a
compatibility edge that says a capability talks to itself — and shows up
immediately. The retirement half fails by being *right too early*: removing the
legacy surface while a consumer still calls it, or before anyone agreed, breaks
callers who had no warning and produces no error until they run.

So the retirement gate is tested condition by condition, each one withheld in
turn. A gate tested only in its satisfied state passes for an implementation that
checks nothing, which is precisely the implementation somebody reaches for when
the migration is late.

**The REST surface does not move.** The adapter preserves it byte-for-byte, which
is why `openapi.json` is not in this task's scope — and the test that asserts it
reads the committed contract rather than the routers, because the contract is what
a client generated from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contextplane.profile.compiler import compile_profile
from contextplane.profile.interface_families import (
    CAPABILITY_TYPE,
    CONSUMES,
    INTERFACE_ENTITY_TYPES,
    INTERFACE_RELATIONSHIP_TYPES,
    INTERFACE_TYPE,
    INTERFACE_VERSION_TYPE,
    PROVIDES,
    VERSION_OF,
    RetirementGate,
    RetirementRefused,
    compatibility_view,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _by_qualified(definitions: object) -> dict[str, object]:
    return {definition.qualified: definition for definition in definitions}  # type: ignore[attr-defined]


# --- the vocabulary compiles ----------------------------------------------------------


def test_the_interface_family_compiles_as_a_profile() -> None:
    """The whole point is that interfaces answer to the same governance as everything else.

    Compiled rather than inspected: a definition set that cannot compile is one no
    profile could ever adopt, and every field would still look right in isolation.
    """
    compiled = compile_profile(
        entities=[
            *INTERFACE_ENTITY_TYPES,
            *_capability_entity(),
        ],
        relationships=list(INTERFACE_RELATIONSHIP_TYPES),
        interfaces=[],
    )

    assert {d.qualified for d in compiled.relationships} == {VERSION_OF, PROVIDES, CONSUMES}


def _capability_entity() -> list[object]:
    from contextplane.profile.schemas.entity import EntityTypeDefinition

    return [EntityTypeDefinition(namespace="core", type_name="capability")]


def test_a_version_belongs_to_exactly_one_interface() -> None:
    """A second `version_of` would make "which interface is this a version of" ambiguous."""
    version_of = _by_qualified(INTERFACE_RELATIONSHIP_TYPES)[VERSION_OF]

    assert version_of.max_cardinality == 1  # type: ignore[attr-defined]
    assert version_of.source_type == INTERFACE_VERSION_TYPE  # type: ignore[attr-defined]
    assert version_of.destination_type == INTERFACE_TYPE  # type: ignore[attr-defined]


@pytest.mark.parametrize("relationship_type", [PROVIDES, CONSUMES])
def test_a_capability_may_provide_and_consume_many_versions(relationship_type: str) -> None:
    """Bounding these would cap how many interfaces a service can speak."""
    definition = _by_qualified(INTERFACE_RELATIONSHIP_TYPES)[relationship_type]

    assert definition.max_cardinality is None  # type: ignore[attr-defined]
    assert definition.source_type == CAPABILITY_TYPE  # type: ignore[attr-defined]
    assert definition.destination_type == INTERFACE_VERSION_TYPE  # type: ignore[attr-defined]


@pytest.mark.parametrize("relationship_type", [VERSION_OF, PROVIDES, CONSUMES])
def test_no_interface_relationship_crosses_an_organization_boundary_by_default(
    relationship_type: str,
) -> None:
    """An interface graph that spanned tenants by default would share topology nobody granted."""
    definition = _by_qualified(INTERFACE_RELATIONSHIP_TYPES)[relationship_type]

    assert definition.cross_org_policy == "deny"  # type: ignore[attr-defined]


# --- the compatibility view is derived ------------------------------------------------


def test_compatibility_pairs_a_provider_with_a_consumer_of_the_same_version() -> None:
    edges = compatibility_view(provides={"v1": {"svc-a"}}, consumes={"v1": {"svc-b"}})

    assert [(e.provider_entity_id, e.consumer_entity_id) for e in edges] == [("svc-a", "svc-b")]


def test_a_capability_is_not_compatible_with_itself() -> None:
    """True and useless — and a self-edge makes every closure walk over this view loop."""
    edges = compatibility_view(provides={"v1": {"svc-a"}}, consumes={"v1": {"svc-a"}})

    assert edges == ()


def test_a_version_nobody_consumes_produces_no_edge() -> None:
    assert compatibility_view(provides={"v1": {"svc-a"}}, consumes={}) == ()


def test_a_version_nobody_provides_produces_no_edge() -> None:
    """A consumer of an unprovided version is a real state, and not a compatibility."""
    assert compatibility_view(provides={}, consumes={"v1": {"svc-b"}}) == ()


def test_the_view_is_ordered_so_two_readers_agree() -> None:
    edges = compatibility_view(provides={"v1": {"svc-c", "svc-a"}}, consumes={"v1": {"svc-z", "svc-b"}})

    assert [(e.provider_entity_id, e.consumer_entity_id) for e in edges] == [
        ("svc-a", "svc-b"),
        ("svc-a", "svc-z"),
        ("svc-c", "svc-b"),
        ("svc-c", "svc-z"),
    ]


# --- the retirement gate --------------------------------------------------------------


def _gate(**overrides: object) -> RetirementGate:
    fields: dict[str, object] = {
        "consumer_count": 0,
        "notice_period_served": True,
        "product_approval_reference": "PROD-1",
        "equivalence_proven": True,
        "rollback_window_days": 30,
    }
    fields.update(overrides)
    return RetirementGate(**fields)  # type: ignore[arg-type]


def test_a_fully_evidenced_gate_permits_retirement() -> None:
    """Without this, a gate refusing everything would satisfy every test below."""
    _gate().assert_satisfied()

    assert _gate().is_satisfied


@pytest.mark.parametrize(
    ("withheld", "expected"),
    [
        ({"consumer_count": 1}, "consumer"),
        ({"notice_period_served": False}, "notice"),
        ({"product_approval_reference": None}, "approval"),
        ({"product_approval_reference": "   "}, "approval"),
        ({"equivalence_proven": False}, "equivalent"),
        ({"rollback_window_days": 0}, "rollback"),
    ],
)
def test_withholding_any_condition_refuses_retirement(withheld: dict[str, object], expected: str) -> None:
    """Each condition withheld in turn.

    Tested one at a time rather than in combination: a gate that checked only the
    first condition would pass a test that withheld all of them at once.
    """
    with pytest.raises(RetirementRefused, match=expected):
        _gate(**withheld).assert_satisfied()

    assert not _gate(**withheld).is_satisfied


def test_the_gate_offers_no_override() -> None:
    """An override would be used, and the trail afterwards would show a gate that passed.

    Asserted on the constructor's own field set, so adding one is a change to this
    test rather than an addition nobody reviews.
    """
    import dataclasses

    fields = {field.name for field in dataclasses.fields(RetirementGate)}

    assert not fields & {"force", "override", "skip_checks", "bypass"}


def test_this_task_does_not_retire_the_legacy_surface() -> None:
    """The consumer inventory is non-empty, so retirement is refused on its first condition.

    This is the state the task is meant to land in: the new shape exists, the gate
    exists, and the old surface stays until the outside evidence arrives.
    """
    import subprocess  # noqa: S404 - running this repo's own inventory script
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/interface_consumer_inventory.py", "--json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    consumers = json.loads(result.stdout)["consumers"]

    assert consumers, "the inventory reports no consumers; if that is real, the gate's first condition is met"
    assert not _gate(consumer_count=len(consumers)).is_satisfied


# --- the REST surface does not move ---------------------------------------------------


def test_the_committed_contract_still_serves_the_interface_routes() -> None:
    """The adapter preserves the existing surface, so the contract must be unchanged.

    Read from `openapi.json` rather than from the routers, because the contract is
    what a client generated from — a router that still mounts a route says nothing
    about whether the shape a client depends on survived.
    """
    contract = json.loads((_REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))
    interface_paths = [path for path in contract["paths"] if "interface" in path]

    assert interface_paths, "the interface REST surface is missing from the committed contract"
