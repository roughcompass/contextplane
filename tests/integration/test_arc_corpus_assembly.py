"""Assembling the governed corpus selection decides over.

`select()` is pure, so everything about *which* rows reach it is decided by
the queries in `CorpusReader`. Three properties matter enough to pin, and
they are not the same property:

**The candidate query may only ever widen.** `rule_applies` is authoritative
for rule matching and treats an empty selector as "matches any", so any SQL
predicate over those selector arrays would drop precisely the broadest
rules. A global obligation naming no task kind is the one an operator is
least willing to lose.

**The tenant predicate is the isolation boundary.** `rule_applies` checks
the requesting tenant only for a `tenant`-scoped rule. A `domain`-,
`capability`-, or `task`-scoped rule gets no tenant check there at all, so
if one of those ever loads from another tenant it applies -- silently, and
with full force. Nothing downstream would catch it.

**Obligations are authoritative here, not a prefilter.** `select()` blocks
on any missing obligation without filtering by applicability, so one
irrelevant obligation loaded here blocks every resolution in the tenant.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.corpus import CorpusReader
from registry.arc.service.selection import select
from registry.arc.types import ActionClass, AuthorityScope, TaskKind, TaskManifest
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-corpus")


def _manifest(**overrides: object) -> TaskManifest:
    fields: dict[str, object] = {
        "session_id": "sess-corpus",
        "task_kind": TaskKind.DEPLOYMENT,
        "requested_action_classes": frozenset({ActionClass.DEPLOY}),
        "environment": "production",
        "data_sensitivity": "internal",
        "repository_identity": "git://example/repo",
    }
    fields.update(overrides)
    return TaskManifest(**fields)  # type: ignore[arg-type]


async def _add_rule(
    factory: async_sessionmaker[AsyncSession],
    seed: ArcSeed,
    *,
    revision_id: uuid.UUID | None = None,
    scope: str = "global",
    target_tenant_id: uuid.UUID | None = None,
    task_kinds: list[str] | None = None,
    is_mandatory: bool = True,
    on_global_revision: bool = False,
) -> uuid.UUID:
    """Attach an applicability rule to a revision.

    `task_kinds=None` is the interesting default: a rule constraining no task
    kind, which `rule_applies` matches against everything.

    `on_global_revision` nulls the rule's tenant, which the schema now requires:
    a rule may only name the tenant its revision names, so a rule on a
    global revision must carry NULL. Expressed as a flag rather than an
    optional tenant id because `None` there is ambiguous between "not supplied"
    and "deliberately global" -- a distinction the first version of this helper
    got wrong and silently fell back to the seed's tenant.
    """
    rule_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  rule_id, revision_id, tenant_id, scope, target_tenant_id, task_kinds,"
                "  effective_from, is_mandatory"
                ") VALUES (:rid, :rev, :tid, :scope, :target, CAST(:kinds AS TEXT[]), :efrom, :mand)"
            ),
            {
                "rid": rule_id,
                "rev": revision_id or seed.revision_id,
                "tid": None if on_global_revision else seed.tenant_id,
                "scope": scope,
                "target": target_tenant_id,
                "kinds": task_kinds,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "mand": is_mandatory,
            },
        )
    return rule_id


async def _reader(factory: async_sessionmaker[AsyncSession]) -> CorpusReader:
    return CorpusReader(factory)


# --- candidates: the prefilter may widen, never narrow -------------------------


@pytest.mark.asyncio
async def test_a_directive_in_my_tenant_with_a_matching_rule_is_a_candidate(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _add_rule(factory, seed, task_kinds=["deployment"])

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert [d.directive_id for d, _, _ in assembled.candidates] == [seed.directive_id]


@pytest.mark.asyncio
async def test_a_rule_naming_no_task_kind_is_still_a_candidate(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The case a SQL `= ANY(task_kinds)` predicate would silently drop.

    An empty selector means "no constraint on this dimension", so this rule
    applies to every manifest -- which makes it exactly the rule a global
    obligation is written as, and the worst one to lose.
    """
    await _add_rule(factory, seed, task_kinds=None)

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert [d.directive_id for d, _, _ in assembled.candidates] == [seed.directive_id]
    # And it must survive the authoritative matcher too, not merely load.
    assert select(assembled).mandatory


@pytest.mark.asyncio
async def test_a_rule_for_another_task_kind_still_loads_and_selection_drops_it(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Narrowing is selection's job, and doing it twice is how the two drift.

    The row loading is not a bug -- it is the contract. What matters is that
    the authoritative matcher is the one that rejects it.
    """
    await _add_rule(factory, seed, task_kinds=["read_only"])

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert len(assembled.candidates) == 1
    assert not select(assembled).mandatory


# --- the tenant boundary -------------------------------------------------------


@pytest.mark.asyncio
async def test_another_tenants_domain_scoped_rule_never_loads(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The isolation property, on the scope that has no other guard.

    `rule_applies` tenant-checks only `tenant`-scoped rules. A
    `domain`-scoped rule belonging to someone else would pass every check
    downstream, so if this query ever loads one, that tenant's policy
    silently governs this tenant's agents.
    """
    other = await seed_arc(factory, slug_prefix="arc-corpus-other")
    await _add_rule(factory, other, scope="domain", task_kinds=None)

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    leaked = [d.directive_id for d, _, _ in assembled.candidates if d.directive_id == other.directive_id]
    assert not leaked, "another tenant's domain-scoped rule reached this tenant's selection"


@pytest.mark.asyncio
async def test_a_global_revision_is_a_candidate_for_any_tenant(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """`tenant_id IS NULL` means deployment-wide, and must not be filtered out
    by the same predicate that excludes other tenants."""
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_artifacts SET tenant_id = NULL WHERE artifact_id = :aid"),
            {"aid": seed.artifact_id},
        )
        # Global content may not be plaintext: global rows use the deployment
        # key hierarchy, which the schema enforces.
        await session.execute(
            text("UPDATE arc_revisions SET tenant_id = NULL, source_body_plaintext = NULL " "WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )
        await session.execute(
            text(
                "UPDATE arc_directives SET tenant_id = NULL, compact_statement_plaintext = NULL, "
                "  compact_statement_ciphertext = :ct WHERE revision_id = :rid"
            ),
            {"rid": seed.revision_id, "ct": b"sealed"},
        )
    await _add_rule(factory, seed, scope="global", task_kinds=None, on_global_revision=True)

    unrelated = uuid.uuid4()
    try:
        assembled = await (await _reader(factory)).assemble(tenant_id=unrelated, manifest=_manifest(), as_of=ARC_NOW)
        assert [d.directive_id for d, _, _ in assembled.candidates] == [seed.directive_id]
    finally:
        # Restored, because global is the one scope that is visible from
        # every other test's tenant. Left behind, this revision would join
        # the candidate set of every later assembly in the session and turn
        # unrelated assertions into confusing failures.
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE arc_revisions SET tenant_id = :tid WHERE revision_id = :rid"),
                {"tid": seed.tenant_id, "rid": seed.revision_id},
            )
            await session.execute(
                text("UPDATE arc_artifacts SET tenant_id = :tid WHERE artifact_id = :aid"),
                {"tid": seed.tenant_id, "aid": seed.artifact_id},
            )
            await session.execute(
                text("UPDATE arc_directives SET tenant_id = :tid WHERE revision_id = :rid"),
                {"tid": seed.tenant_id, "rid": seed.revision_id},
            )
            await session.execute(
                text("UPDATE arc_applicability_rules SET tenant_id = :tid WHERE revision_id = :rid"),
                {"tid": seed.tenant_id, "rid": seed.revision_id},
            )


# --- revision lifecycle and window --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["draft", "revoked", "superseded"])
async def test_a_revision_that_does_not_govern_is_not_a_candidate(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, state: str
) -> None:
    """Only `active` and `expired` govern. Selection re-checks none of this,
    so a draft loaded here would be enforced as though it had shipped."""
    await _add_rule(factory, seed, task_kinds=None)
    # The schema requires a revoked revision to carry `revoked_at` and a
    # superseded one to name its successor, so each state sets its own
    # companion column rather than one CASE expression whose NULL branch has
    # no inferable type.
    companion = {
        "draft": "",
        "revoked": ", revoked_at = :now",
        "superseded": ", superseded_by_revision_id = :rid",
    }[state]
    async with factory() as session, session.begin():
        await session.execute(
            text(f"UPDATE arc_revisions SET lifecycle_state = :state{companion} " "WHERE revision_id = :rid"),
            {"state": state, "rid": seed.revision_id, "now": ARC_NOW},
        )

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert not assembled.candidates


@pytest.mark.asyncio
async def test_an_expired_revision_still_governs(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """A lapsed review does not release the obligation. Dropping it here
    would quietly convert "nobody re-approved this" into "this no longer
    applies", which is the opposite of what a review deadline means."""
    await _add_rule(factory, seed, task_kinds=None)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'expired' WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert [d.directive_id for d, _, _ in assembled.candidates] == [seed.directive_id]


# --- obligations are authoritative --------------------------------------------


async def _add_obligation(
    factory: async_sessionmaker[AsyncSession],
    seed: ArcSeed,
    *,
    snapshot: dict[str, object],
    state: str = "missing_revoked",
) -> uuid.UUID:
    obligation_id = uuid.uuid4()
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_mandatory_obligations ("
                "  obligation_id, artifact_id, directive_id, current_revision_id,"
                "  applicability_snapshot, applicability_digest, obligation_state, effective_from"
                ") VALUES (:oid, :aid, :did, :rid, CAST(:snap AS JSONB), :digest, :state, :efrom)"
            ),
            {
                "oid": obligation_id,
                "aid": seed.artifact_id,
                "did": seed.directive_id,
                # A satisfied obligation must name the revision satisfying
                # it; a tombstoned one must not, because there is no longer a
                # revision to name. The schema enforces the first half.
                "rid": seed.revision_id if state == "satisfied" else None,
                "snap": canonical,
                "digest": "c" * 64,
                "state": state,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
            },
        )
    return obligation_id


@pytest.mark.asyncio
async def test_a_missing_obligation_that_applies_is_loaded_and_blocks(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The tombstone doing its job: nothing is present to point at, and the
    resolution still blocks."""
    await _add_obligation(factory, seed, snapshot={"scope": "global", "task_kinds": ["deployment"]})

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert len(assembled.obligations) == 1
    assert select(assembled).blocked_reasons


@pytest.mark.asyncio
async def test_an_obligation_for_a_different_task_kind_does_not_block(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """`select()` blocks on ANY missing obligation it is handed, with no
    applicability filter of its own. So one irrelevant obligation loaded here
    would block every resolution in the tenant -- an outage, produced by a
    tombstone for something the caller never asked to do."""
    await _add_obligation(factory, seed, snapshot={"scope": "global", "task_kinds": ["read_only"]})

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert not assembled.obligations
    assert not select(assembled).blocked_reasons


@pytest.mark.asyncio
async def test_another_tenants_obligation_does_not_block_me(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    other = await seed_arc(factory, slug_prefix="arc-corpus-obl")
    await _add_obligation(factory, other, snapshot={"scope": "global"})

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert not assembled.obligations


@pytest.mark.asyncio
async def test_an_unreadable_obligation_snapshot_still_blocks(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """An unreadable tombstone must be treated as applying, not dropped.

    This is the opposite of what an unreadable *exception* gets, and the
    asymmetry is the whole point: dropping an exception leaves the stricter
    original in force, while dropping an obligation removes a blocker. A
    resolution that came back `ready` because nobody could parse the row
    recording a revoked mandatory control is precisely the failure these
    tombstones exist to prevent.

    Reachable because the snapshot is JSONB frozen at activation and never
    revalidated -- narrowing a closed vocabulary later, or reading a row
    written by an older build, produces exactly this.
    """
    await _add_obligation(factory, seed, snapshot={"scope": "not-a-real-scope"})

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert len(assembled.obligations) == 1
    assert select(assembled).blocked_reasons, "an unreadable missing obligation must still block"


@pytest.mark.asyncio
async def test_an_unreadable_but_satisfied_obligation_does_not_block(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Including unreadable obligations costs nothing when they are satisfied.

    `is_missing` is False for a satisfied row, so fail-closed on the
    unreadable case does not turn every parse problem into an outage -- only
    the rows that are both unreadable *and* unsatisfied block.
    """
    await _add_obligation(factory, seed, snapshot={"scope": "not-a-real-scope"}, state="satisfied")

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert len(assembled.obligations) == 1
    assert not select(assembled).blocked_reasons


@pytest.mark.asyncio
async def test_a_satisfied_obligation_is_loaded_but_does_not_block(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _add_obligation(
        factory,
        seed,
        snapshot={"scope": "global", "task_kinds": ["deployment"]},
        state="satisfied",
    )

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    assert len(assembled.obligations) == 1
    assert not select(assembled).blocked_reasons


# --- rule rehydration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rule_scope_survives_the_round_trip(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """Scope drives precedence ordering, so a scope read back wrong would
    reorder which directive wins without any other symptom."""
    await _add_rule(factory, seed, scope="tenant", target_tenant_id=seed.tenant_id, task_kinds=None)

    assembled = await (await _reader(factory)).assemble(tenant_id=seed.tenant_id, manifest=_manifest(), as_of=ARC_NOW)

    ((_, rule, _),) = assembled.candidates
    assert rule.scope is AuthorityScope.TENANT
    assert rule.target_tenant_id == seed.tenant_id
