"""Resume against real data: the bounds hold, and the answer is stable.

Determinism is the property this phase exists to establish, and it is not
provable in a unit test: it is a claim about what two database reads return, in
what order, over rows a previous run inserted.

The stability test is the one that matters most. An agent resuming twice with no
work in between must get the same answer both times -- otherwise a caller
diffing two resumes to see what moved sees churn no work caused, and stops
trusting the diff. A later checkpoint must change the answer, because that is
the only thing that should.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.resume import ContextResumeService, ResumeRequest
from contextplane.service.memory.claim_serving import RECALL_LABEL, RECALL_TRUST
from contextplane.signals.reads import FeedbackReadService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_REF = ("github", "acme/app", "pull_request", "42")

type _Wired = dict[str, Any]


@pytest_asyncio.fixture
async def wired(pg_container: str) -> AsyncIterator[_Wired]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
        intent_id, reference_id = uuid.uuid4(), uuid.uuid4()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": tenant_id, "s": f"rz-{tenant_id.hex[:8]}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:actor, :tenant, :subject, 'resume owner', :now)"
                ),
                {"actor": actor_id, "tenant": tenant_id, "subject": f"resume-{actor_id}", "now": _NOW},
            )
            # The actor participates, so the head read is authorized. Resume
            # reads through the same predicate every other task read uses.
            await session.execute(
                text(
                    "INSERT INTO intent_participant_grants "
                    "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:t, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"t": tenant_id, "task": intent_id, "actor": str(actor_id), "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO context_external_references "
                    "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                    " classification, external_authority, collision_key) "
                    "VALUES (:rid, :t, :sys, :ns, :kind, :eid, 'internal', 'github', :ckey)"
                ),
                {
                    "rid": reference_id,
                    "t": tenant_id,
                    "sys": _REF[0],
                    "ns": _REF[1],
                    "kind": _REF[2],
                    "eid": _REF[3],
                    "ckey": "|".join(_REF),
                },
            )

        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "intent_id": intent_id,
            "reference_id": reference_id,
            "service": ContextResumeService(session_factory=factory, clock=FakeClock(_NOW)),
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        }
    finally:
        await engine.dispose()


async def _checkpoint(wired: _Wired, *, sequence: int, goal: str, next_action: str | None = None) -> uuid.UUID:
    """Append one checkpoint, keeping the chain intact.

    The predecessor is threaded rather than left NULL: the chain constraint
    refuses a checkpoint past the first that names no parent, which is what
    stops a step being written into the middle of somebody else's history.
    """
    checkpoint_id = uuid.uuid4()
    predecessor = wired.get("last_checkpoint_id")
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO intent_checkpoints "
                "(checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, decisions, assumptions, "
                " evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, "
                " digest) "
                "VALUES (:cid, :t, :task, :seq, :pred, :goal, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                " :oq, :next, 'agent-a', :at, 'standard', :digest)"
            ),
            {
                "cid": checkpoint_id,
                "t": wired["tenant_id"],
                "task": wired["intent_id"],
                "seq": sequence,
                "pred": predecessor,
                "goal": goal,
                "oq": f'["q{sequence}"]',
                "next": next_action,
                "at": _NOW + datetime.timedelta(minutes=sequence),
                "digest": f"digest-{sequence}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO intent_heads (tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at) "
                "VALUES (:t, :task, :cid, :seq, :summary, :at) "
                "ON CONFLICT (tenant_id, intent_id) DO UPDATE SET "
                "  head_checkpoint_id = EXCLUDED.head_checkpoint_id, "
                "  head_sequence = EXCLUDED.head_sequence, "
                "  summary = EXCLUDED.summary, "
                "  updated_at = EXCLUDED.updated_at"
            ),
            {
                "t": wired["tenant_id"],
                "task": wired["intent_id"],
                "cid": checkpoint_id,
                "seq": sequence,
                "summary": goal,
                "at": _NOW + datetime.timedelta(minutes=sequence),
            },
        )
        # A reference is evidence a checkpoint cited, so the binding hangs off
        # the checkpoint. Resume reaches the task through it -- the junction has
        # no `task` subject type, and that is the shape the schema intends.
        await session.execute(
            text(
                "INSERT INTO context_reference_bindings "
                "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                "VALUES (:bid, :t, :rid, 'intent_checkpoint', :cid, :now)"
            ),
            {
                "bid": uuid.uuid4(),
                "t": wired["tenant_id"],
                "rid": wired["reference_id"],
                "cid": checkpoint_id,
                "now": _NOW,
            },
        )
    wired["last_checkpoint_id"] = checkpoint_id
    return checkpoint_id


def _request(**overrides: Any) -> ResumeRequest:
    return ResumeRequest(references=(_REF,), **overrides)


async def _receipt(
    wired: _Wired,
    *,
    resolved_at: datetime.datetime,
    items: tuple[str, ...] = (),
) -> uuid.UUID:
    receipt_id = uuid.uuid4()
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_receipts "
                "(receipt_id, tenant_id, intent_id, state, cacheable, resolved_at, requested_by, request_digest) "
                "VALUES (:receipt, :tenant, :task, 'complete', TRUE, :resolved, 'resume-test', :digest)"
            ),
            {
                "receipt": receipt_id,
                "tenant": wired["tenant_id"],
                "task": wired["intent_id"],
                "resolved": resolved_at,
                "digest": f"sha256:{receipt_id.hex}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO context_reference_bindings "
                "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                "VALUES (:binding, :tenant, :reference, 'context_item', :receipt, :resolved)"
            ),
            {
                "binding": uuid.uuid4(),
                "tenant": wired["tenant_id"],
                "reference": wired["reference_id"],
                "receipt": receipt_id,
                "resolved": resolved_at,
            },
        )
        for item in items:
            await session.execute(
                text(
                    "INSERT INTO context_receipt_items "
                    "(item_row_id, receipt_id, receipt_item_id, block, source, item_key) "
                    "VALUES (:row, :receipt, :item, 'canonical', 'resume-test', :item)"
                ),
                {"row": uuid.uuid4(), "receipt": receipt_id, "item": item},
            )
    return receipt_id


async def _identity(wired: _Wired) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tenant, :slug, :slug, :now, TRUE)"
            ),
            {"tenant": tenant_id, "slug": f"foreign-{tenant_id.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                "VALUES (:actor, :tenant, :subject, 'foreign actor', :now)"
            ),
            {"actor": actor_id, "tenant": tenant_id, "subject": f"foreign-{actor_id}", "now": _NOW},
        )
    return tenant_id, actor_id


async def _claim(
    wired: _Wired,
    *,
    consolidated_at: datetime.datetime | None,
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    asserted_valid_from: datetime.datetime | None = None,
) -> uuid.UUID:
    tenant_id = tenant_id or wired["tenant_id"]
    actor_id = actor_id or wired["actor_id"]
    claim_id = claim_id or uuid.uuid4()
    entity_id = uuid.uuid4()
    # When a caller does not separate them, a claim is asserted and reviewed at
    # the same instant. Separating them is what lets one test prove the window
    # is ordered by review time rather than by assertion time.
    created_at = asserted_valid_from or consolidated_at or (_NOW - datetime.timedelta(hours=2))
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities "
                "(entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                "VALUES (:entity, :tenant, 'capability', :name, 'tenant-shared', TRUE, :created)"
            ),
            {
                "entity": entity_id,
                "tenant": tenant_id,
                "name": f"resume-{entity_id.hex}",
                "created": created_at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                " claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                " subject_reference, predicate, value_type, claim_category, value_jsonb, asserted_valid_from,"
                " status, visibility, source_authority, size_bytes, consolidated_at, created_at, confidence,"
                " confidence_scored_at, confidence_inputs, scorer_version, calibration_version, decay_half_life_days"
                ") VALUES ("
                " :claim, :tenant, :tenant, :actor, :entity, :subject, :predicate, 'prose',"
                " 'operational_lifecycle', CAST(:value AS JSONB), :created, 'staged', 'private',"
                " 'observer_extraction', 32, :consolidated, :created, 0.800, :created,"
                " CAST(:inputs AS JSONB), 'scorer.v1', 'calib.v1', 30"
                ")"
            ),
            {
                "claim": claim_id,
                "tenant": tenant_id,
                "actor": actor_id,
                "entity": entity_id,
                "subject": f"resume:{entity_id}",
                "predicate": f"resume.learning.{claim_id.hex}",
                "value": json.dumps(f"learning-{claim_id}"),
                "created": created_at,
                "consolidated": consolidated_at,
                "inputs": json.dumps({"resume_test": True}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                "VALUES (:claim, 'connector_run', :ref)"
            ),
            {"claim": claim_id, "ref": f"resume:{claim_id}"},
        )
    return claim_id


async def _feedback(
    wired: _Wired,
    *,
    feedback_id: uuid.UUID,
    receipt_id: uuid.UUID,
    receipt_item_id: str | None,
    created_at: datetime.datetime,
    learning_eligible: bool = True,
) -> None:
    kind = "item_specific" if receipt_item_id else "receipt_level"
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_feedback ("
                " feedback_id, tenant_id, kind, receipt_id, receipt_item_id, rating, learning_eligible, note,"
                " reporter_id, reporter_type, idempotency_key, content_digest, created_at"
                ") VALUES ("
                " :feedback, :tenant, :kind, :receipt, :item, 'incorrect', :eligible, 'private reporter note',"
                " :reporter, 'human', :key, :digest, :created"
                ")"
            ),
            {
                "feedback": feedback_id,
                "tenant": wired["tenant_id"],
                "kind": kind,
                "receipt": receipt_id,
                "item": receipt_item_id,
                "eligible": learning_eligible,
                "reporter": f"reporter-{feedback_id}",
                "key": f"resume-{feedback_id}",
                "digest": feedback_id.hex,
                "created": created_at,
            },
        )


async def _derivation_locus(
    wired: _Wired,
    *,
    tenant_id: uuid.UUID,
    receipt_id: uuid.UUID,
    receipt_item_id: str | None,
    created_claim_id: uuid.UUID | None,
) -> None:
    derivation_id = uuid.uuid4()
    evidence_kind = "receipt_item" if receipt_item_id else "receipt"
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO claim_derivations ("
                " derivation_id, tenant_id, profile, profile_version, status, applicability, assertion_digest,"
                " source_authority, classification, created_claim_id, created_at"
                ") VALUES ("
                " :derivation, :tenant, 'resume-test', 'v1', :status, 'global', :digest,"
                " 'observer_extraction', 'internal', :claim, :created"
                ")"
            ),
            {
                "derivation": derivation_id,
                "tenant": tenant_id,
                "status": "staged" if created_claim_id else "pending",
                "digest": uuid.uuid4().hex,
                "claim": created_claim_id,
                "created": _NOW,
            },
        )
        await session.execute(
            text(
                "INSERT INTO derivation_evidence_links ("
                " link_id, derivation_id, evidence_kind, receipt_id, receipt_item_id,"
                " source_authority, classification"
                ") VALUES ("
                " :link, :derivation, :kind, :receipt, :item, 'human_feedback', 'internal'"
                ")"
            ),
            {
                "link": uuid.uuid4(),
                "derivation": derivation_id,
                "kind": evidence_kind,
                "receipt": receipt_id,
                "item": receipt_item_id,
            },
        )


# --- Determinism ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_resumes_with_an_unchanged_head_are_identical(wired: _Wired) -> None:
    """The property the phase exists to establish. A caller diffing two resumes
    to see what moved must see only what moved."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second", next_action="carry on")

    first = await wired["service"].resume(wired["ctx"], _request())
    second = await wired["service"].resume(wired["ctx"], _request())

    assert first.head_checkpoint_id == second.head_checkpoint_id
    assert [c.checkpoint_id for c in first.checkpoints] == [c.checkpoint_id for c in second.checkpoints]
    assert first.open_questions == second.open_questions
    assert first.next_action == second.next_action


@pytest.mark.asyncio
async def test_checkpoints_come_back_oldest_first(wired: _Wired) -> None:
    """Read newest-first so the bound keeps the recent end, then reversed so a
    reader gets them in the order they happened."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second")
    await _checkpoint(wired, sequence=3, goal="third")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert [c.sequence for c in state.checkpoints] == [1, 2, 3]


# --- Stability after later work ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_checkpoint_moves_the_head_and_the_answer(wired: _Wired) -> None:
    """The only thing that should change the answer. Without this the
    determinism test above could be passing because resume returns nothing."""
    await _checkpoint(wired, sequence=1, goal="first", next_action="do the first thing")
    before = await wired["service"].resume(wired["ctx"], _request())

    await _checkpoint(wired, sequence=2, goal="second", next_action="do the second thing")
    after = await wired["service"].resume(wired["ctx"], _request())

    assert before.head_sequence == 1
    assert after.head_sequence == 2
    assert before.next_action == "do the first thing"
    assert after.next_action == "do the second thing"


@pytest.mark.asyncio
async def test_open_questions_come_from_the_newest_checkpoint_not_the_union(wired: _Wired) -> None:
    """A question closed three checkpoints ago is not open. Unioning the window
    would resurrect it, and a resumed agent would go and answer it again."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert state.open_questions == ("q2",)


# --- Bounds ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_checkpoint_bound_keeps_the_recent_end(wired: _Wired) -> None:
    """Bounding from the old end would return the beginning of a long task and
    call it resume."""
    for sequence in range(1, 8):
        await _checkpoint(wired, sequence=sequence, goal=f"step {sequence}")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=3))

    assert [c.sequence for c in state.checkpoints] == [5, 6, 7]


@pytest.mark.asyncio
async def test_hitting_a_bound_is_reported_rather_than_silent(wired: _Wired) -> None:
    """A resume that quietly returned three of seven would read as the whole
    story, and the caller would carry on from a middle it believed was the
    start."""
    for sequence in range(1, 8):
        await _checkpoint(wired, sequence=sequence, goal=f"step {sequence}")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=3))

    assert "checkpoints" in state.truncated


@pytest.mark.asyncio
async def test_a_resume_inside_its_bounds_reports_no_truncation(wired: _Wired) -> None:
    """The other half. Without it `truncated` could be set unconditionally."""
    await _checkpoint(wired, sequence=1, goal="only one")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=5))

    assert state.truncated == ()


# --- Authorization -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_participant_is_refused_before_any_resume_arm_runs(wired: _Wired) -> None:
    """A partial 200 could still leak receipts, feedback or newer learning."""
    await _checkpoint(wired, sequence=1, goal="private work")
    stranger = TenantContext(tenant_id=wired["tenant_id"], actor_id=uuid.uuid4(), roles=["producer"])

    with pytest.raises(PermissionError, match="outside the task audience"):
        await wired["service"].resume(stranger, _request())


@pytest.mark.asyncio
async def test_another_tenant_sees_nothing_at_all(wired: _Wired) -> None:
    """The reference itself is tenant-scoped, so a foreign caller does not even
    resolve the work -- it gets the empty answer, not a filtered one."""
    outsider = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])

    state = await wired["service"].resume(outsider, _request())

    assert state.is_empty()


@pytest.mark.asyncio
async def test_an_unknown_reference_resumes_empty_rather_than_failing(wired: _Wired) -> None:
    """ "Start fresh" is a legitimate answer and must not arrive as an error --
    a pipeline resuming a run that has no history yet is the common case."""
    state = await wired["service"].resume(
        wired["ctx"], ResumeRequest(references=(("github", "acme/app", "pull_request", "99999"),))
    )

    assert state.is_empty()
    assert state.checkpoints == ()
    assert state.learning == ()


# --- Feedback from the last resolution ---------------------------------------


@pytest.mark.asyncio
async def test_feedback_is_annotated_never_suppressed_and_unresolved_first(wired: _Wired) -> None:
    receipt_id = await _receipt(
        wired,
        resolved_at=_NOW - datetime.timedelta(hours=1),
        items=("consumed-item", "unresolved-item", "other-locus"),
    )
    consumed_id = uuid.UUID(int=3)
    second_consumed_id = uuid.UUID(int=4)
    unresolved_id = uuid.UUID(int=2)
    receipt_level_id = uuid.UUID(int=1)
    diagnostic_id = uuid.UUID(int=5)
    await _feedback(
        wired,
        feedback_id=consumed_id,
        receipt_id=receipt_id,
        receipt_item_id="consumed-item",
        created_at=_NOW - datetime.timedelta(minutes=1),
    )
    await _feedback(
        wired,
        feedback_id=second_consumed_id,
        receipt_id=receipt_id,
        receipt_item_id="consumed-item",
        created_at=_NOW - datetime.timedelta(minutes=1),
    )
    await _feedback(
        wired,
        feedback_id=unresolved_id,
        receipt_id=receipt_id,
        receipt_item_id="unresolved-item",
        created_at=_NOW - datetime.timedelta(minutes=2),
    )
    await _feedback(
        wired,
        feedback_id=receipt_level_id,
        receipt_id=receipt_id,
        receipt_item_id=None,
        created_at=_NOW - datetime.timedelta(minutes=3),
        learning_eligible=False,
    )
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_feedback ("
                " feedback_id, tenant_id, kind, receipt_id, receipt_item_id, rating, learning_eligible, note,"
                " reporter_id, reporter_type, idempotency_key, content_digest, created_at"
                ") VALUES ("
                " :feedback, :tenant, 'diagnostic_observation', NULL, NULL, 'incorrect', FALSE, NULL,"
                " 'diagnostic-reporter', 'agent', :key, :digest, :created"
                ")"
            ),
            {
                "feedback": diagnostic_id,
                "tenant": wired["tenant_id"],
                "key": f"diagnostic-{diagnostic_id}",
                "digest": diagnostic_id.hex,
                "created": _NOW,
            },
        )

    produced_claim = await _claim(wired, consolidated_at=_NOW - datetime.timedelta(minutes=5))
    await _derivation_locus(
        wired,
        tenant_id=wired["tenant_id"],
        receipt_id=receipt_id,
        receipt_item_id="consumed-item",
        created_claim_id=produced_claim,
    )
    # Same receipt and tenant, wrong item: exact-locus matching must not let
    # this claim consume feedback about `unresolved-item`.
    await _derivation_locus(
        wired,
        tenant_id=wired["tenant_id"],
        receipt_id=receipt_id,
        receipt_item_id="other-locus",
        created_claim_id=produced_claim,
    )
    # Exact item but a foreign derivation: evidence links carry no tenant id,
    # so this is the row that proves the join gets tenancy from the derivation.
    foreign_tenant, foreign_actor = await _identity(wired)
    foreign_claim = await _claim(
        wired,
        consolidated_at=_NOW - datetime.timedelta(minutes=4),
        tenant_id=foreign_tenant,
        actor_id=foreign_actor,
    )
    await _derivation_locus(
        wired,
        tenant_id=foreign_tenant,
        receipt_id=receipt_id,
        receipt_item_id="unresolved-item",
        created_claim_id=foreign_claim,
    )
    # Exact receipt-level evidence that has not produced a claim is unresolved.
    await _derivation_locus(
        wired,
        tenant_id=wired["tenant_id"],
        receipt_id=receipt_id,
        receipt_item_id=None,
        created_claim_id=None,
    )

    service = FeedbackReadService(wired["factory"])
    page = await service.resume_page(wired["ctx"], receipt_id=receipt_id, bound=10)

    assert [item.feedback_id for item in page.items] == [
        unresolved_id,
        receipt_level_id,
        consumed_id,
        second_consumed_id,
    ]
    assert [item.consumed for item in page.items] == [False, False, True, True]
    assert page.items[1].learning_eligible is False
    assert diagnostic_id not in {item.feedback_id for item in page.items}
    assert not page.truncated
    assert not hasattr(page.items[0], "note")
    assert not hasattr(page.items[0], "reporter_id")

    bounded = await service.resume_page(wired["ctx"], receipt_id=receipt_id, bound=2)
    assert [item.feedback_id for item in bounded.items] == [unresolved_id, receipt_level_id]
    assert bounded.truncated


# --- Reviewed learning newer than the last resolution ------------------------


@pytest.mark.asyncio
async def test_resume_returns_only_reviewed_learning_newer_than_the_last_receipt(wired: _Wired) -> None:
    await _checkpoint(wired, sequence=1, goal="resume with learning")
    cutoff = _NOW - datetime.timedelta(hours=1)
    receipt_id = await _receipt(wired, resolved_at=cutoff)

    newest = await _claim(
        wired,
        claim_id=uuid.UUID(int=11),
        consolidated_at=_NOW - datetime.timedelta(minutes=10),
    )
    newer = await _claim(
        wired,
        claim_id=uuid.UUID(int=12),
        consolidated_at=_NOW - datetime.timedelta(minutes=30),
    )
    old = await _claim(wired, consolidated_at=cutoff - datetime.timedelta(seconds=1))
    boundary = await _claim(wired, consolidated_at=cutoff)
    unreviewed = await _claim(wired, consolidated_at=None)
    future = await _claim(wired, consolidated_at=_NOW + datetime.timedelta(minutes=1))
    foreign_tenant, foreign_actor = await _identity(wired)
    foreign = await _claim(
        wired,
        consolidated_at=_NOW - datetime.timedelta(minutes=20),
        tenant_id=foreign_tenant,
        actor_id=foreign_actor,
    )

    bounded = await wired["service"].resume(wired["ctx"], _request(learning_bound=1))
    assert bounded.receipts[0].receipt_id == receipt_id
    assert [claim.claim_id for claim in bounded.learning] == [newest]
    assert "learning" in bounded.truncated

    full = await wired["service"].resume(wired["ctx"], _request(learning_bound=10))
    assert [claim.claim_id for claim in full.learning] == [newest, newer]
    assert {old, boundary, unreviewed, future, foreign}.isdisjoint({claim.claim_id for claim in full.learning})
    assert all(claim.citations for claim in full.learning)
    assert all(claim.label == RECALL_LABEL and claim.trust == RECALL_TRUST for claim in full.learning)


# --- Never a transcript --------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_returns_conclusions_and_never_an_exchange(wired: _Wired) -> None:
    """The checkpoint chain exists so an agent records what it concluded rather
    than everything it said. Handing back the raw exchange would make the
    summary decorative."""
    await _checkpoint(wired, sequence=1, goal="decided to use the kit", next_action="wire it up")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert state.head_summary == "decided to use the kit"
    assert state.next_action == "wire it up"
    assert not hasattr(state, "transcript")
    assert not hasattr(state, "messages")


@pytest.mark.asyncio
async def test_the_learning_window_is_ordered_by_review_time_not_assertion_time(wired: _Wired) -> None:
    """The property that makes this its own read rather than another filter.

    Two claims, both reviewed after the last receipt, with their assertion order
    reversed against their review order. A caller asking "what became reviewable
    since I last looked" wants the most recently *reviewed* one; ordering the
    same window by assertion time and then applying a bound returns the other,
    which is a different answer wearing the same shape.

    Asserted with a bound of one, because that is the only way the ordering can
    be observed at all -- with room for both, either ordering returns both and
    the distinction is invisible.
    """
    await _checkpoint(wired, sequence=1, goal="resume ordered by review")
    cutoff = _NOW - datetime.timedelta(hours=1)
    await _receipt(wired, resolved_at=cutoff)

    reviewed_last = await _claim(
        wired,
        claim_id=uuid.UUID(int=21),
        consolidated_at=_NOW - datetime.timedelta(minutes=1),
        asserted_valid_from=_NOW - datetime.timedelta(hours=5),
    )
    asserted_last = await _claim(
        wired,
        claim_id=uuid.UUID(int=22),
        consolidated_at=_NOW - datetime.timedelta(minutes=30),
        asserted_valid_from=_NOW - datetime.timedelta(minutes=2),
    )

    bounded = await wired["service"].resume(wired["ctx"], _request(learning_bound=1))

    assert [claim.claim_id for claim in bounded.learning] == [
        reviewed_last
    ], "the bound must keep the most recently reviewed claim, not the most recently asserted one"
    assert "learning" in bounded.truncated

    both = await wired["service"].resume(wired["ctx"], _request(learning_bound=10))
    assert [claim.claim_id for claim in both.learning] == [reviewed_last, asserted_last]
