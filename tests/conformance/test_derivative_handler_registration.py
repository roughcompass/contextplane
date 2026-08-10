"""Every derivative kind the schema stores has a handler in the shipped wiring.

The release gate, asserted against the composition a deployment actually runs
rather than against the set of handler classes that happen to exist. Those are
different questions, and only the second one matters: a handler written, tested
and never registered removes nothing, and the participant enqueues propagation
items for its kind regardless. The queue then grows work no handler can apply,
every item fails its way to `failed`, and `pending_overdue` counts them — which is
the fail-closed behaviour working correctly on a defect that a static check of
handler classes would have called covered.

So this builds the registry through `wiring.derivatives.build_handler_registry` —
the same function the scheduler calls, not a second assembly that mirrors it. A
pin that rebuilt the registry from its own list of families would agree with
itself forever while the deployment shipped a gap.

A predecessor of this gate read handler classes out of the source with an AST
sweep, looking for the kind each one declared. It could only see kinds spelled as
string literals, so the six handlers that declare theirs as `derivatives.KIND_*`
were invisible to it and the gate sat expected-to-fail while coverage was in fact
complete. That is the specific failure this shape is chosen against: ask the
registry, because the registry is what the drain asks.
"""

from __future__ import annotations

from contextplane.retention import derivatives, tombstones
from contextplane.wiring.derivatives import build_handler_registry

#: No key material, deliberately. Building the registry must not need a configured
#: deployment: the receipt handler takes a salt resolver but only resolves a salt
#: when it applies work to a real receipt, and a gate that required real keys would
#: be a gate that only runs where keys are configured.
_UNCONFIGURED_SALTS = tombstones.KeyedTenantSalt({}, active_key_id=None)


def test_the_shipped_wiring_registers_a_handler_for_every_kind() -> None:
    """Nothing unhandled — the assertion a release is allowed to depend on.

    Stated as the empty tuple rather than a count, so the failure message names
    the kinds that are missing instead of reporting that two numbers differ.
    """
    registry = build_handler_registry(_UNCONFIGURED_SALTS)

    assert registry.unhandled_kinds() == (), (
        f"derivative kinds with no registered handler: {registry.unhandled_kinds()}. "
        "Each is an artefact holding erased content that nothing will remove, while "
        "the erasure participants enqueue propagation work for it regardless — so the "
        "queue accumulates items that fail, and every read that fails closed on "
        "overdue work stays closed."
    )


def test_the_registry_covers_the_schemas_kinds_exactly_and_in_its_order() -> None:
    """Exact and ordered, not a superset check.

    `kinds` is reported in the schema's own declaration order, so pinning the
    whole tuple catches a kind added to `DERIVATIVE_KINDS` without a handler *and*
    a handler registered for a kind the schema will not store — the second of
    which the registry refuses at registration, making this the pin that would
    notice if that refusal were ever loosened.
    """
    registry = build_handler_registry(_UNCONFIGURED_SALTS)

    assert registry.kinds == derivatives.DERIVATIVE_KINDS


def test_every_registered_kind_resolves_to_a_versioned_handler() -> None:
    """A registration is only useful if the drain can get the handler back out.

    Every derivative registration records the handler version that wrote it, so a
    handler reachable but unversioned would produce rows nobody can later judge
    rebuildable. Cheap to assert here and invisible until an artefact needs
    rebuilding otherwise.
    """
    registry = build_handler_registry(_UNCONFIGURED_SALTS)

    for kind in derivatives.DERIVATIVE_KINDS:
        handler = registry.handler_for(kind)
        assert handler.kind == kind
        assert handler.version, f"the handler for {kind!r} registers no version"
