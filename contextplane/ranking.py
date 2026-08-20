"""The parameters that decide an order, and the only place they may live.

A weight, a threshold, a floor or a ladder position is a *governed magnitude*:
change it and results reorder, silently and everywhere. Held as a literal in the
module that uses it, such a number has no owner, no recorded reason and no way
for a reviewer to find its siblings. This module holds them instead, in a
committed artifact, so the set is enumerable and every entry carries the reason
it holds its value.

**What this governs, and what it deliberately does not.** It governs the
*parameters*. It does not attempt to detect the *act* of ordering: any sequence
of comparisons or partitions produces an order, so a mechanical closure over
"code that ranks" degenerates into a closure over all code. Three independent
designs for such a closure were attempted and each was defeated by separating
the arithmetic from the sort — the score computed in one function, the ordering
done elsewhere on a bare attribute. Governing the magnitudes is the part that
*is* closeable: a float literal in a weights position is a syntactic fact a gate
can find, where "this comparison is semantically a ranking" is not. The boundary
is stated rather than papered over, because a gate believed to be exhaustive is
worse than one known to be partial.

**Why it sits at the bottom of the import graph.** `pyproject.toml`'s
`[tool.importlinter]` contract is a flat layering with `exhaustive = true`, and
an upper layer may import a lower one but never the reverse. Governed magnitudes
are consumed from `service` (fusion weights), from `service/governance`
(authority ladders) and would be unreachable from anything below wherever else
they were placed. Only the bottom layer is legal *and* universal, so this module
imports nothing from `contextplane` at all — that is a constraint the contract
imposes, not a purity preference.

**Fail-closed and fail-empty.** An unknown model id raises. A registry whose
population is zero raises at import. The second rule matters as much as the
first: a gate whose population is empty and which reports success is the exact
failure `scripts/checklib.py` exists to prevent, and an empty registry would let
every governed magnitude be deleted while the mechanism still passed.
"""

from __future__ import annotations

import json
import pathlib
from typing import Final

__all__ = [
    "REGISTRY_PATH",
    "GovernedMagnitude",
    "UngovernedMagnitude",
    "ladder",
    "model_ids",
    "requires_validated",
    "validation_status",
    "weights",
]

REGISTRY_PATH: Final = pathlib.Path(__file__).with_name("ranking_registry.json")

#: The forms a governed magnitude may take. A fourth form is a deliberate change
#: to this module, not something a feature branch adds in passing: the ceiling on
#: expressiveness is what keeps the registry reviewable.
_FORMS: Final = frozenset({"weights", "ladder", "threshold"})

#: What an entry asserts about having been checked. `validated` records a check
#: that happened, with who ran it and what came out. `grandfathered` records
#: that none has, which is the honest state for behaviour that predates the
#: registry: an entry unable to say "nobody checked this" would have to claim
#: the opposite, and a registry that silently upgrades unexamined numbers to
#: examined ones is worse than no registry at all.
_VALIDATION_STATUSES: Final = frozenset({"validated", "grandfathered"})

#: The two shapes a magnitude may hold: a named weight map, or an ordered ladder.
type Parameters = dict[str, float] | list[str]


class UngovernedMagnitude(RuntimeError):
    """A magnitude was requested that the registry does not govern.

    Deliberately not a subclass of `contextplane.exceptions.RegistryError`: that
    module is a layer above this one, and importing it here would invert the
    import contract. Callers that need an HTTP status map it at their boundary.
    """


class GovernedMagnitude:
    """One registry entry, frozen after load."""

    __slots__ = ("_parameters", "form", "model_id", "reason", "requires_validated", "validation_status")

    def __init__(
        self,
        model_id: str,
        form: str,
        parameters: Parameters,
        reason: str,
        validation_status: str,
        requires_validated: bool,
    ) -> None:
        self.model_id = model_id
        self.form = form
        self.reason = reason
        self.validation_status = validation_status
        self.requires_validated = requires_validated
        self._parameters = parameters

    def __repr__(self) -> str:
        return f"GovernedMagnitude(model_id={self.model_id!r}, form={self.form!r})"


def _load() -> dict[str, GovernedMagnitude]:
    """Read the committed registry, refusing anything malformed or empty."""
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    entries = raw.get("magnitudes")
    if not isinstance(entries, list) or not entries:
        # An empty population is the failure this check exists to prevent, so it
        # is fatal rather than an empty mapping somebody later reads as "nothing
        # to govern".
        msg = f"{REGISTRY_PATH.name} governs no magnitudes; an empty registry is a defect, not a state"
        raise UngovernedMagnitude(msg)

    loaded: dict[str, GovernedMagnitude] = {}
    for entry in entries:
        model_id = entry["model_id"]
        form = entry["form"]
        if form not in _FORMS:
            msg = f"{model_id}: form {form!r} is not one of {sorted(_FORMS)}"
            raise UngovernedMagnitude(msg)
        if not entry.get("reason", "").strip():
            # A magnitude without a stated reason is the literal it replaced,
            # moved. The reason is the whole reason the registry is an
            # improvement over a constant in the module that uses it.
            msg = f"{model_id}: every governed magnitude states why it holds its value"
            raise UngovernedMagnitude(msg)
        if model_id in loaded:
            msg = f"{model_id}: declared twice"
            raise UngovernedMagnitude(msg)

        validation = entry.get("validation") or {}
        status = validation.get("status")
        if status not in _VALIDATION_STATUSES:
            # Absent is refused rather than defaulted. Defaulting to
            # `grandfathered` would let an entry omit the field and be quietly
            # exempt; defaulting to `validated` would assert a check nobody ran.
            msg = (
                f"{model_id}: validation.status is {status!r}; every magnitude declares "
                f"one of {sorted(_VALIDATION_STATUSES)} -- an entry that cannot say whether "
                "anybody checked it is the state this field exists to make impossible"
            )
            raise UngovernedMagnitude(msg)
        if status == "grandfathered" and not validation.get("reason", "").strip():
            msg = f"{model_id}: grandfathered without a stated reason"
            raise UngovernedMagnitude(msg)
        if status == "validated" and not all(
            validation.get(k) for k in ("validated_by", "validated_on", "method", "result")
        ):
            # A validated status is a claim about the world. Without who ran the
            # check, when, how and with what outcome, it is a word rather than
            # evidence, and the word is what a later reader would trust.
            msg = (
                f"{model_id}: validated requires validated_by, validated_on, method and result; "
                "a status without its evidence is an assertion nobody can check"
            )
            raise UngovernedMagnitude(msg)

        loaded[model_id] = GovernedMagnitude(
            model_id=model_id,
            form=form,
            parameters=entry["parameters"],
            reason=entry["reason"],
            validation_status=status,
            requires_validated=bool(entry.get("requires_validated", False)),
        )
    return loaded


_REGISTRY: Final[dict[str, GovernedMagnitude]] = _load()


def model_ids() -> tuple[str, ...]:
    """Every governed model id, sorted. The gate reads this to check coverage."""
    return tuple(sorted(_REGISTRY))


def validation_status(model_id: str) -> str:
    """Whether *model_id* has been independently validated, or is grandfathered."""
    entry = _REGISTRY.get(model_id)
    if entry is None:
        msg = f"{model_id!r} is not a governed magnitude"
        raise UngovernedMagnitude(msg)
    return entry.validation_status


def requires_validated(model_id: str) -> bool:
    """Whether a consuming feature flag may only activate on a validated value."""
    entry = _REGISTRY.get(model_id)
    if entry is None:
        msg = f"{model_id!r} is not a governed magnitude"
        raise UngovernedMagnitude(msg)
    return entry.requires_validated


def _entry(model_id: str, expected_form: str) -> GovernedMagnitude:
    entry = _REGISTRY.get(model_id)
    if entry is None:
        msg = f"{model_id!r} is not a governed magnitude; add it to {REGISTRY_PATH.name} with a reason"
        raise UngovernedMagnitude(msg)
    if entry.form != expected_form:
        msg = f"{model_id!r} is a {entry.form!r}, requested as {expected_form!r}"
        raise UngovernedMagnitude(msg)
    return entry


def weights(model_id: str) -> dict[str, float]:
    """The frozen weight map for *model_id*.

    A fresh dict each call: a caller that mutates its copy cannot reweight
    everyone else's ordering.
    """
    entry = _entry(model_id, "weights")
    parameters = entry._parameters
    if not isinstance(parameters, dict):
        # Unreachable while `_load` enforces form/shape agreement, but the form
        # tag and the payload are two separate fields in a hand-edited artifact,
        # so this refuses rather than trusting the tag.
        msg = f"{model_id!r} is tagged 'weights' but holds {type(parameters).__name__}"
        raise UngovernedMagnitude(msg)
    return {str(k): float(v) for k, v in parameters.items()}


def ladder(model_id: str) -> tuple[str, ...]:
    """The frozen ordered ladder for *model_id*, strongest first."""
    entry = _entry(model_id, "ladder")
    return tuple(str(v) for v in entry._parameters)
