"""A handoff handle carries pointers, and refuses when what they point at moves.

The property under test is not "a handle round-trips". A handle that round-trips
is what a transcript copy does too -- it agrees with itself, which is exactly why
copying loses the evidence. What is asserted here is the difference: change any
one of the things the handle names, and consuming it fails.

So every test below moves one thing and expects a refusal, plus the two controls
that keep those from being vacuous: an unmoved handoff succeeds, and a handle
built by hand from correct-looking parts is refused because it was never derived
from the rows.

No database. The service takes its two reads as injected collaborators, so every
refusal path is reachable from a fixture -- including the ones a real database
makes hard to arrange, like a receipt belonging to another task.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from contextplane.context.handoff import (
    HANDOFF_BINDING_VERSION,
    ContextHandoffService,
    HandoffRefused,
)
from contextplane.exceptions import NotFoundError
from contextplane.types import TenantContext

_TENANT = uuid.uuid4()
_TASK = uuid.uuid4()
_CHECKPOINT = uuid.uuid4()
_RECEIPT = uuid.uuid4()
_AUTHOR = "actor:coding"
_SPECIALIST = "actor:security"


def _ctx(actor: str = _AUTHOR) -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=actor, roles=["member"])


@dataclasses.dataclass(frozen=True)
class _Ref:
    key: str

    def collision_key(self) -> str:
        return self.key


@dataclasses.dataclass(frozen=True)
class _Checkpoint:
    checkpoint_id: uuid.UUID = _CHECKPOINT
    task_id: uuid.UUID = _TASK
    digest: str = "checkpoint-digest-1"
    evidence: tuple[_Ref, ...] = (_Ref("github:pr:41"), _Ref("jira:issue:7"))


@dataclasses.dataclass
class _Receipt:
    receipt_id: uuid.UUID = _RECEIPT
    task_id: uuid.UUID | None = _TASK
    state: str = "complete"
    resolved_at: str = "2026-08-11T00:00:00+00:00"
    requested_by: str = _AUTHOR
    request_digest: str | None = "request-digest-1"


@dataclasses.dataclass
class _Arm:
    block: str


@dataclasses.dataclass
class _Exclusion:
    block: str
    item_key: str


class _Checkpoints:
    """The one read the handoff makes against task memory.

    `readable_by` is what stands in for the audience predicate the real read
    resolves in SQL: an actor outside the set gets the same `NotFoundError` a
    non-participant gets from the real query, because the real query does not
    return a row to refuse.
    """

    def __init__(self, checkpoint: _Checkpoint | None = None, *, readable_by: set[str] | None = None) -> None:
        self.checkpoint = checkpoint or _Checkpoint()
        self.readable_by = readable_by if readable_by is not None else {_AUTHOR, _SPECIALIST}

    async def get_checkpoint(self, ctx: TenantContext, *, checkpoint_id: uuid.UUID) -> Any:
        if str(ctx.actor_id) not in self.readable_by:
            raise NotFoundError(f"checkpoint {checkpoint_id} not found")
        return self.checkpoint


class _Receipts:
    def __init__(
        self,
        receipt: _Receipt | None = None,
        *,
        arms: tuple[_Arm, ...] = (_Arm("workspace"), _Arm("canonical")),
        exclusions: tuple[_Exclusion, ...] = (_Exclusion("workspace", "cp-9"),),
        readable_by: set[str] | None = None,
    ) -> None:
        self.receipt = receipt if receipt is not None else _Receipt()
        self.arms = arms
        self.exclusions = exclusions
        self.readable_by = readable_by if readable_by is not None else {_AUTHOR, _SPECIALIST}

    async def get(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> Any:
        if str(ctx.actor_id) not in self.readable_by:
            return None
        return self.receipt

    async def exclusions_for(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> tuple[_Exclusion, ...]:
        return self.exclusions

    async def arms_for(self, ctx: TenantContext, *, receipt_id: uuid.UUID) -> tuple[_Arm, ...]:
        return self.arms


def _service(checkpoints: _Checkpoints | None = None, receipts: _Receipts | None = None) -> ContextHandoffService:
    return ContextHandoffService(
        checkpoints=checkpoints or _Checkpoints(),  # type: ignore[arg-type]
        receipts=receipts or _Receipts(),  # type: ignore[arg-type]
    )


async def _issued(service: ContextHandoffService | None = None) -> Any:
    return await (service or _service()).issue(_ctx(), checkpoint_id=_CHECKPOINT, receipt_id=_RECEIPT)


# --- what a handle carries ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_handle_carries_identities_and_digests_and_no_content() -> None:
    """The whole design in one assertion: nothing in a handle is content.

    Written as a field-by-field type check rather than a spot check on one
    attribute, because the failure this guards against is somebody adding
    `goal` or the receipt items later "so the consumer does not have to read
    them" -- which is the transcript copy the module exists to avoid.
    """
    handle = await _issued()
    for field in dataclasses.fields(handle):
        value = getattr(handle, field.name)
        assert isinstance(value, str | uuid.UUID | tuple), field.name
        if isinstance(value, tuple):
            assert all(isinstance(entry, str) for entry in value), field.name


@pytest.mark.asyncio
async def test_the_handle_binds_the_blocks_and_what_was_withheld() -> None:
    """A consumer that cannot see the exclusions reads the evidence as complete."""
    handle = await _issued()
    assert handle.source_blocks == ("canonical", "workspace")
    assert handle.exclusions == ("workspace/cp-9",)
    assert handle.external_refs == ("github:pr:41", "jira:issue:7")


@pytest.mark.asyncio
async def test_the_task_comes_from_the_checkpoint_rather_than_the_caller() -> None:
    handle = await _issued()
    assert handle.task_id == _TASK
    assert handle.binding_version == HANDOFF_BINDING_VERSION


# --- the control: an unmoved handoff succeeds ---------------------------------


@pytest.mark.asyncio
async def test_a_second_actor_consumes_an_unmoved_handoff() -> None:
    """The control every refusal below is measured against.

    Without it, a service that refused unconditionally would pass all of them.
    """
    service = _service()
    handle = await _issued(service)

    consumed = await service.consume(_ctx(_SPECIALIST), handle)

    assert consumed == handle
    # Who issued it is a fact about the past; consuming does not rewrite it.
    assert consumed.issued_by == _AUTHOR


# --- one thing moves, and the handoff refuses ---------------------------------


@pytest.mark.asyncio
async def test_a_moved_checkpoint_revision_refuses() -> None:
    """The case the digest exists for: same id, different content."""
    checkpoints = _Checkpoints()
    service = _service(checkpoints=checkpoints)
    handle = await _issued(service)

    checkpoints.checkpoint = _Checkpoint(digest="checkpoint-digest-2")

    with pytest.raises(HandoffRefused, match="not the evidence it was issued against"):
        await service.consume(_ctx(_SPECIALIST), handle)


@pytest.mark.asyncio
async def test_a_changed_exclusion_set_refuses() -> None:
    """Withholding more, or less, changes what the evidence means."""
    receipts = _Receipts()
    service = _service(receipts=receipts)
    handle = await _issued(service)

    receipts.exclusions = ()

    with pytest.raises(HandoffRefused):
        await service.consume(_ctx(_SPECIALIST), handle)


@pytest.mark.asyncio
async def test_a_changed_block_set_refuses() -> None:
    receipts = _Receipts()
    service = _service(receipts=receipts)
    handle = await _issued(service)

    receipts.arms = (_Arm("workspace"),)

    with pytest.raises(HandoffRefused):
        await service.consume(_ctx(_SPECIALIST), handle)


@pytest.mark.asyncio
async def test_a_rewritten_receipt_refuses() -> None:
    receipts = _Receipts()
    service = _service(receipts=receipts)
    handle = await _issued(service)

    receipts.receipt = _Receipt(request_digest="request-digest-2")

    with pytest.raises(HandoffRefused):
        await service.consume(_ctx(_SPECIALIST), handle)


# --- authorization is re-resolved, not inherited ------------------------------


@pytest.mark.asyncio
async def test_an_outsider_holding_a_valid_handle_is_denied() -> None:
    """Holding a handle grants nothing.

    The handle is genuinely valid and genuinely matches the rows. The only thing
    wrong is who is presenting it, which is the whole point: a handle that
    authorized its bearer would be a capability outliving the grant it was
    minted under.
    """
    checkpoints = _Checkpoints(readable_by={_AUTHOR, _SPECIALIST})
    service = _service(checkpoints=checkpoints)
    handle = await _issued(service)

    checkpoints.readable_by = {_AUTHOR, _SPECIALIST}
    outsider = _ctx("actor:outsider")

    with pytest.raises(HandoffRefused, match="not readable by this actor"):
        await service.consume(outsider, handle)


@pytest.mark.asyncio
async def test_a_specialist_whose_grant_was_revoked_is_denied() -> None:
    """Revocation between issuance and consumption has to bite."""
    checkpoints = _Checkpoints()
    service = _service(checkpoints=checkpoints)
    handle = await _issued(service)

    checkpoints.readable_by = {_AUTHOR}

    with pytest.raises(HandoffRefused):
        await service.consume(_ctx(_SPECIALIST), handle)


@pytest.mark.asyncio
async def test_an_unreadable_receipt_refuses_without_saying_which_half_failed() -> None:
    """One refusal for every cause: the differences are what a prober enumerates."""
    receipts = _Receipts(readable_by={_AUTHOR})
    service = _service(receipts=receipts)
    handle = await _issued(service)

    with pytest.raises(HandoffRefused, match="not readable by this actor"):
        await service.consume(_ctx(_SPECIALIST), handle)


# --- the halves must belong together ------------------------------------------


@pytest.mark.asyncio
async def test_a_receipt_from_another_task_refuses() -> None:
    """Both halves check out individually, and together they are a fabrication.

    This is the case injection makes reachable and a database makes awkward: a
    specialist presenting evidence they really did gather, on a task where they
    really are authorized, as if it supported work on a different task.
    """
    receipts = _Receipts(_Receipt(task_id=uuid.uuid4()))
    service = _service(receipts=receipts)

    with pytest.raises(HandoffRefused, match="same task"):
        await service.issue(_ctx(), checkpoint_id=_CHECKPOINT, receipt_id=_RECEIPT)


# --- a handle is not trusted about itself -------------------------------------


@pytest.mark.asyncio
async def test_a_handmade_handle_is_refused_however_well_formed() -> None:
    """The validator never reads the digest out of the thing it is validating.

    A hand-built handle whose every visible field is right, and whose
    `handle_digest` is simply wrong, is refused -- so the digest is being
    recomputed from rows rather than taken from the handle. Without this, a
    validator that compared the handle against its own digest would accept
    anything anyone wrote.
    """
    service = _service()
    handle = await _issued(service)
    forged = dataclasses.replace(handle, handle_digest="0" * 64)

    with pytest.raises(HandoffRefused):
        await service.consume(_ctx(_SPECIALIST), forged)


@pytest.mark.asyncio
async def test_a_handle_bound_under_another_version_is_refused() -> None:
    """A handle is only as good as the rule it was minted by."""
    service = _service()
    handle = await _issued(service)
    stale = dataclasses.replace(handle, binding_version="context-handoff.v0")

    with pytest.raises(HandoffRefused, match="minted by"):
        await service.consume(_ctx(_SPECIALIST), stale)
