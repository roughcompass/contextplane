"""Publication is one-way: a revision that exists cannot be changed or removed.

Every later validator resolves against a published revision, so the guarantee
those validators depend on is not "the document was correct when written" but
"the document is still the one that was written". That makes immutability a
property of the write path rather than of anyone's discipline, and the write path
is what these tests exercise — through the service, against a real database, with
the constraints and triggers in place.

Publication also compiles before it writes, never after. A row that exists having
compiled nothing would be an authority for a schema no compiler ever accepted, and
nothing downstream could tell the difference.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.profile import service as profile_service
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition

_NAMESPACE = "northwind"


class _FixedClock:
    """A clock that does not move, so a digest is never a function of the time."""

    def __init__(self) -> None:
        import datetime

        self._now = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)

    def now(self) -> object:
        return self._now


def _entity(type_name: str, *, namespace: str = _NAMESPACE) -> EntityTypeDefinition:
    return EntityTypeDefinition(namespace=namespace, type_name=type_name)


def _relationship(type_name: str, *, namespace: str = _NAMESPACE) -> RelationshipTypeDefinition:
    return RelationshipTypeDefinition(
        namespace=namespace,
        type_name=type_name,
        source_type=f"{_NAMESPACE}:warehouse",
        destination_type=f"{_NAMESPACE}:depot",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )


def _relationship_to_a_missing_endpoint(type_name: str) -> RelationshipTypeDefinition:
    """A relationship naming an entity type the profile never declares.

    Chosen over a duplicate definition because the compiler *deduplicates*
    identical definitions rather than refusing them -- a set with the same entity
    twice compiles cleanly, so it proves nothing. An endpoint that names no
    declared type cannot ever be validated, which is a conflict the compiler does
    raise.
    """
    return RelationshipTypeDefinition(
        namespace=_NAMESPACE,
        type_name=type_name,
        source_type=f"{_NAMESPACE}:warehouse",
        destination_type=f"{_NAMESPACE}:no_such_type",
        direction="directed",
        cardinality_scope="per_source",
        authority="observed",
        cross_org_policy="deny",
    )


@pytest_asyncio.fixture
async def publication(pg_container: str) -> AsyncIterator[dict[str, object]]:
    """A tenant and a ProfileService wired to it."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'profiles')"),
                {"t": tenant_id, "s": f"pp-{tenant_id.hex[:10]}"},
            )
        yield {
            "factory": factory,
            "tenant": tenant_id,
            "service": profile_service.ProfileService(factory, clock=_FixedClock()),
        }
    finally:
        await engine.dispose()


async def _publish(
    fixture: dict[str, object],
    *,
    profile_name: str | None = None,
    semantic_version: str = "1.0.0",
    entities: Sequence[EntityTypeDefinition] | None = None,
    relationships: Sequence[RelationshipTypeDefinition] | None = None,
) -> profile_service.PublishedRevision:
    """Publish under a name unique to this test unless the caller pins one.

    `uq_profile_revisions_version` is on (family, name, version) and is **global**,
    not per-tenant -- a published profile is an authority the whole deployment
    resolves against, so two tenants cannot each own "platform/core/1.0.0". Every
    test in this file shares one database, so a fixed default name would make each
    test collide with its neighbours and pass or fail on execution order. A test
    that wants the collision asks for it by pinning the name.
    """
    service: profile_service.ProfileService = fixture["service"]  # type: ignore[assignment]
    tenant: uuid.UUID = fixture["tenant"]  # type: ignore[assignment]
    resolved = profile_name if profile_name is not None else f"core-{tenant.hex[:12]}"
    return await service.publish_revision(
        profile_family="platform",
        profile_name=resolved,
        semantic_version=semantic_version,
        entities=tuple(entities if entities is not None else (_entity("warehouse"), _entity("depot"))),
        relationships=tuple(relationships if relationships is not None else (_relationship("stocks"),)),
        interfaces=(),
        compatibility="backward_compatible",
        published_by="platform@example.test",
    )


# ---------------------------------------------------------------------------
# What a successful publication produces


async def test_a_published_revision_is_retrievable_by_its_own_identity(
    publication: dict[str, object],
) -> None:
    """The row is the authority, so reading it back is the whole contract."""
    published = await _publish(publication)
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    loaded = await service.get_revision(published.profile_revision_id)

    assert loaded is not None
    assert loaded.profile_revision_id == published.profile_revision_id
    assert loaded.profile_family == "platform"
    assert loaded.semantic_version == "1.0.0"
    assert loaded.document_digest == published.document_digest


async def test_the_document_digest_identifies_the_content_and_not_the_publication(
    publication: dict[str, object],
) -> None:
    """Two revisions with different content get different digests.

    Asserted rather than assumed because the digest is what every later validator
    compares against: a digest that varied per publication would make every
    comparison false, and one that ignored content would make every comparison
    true. Both failures are silent.
    """
    tenant: uuid.UUID = publication["tenant"]  # type: ignore[assignment]
    first = await _publish(publication, profile_name=f"alpha-{tenant.hex[:8]}")
    second = await _publish(
        publication,
        profile_name=f"beta-{tenant.hex[:8]}",
        entities=(_entity("warehouse"), _entity("depot"), _entity("annex")),
    )

    assert first.document_digest != second.document_digest


async def test_an_unknown_revision_reads_as_absent_rather_than_raising(
    publication: dict[str, object],
) -> None:
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]
    assert await service.get_revision(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# Immutability, which is the reason this service is the only writer


async def test_a_published_revision_cannot_be_updated(publication: dict[str, object]) -> None:
    """Refused at the service, not left to the caller's restraint.

    A validator that resolved a revision id twice and got two different documents
    would have no way to notice; there is no version to compare and no signature
    to break. So the write path refuses instead.
    """
    published = await _publish(publication)
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    with pytest.raises(profile_service.PublishedDocumentIsImmutable):
        await service.update_revision(published.profile_revision_id)


async def test_a_published_revision_cannot_be_deleted(publication: dict[str, object]) -> None:
    """Deletion is worse than mutation: every binding naming it dangles."""
    published = await _publish(publication)
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    with pytest.raises(profile_service.PublishedDocumentIsImmutable):
        await service.delete_revision(published.profile_revision_id)


async def test_republishing_the_same_identity_is_refused(publication: dict[str, object]) -> None:
    """The second write is the dangerous one, because it looks like an update.

    Family, name and version together are the identity a caller resolves by. A
    second document under that identity would make "which one did the validator
    see" unanswerable, so it is refused rather than versioned silently.
    """
    tenant: uuid.UUID = publication["tenant"]  # type: ignore[assignment]
    pinned = f"repub-{tenant.hex[:8]}"
    await _publish(publication, profile_name=pinned, semantic_version="1.0.0")

    with pytest.raises(profile_service.DuplicatePublicationError):
        await _publish(publication, profile_name=pinned, semantic_version="1.0.0")


async def test_a_second_version_of_the_same_profile_is_allowed(
    publication: dict[str, object],
) -> None:
    """The counterpart, so the refusal above is not mistaken for "one per name".

    A profile that could never publish a second version would make compatibility
    declarations pointless.
    """
    first = await _publish(publication, semantic_version="1.0.0")
    # Different content, not just a different version string: uq_profile_revisions_digest
    # is on (family, name, digest), so republishing identical bytes under a new version
    # is refused -- "a second row would give one document two revision ids".
    second = await _publish(
        publication,
        semantic_version="1.1.0",
        entities=(_entity("warehouse"), _entity("depot"), _entity("annex")),
    )

    assert first.profile_revision_id != second.profile_revision_id


# ---------------------------------------------------------------------------
# Compilation happens before the write, so a conflict never reaches the table


async def test_a_document_the_compiler_rejects_is_never_written(
    publication: dict[str, object],
) -> None:
    """A document that does not compile leaves no row behind.

    Checked by counting rows afterwards rather than by trusting the exception: a
    service that raised *after* inserting would pass an exception-only assertion
    while leaving exactly the authority this table must not contain.

    The conflict is an endpoint naming no declared type. A duplicate definition
    would not do -- the compiler deduplicates identical definitions, so a set
    containing the same entity twice compiles cleanly and this test would pass
    while proving nothing about rejection at all.
    """
    factory: async_sessionmaker[AsyncSession] = publication["factory"]  # type: ignore[assignment]
    async with factory() as session:
        before = (await session.execute(text("SELECT count(*) FROM profile_revisions"))).scalar_one()

    with pytest.raises(profile_service.ProfileConflictError):
        await _publish(publication, relationships=(_relationship_to_a_missing_endpoint("stocks"),))

    async with factory() as session:
        after = (await session.execute(text("SELECT count(*) FROM profile_revisions"))).scalar_one()
    assert after == before, "a rejected document left a row behind"


# ---------------------------------------------------------------------------
# Extensions, which are only meaningful against a core revision that exists


async def test_an_extension_publishes_against_an_existing_core_revision(
    publication: dict[str, object],
) -> None:
    core = await _publish(publication)
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    extension = await service.publish_extension(
        tenant_id=publication["tenant"],  # type: ignore[arg-type]
        namespace=_NAMESPACE,
        target_core_revision_id=core.profile_revision_id,
        entities=(_entity("bonded_warehouse"),),
        relationships=(),
        interfaces=(),
        published_by="tenant@example.test",
    )

    assert extension.target_core_revision_id == core.profile_revision_id
    assert extension.tenant_id == publication["tenant"]
    assert extension.document_digest


async def test_an_extension_naming_no_existing_core_revision_is_refused(
    publication: dict[str, object],
) -> None:
    """Resolved before compiling, so the failure names the target.

    Left to the foreign key, this surfaces as a constraint violation naming a
    column, which tells an operator nothing about which revision they got wrong.
    """
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    with pytest.raises(profile_service.ProfilePublicationError):
        await service.publish_extension(
            tenant_id=publication["tenant"],  # type: ignore[arg-type]
            namespace=_NAMESPACE,
            target_core_revision_id=uuid.uuid4(),
            entities=(_entity("bonded_warehouse"),),
            relationships=(),
            interfaces=(),
            published_by="tenant@example.test",
        )


async def test_an_extension_conflicting_inside_itself_is_reported_as_the_extensions(
    publication: dict[str, object],
) -> None:
    """An extension's families must stand on their own before composition.

    Compiled alone first, so a duplicate inside the extension is the extension's
    fault rather than being reported as an incompatibility with core — which is
    where an operator would then go looking.
    """
    core = await _publish(publication)
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]

    with pytest.raises(profile_service.ProfilePublicationError):
        await service.publish_extension(
            tenant_id=publication["tenant"],  # type: ignore[arg-type]
            namespace=_NAMESPACE,
            target_core_revision_id=core.profile_revision_id,
            entities=(_entity("bonded_warehouse"),),
            relationships=(_relationship_to_a_missing_endpoint("bonded_stocks"),),
            interfaces=(),
            published_by="tenant@example.test",
        )


# ---------------------------------------------------------------------------
# The guarantee that actually holds: the database refuses, not just the service
#
# The two tests above prove `ProfileService` declines to update or delete. That is
# the service keeping its own contract, and it is not the same claim as
# immutability -- an operator with a psql session never goes through the service.
# What makes a published document immutable is a BEFORE UPDATE OR DELETE trigger
# on each of the five published tables, and these tests go at it directly.


#: Each protected table with its own primary key, so the UPDATE below can assign a
#: column to itself. A shared column name would have been convenient and wrong:
#: three of these five have no `published_at`, so an update naming it fails on a
#: missing column and never reaches the trigger — passing for the wrong reason on
#: two tables and failing for the wrong reason on three.
#: The two the publication path actually populates, so a behavioural refusal can be
#: observed. A BEFORE UPDATE trigger never fires when no row matches, so pointing
#: this at an empty table would assert nothing while looking thorough.
_POPULATED_IMMUTABLE_TABLES = [
    ("profile_revisions", "profile_revision_id"),
    ("profile_extensions", "extension_revision_id"),
]

#: The other three carry the same trigger and have **no writer yet** --
#: `ProfileService` inserts into `profile_revisions` and `profile_extensions` only.
#: They are the compiled projections a later validator will read, so their
#: protection is asserted structurally here rather than behaviourally, and this list
#: is where the missing writer is visible.
_UNPOPULATED_IMMUTABLE_TABLES = [
    "entity_type_definitions",
    "relationship_type_definitions",
    "profile_compile_results",
]


@pytest.mark.parametrize(("table", "column"), _POPULATED_IMMUTABLE_TABLES)
async def test_a_direct_update_is_refused_by_the_database(
    publication: dict[str, object], table: str, column: str
) -> None:
    """The refusal, observed on the tables that have rows to refuse it for."""
    await _publish(publication)
    factory: async_sessionmaker[AsyncSession] = publication["factory"]  # type: ignore[assignment]

    with pytest.raises(Exception, match="append-only|is refused"):
        async with factory() as session, session.begin():
            # Self-assignment: it changes nothing, so only the trigger can refuse it.
            await session.execute(text(f"UPDATE {table} SET {column} = {column}"))


async def test_a_direct_delete_is_refused_by_the_database(
    publication: dict[str, object],
) -> None:
    """Deletion is the worse of the two: every binding naming the row dangles."""
    published = await _publish(publication)
    factory: async_sessionmaker[AsyncSession] = publication["factory"]  # type: ignore[assignment]

    with pytest.raises(Exception, match="append-only|is refused"):
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM profile_revisions WHERE profile_revision_id = :r"),
                {"r": published.profile_revision_id},
            )

    # And the row is still there, which is the property the refusal exists to hold.
    service: profile_service.ProfileService = publication["service"]  # type: ignore[assignment]
    assert await service.get_revision(published.profile_revision_id) is not None


async def test_bindings_are_deliberately_not_immutable(
    publication: dict[str, object],
) -> None:
    """The omission from that list of five, asserted so it stays deliberate.

    A binding's whole purpose is to move through states, so a trigger refusing
    UPDATE on `profile_bindings` would make the state machine unimplementable. This
    test fails if somebody "completes" the set by adding the sixth trigger.
    """
    factory: async_sessionmaker[AsyncSession] = publication["factory"]  # type: ignore[assignment]
    async with factory() as session:
        triggers = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'profile_bindings' AND NOT t.tgisinternal"
                )
            )
        ).scalar_one()

    assert triggers == 0, "profile_bindings must stay mutable; its states change by design"


@pytest.mark.parametrize("table", _UNPOPULATED_IMMUTABLE_TABLES)
async def test_every_published_projection_carries_the_immutability_trigger(
    publication: dict[str, object], table: str
) -> None:
    """Structural, because these three have no writer to produce a row yet.

    The migration spells its five triggers out one by one rather than looping over a
    list, so that `profile_bindings`' absence is visible to a reader. This asserts
    the three that publication does not populate still carry theirs — otherwise the
    day something starts writing them, the protection everyone assumes is there
    would turn out never to have been checked.
    """
    factory: async_sessionmaker[AsyncSession] = publication["factory"]  # type: ignore[assignment]
    async with factory() as session:
        triggers = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = :t AND NOT t.tgisinternal"
                ),
                {"t": table},
            )
        ).scalar_one()

    assert triggers >= 1, f"{table} has no immutability trigger"
