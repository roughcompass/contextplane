"""What the claim-derivative handler decides, and what the chain says must be reached.

The integration suite proves the writes against a real database. These prove the
decisions a live query cannot check: that a locator this handler did not write is
refused rather than guessed at, that a rebuild is refused rather than reported done,
and that the referent set names *every* record a chain read — the property that makes a
revoked signal's propagation arrive here at all, and the one a passing end-to-end test
would still hold if half the sources were dropped.

Every test asserts a behaviour. None exists to move a coverage number.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.retention import derivatives, policies
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.claim_erasure_writes import CLAIM_STATUS_CLOSED
from contextplane.service.memory.derivation import Evidence
from contextplane.service.memory.derivative_handlers import (
    AUDIENCE_PARTITION,
    HANDLER_VERSION,
    STATUS_INVALIDATED,
    ClaimDerivativeHandler,
    derivation_from_locator,
    locator_for,
)
from contextplane.service.memory.evidence import EvidenceRefused, source_referents

_TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def _evidence(kind: str, **overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "kind": kind,
        "source_authority": AUTHORITY_OBSERVER_EXTRACTION,
        "classification": "internal",
    }
    fields.update(overrides)
    return Evidence(**fields)


def _registration(locator: str, *, tenant_id: uuid.UUID = _TENANT) -> derivatives.Registration:
    return derivatives.Registration(
        derivative_id=uuid.uuid4(),
        tenant_id=tenant_id,
        derivative_kind=derivatives.KIND_CLAIM_DERIVATIVE,
        storage_locator=locator,
        audience_partition=AUDIENCE_PARTITION,
        classification="internal",
        expires_at=_NOW + datetime.timedelta(days=1),
        blocking=True,
    )


# --- Test doubles -------------------------------------------------------------


class _Row:
    def __init__(self, status: str, created_claim_id: uuid.UUID | None) -> None:
        self.status = status
        self.created_claim_id = created_claim_id


class _Result:
    def __init__(self, row: _Row | None = None, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def one_or_none(self) -> _Row | None:
        return self._row


class _FakeSession:
    """Answers the attempt read with a declared row and counts what was written.

    Keyed on the leading table name rather than the whole statement: these tests care
    which artefacts a reduction reaches and which it leaves alone, not the exact SQL,
    which the integration tier is the right place to pin.
    """

    def __init__(self, attempt: _Row | None, *, affected: int = 1) -> None:
        self._attempt = attempt
        self._affected = affected
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        self.params.append(params or {})
        if sql.startswith("SELECT"):
            return _Result(row=self._attempt)
        return _Result(rowcount=self._affected)

    def wrote(self, table: str) -> bool:
        return any(sql.startswith(f"UPDATE {table}") for sql in self.statements)


def _apply(session: _FakeSession, *, locator: str, operation: str = derivatives.OPERATION_DELETE) -> int:
    import asyncio

    return asyncio.run(ClaimDerivativeHandler().apply(session, _registration(locator), operation))  # type: ignore[arg-type]


# --- the locator --------------------------------------------------------------


def test_a_locator_round_trips_through_the_one_spelling_both_sides_use() -> None:
    """The registrar and the handler share the function, so they cannot disagree.

    A locator only one of them can read leaves the derivative unreachable while the
    registration looks complete — which is indistinguishable, from the outside, from
    an artefact that was already reduced.
    """
    derivation_id = uuid.uuid4()
    assert derivation_from_locator(locator_for(derivation_id)) == derivation_id


@pytest.mark.parametrize(
    "locator",
    ["receipt:0d3b1b6e-0000-4000-8000-000000000000", str(uuid.uuid4()), "", "claim_derivation:not-a-uuid"],
)
def test_a_locator_this_handler_did_not_write_is_refused_rather_than_guessed_at(locator: str) -> None:
    """Guessing would reduce the wrong attempt, or none, and report success either way.

    A refusal lands the work item in `failed`, where it is counted as overdue; a silent
    zero would mark it done with the quotations still in place.
    """
    with pytest.raises(derivatives.UnhandledDerivativeKind):
        derivation_from_locator(locator)


# --- the referent set ---------------------------------------------------------


def test_every_record_the_chain_read_becomes_a_referent() -> None:
    """*Every* one: the propagation reaches this attempt through whichever source was
    erased, so a chain that registered only one of three would survive the erasure of
    the other two."""
    signal_id, checkpoint_id, receipt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    referents = source_referents(
        [
            _evidence("signal", signal_id=signal_id),
            _evidence("checkpoint", checkpoint_id=checkpoint_id, checkpoint_digest="sha256:abc"),
            _evidence("receipt", receipt_id=receipt_id),
        ]
    )
    assert referents == (
        (policies.RECORD_EXTERNAL_SIGNAL, signal_id),
        (policies.RECORD_TASK_CHECKPOINT, checkpoint_id),
        (policies.RECORD_CONTEXT_RECEIPT, receipt_id),
    )


def test_an_exact_item_citation_is_retained_on_its_receipts_clock() -> None:
    """The item has no life independent of the receipt it is on, so it contributes the
    receipt as its referent — and two citations of one receipt are one source, not two
    link rows racing to say the same thing."""
    receipt_id = uuid.uuid4()
    referents = source_referents(
        [
            _evidence("receipt_item", receipt_id=receipt_id, receipt_item_id="item-1"),
            _evidence("receipt_item", receipt_id=receipt_id, receipt_item_id="item-2"),
            _evidence("receipt", receipt_id=receipt_id),
        ]
    )
    assert referents == ((policies.RECORD_CONTEXT_RECEIPT, receipt_id),)


def test_an_external_reference_is_not_a_retention_source() -> None:
    """It points at material this product does not hold, so there is no row to expire
    and nothing of the subject's to erase. Registering one would claim a clock over a
    record somebody else owns."""
    assert source_referents([_evidence("external_reference", reference_id=uuid.uuid4())]) == ()


def test_an_empty_chain_is_refused_before_a_referent_is_computed() -> None:
    """The same refusal the chain validation makes, reached the same way: a derivation
    with no inputs has no sources, and answering "no sources" would register an
    artefact nothing can expire."""
    with pytest.raises(EvidenceRefused):
        source_referents([])


# --- the reduction ------------------------------------------------------------


def test_delete_and_redact_both_reduce_and_neither_removes_the_shell() -> None:
    """The approved disposition for a claim is minimization. A handler that implemented
    `delete` literally would destroy the record that the assertion was ever made, which
    is the part an auditor needs when somebody asks what was believed about them."""
    claim_id = uuid.uuid4()
    for operation in (derivatives.OPERATION_DELETE, derivatives.OPERATION_REDACT):
        session = _FakeSession(_Row(status="staged", created_claim_id=claim_id))
        _apply(session, locator=locator_for(uuid.uuid4()), operation=operation)
        assert session.wrote("derivation_evidence_links")
        assert session.wrote("memory_claim_provenance")
        assert session.wrote("memory_claims")
        assert session.wrote("claim_derivations")
        assert not any("DELETE" in sql for sql in session.statements)


def test_the_claim_is_closed_into_the_status_that_serves_nowhere() -> None:
    """Reads filter on `status IN ('staged', 'superseded')`, so this is what takes the
    claim out of every serving path — and the bi-temporal column the schema ties to
    `superseded` is cleared with it, because that biconditional is a CHECK."""
    session = _FakeSession(_Row(status="staged", created_claim_id=uuid.uuid4()))
    _apply(session, locator=locator_for(uuid.uuid4()))

    close = next(
        params
        for sql, params in zip(session.statements, session.params, strict=True)
        if sql.startswith("UPDATE memory_claims")
    )
    assert close["closed"] == CLAIM_STATUS_CLOSED
    statement = next(sql for sql in session.statements if sql.startswith("UPDATE memory_claims"))
    assert "t_invalidated_at = NULL" in statement
    assert "superseded_by" not in statement, "the supersession chain stays walkable; only the quotations go"


def test_an_attempt_with_no_claim_still_has_its_evidence_minimized() -> None:
    """Nothing links a claim to its attempt in the tree as shipped, so this is the live
    shape: the excerpts on the evidence links are the copy that exists, and they are
    what a source's erasure has to reach."""
    session = _FakeSession(_Row(status="pending", created_claim_id=None))
    _apply(session, locator=locator_for(uuid.uuid4()))

    assert session.wrote("derivation_evidence_links")
    assert session.wrote("claim_derivations")
    assert not session.wrote("memory_claim_provenance")
    assert not session.wrote("memory_claims")


def test_an_attempt_the_registration_outlived_is_a_successful_zero() -> None:
    """A retried propagation item whose attempt is already gone has nothing to do.
    Reporting that as an error would turn the normal recovery path into a compliance
    incident and make the queue's `failed` count meaningless."""
    session = _FakeSession(None)
    assert _apply(session, locator=locator_for(uuid.uuid4())) == 0
    assert not any(sql.startswith("UPDATE") for sql in session.statements)


def test_the_attempt_is_read_under_its_tenant_and_locked_before_it_is_written() -> None:
    """Two propagation items for one attempt — an erasure and an expiry arriving
    together — would otherwise interleave the read of the claim with the write of it.
    The tenant condition is the half a live query cannot check: a `WHERE` that forgot it
    still returns the row it was going to return."""
    session = _FakeSession(_Row(status="pending", created_claim_id=None))
    _apply(session, locator=locator_for(uuid.uuid4()))

    read = session.statements[0]
    assert read.startswith("SELECT status, created_claim_id")
    assert "tenant_id = :tenant" in read
    assert "FOR UPDATE" in read
    assert session.params[0]["tenant"] == _TENANT


def test_a_rebuild_is_refused_rather_than_reported_done() -> None:
    """Re-deriving a claim would re-read the evidence this work item is withdrawing.
    The refusal lands the item in `failed`, which `pending_overdue` counts; a silent
    success would mark the artefact handled with its quotations intact."""
    session = _FakeSession(_Row(status="staged", created_claim_id=uuid.uuid4()))
    with pytest.raises(derivatives.UnhandledDerivativeKind, match="cannot be rebuilt"):
        _apply(session, locator=locator_for(uuid.uuid4()), operation=derivatives.OPERATION_REBUILD)
    assert not any(sql.startswith("UPDATE") for sql in session.statements)


def test_an_operation_outside_the_closed_set_is_refused() -> None:
    """The schema stores three. A fourth reaching a handler means something wrote a
    value the CHECK admits nowhere, and acting on it would be acting on a guess."""
    session = _FakeSession(_Row(status="staged", created_claim_id=uuid.uuid4()))
    with pytest.raises(derivatives.UnhandledDerivativeKind, match="not a propagation operation"):
        _apply(session, locator=locator_for(uuid.uuid4()), operation="minimize")


def test_the_handler_declares_the_kind_and_version_a_registration_records() -> None:
    """The kind is what the registry keys on and what the release gate counts; the
    version is what tells two artefacts reduced by different implementations apart
    without re-reading either."""
    handler = ClaimDerivativeHandler()
    assert handler.kind == derivatives.KIND_CLAIM_DERIVATIVE
    assert handler.version == HANDLER_VERSION

    registry = derivatives.HandlerRegistry()
    registry.register(handler)
    assert derivatives.KIND_CLAIM_DERIVATIVE not in registry.unhandled_kinds()
    assert registry.handler_for(derivatives.KIND_CLAIM_DERIVATIVE) is handler


def test_the_invalidated_status_is_one_the_attempt_table_admits() -> None:
    """`claim_derivations` closes the set in a CHECK, so a status this handler invented
    would fail at the write rather than at review."""
    assert STATUS_INVALIDATED == "invalidated"
