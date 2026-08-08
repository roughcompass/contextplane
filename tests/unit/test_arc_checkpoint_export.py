"""Unit tests for `checkpoint_export.py` and `checkpoint_exporter.py`.

No database: the three `queries.operational_chain` functions
`CheckpointExportService.export_checkpoint` calls (`load_checkpoint`,
`mark_exported`, `record_export_failure`) are monkeypatched with an
in-memory fake. `verify_against_sink`'s own row comparison reads
`arc_operational_chain_checkpoints` through a raw `session.execute` this
suite does not fake -- proving that comparison against real rows is
`tests/integration/test_arc_operational_chain.py`'s job; this suite proves
only the "no sink configured" refusal, which needs no row at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

import pytest

from contextplane.arc.service import checkpoint_export as ce
from contextplane.arc.service.queries.operational_chain import CheckpointRow
from contextplane.arc.workers import checkpoint_exporter as worker_module
from contextplane.arc.workers.checkpoint_exporter import CheckpointExporterWorker
from contextplane.exceptions import NotFoundError

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_CHECKPOINT_ID = uuid.uuid4()
_REVISION_ID = uuid.uuid4()


class _FakeClock:
    def __init__(self, moment: datetime.datetime = _NOW) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    def begin(self) -> _NoopTransactionCM:
        return _NoopTransactionCM()


class _SessionCM:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory() -> _SessionCM:
    return _SessionCM(_NullSession())


def _checkpoint_row(**overrides: Any) -> CheckpointRow:
    base: dict[str, Any] = {
        "checkpoint_id": _CHECKPOINT_ID,
        "deployment_id": "test-deployment",
        "revision_id": _REVISION_ID,
        "sequence": 0,
        "head_digest": "a" * 64,
        "exported_at": None,
        "sink_receipt_digest": None,
        "sink_receipt_signature": None,
    }
    base.update(overrides)
    return CheckpointRow(**base)


class FakeCheckpointQueries:
    def __init__(self) -> None:
        self.checkpoints: dict[uuid.UUID, CheckpointRow] = {}
        self.mark_exported_calls: list[dict[str, object]] = []
        self.failure_calls: list[dict[str, object]] = []
        self.mark_exported_returns = True

    def seed(self, row: CheckpointRow) -> None:
        self.checkpoints[row.checkpoint_id] = row

    async def load_checkpoint(self, _session: object, checkpoint_id: uuid.UUID) -> CheckpointRow | None:
        return self.checkpoints.get(checkpoint_id)

    async def mark_exported(
        self,
        _session: object,
        *,
        checkpoint_id: uuid.UUID,
        exported_at: datetime.datetime,
        sink_receipt_digest: str,
        sink_receipt_signature: str,
    ) -> bool:
        self.mark_exported_calls.append(
            {
                "checkpoint_id": checkpoint_id,
                "sink_receipt_digest": sink_receipt_digest,
                "sink_receipt_signature": sink_receipt_signature,
            }
        )
        if self.mark_exported_returns:
            row = self.checkpoints[checkpoint_id]
            self.checkpoints[checkpoint_id] = dataclasses.replace(
                row,
                exported_at=exported_at,
                sink_receipt_digest=sink_receipt_digest,
                sink_receipt_signature=sink_receipt_signature,
            )
        return self.mark_exported_returns

    async def record_export_failure(
        self, _session: object, *, checkpoint_id: uuid.UUID, error_code: str, attempted_at: datetime.datetime
    ) -> None:
        self.failure_calls.append({"checkpoint_id": checkpoint_id, "error_code": error_code})

    async def select_pending_checkpoints(self, _session: object, *, limit: int) -> list[uuid.UUID]:
        pending = [cid for cid, row in self.checkpoints.items() if row.exported_at is None]
        return pending[:limit]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCheckpointQueries:
    f = FakeCheckpointQueries()
    monkeypatch.setattr(ce, "queries", f)
    # `checkpoint_exporter.py` imports the same queries module under its own
    # name, independently of `checkpoint_export.py`'s import -- both have to
    # be patched for a worker-level test to see the same in-memory state.
    monkeypatch.setattr(worker_module, "queries", f)
    return f


class _AcceptingSink:
    """Acknowledges every append immediately, once."""

    def __init__(self) -> None:
        self.append_calls: list[dict[str, object]] = []

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> ce.SinkReceipt:
        self.append_calls.append(
            {
                "deployment_id": deployment_id,
                "revision_id": revision_id,
                "sequence": sequence,
                "head_digest": head_digest,
            }
        )
        return ce.SinkReceipt(receipt_digest="r" * 64, receipt_signature="s" * 64, accepted_at=_NOW)

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> ce.SinkReceipt | None:
        return ce.SinkReceipt(receipt_digest="r" * 64, receipt_signature="s" * 64, accepted_at=_NOW)

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        return 0


class _ConflictingSink:
    """Every append raises an identity conflict -- the sink already holds a
    different digest for this identity."""

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> ce.SinkReceipt:
        raise ce.CheckpointSinkIdentityConflict("identity already claimed with a different digest")

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> ce.SinkReceipt | None:
        raise NotImplementedError

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        raise NotImplementedError


class _FailingSink:
    """Every append raises an unrelated, unexpected error."""

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> ce.SinkReceipt:
        raise RuntimeError("sink unreachable")

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> ce.SinkReceipt | None:
        raise NotImplementedError

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# export_checkpoint
# ---------------------------------------------------------------------------


async def test_a_missing_checkpoint_raises_not_found(fake: FakeCheckpointQueries) -> None:
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock())
    with pytest.raises(NotFoundError):
        await service.export_checkpoint(_CHECKPOINT_ID)


async def test_an_already_exported_checkpoint_is_a_no_op(fake: FakeCheckpointQueries) -> None:
    fake.seed(_checkpoint_row(exported_at=_NOW, sink_receipt_digest="r" * 64, sink_receipt_signature="s" * 64))
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=_AcceptingSink())

    outcome = await service.export_checkpoint(_CHECKPOINT_ID)

    assert outcome is ce.CheckpointExportOutcome.ALREADY_EXPORTED
    assert fake.mark_exported_calls == []


async def test_no_sink_configured_leaves_the_checkpoint_pending(fake: FakeCheckpointQueries) -> None:
    """The honest 'not configured' outcome -- never a pretend success, and
    the row is never touched."""
    fake.seed(_checkpoint_row())
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock())  # sink=None, the default

    outcome = await service.export_checkpoint(_CHECKPOINT_ID)

    assert outcome is ce.CheckpointExportOutcome.SINK_UNAVAILABLE
    assert fake.mark_exported_calls == []
    assert fake.checkpoints[_CHECKPOINT_ID].exported_at is None


async def test_a_successful_export_records_the_sinks_receipt(fake: FakeCheckpointQueries) -> None:
    fake.seed(_checkpoint_row())
    sink = _AcceptingSink()
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=sink)

    outcome = await service.export_checkpoint(_CHECKPOINT_ID)

    assert outcome is ce.CheckpointExportOutcome.EXPORTED
    assert len(sink.append_calls) == 1
    assert sink.append_calls[0]["revision_id"] == _REVISION_ID
    assert fake.checkpoints[_CHECKPOINT_ID].exported_at == _NOW
    assert fake.checkpoints[_CHECKPOINT_ID].sink_receipt_digest == "r" * 64


async def test_a_sink_identity_conflict_raises_integrity_error_with_sink_mismatch(
    fake: FakeCheckpointQueries,
) -> None:
    fake.seed(_checkpoint_row())
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=_ConflictingSink())

    with pytest.raises(ce.CheckpointIntegrityError) as exc_info:
        await service.export_checkpoint(_CHECKPOINT_ID)
    assert exc_info.value.reason_code == "sink_mismatch"
    # Never marked exported -- a mismatch is not a success.
    assert fake.checkpoints[_CHECKPOINT_ID].exported_at is None


async def test_an_unexpected_sink_failure_records_the_attempt_and_raises(fake: FakeCheckpointQueries) -> None:
    fake.seed(_checkpoint_row())
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=_FailingSink())

    with pytest.raises(ce.CheckpointIntegrityError) as exc_info:
        await service.export_checkpoint(_CHECKPOINT_ID)
    assert exc_info.value.reason_code == "sink_failed"
    assert fake.failure_calls == [{"checkpoint_id": _CHECKPOINT_ID, "error_code": ce.ERROR_SINK_FAILED}]


async def test_a_concurrent_pass_that_already_recorded_the_receipt_is_a_duplicate_no_op(
    fake: FakeCheckpointQueries,
) -> None:
    """`mark_exported` returning `False` means another pass's compare-and-
    swap already won between this call's read and its own update -- the
    sink's receipt this call got back is the same one already stored, so
    this is a duplicate, not a failure."""
    fake.seed(_checkpoint_row())
    fake.mark_exported_returns = False
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=_AcceptingSink())

    outcome = await service.export_checkpoint(_CHECKPOINT_ID)

    assert outcome is ce.CheckpointExportOutcome.ALREADY_EXPORTED


# ---------------------------------------------------------------------------
# verify_against_sink -- the one branch provable with no real rows.
# ---------------------------------------------------------------------------


async def test_verify_against_sink_refuses_when_no_sink_is_configured() -> None:
    service = ce.CheckpointExportService(_session_factory, clock=_FakeClock())
    with pytest.raises(ce.CheckpointIntegrityError) as exc_info:
        await service.verify_against_sink(_NullSession(), _REVISION_ID)  # type: ignore[arg-type]
    assert exc_info.value.reason_code == "missing_receipt"


# ---------------------------------------------------------------------------
# CheckpointExporterWorker
# ---------------------------------------------------------------------------


async def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        CheckpointExporterWorker(
            _session_factory, ce.CheckpointExportService(_session_factory, clock=_FakeClock()), limit=0
        )


async def test_run_once_exports_every_due_checkpoint_in_one_bounded_pass(fake: FakeCheckpointQueries) -> None:
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    fake.seed(_checkpoint_row(checkpoint_id=first_id, sequence=0))
    fake.seed(_checkpoint_row(checkpoint_id=second_id, sequence=1, revision_id=uuid.uuid4()))
    export_service = ce.CheckpointExportService(_session_factory, clock=_FakeClock(), sink=_AcceptingSink())
    worker = CheckpointExporterWorker(_session_factory, export_service)

    result = await worker.run_once()

    assert result.due == 2
    assert result.exported == 2
    assert result.sink_unavailable == 0
    assert result.integrity_failed == 0


async def test_run_once_reports_sink_unavailable_when_none_is_configured(fake: FakeCheckpointQueries) -> None:
    fake.seed(_checkpoint_row())
    export_service = ce.CheckpointExportService(_session_factory, clock=_FakeClock())  # sink=None
    worker = CheckpointExporterWorker(_session_factory, export_service)

    result = await worker.run_once()

    assert result.due == 1
    assert result.exported == 0
    assert result.sink_unavailable == 1


async def test_one_checkpoints_integrity_failure_does_not_abandon_the_rest_of_the_batch(
    fake: FakeCheckpointQueries,
) -> None:
    conflicting_id, healthy_id = uuid.uuid4(), uuid.uuid4()
    fake.seed(_checkpoint_row(checkpoint_id=conflicting_id, sequence=0))
    fake.seed(_checkpoint_row(checkpoint_id=healthy_id, sequence=1, revision_id=uuid.uuid4()))

    class _SelectivelyConflictingSink(_AcceptingSink):
        async def append(
            self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
        ) -> ce.SinkReceipt:
            if revision_id == fake.checkpoints[conflicting_id].revision_id:
                raise ce.CheckpointSinkIdentityConflict("mismatch")
            return await super().append(
                deployment_id=deployment_id, revision_id=revision_id, sequence=sequence, head_digest=head_digest
            )

    export_service = ce.CheckpointExportService(
        _session_factory, clock=_FakeClock(), sink=_SelectivelyConflictingSink()
    )
    worker = CheckpointExporterWorker(_session_factory, export_service)

    result = await worker.run_once()

    assert result.due == 2
    assert result.integrity_failed == 1
    assert result.exported == 1
