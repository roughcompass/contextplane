"""An outcome finds the receipt that preceded it, and the ways it silently cannot.

This is the correlation proof, and the reason it runs against a real database is
that the property under test is a *join* — over rows written by two independent
paths, matched on an identity neither path can see the other computing. A fake
would return whatever it was told to, including a join that production does not
make.

**The join is reference-mediated, and nothing else connects the two.** A receipt
binds the external work it was resolved about; an outcome binds the external work
it concluded. They meet because both bindings point at the same reference row,
and a reference row is identified by the tuple `(source_system,
source_namespace, kind, external_id)`. There is no receipt id in the outcome and
no outcome id in the receipt: if the tuples do not agree, nothing anywhere
reports a problem.

**That is why the negative control matters more than the positive one.** The
happy path proves the wiring exists. What the pilot actually needs proved is the
failure mode: an outcome whose `kind` is misspelled is not rejected by anything
below the adapter — it writes a *second* reference row, binds to it correctly,
and answers success. The change then reads as one whose outcome never arrived.
So this module demonstrates that failure concretely by submitting straight
through the ingest chokepoint, and only then shows the adapter refusing it.

Proving the counterfactual rather than only the guard is deliberate. A test that
asserted "the adapter raises" would pass just as well if the underlying failure
did not exist, and would leave nobody able to say why the refusal is worth its
own module.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.lifecycle import UnknownLifecycleReferenceKind
from contextplane.context.schemas.reference import normalize_reference
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.signals.adapters.control_plane import OutcomeRejected, control_plane_outcome_envelope
from contextplane.signals.envelope import SIGNAL_SCHEMA_VERSION, ExternalSignalEnvelopeV1
from contextplane.signals.ingest import SignalIngestService
from contextplane.types import SystemClock, TenantContext

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "lifecycle_outcomes"

#: The work both sides name. One deployment, cited by the receipt that preceded
#: the change and by the outcome that concluded it.
_SOURCE_SYSTEM = "github"
_NAMESPACE = "acme"
_DEPLOYMENT_ID = "deploy-20260809-1157"

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _async_url(sync_or_async: str) -> str:
    return sync_or_async.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def seat(sync_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, an actor, and one registered outcome seat.

    One seat rather than several: this module is about whether an outcome finds
    its receipt, and a second source would only vary something the join does not
    look at.
    """
    tenant_id, actor_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'outcome test')"),
            {"t": tenant_id, "slug": f"outcome-{tenant_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind)"
                " VALUES (:a, :t, :sub, 'CI seat', 'service')"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"s-{actor_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name)" " VALUES (:s, :t, :k, :k)"
            ),
            {"s": source_id, "t": tenant_id, "k": "github-actions"},
        )
        conn.execute(
            text("INSERT INTO memory_source_governance (source_id, tenant_id, authority_tier) VALUES (:s, :t, :tier)"),
            {"s": source_id, "t": tenant_id, "tier": AUTHORITY_OBSERVER_EXTRACTION},
        )
    return tenant_id, actor_id, source_id


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def _ingest(pg_container: str, ctx: TenantContext, envelope: ExternalSignalEnvelopeV1) -> Any:
    """Drive the real chokepoint against the real database."""

    async def run() -> Any:
        engine = create_async_engine(_async_url(pg_container))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            from contextplane.service.memory.source_governance import SourceGovernanceService

            service = SignalIngestService(
                factory,
                clock=SystemClock(),
                governance=SourceGovernanceService(factory, clock=SystemClock()),
            )
            return await service.ingest(ctx, envelope)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _seed_receipt_citing(
    sync_engine: Engine,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: str,
    external_id: str,
) -> uuid.UUID:
    """A stored receipt bound to one piece of external work.

    Written directly rather than by resolving a context request: this module is
    about whether an outcome reaches a receipt, and driving the whole resolve
    path would make the test fail for reasons belonging to another surface. The
    rows are the ones that path writes.
    """
    receipt_id, reference_id = uuid.uuid4(), uuid.uuid4()
    reference = normalize_reference(
        {
            "source_system": _SOURCE_SYSTEM,
            "source_namespace": _NAMESPACE,
            "kind": kind,
            "external_id": external_id,
            "classification": "internal",
            "external_authority": "acme/platform",
        }
    )
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_receipts "
                "(receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
                "VALUES (:r, :t, 'complete', TRUE, :now, :actor)"
            ),
            {"r": receipt_id, "t": tenant_id, "now": _NOW, "actor": str(actor_id)},
        )
        conn.execute(
            text(
                "INSERT INTO context_external_references "
                "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                " classification, external_authority, collision_key, created_at) "
                "VALUES (:ref, :t, :sys, :ns, :kind, :eid, :cls, :auth, :key, :now) "
                "ON CONFLICT (tenant_id, collision_key) DO NOTHING"
            ),
            {
                "ref": reference_id,
                "t": tenant_id,
                "sys": reference.source_system,
                "ns": reference.source_namespace,
                "kind": reference.kind,
                "eid": reference.external_id,
                "cls": reference.classification,
                "auth": reference.external_authority,
                "key": reference.collision_key(),
                "now": _NOW,
            },
        )
        stored = conn.execute(
            text("SELECT reference_id FROM context_external_references WHERE tenant_id = :t AND collision_key = :key"),
            {"t": tenant_id, "key": reference.collision_key()},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO context_reference_bindings "
                "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                "VALUES (:b, :t, :ref, 'context_item', :subject, :now)"
            ),
            {"b": uuid.uuid4(), "t": tenant_id, "ref": stored, "subject": receipt_id, "now": _NOW},
        )
    return receipt_id


def _outcome_envelope(fixture: dict[str, Any], *, source_id: uuid.UUID, producer_id: str) -> ExternalSignalEnvelopeV1:
    return control_plane_outcome_envelope(
        source_id=source_id,
        source_system="github-actions",
        producer_id=producer_id,
        outcome=fixture["outcome"],
        references=tuple(normalize_reference(dict(ref)) for ref in fixture["references"]),
        concluded_at=datetime.datetime.fromisoformat(fixture["concluded_at"]),
        received_at=datetime.datetime.fromisoformat(fixture["received_at"]),
        attempt=fixture["attempt"],
    )


def _joined_signal_ids(sync_engine: Engine, *, tenant_id: uuid.UUID, receipt_id: uuid.UUID) -> list[uuid.UUID]:
    """Every signal reachable from one receipt through shared external work.

    The join production makes, written out rather than delegated, so the test
    fails if the mediation changes shape rather than passing against whatever a
    helper happens to return.
    """
    with sync_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT sig.subject_id "
                "  FROM context_reference_bindings rec "
                "  JOIN context_reference_bindings sig "
                "    ON sig.reference_id = rec.reference_id AND sig.tenant_id = rec.tenant_id "
                " WHERE rec.tenant_id = :t "
                "   AND rec.subject_type = 'context_item' AND rec.subject_id = :r "
                "   AND sig.subject_type = 'external_signal'"
            ),
            {"t": tenant_id, "r": receipt_id},
        ).all()
    return [row[0] for row in rows]


def _reference_rows(sync_engine: Engine, *, tenant_id: uuid.UUID, external_id: str) -> list[str]:
    with sync_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT kind FROM context_external_references "
                " WHERE tenant_id = :t AND external_id = :eid ORDER BY kind"
            ),
            {"t": tenant_id, "eid": external_id},
        ).all()
    return [row[0] for row in rows]


# --- the join the pilot depends on -------------------------------------------


def test_an_outcome_and_the_receipt_before_it_meet_through_the_work_both_name(
    sync_engine: Engine, pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The positive control: one in-set kind, one reference row, one join.

    Asserted through the join production makes rather than by comparing the two
    rows' fields, because agreeing field-by-field is not the same as being
    reachable from one another.
    """
    tenant_id, actor_id, source_id = seat
    receipt_id = _seed_receipt_citing(
        sync_engine, tenant_id=tenant_id, actor_id=actor_id, kind="deployment", external_id=_DEPLOYMENT_ID
    )
    ctx = _ctx(tenant_id, actor_id)

    stored = _ingest(
        pg_container,
        ctx,
        _outcome_envelope(_fixture("deployment_success"), source_id=source_id, producer_id=str(actor_id)),
    )

    assert _joined_signal_ids(sync_engine, tenant_id=tenant_id, receipt_id=receipt_id) == [stored.signal_id]
    assert _reference_rows(sync_engine, tenant_id=tenant_id, external_id=_DEPLOYMENT_ID) == [
        "deployment"
    ], "both sides must converge on one reference row, not two that agree"


def test_the_outcome_keeps_the_authority_its_seat_was_registered_with(
    sync_engine: Engine, pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The adapter carries no authority, so the stored row must show the seat's.

    Worth its own assertion because an adapter that could set it would be one a
    submitter could use to promote its own observation.
    """
    tenant_id, actor_id, source_id = seat
    ctx = _ctx(tenant_id, actor_id)

    stored = _ingest(
        pg_container,
        ctx,
        _outcome_envelope(_fixture("workflow_run_success"), source_id=source_id, producer_id=str(actor_id)),
    )

    with sync_engine.begin() as conn:
        authority = conn.execute(
            text("SELECT authority FROM external_signals WHERE signal_id = :s"), {"s": stored.signal_id}
        ).scalar_one()

    assert authority == AUTHORITY_OBSERVER_EXTRACTION


# --- the failure the refusal exists to prevent -------------------------------


def test_a_misspelled_kind_stores_cleanly_and_then_joins_to_nothing(
    sync_engine: Engine, pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The counterfactual, submitted straight through the chokepoint.

    Nothing below the adapter refuses this. The submission succeeds, a second
    reference row appears, and the receipt citing the correct spelling for the
    same external id reaches none of it. This is what "silently unjoined" means
    concretely, and it is the reason the refusal in the next test is worth a
    module of its own rather than a comment.
    """
    tenant_id, actor_id, source_id = seat
    receipt_id = _seed_receipt_citing(
        sync_engine, tenant_id=tenant_id, actor_id=actor_id, kind="deployment", external_id=_DEPLOYMENT_ID
    )
    ctx = _ctx(tenant_id, actor_id)
    misspelled = normalize_reference(dict(_fixture("misspelled_kind")["references"][0]))

    # Built by hand, bypassing the adapter deliberately: the point is what the
    # layer underneath accepts, not what the adapter does with it.
    stored = _ingest(
        pg_container,
        ctx,
        ExternalSignalEnvelopeV1(
            source_id=source_id,
            source_system="github-actions",
            source_event_id=f"github-actions:deployment:{_DEPLOYMENT_ID}:1",
            producer_id=str(actor_id),
            producer_type="external",
            idempotency_key=f"bypass-{uuid.uuid4()}",
            classification="internal",
            schema_version=SIGNAL_SCHEMA_VERSION,
            event_time=_NOW,
            observed_time=_NOW,
            references=(misspelled,),
            payload={"object": "deployment", "object_id": _DEPLOYMENT_ID, "conclusion": "success"},
        ),
    )

    assert stored.signal_id, "the layer below the adapter accepts this without complaint"
    assert _reference_rows(sync_engine, tenant_id=tenant_id, external_id=_DEPLOYMENT_ID) == [
        "deployment",
        "deploymnet",
    ], "the misspelling wrote its own reference row rather than colliding with the correct one"
    assert (
        _joined_signal_ids(sync_engine, tenant_id=tenant_id, receipt_id=receipt_id) == []
    ), "and the receipt reaches none of it -- no error, no warning, just an outcome nobody can find"


def test_the_adapter_refuses_the_misspelling_that_would_have_been_stored(
    seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The negative control proper, against the shape proved unjoinable above.

    Same submission, same fixture, through the adapter instead of around it.
    """
    _tenant_id, actor_id, source_id = seat

    with pytest.raises(UnknownLifecycleReferenceKind):
        _outcome_envelope(_fixture("misspelled_kind"), source_id=source_id, producer_id=str(actor_id))


def test_the_adapter_refuses_an_outcome_that_cites_no_work_at_all(seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]) -> None:
    """The other unjoinable shape, and the one the envelope surface accepts.

    An outcome with no references is stored, returns success, and is reachable
    from no receipt ever. There is no misspelling to notice here -- the
    submission is simply incomplete in a way nothing downstream can detect.
    """
    _tenant_id, actor_id, source_id = seat

    with pytest.raises(OutcomeRejected):
        _outcome_envelope(_fixture("unjoinable_no_references"), source_id=source_id, producer_id=str(actor_id))


def test_a_requested_action_cannot_be_submitted_as_an_outcome(seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]) -> None:
    """A triggered action is never success, enforced where it is submitted.

    Without this, "the deployment was requested" and "the deployment succeeded"
    reach the ledger as the same kind of record, and the difference is
    unrecoverable afterwards.
    """
    _tenant_id, actor_id, source_id = seat

    with pytest.raises(OutcomeRejected):
        _outcome_envelope(_fixture("requested_not_concluded"), source_id=source_id, producer_id=str(actor_id))


# --- replay ------------------------------------------------------------------


def test_the_same_outcome_resubmitted_converges_on_the_stored_row(
    pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """A redelivery is not a second outcome.

    The retry path is the one a control plane exercises most, and an outcome
    filed twice would double-count the thing the pilot is measuring.
    """
    tenant_id, actor_id, source_id = seat
    ctx = _ctx(tenant_id, actor_id)
    fixture = _fixture("workflow_run_success")
    key = f"replay-{uuid.uuid4()}"

    def submit(concluded_at: datetime.datetime) -> Any:
        return _ingest(
            pg_container,
            ctx,
            control_plane_outcome_envelope(
                source_id=source_id,
                source_system="github-actions",
                producer_id=str(actor_id),
                outcome=fixture["outcome"],
                references=tuple(normalize_reference(dict(ref)) for ref in fixture["references"]),
                concluded_at=concluded_at,
                received_at=datetime.datetime.fromisoformat(fixture["received_at"]),
                attempt=fixture["attempt"],
                idempotency_key=key,
            ),
        )

    utc = datetime.datetime.fromisoformat(fixture["concluded_at"])
    first = submit(utc)
    # Replayed from a queue holding the same instant under a different offset.
    # Without UTC normalization this digests differently and answers a conflict
    # instead of converging, which is the defect the adapter's own docstring names.
    second = submit(utc.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))))

    assert second.signal_id == first.signal_id
