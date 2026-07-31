"""Every ARC audit event has a constant, and the taxonomy is consistent.

The existing AST gate (`tests/conformance/test_audit_action_vocabulary.py`) fails
on a bare string literal in an `action=` kwarg, so a constant must exist before
the emitting code can be written. These assertions cover what that gate cannot:
that the set is complete against the architecture's event list, and that the
names follow one taxonomy.
"""

from __future__ import annotations

from registry.audit import actions

# The audit events the architecture overview enumerates. Anything ARC emits that
# is missing here is an event nobody can grep for.
_REQUIRED = {
    "arc.challenge.issued",
    "arc.challenge.consumed",
    "arc.challenge.expired",
    "arc.context.resolved",
    "arc.context.blocked",
    "arc.context.degraded",
    "arc.jit.granted",
    "arc.jit.denied",
    "arc.artifact.registered",
    "arc.artifact.activated",
    "arc.artifact.revoked",
    "arc.receipt.integrity_failed",
}


def _arc_actions() -> dict[str, str]:
    return {name: getattr(actions, name) for name in actions.__all__ if name.startswith("ARC_")}


def test_every_architecture_audit_event_has_a_constant() -> None:
    values = set(_arc_actions().values())
    assert _REQUIRED <= values, f"missing: {sorted(_REQUIRED - values)}"


def test_all_arc_constants_are_exported() -> None:
    """A constant absent from __all__ is invisible to the vocabulary gate."""
    exported = set(actions.__all__)
    declared = {n for n in dir(actions) if n.startswith("ARC_")}
    assert declared <= exported, f"not exported: {sorted(declared - exported)}"


def test_arc_actions_are_namespaced() -> None:
    """Namespacing is what keeps an ARC event distinguishable in a shared log."""
    for name, value in _arc_actions().items():
        assert value.startswith("arc."), f"{name} = {value!r} is not arc-namespaced"


def test_arc_actions_follow_the_noun_verb_taxonomy() -> None:
    """`arc.<noun>.<verb>` — the registry-wide convention, at least three parts."""
    for name, value in _arc_actions().items():
        assert len(value.split(".")) >= 3, f"{name} = {value!r} is not noun.verb"
        assert value == value.lower(), f"{name} = {value!r} must be lowercase"
        assert " " not in value, f"{name} = {value!r} must not contain spaces"


def test_no_duplicate_action_values() -> None:
    """Two names for one value make a count by action silently wrong."""
    values = list(_arc_actions().values())
    assert len(values) == len(set(values)), "duplicate ARC action value"


def test_arc_actions_do_not_collide_with_existing_ones() -> None:
    non_arc = {getattr(actions, n) for n in actions.__all__ if not n.startswith("ARC_")}
    assert not (set(_arc_actions().values()) & non_arc)
