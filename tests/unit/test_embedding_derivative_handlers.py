"""The retrieval side of erasure propagation: what the handlers remove, and what they refuse.

Three properties are worth stating up front, because they are the ones a reader
should be able to check without running anything:

- **A removal reaches every facet.** One `embeddings` row carries the vector, the
  source text in `text_chunk`, and a `ts_vector` generated from that text; the same
  target may also have a queued or dead-lettered request still holding the text. The
  handler's statements have to reach all of them, so the assertions below name the
  three tables rather than counting calls.
- **An address a handler cannot resolve is a refusal, not a shrug.** A handler that
  returned zero for an unparseable locator would mark the work item done and leave
  the artefact in place — a queue that drains clean while the content stays. Every
  refusal is asserted twice: it raises, *and* the session was never touched.
- **The registrar and the handler agree on the addressing.** They are the two halves
  of one contract, so a test writes a locator through the registrar and parses it
  back through the handler's own parser rather than pinning a string in both places.

The real Postgres proof of the end-to-end property (register, erase the source, drain
the queue, find nothing left) lives in the integration tier; this file proves the
statements, the parameters, and the refusals without a database.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import contextplane.service.retrieval.derivative_handlers as handlers_module
from contextplane.retention import derivatives, policies
from contextplane.service.retrieval.derivative_handlers import (
    ClosureCacheErasure,
    EmbeddingChunkErasure,
    FullTextDocumentErasure,
    UnknownStorageLocator,
    VectorErasure,
    cache_locator,
    retrieval_derivative_handlers,
)
from contextplane.service.retrieval.embedding_index import (
    ARTEFACT_HANDLER_VERSION,
    artefact_locator,
    claim_registration_anchor,
    erase_targets,
    parse_artefact_locator,
    register_claim_artefact,
)

_ANCHOR = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingSession:
    """A session that records every statement and its parameters and executes none."""

    def __init__(self, *, rowcount: int = 1, first: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rowcount = rowcount
        self._first = first

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.calls.append((" ".join(str(stmt).split()), params or {}))
        result = MagicMock()
        result.rowcount = self._rowcount
        result.fetchall.return_value = [object()] * self._rowcount
        result.scalar_one.return_value = uuid.uuid4()
        result.first.return_value = self._first
        return result

    def statements(self, needle: str) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if needle in call[0]]


def _registration(
    *,
    locator: str,
    tenant_id: uuid.UUID | None = None,
    kind: str = derivatives.KIND_VECTOR,
) -> derivatives.Registration:
    return derivatives.Registration(
        derivative_id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        derivative_kind=kind,
        storage_locator=locator,
        audience_partition="tenant",
        classification="confidential",
        expires_at=_ANCHOR,
        blocking=False,
    )


# ---------------------------------------------------------------------------
# Coverage of the kinds
# ---------------------------------------------------------------------------


def test_the_retrieval_kinds_are_covered_and_each_declares_exactly_one() -> None:
    """The four kinds this subsystem owns register cleanly into one registry.

    Registering them is the check: `HandlerRegistry.register` refuses a kind the
    schema does not store and a second handler for a kind already claimed, so a
    handler that declared the wrong string or duplicated another's cannot pass
    here.
    """
    registry = derivatives.HandlerRegistry()
    for handler in retrieval_derivative_handlers():
        registry.register(handler)

    assert registry.kinds == (
        derivatives.KIND_VECTOR,
        derivatives.KIND_EMBEDDING_CHUNK,
        derivatives.KIND_FTS_DOCUMENT,
        derivatives.KIND_CACHE,
    )


def test_no_kind_this_subsystem_does_not_own_is_claimed_by_it() -> None:
    """Claiming a kind another subsystem owns would collide at composition, loudly but late."""
    registry = derivatives.HandlerRegistry()
    for handler in retrieval_derivative_handlers():
        registry.register(handler)

    assert derivatives.KIND_OUTBOX in registry.unhandled_kinds(), (
        "the embedding outbox is covered by the vector registration's own removal, not by "
        "the `outbox` kind, which belongs to the carrier handlers"
    )
    assert derivatives.KIND_SUMMARY in registry.unhandled_kinds()


def test_every_handler_records_a_version_so_a_stale_artefact_is_identifiable() -> None:
    """A registration stores the handler version that wrote it; an empty one would name nothing."""
    for handler in retrieval_derivative_handlers():
        assert handler.version, f"{type(handler).__name__} must record a version"


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_a_locator_round_trips_between_the_store_that_writes_it_and_the_handler_that_reads_it() -> None:
    """The registrar and the handler are two halves of one contract; this is the contract."""
    target_id = uuid.uuid4()
    assert parse_artefact_locator(artefact_locator("claim", target_id)) == ("claim", target_id)


@pytest.mark.parametrize(
    "locator",
    [
        "closure_cache/00000000-0000-0000-0000-000000000001/forward",
        "embeddings/claim",
        "embeddings/claim/not-a-uuid",
        "embeddings/claim/00000000-0000-0000-0000-000000000001/extra",
        "",
    ],
)
def test_an_address_this_store_did_not_write_does_not_parse(locator: str) -> None:
    """Parsing is total and returns nothing rather than guessing a target from a partial match."""
    assert parse_artefact_locator(locator) is None


@pytest.mark.asyncio
async def test_an_unresolvable_address_is_refused_and_nothing_is_written() -> None:
    """Both halves matter: a handler that raised after deleting would be worse than one that never ran."""
    session = _RecordingSession()
    registration = _registration(locator="closure_cache/not-ours/forward")

    with pytest.raises(UnknownStorageLocator, match="does not address the embedding index"):
        await VectorErasure().apply(session, registration, derivatives.OPERATION_DELETE)  # type: ignore[arg-type]

    assert session.calls == []


@pytest.mark.asyncio
async def test_the_cache_handler_refuses_an_address_belonging_to_the_index() -> None:
    """The kind with no production registrar still refuses rather than interpreting."""
    session = _RecordingSession()
    registration = _registration(
        locator=artefact_locator("claim", uuid.uuid4()),
        kind=derivatives.KIND_CACHE,
    )

    with pytest.raises(UnknownStorageLocator, match="does not address the closure cache"):
        await ClosureCacheErasure().apply(session, registration, derivatives.OPERATION_DELETE)  # type: ignore[arg-type]

    assert session.calls == []


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [VectorErasure(), EmbeddingChunkErasure(), FullTextDocumentErasure()])
@pytest.mark.parametrize("operation", [derivatives.OPERATION_DELETE, derivatives.OPERATION_REDACT])
async def test_removal_reaches_the_vector_the_queued_request_and_the_dead_letter(handler: Any, operation: str) -> None:
    """Every facet, every kind, and redact removes rather than edits.

    Redact deleting is the documented reading, not a shortcut: the chunk *is* what
    was embedded, so there is no version of the row with the content taken out that
    still means anything. The three tables are named because the property is "no
    copy survives", and a dead-lettered request holds exactly the text that failed
    to embed.
    """
    tenant_id, target_id = uuid.uuid4(), uuid.uuid4()
    session = _RecordingSession(rowcount=2)

    touched = await handler.apply(
        session,
        _registration(locator=artefact_locator("claim", target_id), tenant_id=tenant_id),
        operation,
    )

    for table in ("DELETE FROM embeddings", "DELETE FROM embedding_outbox ", "DELETE FROM embedding_outbox_failed"):
        statements = session.statements(table)
        assert len(statements) == 1, f"expected exactly one {table} statement"
        assert statements[0][1]["ids"] == [target_id]
        assert statements[0][1]["tid"] == tenant_id, "the delete is scoped to the registration's own tenant"
    assert touched > 0, "the handler reports what it removed, so a caller's count means something"


@pytest.mark.asyncio
async def test_a_removal_that_finds_nothing_reports_zero_rather_than_failing() -> None:
    """Re-applying a partially-completed propagation is the normal recovery path."""
    session = _RecordingSession(rowcount=0)

    touched = await VectorErasure().apply(
        session,  # type: ignore[arg-type]
        _registration(locator=artefact_locator("claim", uuid.uuid4())),
        derivatives.OPERATION_DELETE,
    )

    assert touched == 0


@pytest.mark.asyncio
async def test_rebuilding_a_claim_asks_the_claim_what_it_is_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebuild re-projects rather than re-embedding: an invalidated claim is retracted by the same call."""
    seen: dict[str, Any] = {}

    async def _project(session: Any, *, claim_id: uuid.UUID, now: datetime.datetime) -> bool:
        seen["claim_id"] = claim_id
        return True

    monkeypatch.setattr(handlers_module, "project_claim", _project)
    target_id = uuid.uuid4()
    session = _RecordingSession()

    touched = await VectorErasure().apply(
        session,  # type: ignore[arg-type]
        _registration(locator=artefact_locator("claim", target_id)),
        derivatives.OPERATION_REBUILD,
    )

    assert seen["claim_id"] == target_id
    assert touched == 1
    assert session.statements("DELETE FROM embeddings") == [], "a rebuild of a live claim must not delete its vector"


@pytest.mark.asyncio
async def test_rebuilding_a_target_the_index_cannot_project_removes_it_instead() -> None:
    """Losing a derivative is recoverable; leaving erased content inside one is not."""
    session = _RecordingSession()

    await VectorErasure().apply(
        session,  # type: ignore[arg-type]
        _registration(locator=artefact_locator("fact", uuid.uuid4())),
        derivatives.OPERATION_REBUILD,
    )

    assert len(session.statements("DELETE FROM embeddings")) == 1


@pytest.mark.asyncio
async def test_the_cache_handler_drops_one_root_and_direction_within_one_tenant() -> None:
    """The closure cache is scoped by tenant, root and direction — the unit its refresh path rebuilds."""
    tenant_id, root_id = uuid.uuid4(), uuid.uuid4()
    session = _RecordingSession(rowcount=7)

    touched = await ClosureCacheErasure().apply(
        session,  # type: ignore[arg-type]
        _registration(locator=cache_locator(root_id, "forward"), tenant_id=tenant_id, kind=derivatives.KIND_CACHE),
        derivatives.OPERATION_DELETE,
    )

    statements = session.statements("DELETE FROM closure_cache")
    assert len(statements) == 1
    assert statements[0][1] == {"tid": tenant_id, "root": root_id, "dir": "forward"}
    assert touched == 7


@pytest.mark.asyncio
async def test_rebuilding_a_cached_closure_drops_it_for_its_own_refresh_path_to_repopulate() -> None:
    """The closure cache rebuilds itself from the graph; dropping the rows is the whole of a rebuild."""
    session = _RecordingSession(rowcount=1)

    await ClosureCacheErasure().apply(
        session,  # type: ignore[arg-type]
        _registration(locator=cache_locator(uuid.uuid4(), "reverse"), kind=derivatives.KIND_CACHE),
        derivatives.OPERATION_REBUILD,
    )

    assert len(session.statements("DELETE FROM closure_cache")) == 1


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_artefact_expires_on_the_claims_content_clock_not_on_the_claims_own_life() -> None:
    """A claim is kept for the life of its tenant; the excerpt it quotes is not.

    The embedded text *is* that excerpt, so inheriting the record clock would keep
    the person's words in the index for as long as the tenant exists — past the
    date the policy says the content itself must be reduced.
    """
    session = _RecordingSession()
    claim_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    await register_claim_artefact(session, tenant_id=tenant_id, claim_id=claim_id, anchor=_ANCHOR)  # type: ignore[arg-type]

    registration = session.statements("INSERT INTO derivative_registrations")[0][1]
    assert registration["expires_at"] == policies.payload_deadline(policies.RECORD_MEMORY_CLAIM, _ANCHOR)
    assert registration["expires_at"] != policies.expiry_deadline(policies.RECORD_MEMORY_CLAIM, _ANCHOR)


@pytest.mark.asyncio
async def test_a_claim_artefact_is_linked_to_the_claim_it_was_built_from() -> None:
    """The source link is what an erasure follows to find this artefact at all."""
    session = _RecordingSession()
    claim_id = uuid.uuid4()

    await register_claim_artefact(session, tenant_id=uuid.uuid4(), claim_id=claim_id, anchor=_ANCHOR)  # type: ignore[arg-type]

    link = session.statements("INSERT INTO derivative_source_links")[0][1]
    assert link["cls"] == policies.RECORD_MEMORY_CLAIM
    assert link["sid"] == claim_id


@pytest.mark.asyncio
async def test_a_registration_addresses_the_artefact_the_handler_will_be_asked_to_remove() -> None:
    """Written by the registrar, read by the handler — the same string or the removal never happens."""
    session = _RecordingSession()
    claim_id = uuid.uuid4()

    await register_claim_artefact(session, tenant_id=uuid.uuid4(), claim_id=claim_id, anchor=_ANCHOR)  # type: ignore[arg-type]

    registration = session.statements("INSERT INTO derivative_registrations")[0][1]
    assert registration["kind"] == derivatives.KIND_VECTOR
    assert registration["handler_version"] == ARTEFACT_HANDLER_VERSION
    assert parse_artefact_locator(registration["locator"]) == ("claim", claim_id)


@pytest.mark.asyncio
async def test_the_registration_anchor_is_the_claims_creation_instant() -> None:
    """Anchoring on "now" would push the artefact's expiry past its source's by the claim's age."""
    session = _RecordingSession(first=(_ANCHOR,))

    assert await claim_registration_anchor(session, uuid.uuid4()) == _ANCHOR  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_claim_that_is_already_gone_yields_no_anchor_rather_than_a_guess() -> None:
    """Nothing to register against, and inventing an anchor would invent an expiry."""
    session = _RecordingSession(first=None)

    assert await claim_registration_anchor(session, uuid.uuid4()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_erasing_no_targets_touches_no_table() -> None:
    """The empty selection is the common case on a retry, and it must cost nothing."""
    session = _RecordingSession()

    assert await erase_targets(session, target_type="claim", target_ids=[]) == {  # type: ignore[arg-type]
        "embeddings": 0,
        "outbox_rows": 0,
    }
    assert session.calls == []


# ---------------------------------------------------------------------------
# The build path registers what it writes
# ---------------------------------------------------------------------------


def _drain_session(recorder: _RecordingSession) -> MagicMock:
    session = AsyncMock()
    session.execute = recorder.execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _outbox_row(target_type: str) -> dict[str, Any]:
    return {
        "outbox_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "target_type": target_type,
        "target_id": uuid.uuid4(),
        "text_to_embed": "alpha beta",
        "chunk_plan": [{"index": 0, "start": 0, "end": 2, "text": "alpha beta"}],
        "attempts": 0,
        "enqueued_at": _ANCHOR,
    }


@pytest.mark.asyncio
async def test_the_drain_registers_a_claims_vector_alongside_the_write_that_created_it() -> None:
    """Same transaction, by construction: the registration is issued on the session that wrote the row.

    An unregistered vector is one no erasure reaches, so the registration cannot be
    a follow-up step that a crash between commits could skip.
    """
    from contextplane.service.retrieval.embedding_drain import _process_row

    recorder = _RecordingSession(first=(_ANCHOR,))
    embedder = MagicMock()
    embedder.model_version = "stub-zero"
    embedder.encode = MagicMock(side_effect=lambda texts: np.zeros((len(texts), 4), dtype=np.float32))

    await _process_row(_drain_session(recorder), embedder, _drain_settings(), _outbox_row("claim"), max_attempts=5)

    assert recorder.statements("INSERT INTO embeddings"), "the artefact write must still happen"
    assert recorder.statements("INSERT INTO derivative_registrations"), "and it must be registered"


@pytest.mark.asyncio
async def test_the_drain_registers_nothing_for_a_kind_the_retention_policy_does_not_classify() -> None:
    """A capability fact is not one of the record classes, so a registration would invent its expiry.

    Fact vectors are reached by the actor-scoped erasure participant instead, which
    is why this is a deliberate omission rather than a gap.
    """
    from contextplane.service.retrieval.embedding_drain import _process_row

    recorder = _RecordingSession(first=(_ANCHOR,))
    embedder = MagicMock()
    embedder.model_version = "stub-zero"
    embedder.encode = MagicMock(side_effect=lambda texts: np.zeros((len(texts), 4), dtype=np.float32))

    await _process_row(_drain_session(recorder), embedder, _drain_settings(), _outbox_row("fact"), max_attempts=5)

    assert recorder.statements("INSERT INTO embeddings")
    assert recorder.statements("INSERT INTO derivative_registrations") == []


def _drain_settings() -> Any:
    from contextplane.config import Settings

    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        outbox_batch_size=32,
        outbox_max_attempts=5,
    )
