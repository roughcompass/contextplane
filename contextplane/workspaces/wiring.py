"""Task-memory and layered-context construction: one registration point for both.

One registration entry point per area, called by the composition root — see
`contextplane.service.governance.wiring` for the shape.

Two packages are wired from here, not one. `contextplane.workspaces` owns the
checkpoint chain, the participant audience that gates it, and bounded recall;
`contextplane.context` owns the arms, the resolver, and the receipts a
resolution writes. They are wired together because the composer needs both,
and this is the package that may hold that composition: the module boundary
contract places `workspaces` above `context`, so building context objects here
is a downward dependency, while the reverse would be an upward one.

Everything is built once per deployment rather than per request, so both
transports resolve context over one set of arms. Two composers built
independently could disagree about which service answers a block, and the
resolved envelope would look identical either way. One receipt writer for the
same reason: the clock stamping a resolution is then the same clock
everywhere, and the receipt-lookup surface reads what resolution wrote.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc import ReceiptReader
from contextplane.context.arms import ContextArms
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.references import ReceiptReferenceIndex
from contextplane.context.resolve import ContextResolver
from contextplane.context.resume import ContextResumeService
from contextplane.context.semantic_workspace import Embedder
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import Clock
from contextplane.workspaces.checkpoints import TaskCheckpointService
from contextplane.workspaces.grants import TaskGrantService
from contextplane.workspaces.recall import WorkspaceRecall


@dataclass(frozen=True)
class LayeredContextServices:
    """What this area contributes to the typed container, by container field name."""

    task_checkpoints: TaskCheckpointService
    task_grants: TaskGrantService
    workspace_recall: WorkspaceRecall
    context_arms: ContextArms
    context_receipts: ContextReceiptService
    context_resolver: ContextResolver
    context_reference_index: ReceiptReferenceIndex
    context_resume: ContextResumeService


def build_layered_context_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    retrieval: RetrievalService,
    claim_serving: ClaimServingService,
    arc_receipt_reader: ReceiptReader,
    embedder: Embedder | None = None,
) -> LayeredContextServices:
    """Construct task memory and the layered-context composer over the shared graph.

    The keyword collaborators are the arms' sources, threaded in by the root
    rather than rebuilt here: a second `RetrievalService` would mean a second
    embedding cache, and a second claim or receipt reader would answer a block
    from a different instance than every other read path uses.

    The embedder is threaded for the same reason and matters for a further one:
    the semantic workspace arm is available only when the decision artifact
    approves it *and* a model is present, so a deployment that omits it here
    leaves an approved branch permanently dead. It stays optional because the
    lexical branch needs no model, and a deployment on that branch should not
    be made to load one.
    """
    workspace_recall = WorkspaceRecall(session_factory=session_factory)
    context_arms = ContextArms(
        session_factory=session_factory,
        retrieval=retrieval,
        claims=claim_serving,
        arc_receipts=arc_receipt_reader,
        recall=workspace_recall,
        embedder=embedder,
    )
    context_receipts = ContextReceiptService(session_factory=session_factory, clock=clock)
    return LayeredContextServices(
        task_checkpoints=TaskCheckpointService(session_factory=session_factory, clock=clock),
        task_grants=TaskGrantService(session_factory=session_factory, clock=clock),
        workspace_recall=workspace_recall,
        context_arms=context_arms,
        context_receipts=context_receipts,
        context_resolver=ContextResolver(arms=context_arms, receipts=context_receipts),
        context_reference_index=ReceiptReferenceIndex(session_factory=session_factory),
        context_resume=ContextResumeService(session_factory=session_factory, clock=clock),
    )
