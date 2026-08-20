"""Resolving a scoring magnitude for one tenant: bound extension, else core.

ADR 0004 decided where scoring configuration lives — the committed registry holds
the core default, and a tenant overrides by publishing a profile extension bound
through `plan → validate → activate → rollback`. It also recorded why the
resolver cannot live next to the registry: `contextplane/ranking.py` sits at the
bottom of the import graph and cannot reach the profile system, which is far
above it. So the accessor lives here, beside the services that own bindings.

**One accessor, and every scoring consumer goes through it.** A consumer reading
`ranking.weights(...)` directly gets the core value and silently ignores the
tenant's override — which looks exactly like a tenant whose override did not take
effect, and is indistinguishable from one at every layer above. That is the
failure this module exists to make impossible to write by accident, and it is why
`resolve_weights` returns the core value itself rather than `None`: a caller that
has to fall back manually is a caller who will forget.

**The bound set is read, not inferred.** `profile_binding_extensions` names the
extensions each binding activated, and the resolver joins through it. The first
implementation could not: the binding stored only `extension_set_digest`, so the
accessor enumerated the tenant's extensions against the bound core revision and
checked the digest matched. That is wrong in a way that looks right — enumeration
also finds extensions the tenant published and never bound, so a tenant with one
bound extension and one shelved one produced a digest mismatch and got refused
for having an ordinary configuration. The rollback case is what caught it, which
is the case the lifecycle exists for.

The digest is still checked, and it now does the job it was always suited to: an
integrity check over a set the schema can state. A mismatch means the membership
rows and the digest disagree about what was bound, which is a corrupted binding
rather than a tenant's ordinary state, and the resolver refuses rather than
picking one of the two to believe.

**A tenant with no active binding resolves to core, and that is not an error.**
Most tenants have no extension and never will. `unbound` is the common case, not
a degraded one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane import ranking
from contextplane.profile.bindings import extension_set_digest

#: The extension namespace scoring overrides live in. One namespace rather than
#: one per magnitude: a tenant reweighting salience and retrieval together is
#: making one governance decision, and two extensions would let half of it
#: activate.
SCORING_NAMESPACE: Final = "scoring"

#: Where a resolved value came from, carried with it. A caller logging "tenant X
#: scored with weights W" and unable to say whether W was theirs or the default
#: has recorded the number and lost the fact that matters.
SOURCE_CORE: Final = "core"
SOURCE_EXTENSION: Final = "extension"


class ScoringOverrideRefused(RuntimeError):
    """A scoring override could not be established, published, or trusted."""


#: Mirrors `ranking.py`'s rule, and mirrors its reasoning too: a magnitude
#: without a stated reason is the literal it replaced, moved. A tenant override
#: is held to the same bar as the committed core precisely because it is easier
#: to publish than the core is to change -- if anything, the looser artifact
#: needs the tighter rule.
MIN_REASON_WORDS: Final = 20


def validate_overrides(overrides: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """Check a tenant's proposed overrides against everything the registry checks.

    Refuses an unknown magnitude, a payload whose keys do not match the core's,
    a non-numeric weight, a set that does not sum to one, and a reason under
    twenty words. Returns the weight maps, keyed by magnitude, ready to store.

    **The key set must match the core exactly.** A partial override -- naming
    three of six weights -- would leave the other three at their core values
    while summing to something that is not one, so a tenant would silently be
    scoring on a scale nobody designed. Naming all six is more typing and it is
    the only form whose meaning is unambiguous.
    """
    validated: dict[str, dict[str, float]] = {}
    for model_id, payload in overrides.items():
        # Refused by the registry itself rather than by a second list here. Two
        # places deciding which magnitudes exist is two registries.
        core = ranking.weights(model_id)

        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping) or not parameters:
            msg = f"{model_id}: an override carries a non-empty `parameters` map"
            raise ScoringOverrideRefused(msg)

        if set(parameters) != set(core):
            missing = sorted(set(core) - set(parameters))
            unknown = sorted(set(parameters) - set(core))
            msg = (
                f"{model_id}: an override names every weight the core names, exactly. "
                f"missing={missing} unknown={unknown}. A partial override leaves the rest at core "
                "values and sums to something that is not one, which is a scale nobody designed."
            )
            raise ScoringOverrideRefused(msg)

        try:
            weights = {str(k): float(v) for k, v in parameters.items()}
        except (TypeError, ValueError) as bad:
            msg = f"{model_id}: every weight is a number; got {parameters!r}"
            raise ScoringOverrideRefused(msg) from bad

        if any(value < 0 for value in weights.values()):
            msg = f"{model_id}: a negative weight inverts the signal it names rather than lowering it"
            raise ScoringOverrideRefused(msg)
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            msg = (
                f"{model_id}: weights sum to {sum(weights.values()):.6f} rather than 1.0, so this tenant's "
                "scores would not be comparable with any other tenant's or with their own history"
            )
            raise ScoringOverrideRefused(msg)

        reason = str(payload.get("reason") or "")
        if len(reason.split()) < MIN_REASON_WORDS:
            msg = (
                f"{model_id}: an override states why it holds its value, in at least "
                f"{MIN_REASON_WORDS} words. The core entry does; a tenant's is not held to a lower bar."
            )
            raise ScoringOverrideRefused(msg)

        validated[model_id] = weights
    return validated


class ResolvedMagnitude:
    """A magnitude's value for one tenant, and where it came from."""

    __slots__ = ("model_id", "source", "value")

    def __init__(self, model_id: str, value: dict[str, float], source: str) -> None:
        self.model_id = model_id
        self.value = value
        self.source = source

    def __repr__(self) -> str:
        return f"ResolvedMagnitude(model_id={self.model_id!r}, source={self.source!r})"


_ACTIVE_BINDING_SQL = """
SELECT binding_id, extension_set_digest
FROM profile_bindings
WHERE tenant_id = :tid AND state = 'active'
"""

#: Joined through membership rather than filtered by namespace and target. The
#: binding decides what is bound; the namespace filter only decides which of the
#: bound extensions carry scoring, and an extension in another namespace simply
#: has no `magnitudes` key to contribute.
_BOUND_EXTENSIONS_SQL = """
SELECT e.extension_revision_id, e.canonical_document
FROM profile_binding_extensions m
JOIN profile_extensions e ON e.extension_revision_id = m.extension_revision_id
WHERE m.binding_id = :bid AND e.tenant_id = :tid
"""


async def resolve_weights(session: AsyncSession, *, tenant_id: uuid.UUID, model_id: str) -> ResolvedMagnitude:
    """This tenant's weights for *model_id*: their bound override, else the core.

    Never returns `None` and never asks the caller to fall back. The core value
    is a real answer for the tenants that have no override, which is most of
    them, and making the caller handle absence is how a consumer ends up with two
    code paths of which only one was tested.
    """
    core = ranking.weights(model_id)

    binding = (await session.execute(text(_ACTIVE_BINDING_SQL), {"tid": tenant_id})).one_or_none()
    if binding is None:
        return ResolvedMagnitude(model_id, core, SOURCE_CORE)

    rows = (
        await session.execute(
            text(_BOUND_EXTENSIONS_SQL),
            {"bid": binding.binding_id, "tid": tenant_id},
        )
    ).all()

    # An integrity check, not a discovery mechanism. The membership rows say what
    # was bound; the digest says what the planner intended to bind. They disagree
    # only if something wrote one without the other, which is a corrupted binding
    # -- and picking one of the two to believe would be choosing silently between
    # a governance record and a hash.
    found = [row.extension_revision_id for row in rows]
    if extension_set_digest(found) != binding.extension_set_digest:
        msg = (
            f"tenant {tenant_id}: binding {binding.binding_id} lists extensions that do not digest to its "
            f"recorded `extension_set_digest`. The membership rows and the digest disagree about what this "
            f"binding activated, and scoring on either would be choosing one without saying so."
        )
        raise ScoringOverrideRefused(msg)

    override = _magnitude_from(rows, model_id=model_id)
    if override is None:
        # A bound scoring extension that says nothing about *this* magnitude. It
        # overrides what it names and nothing else, which is what makes core the
        # default rather than a fallback.
        return ResolvedMagnitude(model_id, core, SOURCE_CORE)
    return ResolvedMagnitude(model_id, override, SOURCE_EXTENSION)


def _magnitude_from(rows: Sequence[Any], *, model_id: str) -> dict[str, float] | None:
    """The named magnitude's weights out of the bound extension documents.

    Returns `None` when no bound extension names it. Two extensions naming one
    magnitude is refused rather than resolved by order: whichever won would
    depend on a row order nothing pins, and a tenant would get a different
    weighting depending on how the query planner felt.
    """
    matches: list[dict[str, float]] = []
    for row in rows:
        document = row.canonical_document or {}
        magnitudes = document.get("magnitudes") or {}
        entry = magnitudes.get(model_id)
        if entry is not None:
            matches.append({str(k): float(v) for k, v in entry.items()})

    if not matches:
        return None
    if len(matches) > 1:
        msg = (
            f"{model_id} is overridden by more than one bound extension; whichever won would depend on "
            "a row order nothing pins. One magnitude, one owning extension."
        )
        raise ScoringOverrideRefused(msg)
    return matches[0]


__all__ = [
    "MIN_REASON_WORDS",
    "SCORING_NAMESPACE",
    "SOURCE_CORE",
    "SOURCE_EXTENSION",
    "ResolvedMagnitude",
    "ScoringOverrideRefused",
    "resolve_weights",
    "validate_overrides",
]
