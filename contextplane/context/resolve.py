"""Resolving one context request, once, for every transport that asks.

Five steps: read what instruction set the caller declared, build the arms for
the request, assemble them into the envelope, record the receipt and the
declaration, hand it all back. Only the last part -- turning the
envelope into a response body -- differs between REST and MCP, so only that part
lives in an adapter.

**The sequence is here rather than in each adapter because one of its steps is
easy to omit.** A transport that built arms and assembled them would return a
perfectly well-formed envelope with no stored receipt, and that answer is
indistinguishable from an audited one at the point somebody needs the audit. Two
adapters mean two chances to forget; this module means none.

**The receipt write is not best-effort.** If it fails, the resolution fails. That
is a deliberate trade of availability for evidence: an answer nobody can later
show they were given is the thing receipts exist to prevent, and a caller who
receives one has no way to know the record is missing. Callers who want an
unrecorded read are asking for a different operation than this one.

**The instruction block's emptiness is reported three ways, never one.** A
caller who declared nothing, a caller whose declared set was never submitted, and
a caller for whom no correction applies all receive an empty block, and the three
have different remedies -- only the middle one is a state the caller can leave by
doing something, and reporting all three identically is what would make partial
adoption of the channel invisible.

**The ARC block's emptiness is reported, not merely returned.** The ARC arm is
receipt-anchored -- it serves what an attested resolution already selected -- so
a request naming no ARC receipt yields an empty ARC block. That is a complete
envelope, not a degraded one, because nothing failed. But "empty because you
named no receipt" and "empty because that receipt selected nothing" are the same
value with opposite remedies, and only the first is the caller's to fix, so the
result says which it was.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, Any

from contextplane.context import instructions, lifecycle
from contextplane.context.assembler import assemble
from contextplane.context.instructions import DeclarationOutcome, Disposition
from contextplane.context.schemas.envelope import BLOCK_ARC, BLOCK_EMPTY, BLOCK_INSTRUCTIONS, BLOCK_SUCCESS

if TYPE_CHECKING:
    from contextplane.arc import ArcRequestContext
    from contextplane.context.arms import ContextArms
    from contextplane.context.instructions import InstructionChannel
    from contextplane.context.receipts import ContextReceiptService
    from contextplane.context.schemas.envelope import ContextEnvelopeV1
    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.types import TenantContext

#: Said back to a caller whose ARC block is empty only because the request named
#: no receipt. Phrased as the remedy rather than the diagnosis, because the
#: caller's next action is to supply one.
ARC_NOT_REQUESTED_NOTE = (
    "the ARC block is empty because the request named no arc_receipt_id; "
    "ARC context is served from an attested resolution, so supply one to receive it"
)


@dataclasses.dataclass(frozen=True)
class ResolvedContext:
    """One resolution: the envelope, its stored receipt, and what to explain.

    `receipt_id` is not optional. There is no path through `ContextResolver`
    that returns an envelope without one -- if the receipt could not be written
    the call raised instead, so a caller holding this object holds an auditable
    answer.
    """

    envelope: ContextEnvelopeV1
    receipt_id: uuid.UUID
    arc_block_note: str | None
    #: Which of the three instruction dispositions this resolution ran under.
    #: Always set, including `NOT_DECLARED`, because a caller that has to infer
    #: "declared nothing" from a missing field will infer it for "declared
    #: something we had never seen" too -- and those two are exactly the pair
    #: whose conflation makes partial adoption invisible.
    instruction_disposition: Disposition
    #: Why the instruction block is empty, when it is. `None` when it carries
    #: something, so a note attached regardless does not train callers to ignore
    #: it.
    instruction_block_note: str | None


class ContextResolver:
    """Assembles and records one context resolution.

    Holds no policy of its own. The arms decide what each block contains and
    what trust each item carries, the assembler decides block and envelope
    state, the receipt service decides what a stored resolution looks like.
    This decides only the order, and that the receipt is not skippable.
    """

    def __init__(
        self,
        *,
        arms: ContextArms,
        receipts: ContextReceiptService,
        instruction_channel: InstructionChannel,
    ) -> None:
        self._arms = arms
        self._receipts = receipts
        self._instructions = instruction_channel

    async def resolve(
        self,
        ctx: TenantContext,
        *,
        query: str,
        moment: datetime.datetime,
        arc: ArcRequestContext | None = None,
        arc_receipt_id: uuid.UUID | None = None,
        subject_entity_id: uuid.UUID | None = None,
        intent_ids: tuple[uuid.UUID, ...] = (),
        workspace_term: str | None = None,
        workspace_reference: ExternalReferenceV1 | None = None,
        lifecycle_references: tuple[ExternalReferenceV1, ...] = (),
        instruction_digest: str | None = None,
        limit: int = 25,
        max_age_s: float | None = None,
    ) -> ResolvedContext:
        """Resolve one request and store the receipt that records it.

        A caller who names where they are in a delivery lifecycle gets context
        narrowed to that placement. The profile is built here rather than in
        each transport because building it is also what refuses an unknown
        reference kind, and a refusal that two adapters each have to remember is
        a refusal one of them will eventually skip.

        **The instruction declaration is read here rather than inside its arm**,
        for the same reason: the disposition this resolution records has to be a
        statement about the read the block was built from. Splitting them would
        admit a window in which content arrived between the two, and the record
        would say `declared_known` about a delta read that ran against nothing.
        A malformed digest is refused before any arm runs -- a declaration that
        can never match a submission is indistinguishable, later, from an
        integration that submitted nothing.
        """
        profile = lifecycle.LifecycleProfile.of(lifecycle_references) if lifecycle_references else None
        declaration = await self._instructions.resolve_declaration(ctx, digest=instruction_digest, limit=limit)
        arms = self._arms.for_request(
            ctx,
            query=query,
            moment=moment,
            arc=arc,
            arc_receipt_id=arc_receipt_id,
            subject_entity_id=subject_entity_id,
            intent_ids=intent_ids,
            workspace_term=workspace_term,
            workspace_reference=workspace_reference,
            limit=limit,
            lifecycle=profile,
            declaration=declaration,
        )
        result = await assemble(arms, now=moment, max_age_s=max_age_s)

        # One task id or none. A receipt names the task it describes, and a
        # request spanning several tasks does not describe one -- guessing which
        # would file the evidence under a task that only partly explains it.
        intent_id = intent_ids[0] if len(intent_ids) == 1 else None

        receipt_id = await self._receipts.record(
            ctx,
            result=result,
            intent_id=intent_id,
            request=self._request_record(
                query=query,
                arc_receipt_id=arc_receipt_id,
                subject_entity_id=subject_entity_id,
                intent_ids=intent_ids,
                workspace_term=workspace_term,
                workspace_reference=workspace_reference,
                profile=profile,
                instruction_digest=declaration.digest,
                limit=limit,
                max_age_s=max_age_s,
            ),
        )

        # Recorded against what the caller actually received, not against what
        # the read found. An instruction arm that failed a floor served nothing,
        # so nothing was contradicted -- and a record claiming a contradiction
        # reached an agent that never saw one is worse than no record, because
        # it is the record an evaluator would act on.
        served = (
            declaration
            if result.envelope.block(BLOCK_INSTRUCTIONS).state == BLOCK_SUCCESS
            else (dataclasses.replace(declaration, deltas=()))
        )
        await self._instructions.record(ctx, outcome=served, receipt_id=receipt_id, now=moment)

        return ResolvedContext(
            envelope=result.envelope,
            receipt_id=receipt_id,
            arc_block_note=self._arc_note(result.envelope, arc_receipt_id=arc_receipt_id),
            instruction_disposition=declaration.disposition,
            instruction_block_note=self._instruction_note(result.envelope, declaration=declaration),
        )

    @staticmethod
    def _instruction_note(envelope: ContextEnvelopeV1, *, declaration: DeclarationOutcome) -> str | None:
        """Why the instruction block is empty, when it is and the reason is known.

        Only for an *empty* block. A degraded or failed one already carries its
        own reason from the arm, and overwriting it with "no correction applies"
        would replace a floor's refusal with a statement that the floor did not
        happen.
        """
        block = envelope.block(BLOCK_INSTRUCTIONS)
        if block.state != BLOCK_EMPTY:
            return None
        return instructions.BLOCK_NOTES[declaration.disposition]

    @staticmethod
    def _arc_note(envelope: ContextEnvelopeV1, *, arc_receipt_id: uuid.UUID | None) -> str | None:
        """Explain an empty ARC block that the caller can do something about.

        Only when the block is actually empty. A request that named no receipt
        and still got ARC items has nothing to explain, and a note attached
        regardless would train callers to ignore it.
        """
        if arc_receipt_id is not None:
            return None
        if envelope.block(BLOCK_ARC).state != BLOCK_EMPTY:
            return None
        return ARC_NOT_REQUESTED_NOTE

    @staticmethod
    def _request_record(
        *,
        query: str,
        arc_receipt_id: uuid.UUID | None,
        subject_entity_id: uuid.UUID | None,
        intent_ids: tuple[uuid.UUID, ...],
        workspace_term: str | None,
        workspace_reference: ExternalReferenceV1 | None,
        profile: lifecycle.LifecycleProfile | None,
        instruction_digest: str | None,
        limit: int,
        max_age_s: float | None,
    ) -> dict[str, Any]:
        """The request as the receipt stores it, digestible and reproducible.

        Every input that can change the answer, and nothing else. The external
        reference is reduced to its collision key plus its parts: the key is
        what makes two spellings of one reference compare equal, and the parts
        are what let a reader see which spelling this caller used.
        """
        record: dict[str, Any] = {"query": query, "limit": limit}
        if profile is not None:
            # The references, not the placement derived from them. Placement is
            # a function of these and re-derivable; the references are what the
            # caller actually said, and a receipt that stored only the
            # conclusion could not show the input it was reached from.
            record["lifecycle_references"] = profile.record()
        if arc_receipt_id is not None:
            record["arc_receipt_id"] = str(arc_receipt_id)
        if subject_entity_id is not None:
            record["subject_entity_id"] = str(subject_entity_id)
        if intent_ids:
            record["intent_ids"] = [str(intent_id) for intent_id in intent_ids]
        if instruction_digest is not None:
            # The digest, never the content. The content is stored once under
            # this key; copying it into every receipt would put the caller's
            # instruction set on every resolution in the product, which is the
            # second copy this channel was designed to avoid.
            record["instruction_digest"] = instruction_digest
        if workspace_term is not None:
            record["workspace_term"] = workspace_term
        if max_age_s is not None:
            record["max_age_s"] = max_age_s
        reference = workspace_reference
        if reference is not None:
            record["workspace_reference"] = {
                "collision_key": reference.collision_key(),
                "source_system": reference.source_system,
                "source_namespace": reference.source_namespace,
                "kind": reference.kind,
                "external_id": reference.external_id,
                "revision": reference.revision,
            }
        return record


__all__ = ["ARC_NOT_REQUESTED_NOTE", "ContextResolver", "ResolvedContext"]
