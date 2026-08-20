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

**Extensions are enumerated and then verified, never inferred.** A binding stores
`extension_set_digest` and not the extension ids, so the set that was activated
cannot be read back directly. The accessor finds the tenant's extensions in the
scoring namespace targeting the bound core revision, re-derives the digest over
those ids, and **refuses if it does not match the binding**. A mismatch means the
set found is not the set that was activated — a tenant has published something
since, or an extension was retired — and scoring a tenant by an extension nobody
bound is precisely the ungoverned override the binding lifecycle exists to
prevent. Refusing is louder than falling back to core, and it has to be: falling
back would present an unbound override's absence as normal operation.

**A tenant with no active binding resolves to core, and that is not an error.**
Most tenants have no extension and never will. `unbound` is the common case, not
a degraded one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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
    """The tenant's bound extension set could not be established with certainty."""


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
SELECT profile_revision_id, extension_set_digest
FROM profile_bindings
WHERE tenant_id = :tid AND state = 'active'
"""

_BOUND_EXTENSIONS_SQL = """
SELECT extension_revision_id, canonical_document
FROM profile_extensions
WHERE tenant_id = :tid AND namespace = :ns AND target_core_revision_id = :core
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
            {"tid": tenant_id, "ns": SCORING_NAMESPACE, "core": binding.profile_revision_id},
        )
    ).all()

    # Verify before reading. The digest is over the *set* of extension ids the
    # binding activated, so re-deriving it is the only way to know the rows found
    # here are the rows that were bound.
    found = [row.extension_revision_id for row in rows]
    if extension_set_digest(found) != binding.extension_set_digest:
        msg = (
            f"tenant {tenant_id}: the {SCORING_NAMESPACE!r} extensions published against the bound core "
            f"revision do not digest to the set this binding activated. Scoring with them would apply an "
            f"override nobody bound; scoring without them would present that silently as normal. "
            f"Re-plan the binding, or retire the extension that was published outside it."
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
    "SCORING_NAMESPACE",
    "SOURCE_CORE",
    "SOURCE_EXTENSION",
    "ResolvedMagnitude",
    "ScoringOverrideRefused",
    "resolve_weights",
]
