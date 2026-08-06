"""Organization-scope claim predicates: the deployment's shared vocabulary.

A global predicate means the same thing in every tenant. That is what lets two
tenants' claims about the same subject corroborate or contradict each other
rather than merely coexist -- which is the entire reason the vocabulary is
shared.

**Why this is a separate service from `VocabularyService`.** That one is
tenant-scoped: every method takes a `TenantContext` and every query filters by
it. Adding global mutation there would mean a method that deliberately ignores
the context it was handed, sitting next to methods that must not. Separating
them keeps "this path has no tenant" a property of the module rather than a
condition inside a function.

**Collision is prevented in both directions.** A tenant cannot create a local
predicate matching a global name, and an operator cannot create a global
predicate matching a name any tenant already uses locally. The second is the
awkward one and the one that matters: promoting a term to global while a tenant
means something else by it would silently retype every claim they have already
written. A pre-existing local term has to be reconciled first -- renamed,
deprecated, or agreed -- and never silently absorbed.

**Deprecation, never retyping.** A predicate's declared type is what every
claim written against it was validated by. Changing it in place reinterprets
history. A predicate changes by being deprecated and succeeded.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.storage.models import CLAIM_PREDICATE_KIND, VocabularyValue
from registry.types import Clock

# The value types a claim predicate may declare. Closed, and units are part of
# the name rather than a convention -- "duration" plus an implied unit is
# exactly how one tenant stores minutes and another stores seconds under the
# same predicate.
VALUE_TYPES = frozenset(
    {
        "string",
        "enum",
        "integer",
        "decimal",
        "boolean",
        "duration_seconds",
        "bytes",
        "timestamp_utc",
        "entity_ref",
        "version_predicate",
        "url",
        "prose",
    }
)

# `prose` exists for one category that has no typed decomposition, and is
# barred everywhere else: a predicate that accepts prose accepts anything, and
# a claim whose value is a paragraph cannot be compared, corroborated, or
# contradicted. Free text belongs in provenance.
PROSE_ONLY_CATEGORY = "session_summary"

# How many values of a predicate may hold at one instant.
#
# `single` -- at most one. Two differing values over overlapping effective
#   intervals mean one of them is wrong, which is a disagreement worth surfacing.
# `multi`  -- a set. Many values hold at once, so differing values are two facts
#   rather than two answers, and never disagree.
#
# Declared per predicate because neither the value type nor the category
# determines it. `steward_entity` and `depends_on` are both entity references
# with opposite cardinality; `owned_by_team` and `exposes_operation` are both
# strings with opposite cardinality. Inferring from either is wrong against the
# shipped vocabulary, not merely imprecise.
#
# Where a predicate is genuinely arguable it is `multi`. The errors are not
# symmetric: a wrongly-single predicate produces contested claims no reviewer
# can resolve -- both values are true, so neither supersedes the other, and both
# stay permanently ineligible for promotion. A wrongly-multi predicate only
# misses a disagreement, which decay and human confirmation still surface.
CARDINALITY_SINGLE = "single"
CARDINALITY_MULTI = "multi"

VALUE_CARDINALITIES = frozenset({CARDINALITY_SINGLE, CARDINALITY_MULTI})

CLAIM_CATEGORIES = frozenset(
    {
        "interface_contract",
        "dependency",
        "ownership_stewardship",
        "operational_lifecycle",
        "decision_rationale",
        # What happened, as against what is currently so. Kept separate because decay
        # is keyed on category: an incident that occurred does not become less true
        # with age, while every other category here describes current state and
        # should fade as time passes since anybody checked.
        "incident_history",
        PROSE_ONLY_CATEGORY,
    }
)


@dataclasses.dataclass(frozen=True)
class GlobalPredicate:
    """An organization-wide predicate definition, readable and writable by operators only."""

    value: str
    value_type: str
    claim_category: str
    value_cardinality: str
    definition: str
    deprecated_at: datetime.datetime | None

    @property
    def scope(self) -> str:
        """Always "global" -- lets a caller handling mixed predicate scopes branch without an isinstance check."""
        return "global"


class GlobalVocabularyService:
    """Creates and deprecates organization-scope claim predicates.

    Takes no `TenantContext` anywhere. Authorization is the operator-allowlist
    check the route performs; this service has no tenant to authorize against
    and must not invent one.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create_predicate(
        self,
        *,
        value: str,
        value_type: str,
        claim_category: str,
        definition: str,
        value_cardinality: str = CARDINALITY_MULTI,
    ) -> GlobalPredicate:
        """Define a predicate for the whole deployment, or refuse.

        Cardinality defaults to `multi`, the direction that misses a
        disagreement rather than manufacturing one. A caller that means
        single-valued has to say so.
        """
        self._validate(
            value=value,
            value_type=value_type,
            claim_category=claim_category,
            definition=definition,
            value_cardinality=value_cardinality,
        )

        async with self._session_factory() as session, session.begin():
            await self._assert_no_collision(session, value)
            row = VocabularyValue(
                vocab_id=uuid.uuid4(),
                tenant_id=None,
                kind=CLAIM_PREDICATE_KIND,
                value=value,
                is_system=False,
                deprecated_at=None,
                created_at=self._clock.now(),
                value_type=value_type,
                claim_category=claim_category,
                definition=definition,
                value_cardinality=value_cardinality,
            )
            session.add(row)

        return GlobalPredicate(
            value=value,
            value_type=value_type,
            claim_category=claim_category,
            value_cardinality=value_cardinality,
            definition=definition,
            deprecated_at=None,
        )

    async def deprecate_predicate(self, *, value: str) -> GlobalPredicate:
        """Retire a predicate without removing it.

        The row stays because claims already reference it: a deprecated
        predicate still has to explain what those claims meant. What changes is
        that nothing new may be written against it.

        Idempotent, and a repeat preserves the original timestamp -- when a term
        was retired is a fact about the vocabulary's history, not about the last
        time somebody called this.
        """
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(VocabularyValue).where(
                        VocabularyValue.tenant_id.is_(None),
                        VocabularyValue.kind == CLAIM_PREDICATE_KIND,
                        VocabularyValue.value == value,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                msg = f"global claim predicate {value!r} is not defined"
                raise NotFoundError(msg)
            if row.deprecated_at is None:
                row.deprecated_at = self._clock.now()
            return GlobalPredicate(
                value=row.value,
                value_type=row.value_type or "",
                claim_category=row.claim_category or "",
                value_cardinality=row.value_cardinality or CARDINALITY_MULTI,
                definition=row.definition or "",
                deprecated_at=row.deprecated_at,
            )

    async def list_predicates(self) -> list[GlobalPredicate]:
        """Return all organization-wide predicates, including deprecated ones."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(VocabularyValue)
                    .where(
                        VocabularyValue.tenant_id.is_(None),
                        VocabularyValue.kind == CLAIM_PREDICATE_KIND,
                    )
                    .order_by(VocabularyValue.value)
                )
            ).scalars()
            return [
                GlobalPredicate(
                    value=r.value,
                    value_type=r.value_type or "",
                    claim_category=r.claim_category or "",
                    value_cardinality=r.value_cardinality or CARDINALITY_MULTI,
                    definition=r.definition or "",
                    deprecated_at=r.deprecated_at,
                )
                for r in rows
            ]

    async def local_predicate_inventory(self) -> list[tuple[uuid.UUID, str]]:
        """Every tenant-local claim predicate, for ontology governance.

        The operator surface needs to see divergence -- which local terms exist
        and where -- to decide what should become global. It returns names and
        owning tenants only; no tenant's local vocabulary is exposed to another
        tenant through any tenant-facing route.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(VocabularyValue.tenant_id, VocabularyValue.value)
                    .where(
                        VocabularyValue.tenant_id.is_not(None),
                        VocabularyValue.kind == CLAIM_PREDICATE_KIND,
                    )
                    .order_by(VocabularyValue.value)
                )
            ).all()
            return [(r.tenant_id, r.value) for r in rows]

    async def _assert_no_collision(self, session: AsyncSession, value: str) -> None:
        """Refuse a global name any tenant already means something by.

        The harder direction of the collision rule. Promoting a term while a
        tenant uses it locally would silently retype every claim they have
        written against their own meaning of it -- so a pre-existing local term
        must be reconciled first, never absorbed.
        """
        existing_global = (
            await session.execute(
                select(VocabularyValue).where(
                    VocabularyValue.tenant_id.is_(None),
                    VocabularyValue.kind == CLAIM_PREDICATE_KIND,
                    VocabularyValue.value == value,
                )
            )
        ).scalar_one_or_none()
        if existing_global is not None:
            msg = f"global claim predicate {value!r} already exists"
            raise ConflictError(msg)

        local_owners = (
            await session.execute(
                select(VocabularyValue.tenant_id).where(
                    VocabularyValue.tenant_id.is_not(None),
                    VocabularyValue.kind == CLAIM_PREDICATE_KIND,
                    VocabularyValue.value == value,
                )
            )
        ).all()
        if local_owners:
            msg = (
                f"claim predicate {value!r} already exists locally in {len(local_owners)} tenant(s). "
                "Promoting it would retype the claims already written against their meaning of the "
                "term. Reconcile those first -- rename, deprecate, or agree the definition."
            )
            raise ConflictError(msg)

    def _validate(
        self,
        *,
        value: str,
        value_type: str,
        claim_category: str,
        definition: str,
        value_cardinality: str,
    ) -> None:
        if not value.strip():
            msg = "a predicate needs a name"
            raise ValidationError(msg)
        if value_type not in VALUE_TYPES:
            msg = f"unknown value type {value_type!r}; expected one of {sorted(VALUE_TYPES)}"
            raise ValidationError(msg)
        if claim_category not in CLAIM_CATEGORIES:
            msg = f"unknown claim category {claim_category!r}; expected one of {sorted(CLAIM_CATEGORIES)}"
            raise ValidationError(msg)
        if value_type == "prose" and claim_category != PROSE_ONLY_CATEGORY:
            msg = (
                "only the session-summary category may declare a prose value type; a predicate "
                "accepting prose accepts anything, and a claim whose value is a paragraph cannot "
                "be compared or contradicted"
            )
            raise ValidationError(msg)
        if value_cardinality not in VALUE_CARDINALITIES:
            msg = f"unknown value cardinality {value_cardinality!r}; expected one of " f"{sorted(VALUE_CARDINALITIES)}"
            raise ValidationError(msg)
        if not definition.strip():
            msg = (
                "a predicate needs a definition; an undefined term is how two tenants end up "
                "meaning different things by the same name"
            )
            raise ValidationError(msg)


__all__ = [
    "CARDINALITY_MULTI",
    "CARDINALITY_SINGLE",
    "CLAIM_CATEGORIES",
    "PROSE_ONLY_CATEGORY",
    "VALUE_CARDINALITIES",
    "VALUE_TYPES",
    "GlobalPredicate",
    "GlobalVocabularyService",
]
