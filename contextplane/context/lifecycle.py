"""The caller-supplied lifecycle profile: which governed context applies where the caller is.

A caller working somewhere in a delivery lifecycle can say so, by naming the
work they are on -- the run, the stage, the repository, the change. This module
turns that statement into a selection over what the arms already read.

**Stage is caller data, and this module is why that stays true.** A stage is a
reference whose `external_id` is the calling system's own stage name. Nothing
here validates stage order, stores a stage, or infers what comes next: the
registry would then be answering for a lifecycle it does not run, and its answer
would compete with the one the caller's own system already has. Selection reads
the name and compares it. That is the whole of the vocabulary this module holds
about stages.

**The kind vocabulary is closed, and closing it here is not belt-and-braces.**
`ExternalReferenceV1.collision_key()` puts `kind` in the collision scope, and
the receipt-to-outcome lookup filters on the same tuple. So a reference written
with a misspelled kind stores cleanly, binds cleanly, and then silently fails to
join to the receipt that cited the correct spelling for the same external id --
which reads downstream as "no outcome yet" rather than as an error. A refusal at
the boundary is the difference between a caller fixing a typo now and somebody
reconstructing a missing join later. `LIFECYCLE_REFERENCE_KINDS` is deliberately
importable: the control-plane translation path enforces the same set from this
same constant, because enforcement in one of two paths that must agree is not
enforcement.

**Placement is derived by the same function that recorded it.** The dimensions a
profile selects on come from `applicability_from_references`, which is the
function the derivation path used to place the conclusion in the first place.
Two sides that must agree about where a conclusion belongs share one derivation
of it, rather than each keeping a copy that is correct until one is edited.

**Silence is not a mismatch.** An item is dropped only when it records a
dimension the profile also names and the two disagree. An item that never said
where it applies is not claiming to be stage-specific, and reading its silence
as "applies nowhere" would make a caller who supplies a profile lose every
conclusion recorded before dimensions existed -- hiding governed material as a
side effect of describing yourself more precisely.

**What is dropped is withheld, not disappeared.** Selection returns exclusions,
which the assembler already renders as a degraded block carrying a reason and
the receipt already stores. An item filtered away silently would make a
stage-narrowed block indistinguishable from a block whose sources had nothing --
the one distinction the envelope contract exists to preserve.

Which blocks this selects, and which it deliberately does not. Each is a claim
to be disagreed with, and a block that starts recording a dimension is expected
to add itself here rather than inherit silence:

*Selected.* `observed_claims`. Derivation records repository, stage and work
type on the attempt that produced the claim, so there is a recorded placement to
compare a profile against.

*Not selected, and why.* `arc`. The ARC block serves what an attested resolution
already decided applies, against ARC's own applicability rules. Deciding it
again here would make context resolution a second governance authority whose
answer could disagree with the attested one -- and the disagreement would be
invisible, because both would look like ARC. A caller names the resolution that
matches their situation; that naming is the selection.

*Not selected, and why.* `canonical`. Catalog entities and their facts carry no
placement: there is no recorded dimension to compare, and matching a repository
name against an entity's external id would be inventing a dimension and then
selecting on it. A profile narrows what is *said about* the catalog, not which
catalog exists.

*Not selected, and why.* `workspace`. A checkpoint records no placement either.
Narrowing task memory to the profile's references is available to callers today
through the workspace reference the request already carries, which selects by a
citation the checkpoint actually made rather than by a dimension it never
recorded.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final, Protocol, cast

from sqlalchemy import select

from contextplane.context.assembler import Exclusion
from contextplane.context.schemas.trust import InvalidContextItem
from contextplane.service.memory.derivation import (
    APPLICABILITY_REPOSITORY,
    APPLICABILITY_STAGE,
    APPLICABILITY_WORK_TYPE,
    applicability_dimensions,
    applicability_from_references,
)
from contextplane.service.memory.models import ClaimDerivation

if TYPE_CHECKING:  # pragma: no cover - typing only
    import uuid
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.service.memory.derivation import ReferenceLike
    from contextplane.types import TenantContext

#: The closed v1 kind vocabulary, lowercase. Ten values, and a caller naming an
#: eleventh is refused rather than stored: `kind` is part of a reference's
#: collision scope, so an unrecognised one does not become inert data, it becomes
#: a join that silently finds nothing.
#:
#: Ordered rather than a bare set so a refusal can name the legal values in a
#: stable order -- an error message whose contents reshuffle between runs is one
#: nobody can diff.
LIFECYCLE_REFERENCE_KINDS: Final[tuple[str, ...]] = (
    "run",
    "stage",
    "work_item",
    "repository",
    "artifact",
    "action",
    "build",
    "deployment",
    "incident",
    "outcome",
)

_LEGAL_KINDS: Final[frozenset[str]] = frozenset(LIFECYCLE_REFERENCE_KINDS)

#: The dimensions a profile can derive from references, and therefore the ones it
#: can select on. Kept as a tuple rather than read off the reserved-key list:
#: `capability`, `environment` and `scope` are real dimensions that no reference
#: kind supplies, and selecting on a dimension the profile can never populate
#: would be a filter that is always vacuous.
_SELECTABLE_DIMENSIONS: Final[tuple[str, ...]] = (
    APPLICABILITY_REPOSITORY,
    APPLICABILITY_STAGE,
    APPLICABILITY_WORK_TYPE,
)


class PlacedClaim(Protocol):
    """The one field selection needs off a served claim.

    Structural rather than the serving type itself: selection decides whether a
    claim is placed here, which needs its identity and nothing else. Naming the
    real type would make this module depend on the shape of everything a claim
    carries, and every field added there would look like a field selection
    might read.
    """

    @property
    def claim_id(self) -> uuid.UUID:
        """The claim's identity, which its recorded placement is keyed by."""
        ...


class UnknownLifecycleReferenceKind(InvalidContextItem):
    """A lifecycle reference named a kind outside the closed vocabulary.

    Its own type rather than a bare validation error because the two callers
    that must agree on this vocabulary -- the profile boundary and the
    control-plane translation -- both refuse through it, and a shared refusal is
    what makes "enforced in both places" checkable rather than asserted.
    """


def normalize_reference_kind(kind: str) -> str:
    """The canonical spelling of one lifecycle kind, or a refusal.

    Case is folded because a control plane sending `Deployment` means the kind
    the vocabulary spells `deployment`, and refusing that would be pedantry over
    a difference that cannot cause a wrong join. A *misspelling* is refused,
    because that one can: it stores, it binds, and it never joins.
    """
    normalized = kind.strip().lower()
    if normalized not in _LEGAL_KINDS:
        raise UnknownLifecycleReferenceKind(
            f"unknown lifecycle reference kind {kind!r}; legal kinds are {list(LIFECYCLE_REFERENCE_KINDS)}. "
            "Kind is part of a reference's collision scope, so an unrecognised one would bind "
            "cleanly and then never join to the work it names"
        )
    return normalized


@dataclasses.dataclass(frozen=True)
class LifecycleProfile:
    """Where the caller says they are, as references the registry does not own.

    Holds the references verbatim and the placement derived from them. Both,
    because the placement is what selection compares and the references are what
    a receipt records -- reconstructing either from the other loses something a
    reader needs.
    """

    references: tuple[ExternalReferenceV1, ...]
    #: The dimensions selection compares, derived once at construction. Empty
    #: values are absent rather than blank: a dimension recorded as an empty
    #: string would match nothing and read as a filter that was applied.
    placement: Mapping[str, str]

    @classmethod
    def of(cls, references: Sequence[ExternalReferenceV1]) -> LifecycleProfile:
        """Build a profile, refusing any reference outside the closed vocabulary.

        The refusal is the point of this constructor. Nothing downstream
        re-checks the vocabulary, so a profile that exists is a profile whose
        kinds are legal.
        """
        for reference in references:
            normalize_reference_kind(reference.kind)
        # The derivation side declares the two fields it reads as a mutable
        # protocol; a frozen reference satisfies it in every way that matters
        # and in none that a structural check can see. Cast rather than loosen
        # the protocol: the immutability is the property worth keeping.
        placed = applicability_from_references(cast("Sequence[ReferenceLike]", references))
        recorded = {
            APPLICABILITY_REPOSITORY: placed.repository,
            APPLICABILITY_STAGE: placed.stage,
            APPLICABILITY_WORK_TYPE: placed.work_type,
        }
        return cls(
            references=tuple(references),
            placement={key: value for key, value in recorded.items() if value is not None and value.strip()},
        )

    def selects(self) -> bool:
        """Whether this profile narrows anything at all.

        A profile of references that supply no selectable dimension -- a caller
        naming only a build, say -- is a real profile worth recording on the
        receipt and a filter that would drop nothing. Saying so here keeps the
        arms from paying for a read whose result cannot change the answer.
        """
        return bool(self.placement)

    def excludes(self, dimensions: Mapping[str, str]) -> str | None:
        """Why this profile withholds an item placed here, or `None` to keep it.

        Returns the reason rather than a boolean: the caller records it as an
        exclusion, and a reason reconstructed later from the two placements
        would have to re-derive the comparison that was already made here.
        """
        for dimension in _SELECTABLE_DIMENSIONS:
            wanted = self.placement.get(dimension)
            recorded = dimensions.get(dimension)
            if wanted is None or recorded is None or not recorded.strip():
                # Either the caller did not narrow on this dimension, or the item
                # never said where it applied. Neither is a disagreement.
                continue
            if recorded.strip().lower() != wanted.strip().lower():
                return (
                    f"the item applies to {dimension} {recorded.strip()!r}, "
                    f"and this request is placed at {wanted!r}"
                )
        return None

    def record(self) -> list[dict[str, str | None]]:
        """The profile as a receipt stores it: collision key plus the parts.

        The key is what makes two spellings of one reference compare equal; the
        parts are what let a reader see which spelling this caller used. Same
        shape the receipt already stores for the workspace reference, because a
        second shape for the same thing is a second thing to keep in step.
        """
        return [
            {
                "collision_key": reference.collision_key(),
                "source_system": reference.source_system,
                "source_namespace": reference.source_namespace,
                "kind": reference.kind,
                "external_id": reference.external_id,
                "revision": reference.revision,
            }
            for reference in self.references
        ]


def placement_of(applicability: str) -> Mapping[str, str]:
    """The recorded dimensions of one stored applicability field.

    A thin name over the derivation parser, kept so the selection side reads in
    its own vocabulary and so there is one place to change if placement ever
    moves off that field into columns of its own.
    """
    return applicability_dimensions(applicability)


def partition(
    profile: LifecycleProfile,
    items: Iterable[tuple[str, str]],
) -> tuple[frozenset[str], dict[str, str]]:
    """Split placed items into the keys to keep and the keys to withhold, with reasons.

    Takes `(key, applicability)` pairs rather than domain objects so the rule can
    be proved against a table of placements, without a claim, a database, or a
    serving service standing in the way of the one decision being tested.

    An item this profile has no opinion about is kept. That is the same rule
    `excludes` states, restated here only in the direction the arms consume it.
    """
    withheld: dict[str, str] = {}
    kept: set[str] = set()
    for key, applicability in items:
        reason = profile.excludes(placement_of(applicability))
        if reason is None:
            kept.add(key)
        else:
            withheld[key] = reason
    return frozenset(kept), withheld


async def placements_for_claims(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """The applicability each claim was recorded under, by claim id.

    Read here rather than served alongside the claim because placement belongs
    to the *attempt* that derived it: a served claim carries what it asserts and
    the evidence for it, and widening that shape for one consumer's filter would
    make every serving path answer a question only this one asks.

    The tenant predicate is in the SELECT. Filtering afterwards would mean
    loading placements belonging to another tenant, and the shape of what a
    caller's filter matched against is itself worth not disclosing.

    A claim derived more than once takes its newest attempt. Re-derivation is
    how a conclusion gets re-placed, so the older placement is superseded rather
    than tied with -- and ordering makes the answer independent of the plan.
    """
    if not claim_ids:
        return {}
    stmt = (
        select(ClaimDerivation.created_claim_id, ClaimDerivation.applicability)
        .where(
            ClaimDerivation.tenant_id == tenant_id,
            ClaimDerivation.created_claim_id.in_(tuple(claim_ids)),
        )
        .order_by(ClaimDerivation.created_at)
    )
    placements: dict[uuid.UUID, str] = {}
    for claim_id, applicability in (await session.execute(stmt)).all():
        if claim_id is not None:
            placements[claim_id] = applicability
    return placements


async def narrow[ClaimT: PlacedClaim](
    session_factory: async_sessionmaker[AsyncSession],
    profile: LifecycleProfile,
    claims: Sequence[ClaimT],
    ctx: TenantContext,
) -> tuple[tuple[ClaimT, ...], tuple[Exclusion, ...]]:
    """The claims this profile keeps, and an exclusion for each one it withholds.

    The whole of the arm-facing surface, so the claims arm gains a call rather
    than a rule. Selection is one decision and belongs in one module; spread
    across the arm it would be re-stated the first time a second block wanted
    it, and the two statements would drift.

    Applied after the read and before the caller's bound. A claim withheld for
    placement must not consume one of the caller's slots -- narrowing that also
    shortened the answer would make a profile look like a smaller `limit`.

    Exclusions are sorted by item key so two identical resolutions produce
    identical evidence; the underlying read is ordered, but the withheld set is
    a mapping and a mapping's order is not part of anything checkable.

    Opens its own session rather than borrowing the arm's. The placement read
    is not the read whose erasure guard the arm holds -- it reads where a
    conclusion was placed, never the conclusion -- so sharing a session would
    imply a coupling between the two that does not exist.
    """
    withheld: dict[uuid.UUID, str] = {}
    async with session_factory() as session:
        placements = await placements_for_claims(
            session, tenant_id=ctx.tenant_id, claim_ids=[claim.claim_id for claim in claims]
        )
    for claim_id, applicability in placements.items():
        reason = profile.excludes(placement_of(applicability))
        if reason is not None:
            withheld[claim_id] = reason
    kept = tuple(claim for claim in claims if claim.claim_id not in withheld)
    exclusions = tuple(
        Exclusion(item_key=str(claim_id), reason=withheld[claim_id]) for claim_id in sorted(withheld, key=str)
    )
    return kept, exclusions


__all__ = [
    "LIFECYCLE_REFERENCE_KINDS",
    "narrow",
    "LifecycleProfile",
    "UnknownLifecycleReferenceKind",
    "normalize_reference_kind",
    "partition",
    "placement_of",
    "placements_for_claims",
]
