"""Which revisions exist, and the number the two terminal acts differ over.

E22-T8. Seven paths already act on a revision and every one is keyed by an id
the caller must already hold. There was no way to ask what exists, which is why
the lifecycle screen is four text boxes — nothing could have been designed well
against that surface.

The load-bearing assertion here is `resolutions_under_revision`. The screen's
own copy argues the two endings differ in **reach over the past**: invalidate
puts *"every resolution made while this revision was active"* in question,
revoke leaves *"everything resolved while this revision was in force"* standing.
Without that count a reader chooses between two irreversible acts on the
strength of a paragraph.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc import REVISION_MAX_PAGE_SIZE, RevisionIndexService, parse_revision_cursor
from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import seed_challenge
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)
_LATER = _NOW + datetime.timedelta(days=30)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"rev-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'op', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"rev-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"])


async def _revision(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext | None,
    *,
    tenant_id: uuid.UUID | None = None,
    state: str = "draft",
    slug: str | None = None,
    review_expires_at: datetime.datetime = _LATER,
    created_at: datetime.datetime = _NOW,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One artifact and one revision on it. Returns `(artifact_id, revision_id)`."""
    artifact_id, revision_id = uuid.uuid4(), uuid.uuid4()
    owner = tenant_id if ctx is None else ctx.tenant_id
    successor = uuid.uuid4() if state == "superseded" else None

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at,"
                "  created_by_issuer, created_by_subject"
                ") VALUES (:a, :t, :s, 'policy', :title, :n, :issuer, :subject)"
            ),
            {
                "a": artifact_id,
                "t": owner,
                "s": slug or f"policy-{artifact_id.hex[:8]}",
                "title": slug or f"Policy {artifact_id.hex[:8]}",
                "n": created_at,
                "issuer": "https://idp.example.test",
                "subject": f"seed-{artifact_id.hex[:8]}",
            },
        )
        if successor is not None:
            # A superseded revision names a real successor, and the CHECK fires
            # on insert -- so the successor is written first rather than linked
            # afterwards by an update that would never get the chance.
            await _insert_revision(
                session,
                revision_id=successor,
                artifact_id=artifact_id,
                owner=owner,
                state="active",
                created_at=created_at,
                review_expires_at=review_expires_at,
            )
        await _insert_revision(
            session,
            revision_id=revision_id,
            artifact_id=artifact_id,
            owner=owner,
            state=state,
            created_at=created_at,
            review_expires_at=review_expires_at,
            superseded_by=successor,
        )
    return artifact_id, revision_id


async def _insert_revision(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    artifact_id: uuid.UUID,
    owner: uuid.UUID | None,
    state: str,
    created_at: datetime.datetime,
    review_expires_at: datetime.datetime,
    superseded_by: uuid.UUID | None = None,
) -> None:
    """One revision row, with whatever its state obliges it to carry.

    `(source_system, source_revision_locator, content_digest)` is globally
    unique -- one upstream revision maps to one ARC revision -- so each row
    derives its own identity from its id.
    """
    await session.execute(
        text(
            "INSERT INTO arc_revisions ("
            "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
            "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
            "  review_expires_at, detail_audience, freshness_basis, content_classification,"
            "  content_retention_until, content_storage_mode,"
            "  revoked_at, activated_at, superseded_by_revision_id, created_at"
            ") VALUES ("
            "  :r, :a, :t, 'git', 'repo/policy.md', :loc, :digest, :state, :n,"
            "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
            "  :retention, 'none', :revoked_at, :activated_at, :superseded_by, :n)"
        ),
        {
            "r": revision_id,
            "a": artifact_id,
            "t": owner,
            "loc": f"commit:{revision_id.hex[:12]}",
            "digest": revision_id.hex + revision_id.hex,
            "state": state,
            "n": created_at,
            "review": review_expires_at,
            "retention": _LATER,
            # A state carries its own evidence: the schema refuses a `revoked`
            # revision with no `revoked_at` and a `superseded` one with no
            # successor. Seeding a state without it seeds a row that cannot exist.
            "revoked_at": created_at if state == "revoked" else None,
            "activated_at": created_at if state in {"active", "revoked", "superseded"} else None,
            "superseded_by": superseded_by,
        },
    )


async def _resolution_under(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    omitted: bool = False,
) -> None:
    """One receipt that selected this revision, optionally having omitted it.

    A receipt needs a consumed challenge and a dozen NOT NULL columns; the
    shape is copied from `test_arc_workers.py`'s own stub rather than
    reconstructed, because a seed that drifts from the schema fails as a
    fixture error wearing the shape of a result.
    """
    receipt_id = uuid.uuid4()
    challenge_id = await seed_challenge(factory, tenant_id=ctx.tenant_id)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_receipts ("
                "  receipt_id, challenge_id, tenant_id, actor_id, host_id, session_id,"
                "  manifest_fingerprint, attestation_id, resolution_status,"
                "  selection_engine_version, build_revision, canonical_profile_versions,"
                "  selection_config_digest, evaluated_at, freshness_basis, budget_limit_bytes,"
                "  response_replay_ciphertext, response_replay_nonce, response_replay_key_id"
                ") VALUES ("
                "  :r, :c, :t, :a, 'host-1', 'sess-1', :fp, :att, 'ready',"
                "  'test-engine', 'test-build', '{}', :digest, :n, 'revision_pinned_only', 12288,"
                "  :cipher, :nonce, 'test-key')"
            ),
            {
                "r": receipt_id,
                "c": challenge_id,
                "t": ctx.tenant_id,
                "a": ctx.actor_id,
                "fp": "f" * 64,
                "att": f"att-{receipt_id.hex[:12]}",
                "digest": "c" * 64,
                "n": _NOW,
                "cipher": b"stub-ciphertext",
                "nonce": b"stub-nonce-12",
            },
        )
        # A trigger refuses a challenge that has receipts and is not marked
        # consumed: one challenge is spent by exactly one resolution.
        await session.execute(
            text("UPDATE arc_context_challenges SET consumed_at = :n WHERE challenge_id = :c"),
            {"n": _NOW, "c": challenge_id},
        )
        await session.execute(
            text(
                "INSERT INTO arc_receipt_selected_revisions "
                "(receipt_id, revision_id, tenant_id, artifact_id, is_mandatory, was_omitted, omission_reason) "
                "VALUES (:r, :rev, :t, :a, TRUE, :omitted, :reason)"
            ),
            {
                "r": receipt_id,
                "rev": revision_id,
                "t": ctx.tenant_id,
                "a": artifact_id,
                "omitted": omitted,
                "reason": "not_applicable" if omitted else None,
            },
        )


def _index(factory: async_sessionmaker[AsyncSession]) -> RevisionIndexService:
    return RevisionIndexService(factory, clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_a_reader_can_ask_which_revisions_exist(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole of E22-T8. Every other path needs an id the caller already has."""
    ctx = await _tenant(factory)
    _, revision = await _revision(factory, ctx, slug="retry-policy")

    page = await _index(factory).list_revisions(ctx)

    rows = {row.revision_id: row for row in page.items}
    assert revision in rows
    assert rows[revision].artifact_slug == "retry-policy"
    assert rows[revision].artifact_kind == "policy"


@pytest.mark.asyncio
async def test_the_row_says_how_much_was_decided_under_the_revision(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The field the choice between the two terminal acts turns on.

    Invalidate puts these resolutions in question; revoke leaves them standing.
    A row without the number leaves a reader choosing on prose.
    """
    ctx = await _tenant(factory)
    artifact, revision = await _revision(factory, ctx, state="active")
    for _ in range(3):
        await _resolution_under(factory, ctx, artifact_id=artifact, revision_id=revision)

    rows = {r.revision_id: r for r in (await _index(factory).list_revisions(ctx)).items}

    assert rows[revision].resolutions_under_revision == 3


@pytest.mark.asyncio
async def test_an_omitted_selection_was_not_something_decided_under(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A revision a resolution considered and left out is not one anything was
    decided under.

    Counting it would inflate the number a reader uses to judge blast radius, in
    the direction that makes invalidate look worse than it is — which is the
    direction that matters, because invalidate is the act that reaches backwards.
    """
    ctx = await _tenant(factory)
    artifact, revision = await _revision(factory, ctx, state="active")
    await _resolution_under(factory, ctx, artifact_id=artifact, revision_id=revision)
    await _resolution_under(factory, ctx, artifact_id=artifact, revision_id=revision, omitted=True)

    rows = {r.revision_id: r for r in (await _index(factory).list_revisions(ctx)).items}

    assert rows[revision].resolutions_under_revision == 1


@pytest.mark.asyncio
async def test_the_row_reports_columns_and_not_an_activation_verdict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three facts off the row, and no answer to "can this activate".

    That question is ten predicates computed as if the caller were the one
    activating, and it stays at its own endpoint. A list answering it would be a
    second, weaker computation two surfaces could disagree over.
    """
    ctx = await _tenant(factory)
    _, revision = await _revision(factory, ctx, state="draft", review_expires_at=_LATER)

    row = next(r for r in (await _index(factory).list_revisions(ctx)).items if r.revision_id == revision)

    # Approval evidence sits behind a chain of foreign keys, so the *True* case
    # is proved without a database in `tests/unit/test_arc_revision_index.py`;
    # what matters here is that all three are read off the row and that no
    # verdict field exists beside them.
    assert (row.is_draft, row.has_approval_evidence, row.review_expired) == (True, False, False)
    assert not hasattr(row, "can_activate")
    assert not hasattr(row, "eligible")


@pytest.mark.asyncio
async def test_an_expired_review_window_is_reported_against_the_clock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _tenant(factory)
    _, revision = await _revision(factory, ctx, review_expires_at=_NOW - datetime.timedelta(days=1))

    rows = {r.revision_id: r for r in (await _index(factory).list_revisions(ctx)).items}
    assert rows[revision].review_expired


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "terminal"),
    [("draft", False), ("active", False), ("revoked", True), ("expired", True), ("superseded", True)],
)
async def test_a_finished_revision_says_that_nothing_further_is_possible(
    factory: async_sessionmaker[AsyncSession], state: str, terminal: bool
) -> None:
    """A reader deciding what to do about a finished revision is told the answer
    is "nothing", rather than inferring it from a state name."""
    ctx = await _tenant(factory)
    _, revision = await _revision(factory, ctx, state=state)

    rows = {row.revision_id: row for row in (await _index(factory).list_revisions(ctx)).items}

    assert rows[revision].is_terminal is terminal


@pytest.mark.asyncio
async def test_a_platform_revision_is_visible_to_the_tenant_it_governs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant is governed by platform-scoped revisions, so a list that hid
    them would show a partial account of what is in force over them."""
    ctx = await _tenant(factory)
    _, platform = await _revision(factory, None, tenant_id=None, state="active")

    found = {row.revision_id for row in (await _index(factory).list_revisions(ctx)).items}

    assert platform in found


@pytest.mark.asyncio
async def test_another_tenants_revisions_are_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine, theirs = await _tenant(factory), await _tenant(factory)
    _, ours = await _revision(factory, mine)
    _, yours = await _revision(factory, theirs)

    found = {row.revision_id for row in (await _index(factory).list_revisions(mine)).items}

    assert ours in found
    assert yours not in found


@pytest.mark.asyncio
async def test_the_state_filter_is_closed(factory: async_sessionmaker[AsyncSession]) -> None:
    """An unrecognised state would return an empty page that reads as "none are
    in that state" rather than as "that is not a state"."""
    ctx = await _tenant(factory)
    with pytest.raises(ValidationError, match="unknown lifecycle state"):
        await _index(factory).list_revisions(ctx, lifecycle_state="probably_fine")


@pytest.mark.asyncio
async def test_the_state_filter_selects(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx = await _tenant(factory)
    _, draft = await _revision(factory, ctx, state="draft")
    _, active = await _revision(factory, ctx, state="active")

    found = {row.revision_id for row in (await _index(factory).list_revisions(ctx, lifecycle_state="active")).items}

    # Membership rather than equality: platform-scoped revisions are visible to
    # every tenant by design, so a set-equality assertion here would fail the
    # moment any other test seeds one — and would be asserting the absence of a
    # row this service is correct to return.
    assert active in found
    assert draft not in found


@pytest.mark.asyncio
async def test_the_cursor_walks_the_list_without_repeating_or_skipping(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both keyset components are fixed for a row's lifetime, which for a screen
    offering two irreversible acts is worth more than it costs."""
    ctx = await _tenant(factory)
    written = [
        (await _revision(factory, ctx, created_at=_NOW - datetime.timedelta(minutes=index)))[1] for index in range(5)
    ]

    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(20):
        page = await _index(factory).list_revisions(ctx, cursor=cursor, page_size=2)
        seen.extend(row.revision_id for row in page.items)
        if page.next_cursor is None:
            break
        cursor = parse_revision_cursor(page.next_cursor)

    # Filtered to this test's own rows: platform-scoped revisions seeded
    # elsewhere are legitimately in the page, and the property under test is
    # that the walk neither skips nor repeats — not that nothing else exists.
    mine = [revision for revision in seen if revision in set(written)]

    assert mine == written, "the cursor skipped or reordered a revision"
    assert len(seen) == len(set(seen)), "the cursor served a revision twice"


@pytest.mark.asyncio
async def test_a_page_larger_than_the_ceiling_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _tenant(factory)
    with pytest.raises(ValidationError, match="page_size"):
        await _index(factory).list_revisions(ctx, page_size=REVISION_MAX_PAGE_SIZE + 1)
