"""Integration tests for artifact families and the proposal state machine,
against a real Postgres.

What the unit suite (`tests/unit/test_arc_proposal.py`) cannot prove with a
fake session: that the database constraints Appendix B.2 names actually
hold when a real row tries to violate them, and that two truly concurrent
callers racing to open a proposal on the same artifact resolve
deterministically -- one winner, one `arc_proposal_state_conflict` -- the
way `test_arc_source_admission.py`'s own advisory-lock race test proves its
lock actually holds.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.proposal import ProposalService, ProposalStateConflict
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, tenant_id: uuid.UUID, roles: list[str] | None = None) -> ArcRequestContext:
    """*tenant_id* must name a real, seeded tenant row: every successful
    write here emits an audit-outbox row, and `arc_audit_outbox.tenant_id`
    is a real foreign key -- a random unseeded UUID would fail the write,
    not the check under test."""
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=_OPERATOR)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _service(factory: async_sessionmaker[AsyncSession]) -> ProposalService:
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    return ProposalService(factory, authorization=authorization, clock=FakeClock(_NOW))


async def _insert_thread(session: AsyncSession, *, proposal_id: uuid.UUID, artifact_id: uuid.UUID) -> None:
    """The bare thread row the constraint-proof tests below build their
    version rows against -- raw SQL, not `ProposalService.open_proposal`,
    because these tests are proving the database constraint itself, not
    the service method that normally sits in front of it."""
    await session.execute(
        text("INSERT INTO arc_authoring_proposals (proposal_id, artifact_id, created_at) VALUES (:pid, :aid, :now)"),
        {"pid": proposal_id, "aid": artifact_id, "now": _NOW},
    )


# ---------------------------------------------------------------------------
# Concurrency: two callers racing to open a proposal on the same artifact.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_open_rejected(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    """The proof this task calls for: a lock that isn't raced isn't known
    to hold. Two truly concurrent `open_proposal` calls against the same
    brand-new artifact, each on its own connection, must resolve to exactly
    one `open` version and one `arc_proposal_state_conflict` -- never two
    live versions and never a crash on either side."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"race-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory)
    source_evidence_id = await seed_source_evidence(factory)
    service = _service(factory)
    ctx = _ctx(tenant_id=tenant_id)

    async def _attempt() -> object:
        try:
            return await service.open_proposal(ctx, artifact_id=artifact_id, source_evidence_id=source_evidence_id)
        except ProposalStateConflict as exc:
            return exc

    first, second = await asyncio.gather(_attempt(), _attempt())
    outcomes = [first, second]
    winners = [o for o in outcomes if isinstance(o, ProposalStateConflict) is False]
    losers = [o for o in outcomes if isinstance(o, ProposalStateConflict)]
    assert len(winners) == 1, f"exactly one call must win the race, got {outcomes}"
    assert len(losers) == 1, f"exactly one call must lose the race, got {outcomes}"
    assert winners[0].proposal_version == 1  # type: ignore[union-attr]

    async with factory() as session:
        thread_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_authoring_proposals WHERE artifact_id = :aid"), {"aid": artifact_id}
            )
        ).scalar()
        version_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_authoring_proposal_versions "
                    "WHERE artifact_id = :aid AND state IN ('open', 'submitted', 'approved')"
                ),
                {"aid": artifact_id},
            )
        ).scalar()
    assert thread_count == 1, "the race must not create two threads for one artifact"
    assert version_count == 1, "the race must leave exactly one live version, not two"


@pytest.mark.asyncio
async def test_two_new_families_race_to_the_same_slug(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The other race this surface has to resolve deterministically:
    `create_family` under the same (scope, slug) pair. `uq_arc_artifacts_
    scope_slug` is the backstop; this proves it actually serializes two
    concurrent creators into one winner rather than two rows."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"race2-{uuid.uuid4().hex[:8]}")
    service = _service(factory)
    ctx = _ctx(tenant_id=tenant_id)
    slug = f"race-slug-{uuid.uuid4().hex[:8]}"

    async def _attempt() -> object:
        try:
            return await service.create_family(
                ctx, slug=slug, kind="policy", owning_scope="global", target_tenant_id=None, title="Race"
            )
        except Exception as exc:
            return exc

    first, second = await asyncio.gather(_attempt(), _attempt())
    succeeded = [o for o in (first, second) if not isinstance(o, Exception)]
    failed = [o for o in (first, second) if isinstance(o, Exception)]
    assert len(succeeded) == 1, f"exactly one create_family must win the slug race, got {(first, second)}"
    assert len(failed) == 1

    async with factory() as session:
        count = (
            await session.execute(text("SELECT COUNT(*) FROM arc_artifacts WHERE slug = :slug"), {"slug": slug})
        ).scalar()
    assert count == 1, "the scope+slug unique index must leave exactly one family row"


# ---------------------------------------------------------------------------
# Appendix B.2 constraints, proven by attempting the violation directly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_key_rejects_a_duplicate_proposal_id_and_version(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id = await seed_artifact_family(factory)
    source_evidence_id = await seed_source_evidence(factory)
    proposal_id = uuid.uuid4()

    async with factory() as session, session.begin():
        await _insert_thread(session, proposal_id=proposal_id, artifact_id=artifact_id)
        await session.execute(
            text(
                "INSERT INTO arc_authoring_proposal_versions ("
                "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                "  opened_by_issuer, opened_by_subject, created_at"
                ") VALUES (:pid, 1, :aid, NULL, 'open', :sid, :issuer, :subject, :now)"
            ),
            {
                "pid": proposal_id,
                "aid": artifact_id,
                "sid": source_evidence_id,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
                "now": _NOW,
            },
        )

    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO arc_authoring_proposal_versions ("
                    "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                    "  opened_by_issuer, opened_by_subject, created_at"
                    ") VALUES (:pid, 1, :aid, NULL, 'rejected', :sid, :issuer, :subject, :now)"
                ),
                {
                    "pid": proposal_id,
                    "aid": artifact_id,
                    "sid": source_evidence_id,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                    "now": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_revision_id_bijection_rejects_a_second_version_with_the_same_revision(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`UNIQUE (revision_id)` is the `(proposal_id, proposal_version) <->
    revision_id` bijection: two different versions may never point at the
    same revision."""
    artifact_id = await seed_artifact_family(factory)
    source_evidence_id = await seed_source_evidence(factory)
    revision_id = await _seed_bare_revision(factory, artifact_id)
    proposal_id = uuid.uuid4()

    async with factory() as session, session.begin():
        await _insert_thread(session, proposal_id=proposal_id, artifact_id=artifact_id)
        # Two *terminal* versions on the same thread -- this is the shape
        # the partial unique index must allow (see the test below); what
        # must not be allowed is both claiming the same revision_id.
        await session.execute(
            text(
                "INSERT INTO arc_authoring_proposal_versions ("
                "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id, revision_id,"
                "  opened_by_issuer, opened_by_subject, created_at"
                ") VALUES (:pid, 1, :aid, NULL, 'activated', :sid, :rid, :issuer, :subject, :now)"
            ),
            {
                "pid": proposal_id,
                "aid": artifact_id,
                "sid": source_evidence_id,
                "rid": revision_id,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
                "now": _NOW,
            },
        )

    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO arc_authoring_proposal_versions ("
                    "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id, revision_id,"
                    "  opened_by_issuer, opened_by_subject, created_at"
                    ") VALUES (:pid, 2, :aid, NULL, 'rejected', :sid, :rid, :issuer, :subject, :now)"
                ),
                {
                    "pid": proposal_id,
                    "aid": artifact_id,
                    "sid": source_evidence_id,
                    "rid": revision_id,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                    "now": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_partial_unique_allows_many_terminal_but_only_one_live_version(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The constraint's whole purpose, proven both ways: a second *live*
    (`open`/`submitted`/`approved`) row for the same thread is refused, but
    any number of *terminal* rows are not -- the partial index only ever
    locks out a live competitor, never history."""
    artifact_id = await seed_artifact_family(factory)
    source_evidence_id = await seed_source_evidence(factory)
    proposal_id = uuid.uuid4()

    async def _insert_version(proposal_version: int, state: str) -> None:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_authoring_proposal_versions ("
                    "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                    "  opened_by_issuer, opened_by_subject, created_at"
                    ") VALUES (:pid, :pv, :aid, NULL, :state, :sid, :issuer, :subject, :now)"
                ),
                {
                    "pid": proposal_id,
                    "pv": proposal_version,
                    "aid": artifact_id,
                    "state": state,
                    "sid": source_evidence_id,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                    "now": _NOW,
                },
            )

    async with factory() as session, session.begin():
        await _insert_thread(session, proposal_id=proposal_id, artifact_id=artifact_id)

    # One live row: the first version, still open.
    await _insert_version(1, "open")

    # A second *live* row for the same thread is refused -- this is the
    # rule's whole point, proven directly rather than only through
    # `ProposalService`'s own application-level check.
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO arc_authoring_proposal_versions ("
                    "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                    "  opened_by_issuer, opened_by_subject, created_at"
                    ") VALUES (:pid, 2, :aid, NULL, 'submitted', :sid, :issuer, :subject, :now)"
                ),
                {
                    "pid": proposal_id,
                    "aid": artifact_id,
                    "sid": source_evidence_id,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                    "now": _NOW,
                },
            )

    # Terminalize version 1, then land version 2 as terminal too -- neither
    # touches the partial index, so both must succeed even though version
    # 1's row (now `rejected`) is still there.
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_authoring_proposal_versions SET state = 'rejected' "
                "WHERE proposal_id = :pid AND proposal_version = 1"
            ),
            {"pid": proposal_id},
        )
    await _insert_version(2, "withdrawn")
    await _insert_version(3, "superseded")
    await _insert_version(4, "stale")

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_authoring_proposal_versions WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).scalar()
    assert count == 4, "four terminal versions must all coexist once none of them is live"


@pytest.mark.asyncio
async def test_state_check_rejects_a_value_outside_the_closed_vocabulary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id = await seed_artifact_family(factory)
    source_evidence_id = await seed_source_evidence(factory)
    proposal_id = uuid.uuid4()

    async with factory() as session, session.begin():
        await _insert_thread(session, proposal_id=proposal_id, artifact_id=artifact_id)

    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO arc_authoring_proposal_versions ("
                    "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                    "  opened_by_issuer, opened_by_subject, created_at"
                    ") VALUES (:pid, 1, :aid, NULL, 'not_a_real_state', :sid, :issuer, :subject, :now)"
                ),
                {
                    "pid": proposal_id,
                    "aid": artifact_id,
                    "sid": source_evidence_id,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                    "now": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_artifact_title_length_check_rejects_an_overlong_title(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`ck_arc_artifacts_title_len`: a fixture that only ever seeds a short
    title would satisfy the NOT NULL this migration added without ever
    exercising this CHECK -- proven directly here instead."""
    artifact_id = uuid.uuid4()
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO arc_artifacts ("
                    "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                    ") VALUES (:aid, NULL, :slug, 'policy', :title, :now, :issuer, :subject)"
                ),
                {
                    "aid": artifact_id,
                    "slug": f"toolong-{artifact_id.hex[:8]}",
                    "title": "x" * 201,
                    "now": _NOW,
                    "issuer": _ISSUER,
                    "subject": _OPERATOR,
                },
            )

    async with factory() as session, session.begin():
        # The boundary case succeeds: this is what proves the refusal above
        # is about length, not about the row being otherwise malformed.
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, NULL, :slug, 'policy', :title, :now, :issuer, :subject)"
            ),
            {
                "aid": artifact_id,
                "slug": f"exactly200-{artifact_id.hex[:8]}",
                "title": "x" * 200,
                "now": _NOW,
                "issuer": _ISSUER,
                "subject": _OPERATOR,
            },
        )


async def _seed_bare_revision(factory: async_sessionmaker[AsyncSession], artifact_id: uuid.UUID) -> uuid.UUID:
    """The minimum `arc_revisions` row a `revision_id` foreign key needs.
    Content is inert -- these tests exercise the proposal-version bijection
    constraint, not revision materialisation itself.
    """
    revision_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES ("
                # No body at all (neither ciphertext nor plaintext): the
                # artifact here is global, and a global revision must never
                # carry plaintext (`ck_arc_revisions_no_global_plaintext`).
                "  :rid, :aid, NULL, 'test-system', :locator, :revision_locator, :digest, 'draft', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "revision_locator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": now - datetime.timedelta(days=1),
                "review": now + datetime.timedelta(days=365),
                "retention": now + datetime.timedelta(days=730),
                "now": now,
            },
        )
    return revision_id
