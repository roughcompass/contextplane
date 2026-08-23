"""Binding one specialist's evidence so the next specialist resumes exactly it.

A handoff is where evidence usually stops being evidence. The obvious
implementation copies the transcript: the first actor's checkpoint and the
receipt behind it are serialized into whatever the control plane carries, handed
over, and read by the second actor. What arrives is then a *claim about* what the
first actor saw, and nothing downstream can tell a faithful copy from a stale or
edited one -- the copy is self-describing, so it agrees with itself either way.

**So this module hands over identities and digests, never content.** A handle
names a checkpoint revision, the receipt that produced it, the external
references both cite, the blocks the receipt drew from, and what it withheld.
Every one of those is a pointer plus a digest over the thing pointed at. The
receiving actor reads the real rows through the surfaces that already exist and
checks them against the digests it was handed. If the two disagree, the handoff
refuses -- and the disagreement is *detectable*, which is the property copying
destroys.

**No new endpoint, deliberately.** The handle is a reference set, and both halves
of it are already addressable: checkpoints by id and by digest, receipts by id.
A dedicated transport would add a second way to reach the same rows, with its own
authorization to get wrong, in exchange for nothing the existing surfaces cannot
already carry. Issuing and consuming are service calls made behind those
surfaces.

**Authorization is re-resolved on consumption, not inherited from issuance.**
The handle is not a bearer token and holding one grants nothing. The consuming
actor is re-checked against the task audience at the moment it consumes, through
the same audience-resolved reads every other workspace path uses -- so a
specialist whose grant was revoked between issuance and consumption is denied,
and an outsider handed a handle out of band gets exactly what an outsider gets
without one. The alternative -- treating a validly issued handle as proof --
would make the handle a capability that outlives the grant it was minted under,
which is how a revoked reviewer keeps reading a task for as long as anyone still
has the handle.

**Digests are recomputed here, never trusted from the handle.** A handle carries
its own `handle_digest`, and that digest is only ever *compared* against one
computed from freshly-read rows. A validator that read the digest out of the
handle and checked the handle against it would agree with any handle at all,
including one somebody wrote from scratch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Protocol

from contextplane.context.receipts import ReceiptNotServable
from contextplane.exceptions import RegistryError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from contextplane.context.receipts import ContextReceiptService
    from contextplane.types import TenantContext


class _Reference(Protocol):
    """The one thing this module needs from an external reference: its identity."""

    def collision_key(self) -> str:
        """The scope two references are the same thing under."""
        ...


class CheckpointRevision(Protocol):
    """The four fields a handle binds from a checkpoint.

    Structural rather than the concrete contract type, and that is a layering
    fact rather than a style preference: task memory sits *above* this package
    in the module contract, so naming its type here would be an upward import.
    The repo's rule for that case is to argue with the contract rather than
    exempt the edge -- and the honest argument is that this module does not need
    the type, it needs an id, a task, a digest and the references. Anything
    satisfying that shape can be handed in, which is also why the tests need no
    database to exercise every refusal.
    """

    checkpoint_id: uuid.UUID
    intent_id: uuid.UUID
    digest: str
    evidence: Sequence[_Reference]


class CheckpointReader(Protocol):
    """The single read this module makes against task memory.

    Injected for the same reason as above, and it keeps the audience resolution
    where it already lives: the real implementation resolves the caller's grants
    inside its own query, and re-stating that here would be the second copy that
    eventually disagrees with the first.
    """

    async def get_checkpoint(self, ctx: TenantContext, *, checkpoint_id: uuid.UUID) -> CheckpointRevision:
        """One checkpoint revision, or raise if this actor may not read it."""
        ...


#: Version of the binding itself, digested along with the evidence. A change to
#: what a handle binds has to invalidate handles issued under the old rule, or a
#: handle minted before a field was added would validate against a validator that
#: now ignores it.
HANDOFF_BINDING_VERSION = "context-handoff.v1"


class HandoffRefused(RegistryError):
    """The handoff was not honoured, and no evidence crossed.

    One exception for every refusal -- unknown ids, wrong task, moved digests --
    because the caller's next move is the same in all of them and the
    differences are exactly what a prober would enumerate. What went wrong is in
    the log; what the caller gets is that it did not happen.
    """


@dataclasses.dataclass(frozen=True)
class HandoffHandle:
    """What crosses between two actors: pointers, digests, and nothing else.

    Frozen, and every field is either an identifier or a hash. If a future edit
    is tempted to add the checkpoint's `goal` or the receipt's items here "so the
    consumer does not have to read them", that is the transcript copy this module
    exists to avoid -- the consumer reading them itself is the whole mechanism.
    """

    binding_version: str
    intent_id: uuid.UUID
    #: The exact checkpoint revision handed over. Its digest is the revision
    #: identity: two checkpoints with the same id and different digests cannot
    #: exist, so a matching pair is a matching revision.
    checkpoint_id: uuid.UUID
    checkpoint_digest: str
    #: The resolution the checkpoint was written against.
    receipt_id: uuid.UUID
    #: Over the receipt's bound facts -- not its prose. See `_receipt_digest`.
    receipt_digest: str
    #: Collision keys of the external work items both halves cite.
    external_refs: tuple[str, ...]
    #: Which blocks the receipt drew from, so the consumer can tell a handoff
    #: that rested on governance from one that rested on workspace memory.
    source_blocks: tuple[str, ...]
    #: What the resolution withheld, as `block/item_key`. Carried because a
    #: consumer that cannot see what was withheld will read the evidence as
    #: complete, and "there was more you did not get" is the part that changes
    #: what a specialist does next.
    exclusions: tuple[str, ...]
    issued_by: str
    handle_digest: str


def _digest(payload: object) -> str:
    """SHA-256 over a canonical JSON encoding.

    Sorted keys and compact separators, matching how receipts digest a request:
    two digests are only comparable if the encoding is, and one that varied with
    key order would make every comparison a false negative.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reference_keys(references: Sequence[_Reference]) -> tuple[str, ...]:
    """External references as their collision keys, sorted.

    The collision key rather than the whole reference: it is the identity the
    rest of the system joins on, it already carries `kind` (a scope of source
    plus id alone merges two different things that both resolve), and sorting
    makes the digest independent of the order the rows came back in.
    """
    return tuple(sorted(reference.collision_key() for reference in references))


class ContextHandoffService:
    """Issues and consumes handoff handles over the surfaces that already exist.

    Holds the two services rather than a session factory: everything it needs is
    already a read on one of them, and both resolve the caller's audience inside
    their own queries. Reaching past them to SQL would mean re-implementing that
    resolution here, which is the second copy that eventually disagrees.
    """

    def __init__(
        self,
        *,
        checkpoints: CheckpointReader,
        receipts: ContextReceiptService,
    ) -> None:
        self._checkpoints = checkpoints
        self._receipts = receipts

    async def issue(
        self,
        ctx: TenantContext,
        *,
        checkpoint_id: uuid.UUID,
        receipt_id: uuid.UUID,
    ) -> HandoffHandle:
        """Bind a checkpoint revision and the receipt behind it into one handle.

        The task is taken from the checkpoint rather than from the caller. A
        caller-supplied task id would be a third thing to agree with the other
        two, and the only way it could disagree is by being wrong.
        """
        checkpoint, receipt_facts = await self._read(ctx, checkpoint_id=checkpoint_id, receipt_id=receipt_id)
        return self._bind(checkpoint, receipt_facts, issued_by=str(ctx.actor_id))

    async def consume(self, ctx: TenantContext, handle: HandoffHandle) -> HandoffHandle:
        """Re-authorize this actor, re-read the evidence, and re-derive the handle.

        Returns a handle rebuilt from what was actually read, so a caller holding
        the result is holding something derived from rows it was authorized to
        see at this moment -- not the object it was handed. The two are compared
        field by field; if they differ, nothing is returned at all.
        """
        if handle.binding_version != HANDOFF_BINDING_VERSION:
            raise HandoffRefused(
                f"handle binds under {handle.binding_version!r} and this build binds under "
                f"{HANDOFF_BINDING_VERSION!r}; a handle is only as good as the rule it was minted by"
            )

        checkpoint, receipt_facts = await self._read(
            ctx, checkpoint_id=handle.checkpoint_id, receipt_id=handle.receipt_id
        )
        # `issued_by` is carried from the handle rather than replaced with this
        # actor: the rebuilt handle has to be comparable to the one presented,
        # and who issued it is a fact about the past that consuming does not
        # change.
        rederived = self._bind(checkpoint, receipt_facts, issued_by=handle.issued_by)

        if rederived != handle:
            raise HandoffRefused(
                "the evidence this handle names is not the evidence it was issued against; "
                "the checkpoint revision, the receipt, or what it withheld has moved since"
            )
        return rederived

    async def _read(
        self,
        ctx: TenantContext,
        *,
        checkpoint_id: uuid.UUID,
        receipt_id: uuid.UUID,
    ) -> tuple[CheckpointRevision, dict[str, object]]:
        """Both halves, each through its own audience-resolved surface.

        The checkpoint read resolves the task audience inside its query, so a
        non-participant does not get a row to be refused afterwards -- there is
        nothing to refuse. That is what makes an outsider's handoff attempt
        indistinguishable from a handoff of a checkpoint that does not exist,
        which is the answer an outsider should get.
        """
        try:
            checkpoint = await self._checkpoints.get_checkpoint(ctx, checkpoint_id=checkpoint_id)
        except RegistryError as exc:
            raise HandoffRefused("the handed-over checkpoint is not readable by this actor") from exc

        receipt = await self._receipts.get(ctx, receipt_id=receipt_id)
        if receipt is None:
            raise HandoffRefused("the handed-over receipt is not readable by this actor")
        if receipt.intent_id != checkpoint.intent_id:
            # A receipt from another task would let a specialist present evidence
            # gathered somewhere they are authorized as if it supported work
            # here, and both halves would individually check out.
            raise HandoffRefused("the receipt and the checkpoint do not belong to the same task")

        # Both reads refuse a receipt that may not be shown -- unhydrated, or
        # withheld while an incident affecting its inputs is worked. Translated
        # into this module's own refusal, the way the checkpoint read above
        # translates `RegistryError`: a caller that learned to handle
        # `HandoffRefused` must not also have to learn a receipt type, and an
        # escaping one would reach them as a fault rather than as an answer.
        #
        # Refusing is the point rather than a cost. A handoff assembled from an
        # unhydrated receipt would report "nothing was withheld" about a
        # resolution that has not finished recording what it withheld, and one
        # assembled from a withheld receipt would hand a second agent exactly
        # the content the quarantine is keeping back.
        try:
            exclusions = await self._receipts.exclusions_for(ctx, receipt_id=receipt_id)
            arms = await self._receipts.arms_for(ctx, receipt_id=receipt_id)
        except ReceiptNotServable as exc:
            raise HandoffRefused("the handed-over receipt cannot be presented as evidence right now") from exc
        return checkpoint, {
            "receipt_id": str(receipt.receipt_id),
            "state": receipt.state,
            "resolved_at": receipt.resolved_at,
            "requested_by": receipt.requested_by,
            "request_digest": receipt.request_digest,
            "blocks": tuple(sorted(arm.block for arm in arms)),
            "exclusions": tuple(sorted(f"{row.block}/{row.item_key}" for row in exclusions)),
        }

    def _bind(
        self,
        checkpoint: CheckpointRevision,
        receipt_facts: dict[str, object],
        *,
        issued_by: str,
    ) -> HandoffHandle:
        """Assemble the handle and digest it as one unit.

        One digest over everything rather than a digest per part: the parts are
        only evidence together. A handle whose checkpoint digest matched and
        whose exclusion list did not would still be a handle that passed "the
        checkpoint is right", and a consumer checking parts independently would
        have to remember to check all of them.
        """
        receipt_digest = _digest(receipt_facts)
        blocks = receipt_facts["blocks"]
        exclusions = receipt_facts["exclusions"]
        body = {
            "binding_version": HANDOFF_BINDING_VERSION,
            "intent_id": str(checkpoint.intent_id),
            "checkpoint_id": str(checkpoint.checkpoint_id),
            "checkpoint_digest": checkpoint.digest,
            "receipt_id": str(receipt_facts["receipt_id"]),
            "receipt_digest": receipt_digest,
            "external_refs": list(_reference_keys(checkpoint.evidence)),
            "source_blocks": list(blocks) if isinstance(blocks, tuple) else [],
            "exclusions": list(exclusions) if isinstance(exclusions, tuple) else [],
            "issued_by": issued_by,
        }
        return HandoffHandle(
            binding_version=HANDOFF_BINDING_VERSION,
            intent_id=checkpoint.intent_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_digest=checkpoint.digest,
            receipt_id=uuid.UUID(str(receipt_facts["receipt_id"])),
            receipt_digest=receipt_digest,
            external_refs=_reference_keys(checkpoint.evidence),
            source_blocks=blocks if isinstance(blocks, tuple) else (),
            exclusions=exclusions if isinstance(exclusions, tuple) else (),
            issued_by=issued_by,
            handle_digest=_digest(body),
        )


__all__ = [
    "HANDOFF_BINDING_VERSION",
    "CheckpointReader",
    "CheckpointRevision",
    "ContextHandoffService",
    "HandoffHandle",
    "HandoffRefused",
]
