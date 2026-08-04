"""The claims subsystem's erasure participant: derived personal data goes too.

Session events are what a person said; claims are what the system concluded
from it. An erasure that deletes the events and keeps the conclusions has not
erased the person — the claim value, the provenance excerpt (their verbatim
words), and the claim's embedding are all still readable, and two of the three
are still *searchable*. This participant existed as a gap first: erasures
reported success while every derived claim survived.

**What is erased.** Two selections, both scoped to the requesting tenant the
way every other participant scopes (a multi-tenant actor's other-tenant claims
need an erasure request in that tenant):

1. *Preference claims* — everything in the target actor's preference
   namespace. Those are facts about the person, erased regardless of who
   authored the row (confirmation copies the namespace onto curator-authored
   successors) and regardless of provenance.
2. *No-independent-evidence claims* — the target's authored claims where no
   provenance row points anywhere but the person's own sessions. A row
   disqualifies a claim from erasure iff it is non-session evidence (a
   document, a commit, a connector run, a curator's confirmation) or a
   session event that still exists and belongs to a *different* actor.
   Dangling refs never disqualify: an event already erased is nobody's
   independent evidence. That keeps the rule stable across retries — the
   outcome cannot depend on whether session-event deletion happened to commit
   on an earlier attempt — and it sweeps up the residue of erasures performed
   before this participant existed.

**Selection lives here; every write lands in the table's own module.** The
privileged-writes discipline holds for erasure too: `claims.py` deletes and
repairs claim rows (chain splice/reopen, confirmation clearing, excerpt
scrub), `promotion.py` scrubs what promotion wrote (physically — reversal
deliberately preserves history, and erasure must override exactly that), and
`embedding_index.py` removes vectors and queued embeddings. This module
decides *what*; those modules own *how*.

**One transaction.** Selection, repair, scrub, and every delete commit
together. A partial failure that removed claims but not embeddings would
leave the person's verbatim text searchable forever, because a retry's
selection would no longer find the claims that pointed at it.

**Ordering:** registered before session memory — the other-actor check reads
live events, and the registry stops at the first failure, so events outlive
claim selection.

**Known limitation, stated rather than implied:** session-event provenance
rows on *surviving* claims whose refs were already dangling before this
erasure cannot be attributed to any actor — the events that would say whose
words they were are gone. Erasing them for everyone would destroy other
actors' evidence; keeping them is the conservative reading. Event tombstones
would close this, and do not exist.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.embedding.targets import TARGET_CLAIM
from registry.extraction.strategies import NS_PREFERENCE
from registry.service.claims import erase_claims_for_actor
from registry.service.embedding_index import erase_targets
from registry.service.promotion import erase_promotion_artifacts
from registry.types import TenantContext

_log = logging.getLogger(__name__)

# A provenance row that keeps a claim alive: anything that is not session
# evidence, or session evidence that still resolves to a different actor's
# event. Dangling refs match neither arm and so never disqualify.
_DISQUALIFYING_EVIDENCE = """
SELECT 1 FROM lmm_claim_provenance p
 WHERE p.claim_id = c.claim_id
   AND (
        p.evidence_kind <> 'session_event'
     OR EXISTS (
          SELECT 1 FROM memory_session_events e
           WHERE e.event_id::text = p.evidence_ref
             AND e.actor_id <> :actor
        )
   )
"""

_SELECT_CLAIMS = f"""
SELECT c.claim_id FROM lmm_claims c
 WHERE (c.namespace = :pref_ns)
    OR (
        c.author_actor_id = :actor
    AND c.author_tenant_id = :tid
    AND NOT EXISTS ({_DISQUALIFYING_EVIDENCE})
       )
 FOR UPDATE
"""


class ClaimErasure:
    """Erase an actor's derived claims, their evidence rows, and their vectors."""

    subsystem = "claims"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        pref_ns = NS_PREFERENCE.format(tenant_id=ctx.tenant_id, actor_id=target_actor_id)
        async with self._factory() as session, session.begin():
            selected = [
                row.claim_id
                for row in await session.execute(
                    text(_SELECT_CLAIMS),
                    {"pref_ns": pref_ns, "actor": target_actor_id, "tid": ctx.tenant_id},
                )
            ]

            counts: dict[str, int] = {}
            # Promotion residue first: its journal rows name the claims, so it
            # must read them before the claim deletes take them away.
            counts.update(await erase_promotion_artifacts(session, selected))
            counts.update(
                await erase_claims_for_actor(
                    session,
                    selected=selected,
                    target_actor_id=target_actor_id,
                    tenant_id=ctx.tenant_id,
                )
            )
            counts.update(await erase_targets(session, target_type=TARGET_CLAIM, target_ids=selected))

        _log.info(
            "claim_erasure.complete: actor=%s tenant=%s counts=%s",
            target_actor_id,
            ctx.tenant_id,
            counts,
        )
        return counts


__all__ = ["ClaimErasure"]
