"""What the store believed about something at a past instant.

The supersession chain exists to answer one question -- "what did we believe about this
last month, and why did it change?" -- and that question is only answerable if there is
something that reads it. A chain nobody queries is a chain nobody checks, and a
bi-temporal store whose history is unreadable is a store that pays the cost of keeping
history and gets none of the benefit.

**Two clocks, and only one of them is the point-in-time parameter.** Transaction time
is when the store came to believe something; valid time is when the asserted fact
actually held. A claim recorded today about last quarter is current now and was not
current then, and conflating the two would make the answer to "what did we believe"
depend on what was true.

**Superseded is not deleted.** A closed claim keeps its own confidence, its own
provenance, and the reason it lost, so the answer to a past query is the answer that
was actually served -- not a reconstruction.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.confidence_read import serve
from contextplane.service.memory.trust_transitions import transitions_for


@dataclasses.dataclass(frozen=True)
class BelievedClaim:
    """A claim as the store held it at some instant."""

    claim_id: uuid.UUID
    predicate: str
    value: object
    source_authority: str
    confidence: float | None
    bucket: str | None
    status: str
    superseded_by: uuid.UUID | None
    superseded_reason: str | None
    created_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None
    is_contested: bool

    @property
    def was_current(self) -> bool:
        """Whether the claim was current at read time (not yet superseded)."""
        return self.t_invalidated_at is None


@dataclasses.dataclass(frozen=True)
class ClaimVisibility:
    """The three columns a caller needs to decide who may read one claim row.

    Not a shape `chain_for`/`believed_at` return themselves -- those answer
    the supersession-chain question, not the tenancy one, and this class
    deliberately carries only what a visibility decision needs, nothing a
    reader would mistake for served claim content.
    """

    subject_entity_id: uuid.UUID | None
    visibility: str
    owning_tenant_id: uuid.UUID | None


class ClaimHistoryService:
    """Reads what was believed, then and now."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def believed_at(
        self,
        *,
        subject_entity_id: uuid.UUID,
        predicate: str | None = None,
        as_of: datetime.datetime,
        now: datetime.datetime | None = None,
    ) -> list[BelievedClaim]:
        """Every claim the store held at `as_of`.

        A claim was held then if it had been written by then and had not yet been
        closed -- so a claim superseded yesterday is returned for a query about last
        week, and a claim written this morning is not.

        Confidence is aged to `as_of` rather than to the present, because the question
        is what a reader would have seen. Ageing it to now would report a number that
        was never served.
        """
        moment = now if now is not None else as_of
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT claim_id, predicate, value_jsonb, source_authority, "
                        "       confidence, confidence_scored_at, decay_half_life_days, "
                        "       confidence_hold_until, value_type, status, superseded_by, "
                        "       superseded_reason, created_at, t_invalidated_at, is_contested "
                        "FROM memory_claims "
                        "WHERE subject_entity_id = :eid "
                        # Cast because asyncpg cannot infer the type of a parameter
                        # that only ever appears in a null test.
                        "  AND (CAST(:pred AS TEXT) IS NULL OR predicate = CAST(:pred AS TEXT)) "
                        "  AND created_at <= CAST(:as_of AS TIMESTAMPTZ) "
                        # Still open, or closed after the instant asked about. This is
                        # the whole point-in-time predicate: a claim retired later was
                        # current then.
                        "  AND (t_invalidated_at IS NULL "
                        "       OR t_invalidated_at > CAST(:as_of AS TIMESTAMPTZ)) "
                        "ORDER BY created_at"
                    ),
                    {"eid": subject_entity_id, "pred": predicate, "as_of": as_of},
                )
            ).all()

        return [_to_believed(row, as_of=as_of, now=moment) for row in rows]

    async def chain_for(self, claim_id: uuid.UUID) -> list[BelievedClaim]:
        """A claim and everything that replaced it, oldest first.

        Walks forward rather than backward because that is the direction a reader
        asks in: given the claim I was told about, what happened to it?
        """
        chain: list[BelievedClaim] = []
        current: uuid.UUID | None = claim_id
        seen: set[uuid.UUID] = set()

        async with self._session_factory() as session:
            while current is not None and current not in seen:
                seen.add(current)
                row = (
                    await session.execute(
                        text(
                            "SELECT claim_id, predicate, value_jsonb, source_authority, "
                            "       confidence, confidence_scored_at, decay_half_life_days, "
                            "       confidence_hold_until, value_type, status, superseded_by, "
                            "       superseded_reason, created_at, t_invalidated_at, "
                            "       is_contested "
                            "FROM memory_claims WHERE claim_id = :cid"
                        ),
                        {"cid": current},
                    )
                ).one_or_none()
                if row is None:
                    break
                chain.append(_to_believed(row, as_of=row.created_at, now=row.created_at))
                # A cycle would loop forever. It should be impossible -- a claim cannot
                # supersede itself and closure happens once -- but a walk that trusts
                # its data to be acyclic is one that hangs when it is not.
                current = row.superseded_by

        return chain

    async def trust_history_for(self, claim_id: uuid.UUID) -> list[dict[str, object]]:
        """When this claim fell out of a trust class, oldest first.

        Here rather than on its own surface because it answers the question this
        service already exists for — given the claim I was told about, what
        happened to it — and losing trust to age is one of the things that
        happened. A separate endpoint would make a reader ask twice.

        **`observed_at` is when a sweep noticed, not when the crossing
        happened.** Decay is computed on read, so no code runs at the moment a
        claim crosses a boundary; the two differ by up to one sweep interval and
        the column is named so a reader has to know that.
        """
        async with self._session_factory() as session:
            return await transitions_for(session, claim_id=claim_id)

    async def visibility_rows_for(self, claim_ids: list[uuid.UUID]) -> dict[uuid.UUID, ClaimVisibility]:
        """The tenancy columns for each id, by claim_id -- not the belief content.

        `chain_for`/`believed_at` take no tenant context by design and return
        none of these three columns, so a caller enforcing tenant visibility
        around either one (the REST surface's own router, today) reads them
        here instead of reaching into `memory_claims` itself. Missing ids are
        simply absent from the returned mapping, the same "no row, no entry"
        shape `chain_for` gives a caller that asks for one that does not
        exist.
        """
        if not claim_ids:
            return {}
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT claim_id, subject_entity_id, visibility, owning_tenant_id "
                            "FROM memory_claims WHERE claim_id = ANY(:ids)"
                        ),
                        {"ids": claim_ids},
                    )
                )
                .mappings()
                .all()
            )
        return {
            row["claim_id"]: ClaimVisibility(
                subject_entity_id=row["subject_entity_id"],
                visibility=row["visibility"],
                owning_tenant_id=row["owning_tenant_id"],
            )
            for row in rows
        }


def _to_believed(row: object, *, as_of: datetime.datetime, now: datetime.datetime) -> BelievedClaim:
    confidence: float | None = None
    bucket: str | None = None
    stored = getattr(row, "confidence", None)
    if stored is not None and row.confidence_scored_at is not None:  # type: ignore[attr-defined]
        served = serve(
            stored=float(stored),
            scored_at=row.confidence_scored_at,  # type: ignore[attr-defined]
            half_life_days=float(row.decay_half_life_days or 1.0),  # type: ignore[attr-defined]
            now=max(now, row.confidence_scored_at),  # type: ignore[attr-defined]
            hold_until=row.confidence_hold_until,  # type: ignore[attr-defined]
            value_type=row.value_type,  # type: ignore[attr-defined]
        )
        confidence = served.effective
        bucket = served.bucket

    return BelievedClaim(
        claim_id=row.claim_id,  # type: ignore[attr-defined]
        predicate=row.predicate,  # type: ignore[attr-defined]
        value=row.value_jsonb,  # type: ignore[attr-defined]
        source_authority=row.source_authority,  # type: ignore[attr-defined]
        confidence=confidence,
        bucket=bucket if confidence is not None else None,
        status=row.status,  # type: ignore[attr-defined]
        superseded_by=row.superseded_by,  # type: ignore[attr-defined]
        superseded_reason=row.superseded_reason,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        t_invalidated_at=row.t_invalidated_at,  # type: ignore[attr-defined]
        is_contested=row.is_contested,  # type: ignore[attr-defined]
    )


__all__ = ["BelievedClaim", "ClaimHistoryService", "ClaimVisibility"]
