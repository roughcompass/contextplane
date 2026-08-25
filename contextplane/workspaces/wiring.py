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
from contextplane.config import Settings
from contextplane.context.arms import DEFAULT_ARM_LIMIT, ContextArms
from contextplane.context.assembler import DEFAULT_ARM_TIMEOUT_S, DEFAULT_ITEM_CAP
from contextplane.context.evaluation.fingerprint import resolver_fingerprint
from contextplane.context.evaluation.runs import EvaluationRunService
from contextplane.context.evaluation.simulation import SimulationService
from contextplane.context.instructions import InstructionChannel
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.references import ReceiptReferenceIndex
from contextplane.context.resolve import ContextResolver
from contextplane.context.resume import ContextResumeService
from contextplane.context.semantic_workspace import Embedder
from contextplane.extraction.response_factory import build_response_provider
from contextplane.service.governance.tenants import TenantDirectoryService
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import Clock
from contextplane.workspaces.checkpoints import IntentCheckpointService
from contextplane.workspaces.directory import IntentDirectoryService
from contextplane.workspaces.grants import IntentGrantService
from contextplane.workspaces.recall import WorkspaceRecall


@dataclass(frozen=True)
class LayeredContextServices:
    """What this area contributes to the typed container, by container field name."""

    intent_checkpoints: IntentCheckpointService
    intent_grants: IntentGrantService
    workspace_recall: WorkspaceRecall
    context_arms: ContextArms
    context_receipts: ContextReceiptService
    context_resolver: ContextResolver
    instruction_channel: InstructionChannel
    evaluation_runs: EvaluationRunService
    simulation: SimulationService
    intent_directory: IntentDirectoryService
    tenant_directory: TenantDirectoryService
    context_reference_index: ReceiptReferenceIndex
    context_resume: ContextResumeService


def build_layered_context_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    settings: Settings,
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
    instruction_channel = InstructionChannel(session_factory)
    context_arms = ContextArms(
        session_factory=session_factory,
        retrieval=retrieval,
        claims=claim_serving,
        arc_receipts=arc_receipt_reader,
        recall=workspace_recall,
        instructions=instruction_channel,
        embedder=embedder,
    )
    context_receipts = ContextReceiptService(session_factory=session_factory, clock=clock)
    context_resolver = ContextResolver(
        arms=context_arms,
        receipts=context_receipts,
        instruction_channel=instruction_channel,
    )
    return LayeredContextServices(
        intent_checkpoints=IntentCheckpointService(session_factory=session_factory, clock=clock),
        intent_grants=IntentGrantService(session_factory=session_factory, clock=clock),
        workspace_recall=workspace_recall,
        context_arms=context_arms,
        context_receipts=context_receipts,
        context_resolver=context_resolver,
        instruction_channel=instruction_channel,
        intent_directory=IntentDirectoryService(session_factory=session_factory, clock=clock),
        tenant_directory=TenantDirectoryService(session_factory),
        evaluation_runs=EvaluationRunService(
            session_factory=session_factory,
            resolver=context_resolver,
            clock=clock,
            # Computed once, here, because it describes the deployment and not
            # the request. A value recomputed per run could vary within one
            # process, which would make two runs of the same deployment look
            # incomparable.
            fingerprint=resolver_fingerprint(
                decision=context_arms.recall_decision(),
                embedder_available=embedder is not None,
                arm_limit=DEFAULT_ARM_LIMIT,
                item_cap=DEFAULT_ITEM_CAP,
                arm_timeout_s=DEFAULT_ARM_TIMEOUT_S,
            ),
        ),
        # Built once, with the provider resolved at startup rather than per
        # request. A credential read on a request path is a raise while serving,
        # and a deployment with no provider should learn that at boot rather
        # than the first time somebody clicks simulate.
        simulation=SimulationService(
            session_factory=session_factory,
            resolver=context_resolver,
            clock=clock,
            provider=build_response_provider(settings),
            provider_selector=settings.simulation_provider,
            model_pin=settings.simulation_model,
            max_output_tokens=settings.simulation_max_output_tokens,
            judge_selector=settings.judge_provider,
            judge_model_pin=settings.judge_model,
        ),
        context_reference_index=ReceiptReferenceIndex(session_factory=session_factory),
        context_resume=ContextResumeService(session_factory=session_factory, clock=clock),
    )
