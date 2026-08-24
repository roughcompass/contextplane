"""Every governed assertion carries complete provenance, and only one module writes it.

Partial provenance is worse than none. A row saying `authority = 'canonical_owner'`
and nothing else reads as authoritative while carrying no evidence anyone can
check — so the completeness rules here are refusals, not warnings, and each one is
tested by removing exactly the field it requires.

The single-writer rule is the other half, and it is the half a behavioural test
cannot reach: no amount of exercising the write path proves that nothing *else*
writes the table. That is a structural property of the tree, so it is asserted
structurally, against the same gate `make privileged-writes` runs.

**The corpus test is the one that would rot silently.** A completeness rule is
only worth having if every authority is actually exercised by it; a corpus that
covered three of four would pass forever while the fourth went unchecked. So the
corpus is built by enumerating `AUTHORITIES` rather than by listing cases, which
means an authority added to the vocabulary fails this file until somebody writes
its fixture.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.entities import assertions
from contextplane.entities.provenance import (
    AUTHORITIES,
    CANONICAL_OWNER,
    DERIVED,
    EXTERNAL_AUTHORITY,
    FRESH,
    OBSERVED,
    REVOKED,
    AssertionProvenance,
    IncompleteProvenance,
)
from scripts.check_privileged_writes import RULES

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _provenance(**overrides: object) -> AssertionProvenance:
    """A complete canonical-owner provenance, so a test changes only what it names."""
    fields: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "source_system": "contextplane",
        "source_namespace": "internal",
        "ingested_at": _NOW,
        "authority": CANONICAL_OWNER,
        "freshness_state": FRESH,
        "produced_by": "conformance",
        "validating_profile_revision_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return AssertionProvenance(**fields)  # type: ignore[arg-type]


#: One complete fixture per authority. Keyed by authority so the corpus test can
#: check it against the vocabulary rather than against its own length.
_CORPUS: dict[str, dict[str, object]] = {
    CANONICAL_OWNER: {},
    OBSERVED: {"observed_at": _NOW, "event_time": _NOW},
    EXTERNAL_AUTHORITY: {"external_record_id": "SYS-1", "external_revision": "v7", "observed_at": _NOW},
    DERIVED: {"derivation_method": "closure-walk", "derivation_profile": "default", "confidence": 0.75},
}


# --- completeness -------------------------------------------------------------------


def test_the_corpus_covers_every_authority_the_vocabulary_defines() -> None:
    """An authority with no fixture is an authority whose rules nothing exercises.

    Compared against `AUTHORITIES` rather than against a count, so adding one to
    the vocabulary fails here until its fixture exists — a corpus checked by
    length passes forever while a new case goes untested.
    """
    assert set(_CORPUS) == AUTHORITIES


@pytest.mark.parametrize("authority", sorted(AUTHORITIES))
def test_a_complete_provenance_is_accepted_for_every_authority(authority: str) -> None:
    """The refusals below prove nothing unless the valid shapes are actually valid."""
    provenance = _provenance(authority=authority, **_CORPUS[authority])

    assert provenance.authority == authority


@pytest.mark.parametrize("field", ["source_system", "source_namespace", "produced_by"])
def test_an_assertion_nobody_is_named_for_is_refused(field: str) -> None:
    with pytest.raises(IncompleteProvenance, match=field):
        _provenance(**{field: "   "})


def test_a_governed_assertion_must_name_the_revision_that_validated_it() -> None:
    """The database permits NULL here; a governed assertion does not.

    Without it nothing can later say which rules the assertion was judged
    against, which is the question a governance audit exists to answer.
    """
    with pytest.raises(IncompleteProvenance, match="profile revision"):
        _provenance(validating_profile_revision_id=None)


def test_an_unknown_authority_is_refused() -> None:
    with pytest.raises(IncompleteProvenance, match="authority"):
        _provenance(authority="probably_fine")


def test_an_unknown_freshness_state_is_refused() -> None:
    with pytest.raises(IncompleteProvenance, match="freshness"):
        _provenance(freshness_state="probably_current")


# --- derivation ---------------------------------------------------------------------


def test_a_derived_assertion_states_how_it_was_derived() -> None:
    with pytest.raises(IncompleteProvenance, match="derivation method"):
        _provenance(authority=DERIVED, confidence=0.5)


def test_a_derived_assertion_states_its_confidence() -> None:
    """An inference with no confidence reads as a fact."""
    with pytest.raises(IncompleteProvenance, match="confidence"):
        _provenance(authority=DERIVED, derivation_method="closure-walk")


def test_a_confidence_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(IncompleteProvenance, match="outside"):
        _provenance(authority=DERIVED, derivation_method="closure-walk", confidence=1.5)


@pytest.mark.parametrize("authority", sorted(AUTHORITIES - {DERIVED}))
def test_only_a_derived_assertion_carries_a_confidence(authority: str) -> None:
    """A confidence on a stated fact invites a reader to discount what was never inferred."""
    with pytest.raises(IncompleteProvenance, match="derived"):
        _provenance(authority=authority, confidence=0.9, **_CORPUS[authority])


# --- revocation and external identity -----------------------------------------------


def test_a_revocation_reference_without_a_time_is_refused() -> None:
    with pytest.raises(IncompleteProvenance, match="revocation"):
        _provenance(revocation_ref="ticket-9")


def test_a_revocation_time_without_a_reference_is_refused() -> None:
    with pytest.raises(IncompleteProvenance, match="revocation"):
        _provenance(revoked_at=_NOW)


def test_an_assertion_marked_revoked_records_when_and_by_what() -> None:
    with pytest.raises(IncompleteProvenance, match="revoked"):
        _provenance(freshness_state=REVOKED)


def test_a_complete_revocation_is_accepted() -> None:
    provenance = _provenance(freshness_state=REVOKED, revoked_at=_NOW, revocation_ref="ticket-9")

    assert provenance.freshness_state == REVOKED


@pytest.mark.parametrize("field", ["external_record_id", "external_revision"])
def test_an_external_authority_assertion_identifies_its_upstream_record(field: str) -> None:
    """Without it the acceptance cannot be re-verified against its source, only believed."""
    fields = dict(_CORPUS[EXTERNAL_AUTHORITY])
    fields[field] = None

    with pytest.raises(IncompleteProvenance, match="external"):
        _provenance(authority=EXTERNAL_AUTHORITY, **fields)


@pytest.mark.parametrize("authority", sorted(AUTHORITIES))
def test_an_upstream_record_with_no_observation_time_is_refused_by_name(authority: str) -> None:
    """Every authority, not only `external_authority`.

    The database constraint keys off the column rather than off the authority,
    so a check narrower than the table would let an `observed` assertion naming
    an upstream record reach Postgres and come back as an `IntegrityError`
    several frames from the writer -- where `IncompleteProvenance` is documented
    as "a programming error in a writer" and is the refusal that names the field.
    """
    # Built on each authority's own complete fixture, so the refusal under test
    # is reached rather than pre-empted by that authority's other requirements --
    # a `derived` assertion is refused for its missing derivation method first.
    fields = {**_CORPUS[authority], "external_record_id": "SYS-1", "external_revision": "v7"}
    fields.pop("observed_at", None)

    with pytest.raises(IncompleteProvenance, match="when it was observed"):
        _provenance(authority=authority, **fields)


def test_a_revision_of_no_record_is_refused_by_name() -> None:
    with pytest.raises(IncompleteProvenance, match="revision of some record"):
        _provenance(external_revision="v7", observed_at=_NOW)


def test_an_observation_time_with_no_upstream_record_is_a_real_statement() -> None:
    """The converse is not required, and that is deliberate.

    "We saw this, and the source has no record id for it" is a thing an importer
    can truthfully say. A symmetric constraint -- 0067's shape -- would refuse
    it, which is why this one is conditional on the record id instead.
    """
    provenance = _provenance(authority=OBSERVED, observed_at=_NOW, event_time=_NOW)

    assert provenance.observed_at == _NOW
    assert provenance.external_record_id is None


# --- the single writer, and immutability --------------------------------------------


def test_assertion_provenance_is_a_governed_table_with_one_permitted_writer() -> None:
    """No behavioural test can show that nothing *else* writes the table.

    Asserted against the same rule set `make privileged-writes` enforces, so this
    fails if the registration is removed or a second caller is added — the two
    changes that would quietly reopen the forgery this rule exists to prevent.
    """
    rule = next((r for r in RULES if r.table == "assertion_provenance"), None)

    assert rule is not None, "assertion_provenance must be a governed table"
    assert rule.allowed_callers == frozenset({"contextplane/entities/assertions.py"})


def test_the_permitted_writer_exposes_no_update_path() -> None:
    """Evidence that changed is a new row superseding the old, never an edit.

    A `correct_provenance` function would be indistinguishable at the row level
    from the forgery the single-writer rule prevents: both produce a row whose
    contents no longer match what was originally observed.
    """
    exported = set(assertions.__all__)

    assert exported == {"for_governed_write", "record", "supersede"}
    assert not any(name.startswith(("update", "correct", "amend", "edit")) for name in dir(assertions))


# --- against the real table ----------------------------------------------------------


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant_and_revision(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, revision_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'prov')"),
        {"t": tenant_id, "s": f"pv-{tenant_id.hex[:10]}"},
    )
    await session.execute(
        text(
            "INSERT INTO profile_revisions ("
            "  profile_revision_id, profile_family, profile_name, semantic_version,"
            "  canonical_document, document_digest, compatibility, published_by, published_at"
            ") VALUES (:rid, 'platform', :name, '1.0.0', CAST('{}' AS JSONB), :digest,"
            "          'backward_compatible', 'test', :now)"
        ),
        {"rid": revision_id, "name": f"pv-{revision_id.hex[:12]}", "digest": revision_id.hex, "now": _NOW},
    )
    return tenant_id, revision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", sorted(AUTHORITIES))
async def test_every_authority_in_the_corpus_persists_with_its_fields_intact(
    factory: async_sessionmaker[AsyncSession], authority: str
) -> None:
    """The corpus is 100% complete only if each shape survives the round trip.

    Checked field by field rather than by row count: a writer that dropped
    `derivation_method` on the floor would still produce one row.
    """
    async with factory() as session, session.begin():
        tenant_id, revision_id = await _tenant_and_revision(session)
        provenance = _provenance(
            tenant_id=tenant_id,
            validating_profile_revision_id=revision_id,
            authority=authority,
            **_CORPUS[authority],
        )
        provenance_id = await assertions.record(session, provenance)

    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT authority, freshness_state, source_system, source_namespace, produced_by,"
                        "       validating_profile_revision_id, external_record_id, external_revision,"
                        "       derivation_method, confidence, ingested_at, observed_at, event_time"
                        "  FROM assertion_provenance WHERE provenance_id = :pid"
                    ),
                    {"pid": provenance_id},
                )
            )
            .mappings()
            .one()
        )

    assert row["authority"] == authority
    assert row["freshness_state"] == FRESH
    assert row["validating_profile_revision_id"] == revision_id
    assert row["external_record_id"] == provenance.external_record_id
    assert row["external_revision"] == provenance.external_revision
    assert row["derivation_method"] == provenance.derivation_method
    assert row["confidence"] == provenance.confidence
    # The two caller-supplied times, read back because they were not.
    # `observed_at` was an unchecked field on this round trip, which is exactly
    # the shape this test's docstring warns about: "a writer that dropped
    # `derivation_method` on the floor would still produce one row."
    assert row["observed_at"] == provenance.observed_at
    assert row["event_time"] == provenance.event_time


@pytest.mark.asyncio
async def test_superseding_writes_a_new_row_and_leaves_the_old_one_exactly_as_written(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Provenance is a record of what was observed at a moment.

    The superseded row is not even marked as replaced: a module that could edit
    one row to reflect a later fact could edit any of it. Which provenance an
    assertion currently relies on is a property of the assertion, recorded there.
    """
    async with factory() as session, session.begin():
        tenant_id, revision_id = await _tenant_and_revision(session)
        original = await assertions.record(
            session,
            _provenance(tenant_id=tenant_id, validating_profile_revision_id=revision_id, produced_by="first"),
        )

    async with factory() as session, session.begin():
        superseded, replacement = await assertions.supersede(
            session,
            superseded_provenance_id=original,
            replacement=_provenance(
                tenant_id=tenant_id, validating_profile_revision_id=revision_id, produced_by="second"
            ),
        )

    assert superseded == original
    assert replacement != original

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT provenance_id, produced_by FROM assertion_provenance"
                    " WHERE tenant_id = :t ORDER BY produced_by"
                ),
                {"t": tenant_id},
            )
        ).all()

    assert [(r.provenance_id, r.produced_by) for r in rows] == [(original, "first"), (replacement, "second")]


@pytest.mark.asyncio
async def test_provenance_rolls_back_with_the_assertion_it_describes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Evidence for something that never happened is worse than no evidence."""
    async with factory() as session:
        await session.begin()
        tenant_id, revision_id = await _tenant_and_revision(session)
        await assertions.record(session, _provenance(tenant_id=tenant_id, validating_profile_revision_id=revision_id))
        await session.rollback()

    async with factory() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM assertion_provenance WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()

    assert remaining == 0


# --- the times are the caller's, and the schema keeps them that way -----------


@pytest.mark.asyncio
async def test_provenance_is_never_server_defaulted(factory: async_sessionmaker[AsyncSession]) -> None:
    """No column default on the two times a caller states, or on the record id.

    E12-T2's goal in one assertion. A `DEFAULT now()` on `observed_at` would be a
    server-defaulted value wearing a caller-supplied name, and afterwards it
    would be indistinguishable from a genuine one -- which is the whole reason
    the epic names this property.

    Read off `information_schema` rather than off the migration, because the
    property is about the schema a deployment actually has: a later migration
    adding a default would pass a test that only read 0051.

    `ingested_at` is deliberately not in this list. When the platform took
    delivery is the platform's to state, and the table's own comment keeps the
    three times apart for exactly that reason.
    """
    async with factory() as session:
        defaults = dict(
            (
                await session.execute(
                    text(
                        "SELECT column_name, column_default FROM information_schema.columns "
                        " WHERE table_name = 'assertion_provenance'"
                    )
                )
            ).all()
        )

    for column in ("observed_at", "event_time", "external_record_id", "external_revision"):
        assert defaults[column] is None, (
            f"{column} has acquired a column default ({defaults[column]!r}). It is the caller's to state, "
            "and a defaulted value is indistinguishable afterwards from one a source supplied."
        )


@pytest.mark.asyncio
async def test_an_upstream_record_with_no_observation_time_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The constraint, not the class that also refuses it.

    `AssertionProvenance` refuses this earlier and by name, which is why the
    dataclass check exists -- but the class is one writer's discipline and the
    CHECK is the property. Asserted against the database directly, so removing
    the class's check would not quietly remove the guarantee too.
    """
    async with factory() as session, session.begin():
        tenant_id, revision_id = await _tenant_and_revision(session)

    with pytest.raises(Exception, match="ck_assertion_provenance_external_record_is_dated"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO assertion_provenance ("
                    "  provenance_id, tenant_id, source_system, source_namespace, external_record_id,"
                    "  ingested_at, authority, freshness_state, produced_by,"
                    "  validating_profile_revision_id, created_at"
                    ") VALUES (:pid, :tid, 'sys', 'ns', 'SYS-1', :now, 'external_authority', 'fresh',"
                    "          'test', :rev, :now)"
                ),
                {"pid": uuid.uuid4(), "tid": tenant_id, "rev": revision_id, "now": _NOW},
            )


@pytest.mark.asyncio
async def test_a_revision_of_no_record_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An `external_revision` with no record is a version of nothing, and it
    reads afterwards as though somebody knew which record it belonged to."""
    async with factory() as session, session.begin():
        tenant_id, revision_id = await _tenant_and_revision(session)

    with pytest.raises(Exception, match="ck_assertion_provenance_revision_needs_a_record"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO assertion_provenance ("
                    "  provenance_id, tenant_id, source_system, source_namespace, external_revision,"
                    "  ingested_at, authority, freshness_state, produced_by,"
                    "  validating_profile_revision_id, created_at"
                    ") VALUES (:pid, :tid, 'sys', 'ns', 'v7', :now, 'observed', 'fresh',"
                    "          'test', :rev, :now)"
                ),
                {"pid": uuid.uuid4(), "tid": tenant_id, "rev": revision_id, "now": _NOW},
            )
