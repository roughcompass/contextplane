"""A rule below `global` must name what it is scoped to, and SQL says so.

`scope` buys precedence -- `_SCOPE_ORDER` ranks domain above entity above
intent -- while an empty selector array means "no constraint on this dimension".
A rule claiming a narrow scope and selecting nothing therefore takes the
precedence without the narrowing and matches every manifest, at a higher rank
than the rules that did narrow. Domain and intent scope were unconstrained on
both sides until `0063_rule_scope_selectors`.

**Written against the live schema rather than the Python guard.**
`ApplicabilityRule.__post_init__` refuses these shapes too, and that guard is
the one authors hit. But it is not the only writer: `submission.py` builds
`MaterialisedApplicabilityRule` -- a different dataclass with no such guard --
straight from a candidate payload and inserts it, catching only `IntegrityError`
from the table's own constraints. So the CHECK is what actually holds this write
path, and a test that constructed `ApplicabilityRule` would be testing the wrong
one.

Read through `pg_get_constraintdef` and by attempting the insert, because a
migration that was written and never applied passes a source comparison and says
nothing about what the database will enforce.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc

#: Each narrow scope, the selector columns that satisfy it, and a values clause
#: that names none of them. `global` is absent on purpose: matching everything
#: is what it means, so it is the one scope with nothing to name.
_NARROW_SCOPES: tuple[tuple[str, str], ...] = (
    ("tenant", "ck_arc_rules_tenant_scope_target"),
    ("domain", "ck_arc_rules_domain_scope_target"),
    ("entity", "ck_arc_rules_entity_scope_target"),
    ("intent", "ck_arc_rules_intent_scope_target"),
)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-scope-selector")


async def _insert_rule(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, scope: str, **selectors: object
) -> None:
    columns = ["rule_id", "revision_id", "tenant_id", "scope", "effective_from", "is_mandatory"]
    values = [":rule_id", ":revision_id", ":tenant_id", ":scope", ":effective_from", "TRUE"]
    params: dict[str, object] = {
        "rule_id": uuid.uuid4(),
        "revision_id": seed.revision_id,
        "tenant_id": seed.tenant_id,
        "scope": scope,
        "effective_from": ARC_NOW,
    }
    for name, value in selectors.items():
        columns.append(name)
        values.append(f":{name}")
        params[name] = value

    # Column names are interpolated, values are bound. The names come from this
    # module's own literals and the caller's keyword arguments, never from data.
    async with factory() as session, session.begin():
        await session.execute(
            text(f"INSERT INTO arc_applicability_rules ({', '.join(columns)}) VALUES ({', '.join(values)})"),
            params,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("scope", "constraint"), _NARROW_SCOPES)
async def test_a_narrow_scope_with_no_selector_is_refused(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, scope: str, constraint: str
) -> None:
    with pytest.raises(IntegrityError) as caught:
        await _insert_rule(factory, seed, scope)

    assert constraint in str(caught.value), f"{scope} was refused, but not by {constraint}"


@pytest.mark.asyncio
async def test_an_intent_scoped_rule_needs_the_action_class_too(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The half that decides whether an obligation is owed for what is being done.

    An intent kind alone says "this is a deployment" and leaves every action
    within it unconstrained. `ck_arc_exceptions_scope_selectors` has always
    required both of an exception; this is the rule side catching up.
    """
    with pytest.raises(IntegrityError) as caught:
        await _insert_rule(factory, seed, "intent", intent_kinds=["deployment"])

    assert "ck_arc_rules_intent_scope_target" in str(caught.value)

    # Both together are accepted.
    await _insert_rule(factory, seed, "intent", intent_kinds=["deployment"], action_classes=["deploy"])


@pytest.mark.asyncio
async def test_a_global_rule_needs_no_selector(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """The one exemption, asserted so the guard cannot quietly grow to cover it."""
    await _insert_rule(factory, seed, "global")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "selector"),
    (
        ("domain", {"domain_ids": ["payments"]}),
        ("entity", {"entity_ids": [uuid.UUID("cccccccc-0000-4000-8000-000000000001")]}),
    ),
)
async def test_a_narrow_scope_naming_its_selector_is_accepted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, scope: str, selector: dict[str, object]
) -> None:
    """The constraints refuse the empty shape without refusing the useful one."""
    await _insert_rule(factory, seed, scope, **selector)


@pytest.mark.asyncio
async def test_every_narrow_scope_is_covered_by_a_constraint(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The gate is only worth having if it knows about every scope.

    A sixth `AuthorityScope` added without a selector constraint would be free
    to take precedence while narrowing nothing, and nothing else would notice.
    """
    from contextplane.arc.types import AuthorityScope

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'arc_applicability_rules' AND c.contype = 'c' "
                    "  AND conname LIKE '%_scope_target'"
                )
            )
        ).all()

    assert {row[0] for row in rows} == {constraint for _scope, constraint in _NARROW_SCOPES}
    assert {scope for scope, _c in _NARROW_SCOPES} == {str(s) for s in AuthorityScope if s is not AuthorityScope.GLOBAL}
