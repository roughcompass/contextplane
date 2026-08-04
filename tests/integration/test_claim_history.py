"""What the store believed at a past instant — the reason the chain is kept.

The eighth exit criterion. A bi-temporal store whose history nobody reads pays the cost
of keeping it and gets none of the benefit, so this is the reader that makes
supersession worth doing rather than merely careful.

The distinction these tests protect is between the two clocks. Transaction time is when
the store came to believe something; valid time is when the asserted fact held. A claim
recorded today about last quarter is current now and was not current then, and
conflating them makes "what did we believe" depend on what was true.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.claim_history import ClaimHistoryService
from registry.service.claim_ontology import seed_ontology
from registry.service.claims import ClaimService, Evidence
from registry.service.confirmation import ConfirmationService
from registry.service.consolidation import ConsolidationService
from registry.service.global_vocabulary import GlobalVocabularyService
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _at(minutes: int) -> datetime.datetime:
    return _NOW + datetime.timedelta(minutes=minutes)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


@pytest.fixture
def history(factory: async_sessionmaker[AsyncSession]) -> ClaimHistoryService:
    return ClaimHistoryService(factory)


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, actor_kind: str = "human"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid, aid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"hist-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:a, :t, 'a', :sub, :k, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"s-{aid.hex[:8]}", "k": actor_kind, "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:e, :t, 'capability', 'cap', 'public', TRUE, :n)"
            ),
            {"e": eid, "t": tid, "n": _NOW},
        )
    return tid, aid, eid


async def _arrive(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    at: int,
    value: object,
    predicate: str = "owned_by_team",
) -> uuid.UUID:
    clock = FakeClock(_at(at))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=(Evidence(kind="session_event", ref=f"e{at}"),),
    )
    await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id


# --- exit criterion 8 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_query_before_a_supersession_returns_the_previous_belief(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """The eighth exit criterion. Without this the chain is a cost with no return."""
    tid, aid, subject = await _seed(factory)
    first = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    second = await _arrive(factory, tid, aid, subject, at=60, value="billing")

    before = await history.believed_at(subject_entity_id=subject, as_of=_at(30))
    after = await history.believed_at(subject_entity_id=subject, as_of=_at(90))

    assert [c.claim_id for c in before] == [first]
    assert before[0].value == "platform"
    assert [c.claim_id for c in after] == [second]
    assert after[0].value == "billing"


@pytest.mark.asyncio
async def test_a_claim_written_after_the_instant_is_not_returned(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """Otherwise the answer to "what did we believe" includes things nobody had said
    yet, which is not a history, it is a summary of now."""
    tid, aid, subject = await _seed(factory)
    await _arrive(factory, tid, aid, subject, at=60, value="platform")

    assert await history.believed_at(subject_entity_id=subject, as_of=_at(30)) == []


@pytest.mark.asyncio
async def test_the_superseded_claim_keeps_its_own_score_and_reason(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """The answer to a past query is the answer that was actually served, not a
    reconstruction."""
    tid, aid, subject = await _seed(factory)
    first = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    await _arrive(factory, tid, aid, subject, at=60, value="billing")

    chain = await history.chain_for(first)
    closed = chain[0]

    assert closed.status == "superseded"
    assert closed.superseded_reason == "lost_conflict"
    assert closed.confidence is not None, "a retired claim keeps the score it carried"
    assert closed.bucket is not None
    assert not closed.was_current


@pytest.mark.asyncio
async def test_the_chain_walks_forward_to_what_is_current(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """The direction a reader asks in: given the claim I was told about, what happened
    to it?"""
    tid, aid, subject = await _seed(factory)
    first = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    second = await _arrive(factory, tid, aid, subject, at=60, value="billing")
    third = await _arrive(factory, tid, aid, subject, at=120, value="infra")

    chain = await history.chain_for(first)

    assert [c.claim_id for c in chain] == [first, second, third]
    assert chain[-1].was_current


@pytest.mark.asyncio
async def test_a_confirmation_appears_in_the_chain_with_its_reason(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """A reader can see both what a machine estimated and what a person then said,
    which is the reason confirmation supersedes rather than mutating."""
    tid, aid, subject = await _seed(factory)
    original = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    claims = ClaimService(factory, clock=FakeClock(_at(30)))
    await ConfirmationService(factory, claims, clock=FakeClock(_at(30))).confirm(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"], oidc_subject="s"),
        claim_id=original,
    )

    chain = await history.chain_for(original)

    assert len(chain) == 2
    assert chain[0].superseded_reason == "human_confirmed"
    assert chain[1].source_authority == "owner_human"


@pytest.mark.asyncio
async def test_the_chain_terminates_on_a_cycle_rather_than_hanging(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """A cycle should be impossible -- a claim cannot supersede itself and closure
    happens once -- but a walk that trusts its data to be acyclic hangs when it is
    not, and a hung read is worse than a wrong one."""
    tid, aid, subject = await _seed(factory)
    first = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    second = await _arrive(factory, tid, aid, subject, at=60, value="billing")

    # Force the cycle the schema's constraints would normally prevent.
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE lmm_claims SET superseded_by = :first, status = 'superseded', "
                "    t_invalidated_at = :now, superseded_reason = 'lost_conflict' "
                "WHERE claim_id = :second"
            ),
            {"first": first, "second": second, "now": _at(90)},
        )

    chain = await history.chain_for(first)
    assert len(chain) == 2, "the walk must stop rather than loop"


@pytest.mark.asyncio
async def test_the_query_can_be_narrowed_to_one_predicate(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    tid, aid, subject = await _seed(factory)
    await _arrive(factory, tid, aid, subject, at=0, value="platform")
    await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        predicate="deployment_environment",
        value="staging",
    )

    owners = await history.believed_at(subject_entity_id=subject, predicate="owned_by_team", as_of=_at(60))
    assert [c.predicate for c in owners] == ["owned_by_team"]


@pytest.mark.asyncio
async def test_transaction_time_and_valid_time_are_not_confused(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """A claim recorded today about last quarter is current now and was not current
    then. Answering the point-in-time question with valid time would make "what did we
    believe" depend on what was true."""
    tid, aid, subject = await _seed(factory)
    # Written now, asserting something that held a year ago.
    clock = FakeClock(_at(0))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e0"),),
        asserted_valid_from=_NOW - datetime.timedelta(days=365),
        asserted_valid_to=_NOW - datetime.timedelta(days=300),
    )

    # A query about a year ago finds nothing: nobody believed it then, however long
    # ago the fact it describes held.
    long_ago = await history.believed_at(subject_entity_id=subject, as_of=_NOW - datetime.timedelta(days=330))
    assert long_ago == []

    # A query about now finds it.
    today = await history.believed_at(subject_entity_id=subject, as_of=_at(10))
    assert [c.claim_id for c in today] == [claim.claim_id]


@pytest.mark.asyncio
async def test_a_collapsed_duplicate_is_visible_in_history(
    factory: async_sessionmaker[AsyncSession], history: ClaimHistoryService, ontology: None
) -> None:
    """Collapsing is a supersession too, and a reader asking why a claim disappeared
    deserves "it was a duplicate" rather than silence."""
    tid, aid, subject = await _seed(factory)
    survivor = await _arrive(factory, tid, aid, subject, at=0, value="platform team")
    duplicate = await _arrive(factory, tid, aid, subject, at=60, value="the Platform Team")

    chain = await history.chain_for(duplicate)
    assert chain[0].superseded_reason == "cluster_collapsed"
    assert chain[0].superseded_by == survivor
