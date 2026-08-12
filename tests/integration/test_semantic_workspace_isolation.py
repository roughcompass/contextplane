"""The experimental semantic arm cannot reach outside the caller's audience.

The lexical and reference arms compose their audience predicate in SQL, so a
task the caller does not participate in never enters the query. The semantic
treatment under evaluation has to make the same guarantee against a much easier
mistake: an embedding scan naturally wants to score everything and rank
afterwards, and a post-filter design leaks in four ways that all survive a test
asserting only that the final item list is correct.

- **Scores.** A similarity computed over content the caller cannot read is a
  measurement of that content, whether or not the item is returned.
- **Counts.** A count taken before filtering reports how much there was, which
  is a fact about the other person's task.
- **Fallback.** A scan that widens its corpus when the authorized set yields too
  few matches answers a question the caller was not entitled to ask.
- **Timing.** Work that scales with the unauthorized corpus is observable from
  outside without reading a single item.

So the assertions below are about what the scan *did*, not only about what it
returned: which texts were embedded, how many, and whether any work at all
tracked content outside the audience. The embedder is instrumented for exactly
that reason -- the returned list is the one signal a broken implementation gets
right.

The second half covers the experimental derivative's lifecycle. It has one:
nothing is persisted. A scan embeds live rows at request time and keeps no
vector, no index and no cached score, which is what makes deletion, expiry,
revocation, redaction, classification, access and retention all reduce to the
same check -- ask again and the answer has already changed. Each is proved
separately anyway, because "they all reduce to one mechanism" is a claim about
the implementation, and the seven properties are what the product owes.

Every actor here shares one tenant. Two tenants would be the easy case and would
pass even if participation were ignored entirely.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.assembler import ArmOutcome
from contextplane.context.evaluation import judge, treatments
from contextplane.context.evaluation.scenarios import Scenario
from contextplane.workspaces import derivative_handlers as workspace_derivative_handlers
from contextplane.workspaces import queries_audience as audience_q
from contextplane.workspaces import recall as workspace_recall
from contextplane.workspaces.audience import RESOLVER_EXPLICIT
from contextplane.workspaces.models import IntentCheckpoint
from contextplane.workspaces.recall import WorkspaceRecall
from contextplane.workspaces.schemas.intent_memory import ROLE_CONTRIBUTOR, IntentParticipantGrantV1

pytestmark = pytest.mark.asyncio

_MEMBER = "agent-member"
_OUTSIDER = "agent-outsider"
_HOUR = datetime.timedelta(hours=1)
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: The shared term. Every checkpoint below is written to match it semantically,
#: so a scan that reached too far would have plenty to find.
_TERM = "drain the retry budget"


class _RecordingEmbedder:
    """A deterministic embedder that remembers everything it was asked to embed.

    The instrument the leak tests actually rely on. A post-filter scan returns
    the right items and still embeds the wrong ones, and the returned list
    cannot tell those two implementations apart.
    """

    model_version = "isolation-bag-of-words"
    vocabulary = ("budget", "cache", "drain", "hold", "retry", "salt", "secret", "tenant")

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[1.0 if word in t.lower() else 0.0 for word in self.vocabulary] for t in texts]

    @property
    def embedded_documents(self) -> list[str]:
        """Everything embedded except the query, which is the caller's own text.

        Positional, not by value: the scan puts the query first in each batch,
        and dropping documents that merely *equal* the query would hide a
        checkpoint whose text happens to match the term -- which is the case a
        recall test most wants to see.
        """
        return [text for batch in self.batches for text in batch[1:]]


class _WorkspaceSource:
    """A workspace source over the real reads, for one tenant and one actor.

    `authorized_candidates` is the method under test. It resolves the audience
    first and builds candidates only from what came back -- and it withholds a
    restricted checkpoint exactly as the lexical arm does, because an arm that
    served semantically what another arm withholds on classification would make
    the withholding decorative.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, actor_id: str) -> None:
        self._factory = factory
        self._tenant_id = tenant_id
        self._actor_id = actor_id
        self._recall = WorkspaceRecall(session_factory=factory)
        self.candidate_count = 0

    async def lexical(self, scenario: Scenario) -> ArmOutcome:
        return await self._recall.lexical_arm(
            tenant_id=self._tenant_id, actor_id=self._actor_id, term=scenario.term, moment=_NOW
        )()

    async def reference(self, scenario: Scenario) -> ArmOutcome:
        if scenario.reference is None:
            return ArmOutcome()
        return await self._recall.reference_arm(
            tenant_id=self._tenant_id, actor_id=self._actor_id, moment=_NOW, **scenario.reference
        )()

    async def authorized_candidates(self, scenario: Scenario) -> tuple[treatments.Candidate, ...]:
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(IntentCheckpoint).where(
                        IntentCheckpoint.tenant_id == self._tenant_id,
                        # The same sub-select every other workspace read composes.
                        # Restating the predicate here would be the second copy
                        # that keeps honouring a revoked grant after the others
                        # have stopped.
                        IntentCheckpoint.intent_id.in_(
                            audience_q._authorized_task_ids(
                                tenant_id=self._tenant_id, actor_id=self._actor_id, moment=_NOW
                            )
                        ),
                    )
                )
            ).scalars()
            authorized = tuple(rows)

        candidates = []
        for row in authorized:
            if workspace_recall.classification_for(row.evidence) == "restricted":
                continue
            candidates.append(
                treatments.Candidate(
                    item_key=str(row.checkpoint_id),
                    text=row.goal,
                    # The production item builder, deliberately. A second
                    # construction path here would let the experiment serve a
                    # differently-shaped item than the arm it is compared with.
                    item=workspace_recall._item(row),
                )
            )
        self.candidate_count = len(candidates)
        return tuple(candidates)


def _scenario(*, required: tuple[str, ...] = ()) -> Scenario:
    return Scenario(
        scenario_id="ISO-01",
        kind="task_resume",
        description="a scenario used to drive the real reads",
        tenant_id="unused-here",
        actor_id=_MEMBER,
        term=_TERM,
        reference=None,
        required_item_keys=required or ("placeholder",),
        relevant_item_keys=required or ("placeholder",),
        facts=judge.AuthorizationFacts(),
    )


# --- fixtures ------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": tid, "s": f"iso-{tid.hex[:8]}", "n": "semantic isolation test"},
        )
    return tid


async def _task_with_checkpoint(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    participants: tuple[str, ...],
    goal: str,
    classification: str | None = None,
    grant_expires_at: datetime.datetime | None = None,
    intent_id: uuid.UUID | None = None,
    sequence: int = 1,
) -> tuple[uuid.UUID, uuid.UUID]:
    intent_id = intent_id or uuid.uuid4()
    checkpoint_id = uuid.uuid4()
    evidence = "[]"
    if classification is not None:
        evidence = (
            '[{"source_system": "github", "source_namespace": "acme/platform", '
            f'"kind": "commit", "external_id": "abc123", "classification": "{classification}"}}]'
        )
    async with factory() as session, session.begin():
        for actor in participants:
            await audience_q.insert_grant(
                session,
                tenant_id=tenant_id,
                grant=IntentParticipantGrantV1(
                    intent_id=intent_id,
                    actor_id=actor,
                    role=ROLE_CONTRIBUTOR,
                    granted_by="agent-owner",
                    granted_at=_NOW - _HOUR,
                    expires_at=grant_expires_at,
                    resolver_version=RESOLVER_EXPLICIT,
                ),
            )
        await session.execute(
            text(
                """
                INSERT INTO intent_checkpoints
                    (checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, evidence,
                     next_action, author, recorded_at, retention_policy, digest)
                VALUES (:cid, :tid, :task, :seq, NULL, :goal, CAST(:ev AS jsonb),
                        'keep going', :author, :rec, 'standard', :digest)
                """
            ),
            {
                "cid": checkpoint_id,
                "tid": tenant_id,
                "task": intent_id,
                "seq": sequence,
                "goal": goal,
                "ev": evidence,
                "author": _MEMBER,
                "rec": _NOW,
                "digest": checkpoint_id.hex[:16],
            },
        )
    return intent_id, checkpoint_id


async def _minimize(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, checkpoint_id: uuid.UUID) -> None:
    """Erase one checkpoint's body through the one UPDATE the trigger admits.

    The raw DELETE and the convenient `SET goal = 'whatever'` are both refused
    by the database, which is the point: erasure is minimize-and-tombstone, and
    a test that reached for either would be exercising a path production cannot
    take.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text(workspace_derivative_handlers._MINIMIZE_CHECKPOINT),
            {
                "erased": workspace_derivative_handlers.ERASED_CHECKPOINT_GOAL,
                "tenant": tenant_id,
                "cid": checkpoint_id,
            },
        )


async def _scan(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    actor_id: str,
    embedder: _RecordingEmbedder,
) -> tuple[ArmOutcome, _WorkspaceSource]:
    source = _WorkspaceSource(factory, tenant_id=tenant_id, actor_id=actor_id)
    candidates = await source.authorized_candidates(_scenario())
    return (
        treatments.exact_scan(query=_TERM, candidates=candidates, embedder=embedder),
        source,
    )


# --- the four leaks ------------------------------------------------------------


async def test_an_unauthorized_checkpoint_is_never_scored(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The headline claim. The outsider's checkpoint matches the term better
    than the member's does, so a scan that could see it would rank it first."""
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry cache")
    await _task_with_checkpoint(factory, tenant, participants=(_OUTSIDER,), goal="drain the retry budget exactly")

    embedder = _RecordingEmbedder()
    scanned, _ = await _scan(factory, tenant, _MEMBER, embedder)

    assert "drain the retry budget exactly" not in embedder.embedded_documents
    assert embedder.embedded_documents == ["drain the retry cache"]
    assert [i.payload["goal"] for i in scanned.items] == ["drain the retry cache"]


async def test_the_candidate_count_is_the_authorized_count_not_the_tenant_count(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """A count taken before filtering reports how much there was, which is a
    fact about somebody else's task."""
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry budget")
    for n in range(5):
        await _task_with_checkpoint(factory, tenant, participants=(_OUTSIDER,), goal=f"drain the retry budget {n}")

    embedder = _RecordingEmbedder()
    _, source = await _scan(factory, tenant, _MEMBER, embedder)

    assert source.candidate_count == 1
    assert len(embedder.embedded_documents) == 1


async def test_an_empty_authorized_set_does_not_fall_back_to_a_wider_corpus(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The tenant is full of matching material and the caller participates in
    nothing. "No authorized matches" is the correct answer, and widening the
    corpus to improve it would answer a question nobody asked."""
    for n in range(5):
        await _task_with_checkpoint(factory, tenant, participants=(_OUTSIDER,), goal=f"drain the retry budget {n}")

    embedder = _RecordingEmbedder()
    scanned, source = await _scan(factory, tenant, _MEMBER, embedder)

    assert scanned.items == ()
    assert scanned.exclusions == ()
    assert source.candidate_count == 0
    assert embedder.embedded_documents == [], "an empty authorized set must embed nothing at all"


async def test_the_work_done_does_not_track_the_unauthorized_corpus(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Timing, measured as work rather than as a clock.

    A wall-clock assertion on a shared machine is a flaky test that proves
    nothing on the run where it passes. What a caller can actually observe from
    outside is that the scan's cost moved with content they cannot read, so the
    check is on the quantity of work: embed the same authorized set against two
    very different unauthorized corpora and the effort must be identical.
    """
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry budget")

    lean = _RecordingEmbedder()
    await _scan(factory, tenant, _MEMBER, lean)

    for n in range(40):
        await _task_with_checkpoint(factory, tenant, participants=(_OUTSIDER,), goal=f"drain the retry budget {n}")

    crowded = _RecordingEmbedder()
    await _scan(factory, tenant, _MEMBER, crowded)

    assert crowded.embedded_documents == lean.embedded_documents


# --- the derivative lifecycle, one property at a time --------------------------


async def test_deletion_takes_the_erased_body_out_of_what_the_scan_can_match(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Deletion here is minimization, because the chain is append-only.

    A checkpoint is never removed -- the row survives so the predecessor links
    resume walks -- and the approved disposition clears the body instead. What
    the scan owes is therefore not "the row is gone" but "the erased words no
    longer match", and this asserts the second, which is the one a person
    exercising erasure actually cares about.
    """
    _, checkpoint_id = await _task_with_checkpoint(
        factory, tenant, participants=(_MEMBER,), goal="drain the retry budget"
    )
    before, _ = await _scan(factory, tenant, _MEMBER, _RecordingEmbedder())
    assert [i.receipt_item_id.item_key for i in before.items] == [str(checkpoint_id)]

    await _minimize(factory, tenant, checkpoint_id)

    embedder = _RecordingEmbedder()
    after, source = await _scan(factory, tenant, _MEMBER, embedder)
    assert source.candidate_count == 1, "the row survives; it is its content that was erased"
    assert embedder.embedded_documents == [workspace_derivative_handlers.ERASED_CHECKPOINT_GOAL]
    assert after.items == (), "an erased body no longer reaches the similarity floor"


async def test_an_expired_grant_stops_the_scan_seeing_the_task(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Expiry is evaluated against the caller-supplied moment, so a grant that
    lapsed before it takes the task out of the candidate set rather than out of
    the results."""
    await _task_with_checkpoint(
        factory,
        tenant,
        participants=(_MEMBER,),
        goal="drain the retry budget",
        grant_expires_at=_NOW - datetime.timedelta(minutes=1),
    )
    embedder = _RecordingEmbedder()
    scanned, source = await _scan(factory, tenant, _MEMBER, embedder)
    assert scanned.items == ()
    assert source.candidate_count == 0
    assert embedder.embedded_documents == []


async def test_revocation_stops_the_scan_seeing_the_task(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Distinct from expiry: nothing about time changed, a person ended the
    participation. The scan reaches revocation through the same predicate as
    every other read, which is what stops it being the one path that keeps
    answering."""
    intent_id, _ = await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry budget")
    assert (await _scan(factory, tenant, _MEMBER, _RecordingEmbedder()))[0].items

    async with factory() as session, session.begin():
        await audience_q.revoke_grant(session, tenant_id=tenant, intent_id=intent_id, actor_id=_MEMBER, moment=_NOW)

    scanned, source = await _scan(factory, tenant, _MEMBER, _RecordingEmbedder())
    assert scanned.items == ()
    assert source.candidate_count == 0


async def test_a_non_participant_has_no_access_and_learns_nothing(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """No items and no exclusion. An exclusion here would report that a task
    exists but is not theirs, which is exactly the discovery the audience
    boundary exists to prevent."""
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry budget")
    scanned, source = await _scan(factory, tenant, _OUTSIDER, _RecordingEmbedder())
    assert scanned.items == ()
    assert scanned.exclusions == ()
    assert source.candidate_count == 0


async def test_a_restricted_checkpoint_is_withheld_from_the_scan_too(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Inside the caller's own task, so the lexical arm withholds it with an
    exclusion. A semantic arm that served it anyway would make that withholding
    decorative -- and it is the same content either way."""
    await _task_with_checkpoint(
        factory,
        tenant,
        participants=(_MEMBER,),
        goal="drain the retry budget with the secret",
        classification="restricted",
    )
    embedder = _RecordingEmbedder()
    scanned, source = await _scan(factory, tenant, _MEMBER, embedder)

    assert scanned.items == ()
    assert source.candidate_count == 0
    assert embedder.embedded_documents == [], "restricted content must not even be embedded"


async def test_redaction_leaves_no_erased_word_anywhere_the_scan_touched(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The same minimization, asked a different question.

    Deletion asks whether the erased checkpoint still matches. Redaction asks
    whether any part of the removed text survived in what was scanned -- in the
    embedder's input, in the served payload, in a cached vector. It takes effect
    on the next request rather than on the next reindex precisely because
    nothing was stored; a persisted embedding would keep answering from text
    that no longer exists in the row it was built from.
    """
    _, checkpoint_id = await _task_with_checkpoint(
        factory, tenant, participants=(_MEMBER,), goal="drain the retry budget for tenant secret"
    )
    first = _RecordingEmbedder()
    await _scan(factory, tenant, _MEMBER, first)
    assert "secret" in first.embedded_documents[0]

    await _minimize(factory, tenant, checkpoint_id)

    second = _RecordingEmbedder()
    scanned, _ = await _scan(factory, tenant, _MEMBER, second)
    assert all("secret" not in t for t in second.embedded_documents)
    assert all("secret" not in str(item.payload) for item in scanned.items)


async def test_the_scan_registers_no_derivative_to_retain(
    factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """Retention, stated as the absence of anything to retain.

    Every persisted derivative needs a retention window, an erasure participant
    and a propagation handler. This one has none of those because it stores
    nothing: the vectors exist for the duration of the call. That is a claim
    about rows, so it is checked against rows -- and it is what makes the six
    properties above hold without a sweep having to run.
    """
    await _task_with_checkpoint(factory, tenant, participants=(_MEMBER,), goal="drain the retry budget")

    async def _counts() -> dict[str, int]:
        async with factory() as session:
            return {
                table: int(
                    (
                        await session.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant})
                    ).scalar_one()
                )
                for table in ("derivative_registrations", "derivative_work_outbox", "embeddings")
            }

    before = await _counts()
    await _scan(factory, tenant, _MEMBER, _RecordingEmbedder())
    assert await _counts() == before
    assert before == {"derivative_registrations": 0, "derivative_work_outbox": 0, "embeddings": 0}
