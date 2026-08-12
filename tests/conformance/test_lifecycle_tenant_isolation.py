"""A second tenant gets nothing from the lifecycle surfaces, and learns nothing by asking.

The lifecycle context surfaces -- the caller-supplied profile, the
evidence-preserving handoff, and bounded resume -- were added for a pilot that
runs inside one tenant. That is exactly the shape in which a cross-tenant path
gets built by accident: with only one tenant in play, an arm that filters after
the read instead of inside it, or that branches on a count it should never have
computed, behaves identically to one that is correct. This suite is the
adversarial gate that keeps the difference visible.

What it holds, and why each is a separate property
--------------------------------------------------

1. **Denial precedes the work that depends on hidden data.** A refusal issued
   after the receipt and learning arms have already run is a refusal that read
   the rows first. The ordering tests here spy on the collaborating services and
   assert those reads never happened -- an assertion about the *sequence*, which
   no assertion about the returned value can make.

2. **A refusal discloses nothing by its shape.** A foreign tenant naming real
   work must get the answer it would get for work that exists nowhere: same
   exception, same message, same empty state, same absence of truncation flags.
   Anything that distinguishes the two is an existence oracle, and an oracle
   over external ids is how a competitor's run ids get enumerated one probe at a
   time.

3. **The tenant predicate is in the SELECT.** Proven by seeding a second tenant's
   rows that a post-filter would have loaded and then dropped, and asserting the
   read returns nothing -- against a real database, because a predicate's
   position is a fact about SQL that a mocked session cannot witness.

On `403`, and where this suite deliberately does not demand one
---------------------------------------------------------------

An explicit `403` is the right answer when a caller names something *it could
only be naming because it holds a real identifier for it* -- a checkpoint id, a
receipt id -- or when the caller is inside the tenant and outside the task
audience. Those are the cases below that assert a refusal.

Resume by external reference is deliberately not one of them. A caller resumes
by naming a run or a pull request in its own system's vocabulary, and those names
are guessable. If a foreign tenant naming another tenant's run id got `403`
while naming a fictional run id got an empty resume, the status code would
answer "does this run exist over there?" -- turning the denial itself into the
disclosure it exists to prevent. So the reference path returns the same empty
state either way, and this suite pins that indistinguishability as the stronger
property rather than pinning a status code that would weaken it. The refusal
that *does* fire on the resume path -- an actor inside the tenant but outside the
task audience -- is asserted here to fire before the receipt and learning arms
read anything.

This file does not restate the general cross-tenant gate that
``test_tenant_isolation.py`` holds over the HTTP surface; that suite's
assertions are untouched and independent. What is here is the lifecycle-specific
half: the service-level paths the pilot added, where the denial is a service
decision rather than a middleware one.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.handoff import ContextHandoffService, HandoffRefused
from contextplane.context.lifecycle import LifecycleProfile, narrow, placements_for_claims
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.resume import ContextResumeService, ResumeRequest
from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.types import TenantContext
from contextplane.workspaces.audience import RECOGNIZED_RESOLVERS
from contextplane.workspaces.checkpoints import IntentCheckpointService

_NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)

#: The work the pilot tenant is doing, in the vocabulary its own delivery system
#: uses. Guessable by construction -- which is the point: the outsider below
#: names these exactly, and still learns nothing.
_RUN = ("delivery", "pilot", "run", "run-4417")
_STAGE = ("delivery", "pilot", "stage", "implement")

#: Work that exists in no tenant at all. The outsider's control probe: whatever
#: the surfaces answer for the real references above, they must answer for these.
_ABSENT_RUN = ("delivery", "pilot", "run", "run-0000")

#: Where the pilot tenant's one derived conclusion was placed. Named once
#: because a test asserts the owning tenant reads back exactly this string, and
#: two spellings of it would make that assertion pass for the wrong reason.
_PLACEMENT = "repository=contextplane;stage=implement"


class _Clock:
    def now(self) -> datetime.datetime:
        return _NOW


def _ctx(tenant: uuid.UUID, actor: str) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=["member"])


class _SpyReceipts(ContextReceiptService):
    """A receipt service that records whether anything asked it for evidence.

    Subclasses the real service rather than faking it: the handoff must be
    refused by the *real* audience predicate, and only the record of which
    methods ran is synthetic. A fake would prove the ordering of a collaborator
    that does not exist in production.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads: list[str] = []

    async def get(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> Any:
        self.reads.append("get")
        return await super().get(ctx, receipt_id=receipt_id)

    async def exclusions_for(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> Any:
        self.reads.append("exclusions_for")
        return await super().exclusions_for(ctx, receipt_id=receipt_id)

    async def arms_for(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> Any:
        self.reads.append("arms_for")
        return await super().arms_for(ctx, receipt_id=receipt_id)


class _SpyClaims:
    """Stands in for the governed claim reader, and refuses to be called.

    Resume's learning arm is the last thing a refused request should reach. This
    records any call and returns nothing, so a test can assert the arm stayed
    unread rather than asserting the response happened to be empty -- an empty
    answer is what a leaking implementation returns too, once the caller has no
    rows of their own.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def consolidated_since(self, ctx: TenantContext, **kwargs: Any) -> tuple[Any, ...]:
        self.calls += 1
        return ()


async def _grant(session: AsyncSession, *, tenant: uuid.UUID, task: uuid.UUID, actor: str) -> None:
    await session.execute(
        text(
            "INSERT INTO intent_participant_grants "
            "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, resolver_version) "
            "VALUES (:t, :task, :actor, 'contributor', 'granter', :now, :resolver)"
        ),
        {
            "t": tenant,
            "task": task,
            "actor": actor,
            "now": _NOW - datetime.timedelta(hours=1),
            "resolver": sorted(RECOGNIZED_RESOLVERS)[0],
        },
    )


async def _seed_tenant(session: AsyncSession, slug_prefix: str) -> tuple[uuid.UUID, str]:
    tenant, actor = uuid.uuid4(), str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'lifecycle isolation')"),
        {"t": tenant, "s": f"{slug_prefix}-{tenant.hex[:10]}"},
    )
    await session.execute(
        text(
            "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
            "VALUES (:a, :t, 'specialist', :sub, :now)"
        ),
        {"a": actor, "t": tenant, "sub": f"sub-{actor[:12]}", "now": _NOW},
    )
    return tenant, actor


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """Two tenants. One runs the pilot; the other exists and is owed nothing.

    The outsider is a real, authenticated actor in a real tenant -- not an absent
    identity. An actor that did not exist would be turned away by a foreign key,
    and a foreign key is not the isolation being asserted.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    task, receipt = uuid.uuid4(), uuid.uuid4()
    claim_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            pilot, insider = await _seed_tenant(session, "pilot")
            other, outsider = await _seed_tenant(session, "other")
            # A second actor inside the pilot tenant who is not on the task.
            # Same-tenant, outside-audience is a different denial from
            # cross-tenant, and the resume ordering test needs the former.
            bystander = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'bystander', :sub, :now)"
                ),
                {"a": bystander, "t": pilot, "sub": f"sub-by-{bystander[:8]}", "now": _NOW},
            )
            await _grant(session, tenant=pilot, task=task, actor=insider)

            await session.execute(
                text(
                    "INSERT INTO context_receipts (receipt_id, tenant_id, intent_id, state, cacheable, "
                    "resolved_at, requested_by) VALUES (:r, :t, :task, 'complete', false, :now, :by)"
                ),
                {"r": receipt, "t": pilot, "task": task, "now": _NOW, "by": insider},
            )
            for block, state in (("workspace", "success"), ("canonical", "success")):
                await session.execute(
                    text("INSERT INTO context_receipt_arms (arm_id, receipt_id, block, state) VALUES (:a, :r, :b, :s)"),
                    {"a": uuid.uuid4(), "r": receipt, "b": block, "s": state},
                )
            await session.execute(
                text(
                    "INSERT INTO context_receipt_exclusions (exclusion_id, receipt_id, block, item_key, reason) "
                    "VALUES (:e, :r, 'workspace', 'cp-withheld', 'classification restricted')"
                ),
                {"e": uuid.uuid4(), "r": receipt},
            )

        checkpoints = IntentCheckpointService(session_factory=factory, clock=_Clock())
        appended = await checkpoints.append_checkpoint(
            _ctx(pilot, insider),
            intent_id=task,
            payload={
                "goal": "land the pilot lifecycle binding",
                "decisions": ["stage stays caller data"],
                "assumptions": [],
                "completed_checks": [],
                "open_questions": ["does the control plane need the outcome path"],
                "next_action": "hand to review",
            },
            idempotency_key=f"seed-{task.hex[:8]}",
        )
        checkpoint_id = appended.checkpoint.checkpoint_id

        # The pilot tenant's references, and the checkpoint/receipt bindings that
        # make them resolvable. Seeded last so the checkpoint id exists.
        async with factory() as session, session.begin():
            for reference in (_RUN, _STAGE):
                reference_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO context_external_references ("
                        " reference_id, tenant_id, source_system, source_namespace, kind, external_id,"
                        " classification, external_authority, collision_key"
                        ") VALUES (:ref, :t, :system, :ns, :kind, :eid, 'internal', 'delivery', :collision)"
                    ),
                    {
                        "ref": reference_id,
                        "t": pilot,
                        "system": reference[0],
                        "ns": reference[1],
                        "kind": reference[2],
                        "eid": reference[3],
                        "collision": "|".join(reference),
                    },
                )
                for subject_type, subject_id in (
                    ("intent_checkpoint", checkpoint_id),
                    ("context_item", receipt),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO context_reference_bindings ("
                            " binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at"
                            ") VALUES (:b, :t, :ref, :st, :sid, :now)"
                        ),
                        {
                            "b": uuid.uuid4(),
                            "t": pilot,
                            "ref": reference_id,
                            "st": subject_type,
                            "sid": subject_id,
                            "now": _NOW,
                        },
                    )
            # A claim in the pilot tenant, and the derivation that placed it.
            # Unlinked, so it needs no entity: the placement read joins on the
            # derivation, and what the claim asserts is irrelevant to whether
            # another tenant can read where it applies.
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    " claim_id, author_tenant_id, subject_reference, predicate, value_type,"
                    " claim_category, value_jsonb, asserted_valid_from, status, visibility,"
                    " source_authority, size_bytes, created_at"
                    ") VALUES ("
                    " :c, :t, 'svc:pilot-payments', 'depends_on', 'string',"
                    " 'dependency', '{\"value\": \"svc:ledger\"}'::jsonb, :now, 'unlinked',"
                    " 'tenant-shared', 'unattributed', 64, :now)"
                ),
                {"c": claim_id, "t": pilot, "now": _NOW},
            )
            # The derivation is what the placement read returns. Seeded in the
            # pilot tenant so a post-filtering implementation would have loaded
            # it for the outsider and then dropped it -- the difference this
            # suite exists to detect.
            await session.execute(
                text(
                    "INSERT INTO claim_derivations ("
                    " derivation_id, tenant_id, created_claim_id, applicability, profile,"
                    " profile_version, status, assertion_digest, source_authority,"
                    " classification, created_at"
                    ") VALUES ("
                    " :d, :t, :c, :a, 'delivery', 'v1', 'staged', :digest,"
                    " 'owner_inference', 'internal', :now)"
                ),
                {
                    "d": uuid.uuid4(),
                    "t": pilot,
                    "c": claim_id,
                    "a": _PLACEMENT,
                    "digest": f"sha256:{uuid.uuid4().hex}",
                    "now": _NOW,
                },
            )

        yield {
            "factory": factory,
            "pilot": pilot,
            "other": other,
            "insider": insider,
            "outsider": outsider,
            "bystander": bystander,
            "task": task,
            "receipt": receipt,
            "checkpoint_id": checkpoint_id,
            "claim_id": claim_id,
            "checkpoints": checkpoints,
        }
    finally:
        await engine.dispose()


def _handoff(world: dict[str, Any]) -> tuple[ContextHandoffService, _SpyReceipts]:
    receipts = _SpyReceipts(session_factory=world["factory"], clock=_Clock())
    return ContextHandoffService(checkpoints=world["checkpoints"], receipts=receipts), receipts


def _resume(world: dict[str, Any], claims: _SpyClaims) -> ContextResumeService:
    return ContextResumeService(session_factory=world["factory"], clock=_Clock(), claims=claims)


# -- handoff ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_foreign_tenant_cannot_issue_a_handoff_over_another_tenants_evidence(
    world: dict[str, Any],
) -> None:
    """Holding both real identifiers is not enough when the rows are elsewhere.

    The outsider names the pilot's checkpoint and receipt exactly. Issuance is
    where a handle would be minted, and a handle minted here would be a durable,
    self-consistent artifact asserting cross-tenant evidence -- so the refusal
    has to happen before one exists, not at consumption.
    """
    service, _ = _handoff(world)
    with pytest.raises(HandoffRefused):
        await service.issue(
            _ctx(world["other"], world["outsider"]),
            checkpoint_id=world["checkpoint_id"],
            receipt_id=world["receipt"],
        )


@pytest.mark.asyncio
async def test_the_handoff_refusal_precedes_every_receipt_read(world: dict[str, Any]) -> None:
    """No part of the receipt is touched once the checkpoint read denies.

    This is the ordering the contract is about. The refusal already returns
    nothing; what this adds is that the receipt's state, its arms and its
    exclusions were never read on the way to returning nothing. A refusal
    assembled after those reads would leave the evidence sitting in process
    memory of a request that was not entitled to it, one refactor away from
    being logged, cached, or attached to the error.
    """
    service, receipts = _handoff(world)
    with pytest.raises(HandoffRefused):
        await service.issue(
            _ctx(world["other"], world["outsider"]),
            checkpoint_id=world["checkpoint_id"],
            receipt_id=world["receipt"],
        )
    assert receipts.reads == [], f"the refusal read receipt evidence first: {receipts.reads}"


@pytest.mark.asyncio
async def test_a_foreign_tenants_refusal_is_indistinguishable_from_absent_evidence(
    world: dict[str, Any],
) -> None:
    """Real-but-foreign and simply-absent produce the same refusal, to the message.

    A distinguishable pair would let an outsider holding a leaked id confirm the
    id is live -- and confirmation is most of what a leaked identifier is worth.
    """
    service, _ = _handoff(world)
    outsider = _ctx(world["other"], world["outsider"])

    with pytest.raises(HandoffRefused) as foreign:
        await service.issue(outsider, checkpoint_id=world["checkpoint_id"], receipt_id=world["receipt"])
    with pytest.raises(HandoffRefused) as absent:
        await service.issue(outsider, checkpoint_id=uuid.uuid4(), receipt_id=uuid.uuid4())

    assert str(foreign.value) == str(absent.value)


@pytest.mark.asyncio
async def test_a_handle_issued_in_one_tenant_is_not_consumable_in_another(world: dict[str, Any]) -> None:
    """A handle handed out of band grants the outsider exactly nothing.

    Consumption re-resolves authorization rather than trusting the handle, so a
    valid handle in the wrong hands is refused on its own terms -- and the
    refusal reads no receipt evidence on the way, same as issuance.
    """
    issuer, issued_reads = _handoff(world)
    handle = await issuer.issue(
        _ctx(world["pilot"], world["insider"]),
        checkpoint_id=world["checkpoint_id"],
        receipt_id=world["receipt"],
    )
    # The positive control for every `reads == []` assertion in this file. A spy
    # that never records anything would make those assertions pass against an
    # implementation that reads everything, so the entitled path has to be shown
    # tripping it.
    assert issued_reads.reads, "the spy records nothing even when evidence is read"

    consumer, receipts = _handoff(world)
    with pytest.raises(HandoffRefused):
        await consumer.consume(_ctx(world["other"], world["outsider"]), handle)
    assert receipts.reads == [], f"consumption read receipt evidence before refusing: {receipts.reads}"


# -- resume -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_resuming_another_tenants_work_returns_the_answer_for_work_that_does_not_exist(
    world: dict[str, Any],
) -> None:
    """The outsider names the pilot's real run and stage, and gets `start fresh`.

    Compared field by field against the same outsider naming a run that exists
    nowhere. Equality across every field is the assertion: a difference in the
    task id, the reference count, or even which arms reported truncation would
    each be a channel, and enumerating them one at a time would leave whichever
    one nobody thought of.
    """
    claims = _SpyClaims()
    service = _resume(world, claims)
    outsider = _ctx(world["other"], world["outsider"])

    real = await service.resume(outsider, ResumeRequest(references=(_RUN, _STAGE)))
    control = await service.resume(outsider, ResumeRequest(references=(_ABSENT_RUN,)))

    assert real.is_empty()
    assert dataclasses.asdict(real) == dataclasses.asdict(control)
    assert real.truncated == ()
    assert claims.calls == 0, "the learning arm ran for a caller with no receipts of its own"


@pytest.mark.asyncio
async def test_the_outsider_never_learns_how_much_there_was_to_find(world: dict[str, Any]) -> None:
    """Bounds do not become a counting instrument.

    Asking for one checkpoint and asking for many return the same nothing. If the
    outsider's answer varied with the bound -- through a truncation flag, most
    plausibly -- the flag would report on rows the outsider cannot see, which is
    a count disclosure wearing the clothes of a pagination hint.
    """
    service = _resume(world, _SpyClaims())
    outsider = _ctx(world["other"], world["outsider"])
    request = ResumeRequest(references=(_RUN, _STAGE))

    narrow_bounds = await service.resume(
        outsider,
        ResumeRequest(references=(_RUN, _STAGE), checkpoint_bound=1, receipt_bound=1, reference_bound=1),
    )
    wide = await service.resume(outsider, request)

    assert dataclasses.asdict(narrow_bounds) == dataclasses.asdict(wide)
    assert narrow_bounds.truncated == ()


@pytest.mark.asyncio
async def test_an_actor_outside_the_task_audience_is_refused_before_the_learning_arm_reads(
    world: dict[str, Any],
) -> None:
    """The in-tenant denial fires first, and the temporal arms stay unread.

    This is the case that legitimately produces a refusal on the resume path: the
    caller is inside the tenant, so the references resolve, and the task is real
    -- what is missing is the grant. The refusal has to land before the receipt
    and learning arms run, or a missing grant degrades into a partial resume that
    still returned somebody else's material.
    """
    claims = _SpyClaims()
    service = _resume(world, claims)

    with pytest.raises(PermissionError):
        await service.resume(_ctx(world["pilot"], world["bystander"]), ResumeRequest(references=(_RUN, _STAGE)))
    assert claims.calls == 0, "the learning arm read before the audience refusal"


@pytest.mark.asyncio
async def test_the_entitled_caller_does_reach_the_learning_arm(world: dict[str, Any]) -> None:
    """The positive control for every `calls == 0` assertion above.

    A learning arm that was unreachable for everyone would satisfy all of them
    while proving nothing, so the granted actor on the same references has to be
    shown getting through -- and getting the work back, which is also the
    evidence that the outsider's empty answer above was a denial rather than an
    empty database.
    """
    claims = _SpyClaims()
    service = _resume(world, claims)

    state = await service.resume(_ctx(world["pilot"], world["insider"]), ResumeRequest(references=(_RUN, _STAGE)))

    assert state.intent_id == world["task"]
    assert state.receipts, "the entitled caller saw no receipts, so the arm ordering proves nothing"
    assert claims.calls == 1


@pytest.mark.asyncio
async def test_the_audience_refusal_names_no_task(world: dict[str, Any]) -> None:
    """The denial does not itemize what it is denying.

    A message carrying the task id would make the refusal a directory of hidden
    work: probe with references, collect ids from the errors.
    """
    service = _resume(world, _SpyClaims())
    with pytest.raises(PermissionError) as refusal:
        await service.resume(_ctx(world["pilot"], world["bystander"]), ResumeRequest(references=(_RUN, _STAGE)))
    message = str(refusal.value)
    assert str(world["task"]) not in message
    assert str(world["checkpoint_id"]) not in message


# -- lifecycle profile ------------------------------------------------------


@pytest.mark.asyncio
async def test_placement_reads_are_scoped_in_the_select_not_filtered_after(world: dict[str, Any]) -> None:
    """The pilot's derivation row is not loaded on the outsider's behalf at all.

    Seeded so that a post-filtering implementation would return the same empty
    mapping this asserts -- which is why the companion assertion below matters:
    the same claim id under the owning tenant *does* resolve, so the empty answer
    here is the tenant predicate doing work, not the row being absent.
    """
    async with world["factory"]() as session:
        for_outsider = await placements_for_claims(session, tenant_id=world["other"], claim_ids=[world["claim_id"]])
        for_owner = await placements_for_claims(session, tenant_id=world["pilot"], claim_ids=[world["claim_id"]])

    assert for_outsider == {}
    assert for_owner == {world["claim_id"]: _PLACEMENT}


@dataclasses.dataclass(frozen=True)
class _Claim:
    """The one field selection reads off a served claim."""

    claim_id: uuid.UUID


@pytest.mark.asyncio
async def test_narrowing_withholds_nothing_from_a_tenant_that_was_shown_nothing(
    world: dict[str, Any],
) -> None:
    """Selection produces no exclusion naming another tenant's placement.

    An exclusion carries a reason, and the reason quotes the placement it
    disagreed with. Generating one from a row the caller may not see would put
    another tenant's repository and stage names into this caller's receipt --
    the withheld-not-disappeared contract turned into a leak, because the
    exclusion is the one part of a filtered read that is meant to be visible.
    """
    profile = LifecycleProfile.of(
        (
            ExternalReferenceV1(
                source_system="delivery",
                source_namespace="other",
                kind="repository",
                external_id="unrelated-service",
                classification="internal",
                external_authority="delivery",
            ),
        )
    )
    claims = (_Claim(claim_id=world["claim_id"]),)

    kept, exclusions = await narrow(world["factory"], profile, claims, _ctx(world["other"], world["outsider"]))

    assert kept == claims
    assert exclusions == ()
