"""Composing the arms one context resolution reads, over the services that own them.

The assembler decides what the blocks *mean*. It takes arms as injected
callables and deliberately knows nothing about where their rows come from, which
is what lets every one of its failure paths be reachable from a fixture. This
module is the other half of that split: it owns which service answers each
block, and what trust each answer carries. Those are the only two decisions made
here, and this is the only place either is made.

**Every request gets every arm.** An arm missing from the mapping and an arm
that ran and broke are the same value to the assembler -- a failed block -- and
only one of them is ever true. So this module never omits a key, and never
substitutes an empty answer for a broken one. Where a source genuinely has
nothing to say, the arm says so; where it could not be asked, it raises and the
block fails with a reason.

**The ARC arm serves an attested resolution; it does not perform one.** Which
governance directives apply is decided by the ARC resolution path, which attests
its answer and records a receipt naming the exact revisions it served. This arm
reads that receipt back. Deciding applicability again here would make context
resolution a second governance authority whose answer could disagree with the
attested one -- and the disagreement would be invisible, because both would look
like ARC. A request naming no receipt gets an *empty* ARC block: no resolution
was named, which is a complete answer rather than a failure.

**Withheld is not missing.** A directive the resolution omitted, or one whose
source this caller's audience does not permit, is recorded as an exclusion rather
than dropped. The assembler degrades a block that withheld something, so a reader
can tell "there is nothing" from "there is something you may not see" -- the
second is the one that tells them to go and ask for access.

**Trust labels are chosen per source, each for a stated reason.** Every
non-canonical item carries all eight. Canonical items carry none: the canonical
block is the registry's own answer, and attributing it would invite the question
of whether some other authority could have supplied it.

**Which reads refuse when an erasure has not landed, and which do not.** A
derivative is a second copy of a record, so an erasure is only complete once
propagation has reached every copy. Until it has, a read can serve the copy. The
guard is `pending_overdue(blocking_only=True, tenant_id=...)`: it asks whether
this tenant has blocking propagation past due, and a read that could serve
withdrawn material refuses rather than serving it.

The list below is the deliverable, not the guards. Twice now this check was
wired on the one path in front of somebody -- documented as covering "the
serving paths", plural, and covering one -- and both times the miss was found by
a reader who went looking for the *set*. So each read states its answer, and a
new read is expected to add a line here rather than inherit silence:

*Guarded.* `workspace_arm` in all three of its forms. The by-reference and
by-term reads are guarded inside `WorkspaceRecall`; the third form -- neither a
term nor a reference, serving every checkpoint the caller's grants reach -- does
not go through `WorkspaceRecall` at all, so that guard never covered it, and it
is guarded here. It is the broadest of the three, not the narrowest.

*Guarded.* `observed_claims_arm`, here. A claim is a second copy of what its
evidence said; the claim-derivative handler exists to invalidate one whose
evidence was erased, and it refuses `rebuild` outright. So an unlanded
propagation leaves exactly these rows servable.

*Not guarded, and why.* `canonical_arm` serves catalog entities, whose
derivatives (`vector`, `fts_document`, `cache`, `embedding_chunk`) are every one
of them registered non-blocking -- a `blocking_only` guard here would never
fire. Whether those kinds ought to block is a retention-policy question, and it
is not answered by adding a guard that cannot trigger.

*Guarded.* `instructions_arm`. It serves governed corrections authored here, and
it is guarded on a stricter principle than the rule below: a delta is the most
behaviour-changing thing the envelope can carry, so an instruction block exempt
from a floor the rest of the envelope obeys would be a way to reach an agent when
everything else was withheld. It is guarded because it must be, not because a
derivative kind names its table.

*Not guarded, and why.* `arc_arm` serves ARC receipts. The `receipt_link` kind
covers *context* receipts -- `context_receipt_items`, `context_receipt_exclusions`
-- and ARC receipts are a different subsystem with different tables that no
derivative kind names.

**This list has been wrong three times, so it is no longer the authority.** The
rule -- guard a read exactly when it serves a column a *blocking* derivative
handler rewrites -- is derived and checked by
`tests/conformance/test_overdue_guard_covers_the_blocking_reads.py`.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING

from contextplane.context import instructions, semantic_workspace
from contextplane.context import lifecycle as context_lifecycle
from contextplane.context import queries as context_queries
from contextplane.context.arm_payloads import (
    CANONICAL_SOURCE,
    CLAIMS_SOURCE,
    arc_outcome,
    canonical_payload,
    claim_payload,
    claim_trust,
)
from contextplane.context.assembler import (
    ArmOutcome,
    Exclusion,
    canonical_item,
    contextual_item,
    ordered_items,
)
from contextplane.context.instructions import DeclarationOutcome, InstructionChannel
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_INSTRUCTIONS,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
)
from contextplane.service.memory.claim_serving import ClaimQuery
from contextplane.types import TemporalFilter
from contextplane.workers.derivative_propagation import pending_overdue
from contextplane.workspaces import recall as workspace_recall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.arc import ArcRequestContext, ReceiptReader
    from contextplane.context.assembler import ContextArm
    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.service.memory.claim_serving import ClaimServingService
    from contextplane.service.retrieval import RetrievalService
    from contextplane.types import TenantContext
    from contextplane.workspaces.recall import WorkspaceRecall

#: The most items any one arm returns before reporting itself truncated. The
#: assembler applies its own cap on top; this one bounds the *read*, so a caller
#: cannot make one arm decide how much work every other request queues behind.
DEFAULT_ARM_LIMIT = 25


#: How many authorized checkpoints the semantic scan may consider for one
#: request. A read bound, not a protocol threshold: the campaign scanned a
#: forty-scenario world where the authorized set was small enough that no bound
#: mattered, and shipping that unbounded would let one caller with a large
#: audience decide how much embedding work every other request waits behind.
#:
#: Set to the ceiling workspace recall already applies to what any one arm may
#: *return*, rather than to a number chosen here. That makes the scan strictly
#: narrower than what was measured, never wider -- the only direction a runtime
#: bound may move a decision the evidence closed. Hitting it reports the arm
#: truncated, so a short answer is distinguishable from a complete one.
_SEMANTIC_CANDIDATE_CEILING = workspace_recall.MAX_RESULTS


class ContextArms:
    """Builds the arms for one resolution, over the services that own them.

    One instance per deployment, holding the collaborators rather than the
    request: every method takes the caller's identity and bounds, and returns a
    zero-argument callable, because the assembler runs arms concurrently under
    its own timeout and must not need to know what any of them takes.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        retrieval: RetrievalService,
        claims: ClaimServingService,
        arc_receipts: ReceiptReader,
        recall: WorkspaceRecall,
        instructions: InstructionChannel,
        embedder: semantic_workspace.Embedder | None = None,
        decision: semantic_workspace.RecallDecision | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._retrieval = retrieval
        self._claims = claims
        self._arc_receipts = arc_receipts
        self._recall = recall
        self._instructions = instructions
        # The deployment's embedder, threaded in rather than built here: a second
        # `build_embedder` call is a second model load, and the retrieval area
        # already owns the one this process has. `None` means this deployment
        # cannot embed, which is not the same as "semantic is not approved" --
        # the first is a capability and the second is a decision, and the arm
        # below keeps them apart when it says why it served no semantic matches.
        self._embedder = embedder
        # Loaded once, here, rather than per request: a resolution must not be
        # the thing that discovers the decision artifact is missing, and a
        # deployment on a branch that refuses to serve should fail where it is
        # constructed rather than on a caller's first read.
        self._decision = decision if decision is not None else semantic_workspace.load_decision()
        # Startup refusal, and it is deliberately here rather than in `main`.
        # This object is constructed once per deployment by the composition root,
        # so raising here refuses activation of the feature at the moment the
        # feature is assembled -- and it also refuses in any other process that
        # builds arms, which a check in one entry point would miss.
        self._decision.require_service()

    def for_request(
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
        limit: int = DEFAULT_ARM_LIMIT,
        lifecycle: context_lifecycle.LifecycleProfile | None = None,
        declaration: DeclarationOutcome | None = None,
    ) -> dict[str, ContextArm]:
        """Every arm for one request, keyed by block name.

        Returns a mapping the assembler can use directly. It is a plain dict
        rather than four positional returns so a caller cannot reorder the
        blocks by accident, and so the keys are the same constants the envelope
        validates against.
        """
        return {
            BLOCK_CANONICAL: self.canonical_arm(ctx, query=query, moment=moment, limit=limit),
            BLOCK_ARC: self.arc_arm(arc, receipt_id=arc_receipt_id),
            BLOCK_OBSERVED_CLAIMS: self.observed_claims_arm(
                ctx, subject_entity_id=subject_entity_id, moment=moment, limit=limit, lifecycle=lifecycle
            ),
            BLOCK_WORKSPACE: self.workspace_arm(
                ctx,
                term=workspace_term if workspace_term is not None else query,
                reference=workspace_reference,
                intent_ids=intent_ids,
                moment=moment,
                limit=limit,
            ),
            BLOCK_INSTRUCTIONS: self.instructions_arm(ctx, declaration=declaration, moment=moment),
        }

    # -- canonical ---------------------------------------------------------

    def canonical_arm(
        self,
        ctx: TenantContext,
        *,
        query: str,
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> ContextArm:
        """The registry's own answer, from the catalog it owns.

        A search failure is deliberately not caught. The canonical arm failing
        blocks the whole response, and that is the intended outcome: the
        surrounding context without the thing it surrounds reads as the whole
        picture rather than as a gap.
        """

        async def arm() -> ArmOutcome:
            # One more than the bound, so "there is more" is answered by the
            # read rather than inferred from a page that happens to be full.
            results = await self._retrieval.search(ctx, query, limit + 1, TemporalFilter(as_of=moment))
            kept = results[:limit]
            items = [
                canonical_item(
                    source=CANONICAL_SOURCE,
                    item_key=str(result.entity.entity_id),
                    payload=canonical_payload(result),
                )
                for result in kept
            ]
            return ArmOutcome(
                items=ordered_items(items),
                truncated=len(results) > limit,
                # The catalog is the registry's own live state, so this answer is
                # exactly as fresh as the instant it was read at. `None` would
                # claim the arm cannot report freshness, which is not true here
                # and would suppress a staleness bound the caller asked for.
                fresh_as_of=moment,
            )

        return arm

    # -- ARC ---------------------------------------------------------------

    def arc_arm(self, arc: ArcRequestContext | None, *, receipt_id: uuid.UUID | None) -> ContextArm:
        """The directives an attested resolution already served to this caller.

        Both arguments are optional and mean one thing together: a request that
        names no ARC resolution has no ARC context, which is an empty block. It
        is not a failure -- nothing was asked of ARC and nothing broke -- and it
        is not silently dropped either, because the block is still present and
        still says which of the four states it is in.
        """

        async def arm() -> ArmOutcome:
            if arc is None or receipt_id is None:
                return ArmOutcome()
            return arc_outcome(await self._arc_receipts.get_receipt(arc, receipt_id))

        return arm

    # -- observed claims ---------------------------------------------------

    def observed_claims_arm(
        self,
        ctx: TenantContext,
        *,
        subject_entity_id: uuid.UUID | None = None,
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
        lifecycle: context_lifecycle.LifecycleProfile | None = None,
    ) -> ContextArm:
        """Claims recalled from living memory, weighed but not promoted.

        A structural read rather than a ranked one. Ranking would make an
        answer's contents depend on a similarity score nobody asked for, and a
        receipt that cannot be reproduced from its own inputs is decorative.

        A lifecycle profile narrows this to claims placed where the caller is.
        `lifecycle.narrow` owns that rule, and says why no other block shares it.
        """
        # One more than the bound for the same reason the canonical arm asks for
        # it, clamped to what the query type accepts so an over-large caller
        # bound is refused by this arm rather than by the service underneath it.
        bounded = min(limit + 1, ClaimQuery.MAX_LIMIT)

        async def arm() -> ArmOutcome:
            # A claim is a second copy of what its evidence said, and the
            # claim-derivative handler exists to invalidate it when that evidence
            # is erased. That handler refuses `rebuild` outright, which lands the
            # item in `failed` -- so an erasure that should have taken these
            # claims out of serving may simply not have, and this arm is where
            # they would be served from anyway.
            #
            # Its own session, because the read below belongs to
            # `ClaimServingService` and opens one this arm does not hold. That is
            # a narrower guarantee than the workspace read's same-session check:
            # propagation landing between this check and that read would be
            # served. Narrower in the safe direction -- the window can only
            # withhold content that has just become servable, never serve content
            # that has just become unservable.
            async with self._session_factory() as session:
                await self._refuse_if_overdue(session, tenant_id=ctx.tenant_id, moment=moment)
            served = await self._claims.query(
                ctx,
                ClaimQuery(subject_entity_id=subject_entity_id, as_of=moment, limit=bounded),
            )
            excluded: tuple[Exclusion, ...] = ()
            if lifecycle is not None and lifecycle.selects():
                served, excluded = await context_lifecycle.narrow(self._session_factory, lifecycle, served, ctx)
            kept = served[:limit]
            items = [
                contextual_item(
                    block=BLOCK_OBSERVED_CLAIMS,
                    source=CLAIMS_SOURCE,
                    item_key=str(claim.claim_id),
                    payload=claim_payload(claim),
                    trust=claim_trust(claim),
                )
                for claim in kept
            ]
            return ArmOutcome(
                items=ordered_items(items),
                exclusions=excluded,
                truncated=len(served) > limit,
                # Same reasoning as the canonical arm: a live read is as fresh as
                # its own instant. Each claim additionally carries its own
                # `as_of` in trust metadata, which is the age of the *statement*
                # rather than of the read.
                fresh_as_of=moment,
            )

        return arm

    # -- workspace ---------------------------------------------------------

    def workspace_arm(
        self,
        ctx: TenantContext,
        *,
        term: str | None = None,
        reference: ExternalReferenceV1 | None = None,
        intent_ids: tuple[uuid.UUID, ...] = (),
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> ContextArm:
        """Checkpoints from tasks this caller participates in.

        Three reads, one block. A named external reference is the most specific
        thing a caller can supply, so it wins; a search term narrows lexically;
        with neither, the block is every checkpoint the caller's grants reach.
        All three resolve the audience inside the query rather than filtering
        after it, which is why none of them is assembled here.

        **Which reads run is decided by the committed evidence, not by this
        method and not by configuration.** `workspace_recall_decision.json`
        records the branch a pre-registered campaign landed on;
        `semantic_workspace` enforces it. Under the approved branch a term query
        also runs the semantic exact scan over the caller's already-authorized
        checkpoints and merges it with the lexical hits. Under any other branch
        the scan is unavailable and this method is what it was before -- there is
        no setting that changes that in either direction.
        """
        actor = str(ctx.actor_id)

        if reference is not None:
            return self._recall.reference_arm(
                tenant_id=ctx.tenant_id,
                actor_id=actor,
                source_system=reference.source_system,
                source_namespace=reference.source_namespace,
                kind=reference.kind,
                external_id=reference.external_id,
                moment=moment,
                limit=limit,
            )

        if term is not None and term.strip():
            lexical = self._recall.lexical_arm(
                tenant_id=ctx.tenant_id,
                actor_id=actor,
                term=term,
                moment=moment,
                limit=limit,
            )
            if not self._semantic_available():
                return lexical
            # Rebound so the closure captures a `str` rather than the enclosing
            # `str | None`, whose narrowing does not follow it inside.
            needle = term

            async def lexical_and_semantic() -> ArmOutcome:
                return semantic_workspace.merge_outcomes(
                    await lexical(),
                    await self._semantic(ctx, term=needle, actor=actor, moment=moment),
                )

            return lexical_and_semantic

        async def arm() -> ArmOutcome:
            async with self._session_factory() as session:
                # The third workspace read, and the only one that does not go
                # through `WorkspaceRecall` -- so the guard wired there does not
                # cover it. It is also the broadest of the three: with no term
                # and no reference this serves every checkpoint the caller's
                # grants reach, which is the largest surface an unlanded erasure
                # could be served from, not the smallest.
                #
                # In the read's own session, which is the property the recall
                # guard was built for: a check on another connection can pass
                # against a state the read that trusted it never sees.
                await self._refuse_if_overdue(session, tenant_id=ctx.tenant_id, moment=moment)
                return await context_queries.workspace_arm(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=actor,
                    moment=moment,
                    intent_ids=intent_ids,
                    limit=limit,
                )

        return arm

    # -- instructions ------------------------------------------------------

    def instructions_arm(
        self,
        ctx: TenantContext,
        *,
        declaration: DeclarationOutcome | None,
        moment: datetime.datetime,
    ) -> ContextArm:
        """Governed corrections to the instruction set the caller declared.

        The read already happened -- the resolver performs it, because it also
        has to record what was declared and the disposition it records must be a
        statement about the same read the block was built from. What is left
        here is turning that outcome into items, and passing it through the
        floors every other arm passes.

        **`_refuse_if_overdue` is called here for the same reason it is called on
        the claims and workspace arms, and the reason matters more here.** A
        delta is the most behaviour-changing thing the envelope can carry. An
        instruction block exempt from a floor the rest of the envelope obeys
        would be a way to reach an agent when everything else was withheld --
        precisely the channel-around-the-floor the decision behind this block
        refuses. There is no argument from "instructions are not data" available
        here, because that argument is the one that would create the hole.

        An empty declaration yields an empty block rather than an absent one, and
        the resolver says which of the three dispositions produced it: three
        identical empty blocks with different causes are what teach a caller to
        ignore all three.
        """

        async def arm() -> ArmOutcome:
            if declaration is None or not declaration.deltas:
                return ArmOutcome()
            async with self._session_factory() as session:
                await self._refuse_if_overdue(session, tenant_id=ctx.tenant_id, moment=moment)
            return ArmOutcome(
                items=ordered_items(instructions.delta_items(declaration)),
                # A delta is authored and then withdrawn or left standing; the
                # read above is as fresh as the instant it ran, and each item
                # additionally carries its own authoring time.
                fresh_as_of=moment,
            )

        return arm

    async def _refuse_if_overdue(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        moment: datetime.datetime,
    ) -> None:
        """Ask, before serving, whether this tenant's erasures have actually landed.

        The same check `WorkspaceRecall` runs, on the arms that read here instead
        of there. It raises `workspace_recall.OverdueDerivativeRefusal` rather
        than a type of its own: a caller that catches one refusal and not the
        other would be protected on whichever arm it happened to name, and two
        spellings of "this tenant's erasures are late" is the shape that let the
        gap below go unnoticed in the first place.

        `blocking_only` and `tenant_id` are both deliberate and both come from
        the ratified design -- a non-blocking derivative is not what makes a read
        unsafe, and refusing one tenant for another tenant's late propagation
        costs availability and buys no privacy.
        """
        overdue = await pending_overdue(session, now=moment, blocking_only=True, tenant_id=tenant_id)
        if overdue:
            raise workspace_recall.OverdueDerivativeRefusal(
                f"{overdue} blocking derivative propagation item(s) are past due for this tenant; "
                "this arm does not serve content whose erasure has not reached it"
            )

    def recall_decision(self) -> semantic_workspace.RecallDecision:
        """The branch this deployment serves under.

        Exposed rather than reached for, because the evaluation fingerprint needs
        it and a second `load_decision()` at the composition root would be a
        second read of a committed artifact that could disagree with the one this
        object enforces.
        """
        return self._decision

    def _semantic_available(self) -> bool:
        """Whether this deployment both may and can run the semantic scan.

        Two conditions, kept separate on purpose. `semantic_approved` is the
        recorded decision; an embedder is the capability. A deployment that has
        one without the other serves lexical only, which is the same answer for
        two different reasons -- and the reasons are worth keeping distinct,
        because one is fixed by wiring an embedder and the other is not fixable
        without new evidence.
        """
        return self._decision.semantic_approved and self._embedder is not None

    async def _semantic(
        self,
        ctx: TenantContext,
        *,
        term: str,
        actor: str,
        moment: datetime.datetime,
    ) -> ArmOutcome:
        """The approved arm: resolve the caller's audience, then scan only that.

        The authorized read comes first and its result is the whole candidate
        set. Nothing here re-checks authorization and nothing here can widen it,
        because the widening would have to happen in the read above and that read
        resolves the audience inside its own SELECT.
        """
        embedder = self._embedder
        if embedder is None:  # pragma: no cover - guarded by _semantic_available
            return ArmOutcome()
        async with self._session_factory() as session:
            authorized = await context_queries.workspace_arm(
                session,
                tenant_id=ctx.tenant_id,
                actor_id=actor,
                moment=moment,
                limit=_SEMANTIC_CANDIDATE_CEILING,
            )
        candidates = tuple(
            semantic_workspace.Candidate(
                item_key=item.receipt_item_id.item_key,
                text=str(item.payload.get("goal", "")),
                item=item,
            )
            for item in authorized.items
        )
        scanned = semantic_workspace.exact_scan(
            query=term,
            candidates=candidates,
            embedder=embedder,
            decision=self._decision,
        )
        if not authorized.truncated:
            return scanned
        # The candidate ceiling cut the audience read short, so the scan saw part
        # of what the caller may see. Reported rather than silently absorbed: a
        # semantic answer over a truncated candidate set is incomplete in a way
        # its item count does not show.
        return dataclasses.replace(scanned, truncated=True)


__all__ = [
    "BLOCK_NAMES",
    "DEFAULT_ARM_LIMIT",
    "ContextArms",
]
