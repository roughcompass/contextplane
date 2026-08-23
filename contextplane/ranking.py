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

**And validation-gated means it cannot serve, not that a check disapproves.** An
entry marked `requires_validated: true` whose status is anything but `validated`
refuses the whole registry at import, so a process holding one does not start.
`scripts/check_governed_magnitudes.py` enforces the same rule on the artifact,
and both are wanted: the gate protects the review, this protects the run, and a
gate is a thing somebody can skip. The rule is deliberately not lazy -- it does
not wait for an accessor to ask -- because "did any code path happen to read
this magnitude on this deployment" is not something a governance guarantee
should depend on.

This is the same shape as `assert_drafter_decision_permits_serving`, which
refuses to boot when a runtime flag claims more than a committed decision
artifact earned, and it is deliberately reached without a feature-flag
mechanism: reading the number *is* the activation.
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
    "threshold",
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
_VALIDATION_STATUSES: Final = frozenset({"validated", "derived", "grandfathered"})

#: The statuses that satisfy a consumer's `requires_validated`. `derived`
#: qualifies and `grandfathered` never does: a reproducible derivation is a
#: stronger warrant than a validation somebody ran once, while grandfathered
#: says plainly that nobody checked. Named rather than written as two
#: comparisons, so a fourth status cannot be added without somebody deciding
#: which side of this line it falls on.
_GATE_SATISFYING: Final = frozenset({"validated", "derived"})

#: What a `derived` status has to show. An acceptance-sampling parameter follows
#: by arithmetic from a stated defect rate and consumer's risk -- there is no
#: held-out result, because it is not a prediction. `validated` would mean
#: inventing a method and a result for a check nobody ran; `grandfathered` would
#: assert nobody checked, which is false the other way. The derivation *is* the
#: check, and these two fields are what make it reproducible by anybody with a
#: calculator.
_DERIVATION_FIELDS: Final = ("derived_from", "derivation")

#: The three shapes a magnitude may hold: a named weight map, an ordered ladder,
#: or a single number. One shape per form in `_FORMS`, which is why adding a
#: form is a change to this module rather than a field a branch fills in.
type Parameters = dict[str, float] | list[str] | float


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
        if status == "derived" and not all(str(validation.get(k) or "").strip() for k in _DERIVATION_FIELDS):
            # The mirror of the `validated` rule below, for the status whose
            # evidence is arithmetic rather than a measurement. A derivation
            # nobody can reproduce is a number with a nicer word on it.
            msg = (
                f"{model_id}: derived requires derived_from and derivation; "
                "a derivation nobody can reproduce is a number with a nicer word on it"
            )
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

        gated = bool(entry.get("requires_validated", False))
        if gated and status not in _GATE_SATISFYING:
            # The rule the whole `requires_validated` field exists for, enforced
            # here and not only by `scripts/check_governed_magnitudes.py`.
            #
            # The gate protects the artifact and this protects the process, and
            # the two are different failures: a gate is a thing somebody can
            # skip, and until this existed the running service was strictly more
            # permissive than the pipeline that reviewed it. That is backwards
            # for a module whose stated posture is that an unknown id raises and
            # an empty registry raises at import.
            #
            # It refuses the whole registry rather than just this entry, which
            # is the same shape as every other refusal above -- and it is what
            # makes the guarantee unconditional. Refusing lazily, at whichever
            # accessor happens to ask, would protect a magnitude only as far as
            # some code path reads it, and "was this read on this deployment"
            # is not a question a governance rule should depend on. Refusing at
            # load means the process does not start, because `_REGISTRY` is
            # bound at import and every consumer imports this module.
            msg = (
                f"{model_id}: requires_validated is true but the status is {status!r}. "
                "A magnitude whose consumer demands validation cannot serve on a number "
                "nobody checked -- record the evidence, record the derivation, or clear the flag"
            )
            raise UngovernedMagnitude(msg)

        loaded[model_id] = GovernedMagnitude(
            model_id=model_id,
            form=form,
            parameters=entry["parameters"],
            reason=entry["reason"],
            validation_status=status,
            requires_validated=gated,
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
    parameters = entry._parameters
    if not isinstance(parameters, list):
        # The guard `weights` always had and this one did not. It was invisible
        # while the only other shape was a dict -- iterating one yields its keys,
        # so a mistagged weights entry came back as a ladder of field names
        # rather than as an error. Adding `float` to the payload types is what
        # surfaced it, since a number is not iterable at all.
        msg = f"{model_id!r} is tagged 'ladder' but holds {type(parameters).__name__}"
        raise UngovernedMagnitude(msg)
    return tuple(str(v) for v in parameters)


def threshold(model_id: str) -> float:
    """The single governed number for *model_id* — a floor, ceiling or cutoff.

    `_FORMS` has admitted `threshold` since this module was written and nothing
    could read one, so the registry accepted a form it could not serve: an entry
    declaring it would load, and then be unreachable through either accessor.
    Found by the first review of ordering sites, which is also what produced the
    entries that now use it.

    Same shape refusal as the other two: the form tag and the payload are
    separate fields in a hand-edited artifact, so a `threshold` holding a map is
    refused rather than coerced.
    """
    entry = _entry(model_id, "threshold")
    parameters = entry._parameters
    if isinstance(parameters, dict | list):
        msg = f"{model_id!r} is tagged 'threshold' but holds {type(parameters).__name__}"
        raise UngovernedMagnitude(msg)
    return float(parameters)
