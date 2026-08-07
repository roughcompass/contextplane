"""ADR 041 Sec.9: the shadow overlay must not be achieved by widening the
production corpus query's lifecycle filter.

Three claims, proven independently so neither can hide a gap in the other:

**Against real Postgres: `corpus.py`'s own lifecycle filter genuinely
excludes what it claims to, in both directions.** Five revisions, one per
`RevisionLifecycleState` member, are seeded under one tenant and read back
through `CorpusReader._candidates` -- the exact prefilter every resolution
already runs through. `active`/`expired` must appear; `superseded`/
`revoked`/`draft` must not. A filter proven only on the happy path (every
eligible revision merely present) is indistinguishable from no filter at
all, so both directions are asserted from the same seeded set in one call.

**`_SELECTABLE_LIFECYCLE` itself is unchanged.** Asserted against its
exact, named value -- `("active", "expired")` -- so a future edit that
widened it to admit `draft` (the shortcut this task's contract forbids)
fails here immediately, before the real-Postgres check above would even
need to catch it.

**`shadow.py`'s overlay is what (and the only thing that) admits the one
draft under observation.** `overlay_candidate_set` is pure and DB-free by
construction; the candidate's own entries are built from its frozen JSON
document, never from a lifecycle-filtered row, and the substituted
family's own baseline entry is removed rather than double-counted.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import registry.arc.service.corpus as corpus_module
from registry.arc.service.corpus import CorpusReader
from registry.arc.service.shadow import overlay_candidate_set
from registry.arc.types import (
    ApplicabilityRule,
    AuthorityScope,
    Directive,
    DirectiveType,
)
from tests.helpers.arc_fixtures import AllowAllIntegrity
from tests.helpers.seeding import seed_tenant_and_actor

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

# Appendix A.3's own closed vocabulary, restated here as the exact set this
# test must cover both directions of -- not a subset, and not a guess at
# what "every lifecycle state" means. `active` is seeded first: `superseded`
# needs an existing revision to point its `superseded_by_revision_id` FK at.
_ALL_LIFECYCLE_STATES = ("active", "draft", "superseded", "revoked", "expired")


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_revision_with_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    lifecycle_state: str,
    superseded_by_revision_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One artifact + one revision (in *lifecycle_state*) + one directive +
    one global rule -- the minimum `CorpusReader._candidates` joins across.

    `superseded`/`revoked` each carry a same-transaction CHECK the baseline
    migration already enforces (`ck_arc_revisions_superseded_link`/`ck_arc_
    revisions_revoked_at`) -- satisfied here with a real FK target and a
    real timestamp rather than relaxed for the seed.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    rule_id = uuid.uuid4()
    revoked_at = _NOW if lifecycle_state == "revoked" else None
    await session.execute(
        text(
            "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, title, created_at, "
            " created_by_issuer, created_by_subject) "
            "VALUES (:aid, :tid, :slug, 'policy', :title, :now, 'https://idp.example.test', 'seed')"
        ),
        {"aid": artifact_id, "tid": tenant_id, "slug": f"lc-{revision_id.hex[:8]}", "title": "t", "now": _NOW},
    )
    await session.execute(
        text(
            "INSERT INTO arc_revisions (revision_id, artifact_id, tenant_id, source_system, "
            " source_canonical_locator, source_revision_locator, content_digest, lifecycle_state, "
            " effective_from, review_expires_at, detail_audience, freshness_basis, content_classification, "
            " content_retention_until, content_storage_mode, source_body_plaintext, created_at, "
            " superseded_by_revision_id, revoked_at) "
            "VALUES (:rid, :aid, :tid, 'test', :loc, :revloc, :digest, :state, :efrom, :review, "
            " 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', 'body', :now, "
            " :superseded_by, :revoked_at)"
        ),
        {
            "rid": revision_id,
            "aid": artifact_id,
            "tid": tenant_id,
            "loc": f"loc://{revision_id.hex[:8]}",
            "revloc": f"loc://{revision_id.hex[:8]}@1",
            "digest": revision_id.hex + revision_id.hex,
            "state": lifecycle_state,
            "efrom": _NOW - datetime.timedelta(days=1),
            "review": _NOW + datetime.timedelta(days=365),
            "retention": _NOW + datetime.timedelta(days=730),
            "now": _NOW,
            "superseded_by": superseded_by_revision_id,
            "revoked_at": revoked_at,
        },
    )
    await session.execute(
        text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:did, :aid)"),
        {"did": directive_id, "aid": artifact_id},
    )
    await session.execute(
        text(
            "INSERT INTO arc_directives (directive_id, revision_id, tenant_id, directive_type, "
            " compact_statement_plaintext, source_anchor) "
            "VALUES (:did, :rid, :tid, 'citation_only', 'stmt', 'anchor')"
        ),
        {"did": directive_id, "rid": revision_id, "tid": tenant_id},
    )
    await session.execute(
        text(
            "INSERT INTO arc_applicability_rules (rule_id, revision_id, scope, effective_from, is_mandatory) "
            "VALUES (:rlid, :rid, 'global', :efrom, false)"
        ),
        {"rlid": rule_id, "rid": revision_id, "efrom": _NOW - datetime.timedelta(days=1)},
    )
    return revision_id


@pytest.mark.asyncio
async def test_production_query_includes_eligible_and_excludes_every_ineligible_state(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """One revision per `RevisionLifecycleState` member, read back through
    the exact prefilter production resolution uses. `active`/`expired`
    (the two `_SELECTABLE_LIFECYCLE` names) must appear; every other
    member must not -- proven from the same seeded set in the same call,
    so passing on the included half cannot hide a failure on the excluded
    half.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug="arc-lifecycle-filter")
    revision_by_state: dict[str, uuid.UUID] = {}
    async with factory() as session, session.begin():
        for state in _ALL_LIFECYCLE_STATES:
            superseded_by = revision_by_state.get("active") if state == "superseded" else None
            revision_by_state[state] = await _seed_revision_with_rule(
                session, tenant_id=tenant_id, lifecycle_state=state, superseded_by_revision_id=superseded_by
            )

    reader = CorpusReader(factory, integrity=AllowAllIntegrity())  # type: ignore[arg-type]
    async with factory() as session:
        candidates = await reader._candidates(session, tenant_id=tenant_id, as_of=_NOW)
    seen_revision_ids = {directive.revision_id for directive, _rule, _eff in candidates}

    for eligible in ("active", "expired"):
        assert revision_by_state[eligible] in seen_revision_ids, f"{eligible!r} must be included"
    for ineligible in ("draft", "superseded", "revoked"):
        assert revision_by_state[ineligible] not in seen_revision_ids, f"{ineligible!r} must be excluded"


def test_corpus_selectable_lifecycle_is_exactly_active_and_expired() -> None:
    """The production query's own lifecycle parameter list, unchanged.
    Widening this tuple (to admit `draft`, for instance) is exactly the
    shortcut ADR 041 Sec.9 forbids -- shadow must reach a draft through
    its own overlay, never through this list."""
    assert corpus_module._SELECTABLE_LIFECYCLE == ("active", "expired")


def _entry(
    revision_id: uuid.UUID, *, directive_id: uuid.UUID | None = None
) -> tuple[Directive, ApplicabilityRule, datetime.datetime]:
    did = directive_id or uuid.uuid4()
    directive = Directive(
        directive_id=did,
        revision_id=revision_id,
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="anchor",
    )
    rule = ApplicabilityRule(rule_id=uuid.uuid4(), revision_id=revision_id, scope=AuthorityScope.GLOBAL)
    return (directive, rule, _NOW)


def test_overlay_substitutes_the_family_baseline_without_touching_others() -> None:
    """The pure half of the proof: the candidate's own entries (built from
    JSON, never from a lifecycle-filtered row) enter through the overlay;
    the substituted family's own baseline is removed rather than double-
    counted; an unrelated family's entry is untouched."""
    other_family_revision = uuid.uuid4()
    baseline_revision = uuid.uuid4()
    candidate_revision = uuid.uuid4()

    baseline_candidates = (_entry(other_family_revision), _entry(baseline_revision))
    overlay_entries = (_entry(candidate_revision),)

    result = overlay_candidate_set(
        baseline_candidates, baseline_revision_id=baseline_revision, overlay_entries=overlay_entries
    )
    result_revision_ids = {directive.revision_id for directive, _rule, _eff in result}

    assert result_revision_ids == {other_family_revision, candidate_revision}, (
        "the overlay's output must be exactly the untouched unrelated entry plus the candidate's own -- "
        "not the substituted baseline"
    )


def test_overlay_with_no_baseline_to_substitute_only_adds_the_candidate() -> None:
    """A brand-new artifact family (no active baseline yet, `baseline_
    revision_id=None`) adds the candidate without removing anything --
    proving the "drop" half of the overlay is conditional on there being
    something to drop, not an unconditional wipe."""
    unrelated_revision = uuid.uuid4()
    candidate_revision = uuid.uuid4()
    baseline_candidates = (_entry(unrelated_revision),)
    overlay_entries = (_entry(candidate_revision),)

    result = overlay_candidate_set(baseline_candidates, baseline_revision_id=None, overlay_entries=overlay_entries)
    result_revision_ids = {directive.revision_id for directive, _rule, _eff in result}
    assert result_revision_ids == {unrelated_revision, candidate_revision}
