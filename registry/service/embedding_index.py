"""The one way into the embedding index, for every kind of thing that gets embedded.

Three operations and one rule. `enqueue` puts a request on the outbox, `retract` removes a
target's vectors and any pending request for it, and `project_claim` reads a claim and
decides which of the two applies. Everything that indexes anything goes through here, so
the queue has a single writer and the upsert policy lives in one place.

**Why a projection rather than an event.** `project_claim` reads the claim row and decides
from what it finds, instead of trusting its caller to say what happened. That matters
because consolidation can close a claim and mark it consolidated in the same flow — a hook
that took "consolidated" as an instruction to index would index a claim it had just
retired. Reading the row cannot get that wrong, and it makes the function idempotent and
reusable as the backfill.

**Why retraction is correctness, not tidiness.** The read arms already refuse unservable
claims, so a retired claim's vector cannot produce a wrong answer directly. But an ANN
query is `ORDER BY vector <-> q LIMIT k` — every dead vector in the index occupies a
candidate slot that a live one could have used. Left in, they are a silent, unbounded
recall loss on the queries that do matter.

**A servable claim always has an owning tenant.** Not defended with a fallback: the schema
proves it. `ck_lmm_claims_owner` ties a null owning tenant to a null subject, and
`ck_lmm_claims_unlinked` ties a null subject to `status = 'unlinked'` — which is not one of
the servable statuses. So the assertion here is a statement about the schema, and if it
ever fires the schema changed underneath it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.embedding.targets import TARGET_CLAIM

# The statuses a claim can hold and still be served. Deliberately a separate statement of
# the rule from the read path's `_SERVABLE_AS_OF`, because the two ask different
# questions: this one asks "is this servable now, at this transition", and the read path
# asks "was it servable as of the caller's instant". A conformance test holds them to
# agreeing rather than a shared string pretending they are one rule.
_SERVABLE_STATUSES: Final[tuple[str, ...]] = ("staged", "superseded")

_CLAIM_ROW_SQL = """
SELECT c.claim_id,
       c.owning_tenant_id,
       c.predicate,
       c.value_jsonb AS value,
       c.status,
       c.consolidated_at,
       c.t_invalidated_at
  FROM lmm_claims c
 WHERE c.claim_id = :claim_id
"""


def index_text(predicate: str, value: Any) -> str:
    """What gets embedded for a claim.

    The predicate and the value in words rather than raw JSON. A caller asking "who owns
    the auth service" is writing prose, and matching prose against `{"value":"platform"}`
    puts the burden of speaking JSON on the question.

    The same rule is rendered in SQL by the migration that re-enqueues everything after a
    vector-width change. A conformance test asserts the two produce identical text,
    because one rule in two languages drifts otherwise.
    """
    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return f"{predicate.replace('_', ' ')}: {rendered}"


async def enqueue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    text_to_embed: str,
    chunk_plan: list[dict[str, Any]],
    now: Any,
) -> None:
    """Queue one embedding request, replacing any request already pending for the target.

    An upsert rather than an insert. Without it, five edits to one row before the drain
    ticks queue five requests that each embed a successively staler text, and the drain
    does the work five times to arrive where one request would have put it. Resetting
    `attempts` and clearing the error is right for the same reason: the newest text has
    not failed yet, and inheriting a predecessor's attempt count would dead-letter it
    early.
    """
    await session.execute(
        text(
            "INSERT INTO embedding_outbox "
            "  (outbox_id, tenant_id, target_type, target_id, text_to_embed, "
            "   chunk_plan, enqueued_at, attempts) "
            "VALUES (gen_random_uuid(), :tid, :kind, :target, :body, "
            "        CAST(:plan AS JSONB), :now, 0) "
            "ON CONFLICT (tenant_id, target_type, target_id) DO UPDATE SET "
            "  text_to_embed = EXCLUDED.text_to_embed, "
            "  chunk_plan = EXCLUDED.chunk_plan, "
            "  enqueued_at = EXCLUDED.enqueued_at, "
            "  attempts = 0, "
            "  last_error = NULL, "
            "  last_attempt_at = NULL"
        ),
        {
            "tid": tenant_id,
            "kind": target_type,
            "target": target_id,
            "body": text_to_embed,
            "plan": json.dumps(chunk_plan),
            "now": now,
        },
    )


async def enqueue_many(
    session: AsyncSession,
    *,
    rows: list[dict[str, Any]],
    now: Any,
) -> None:
    """Queue many requests in one statement.

    Exists so the bulk sync path stays set-based. Routing a thousand synced facts through
    `enqueue` one at a time would turn one statement into a thousand round trips, and the
    point of having a single writer is that the policy lives in one place — not that every
    caller has to use the slow shape.

    Each row needs `tenant_id`, `target_type`, `target_id`, `text_to_embed` and
    `chunk_plan`. The conflict clause matches `enqueue`, so a batch containing two rows
    for one target keeps the last.
    """
    if not rows:
        return
    await session.execute(
        text(
            "INSERT INTO embedding_outbox "
            "  (outbox_id, tenant_id, target_type, target_id, text_to_embed, "
            "   chunk_plan, enqueued_at, attempts) "
            "SELECT gen_random_uuid(), "
            "  unnest(CAST(:tids AS uuid[])), "
            "  unnest(CAST(:kinds AS text[])), "
            "  unnest(CAST(:targets AS uuid[])), "
            "  unnest(CAST(:bodies AS text[])), "
            "  unnest(CAST(:plans AS text[]))::jsonb, "
            "  unnest(CAST(:nows AS timestamptz[])), "
            "  0 "
            "ON CONFLICT (tenant_id, target_type, target_id) DO UPDATE SET "
            "  text_to_embed = EXCLUDED.text_to_embed, "
            "  chunk_plan = EXCLUDED.chunk_plan, "
            "  enqueued_at = EXCLUDED.enqueued_at, "
            "  attempts = 0, "
            "  last_error = NULL, "
            "  last_attempt_at = NULL"
        ),
        {
            "tids": [str(r["tenant_id"]) for r in rows],
            "kinds": [str(r["target_type"]) for r in rows],
            "targets": [str(r["target_id"]) for r in rows],
            "bodies": [str(r["text_to_embed"]) for r in rows],
            "plans": [json.dumps(r["chunk_plan"]) for r in rows],
            "nows": [now for _ in rows],
        },
    )


async def retract(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
) -> dict[str, int]:
    """Remove a target's vectors and any pending request for it.

    Returns per-table counts rather than one number, so a caller reporting what it removed
    can be checked against something. Deleting nothing returns zeros, which is what makes
    this safe to call unconditionally.
    """
    vectors = await session.execute(
        text(
            "DELETE FROM embeddings "
            " WHERE tenant_id = :tid AND target_type = :kind AND target_id = :target "
            " RETURNING embedding_id"
        ),
        {"tid": tenant_id, "kind": target_type, "target": target_id},
    )
    queued = await session.execute(
        text(
            "DELETE FROM embedding_outbox "
            " WHERE tenant_id = :tid AND target_type = :kind AND target_id = :target "
            " RETURNING outbox_id"
        ),
        {"tid": tenant_id, "kind": target_type, "target": target_id},
    )
    return {"vectors": len(vectors.fetchall()), "queued": len(queued.fetchall())}


async def project_claim(session: AsyncSession, *, claim_id: uuid.UUID, now: Any) -> bool:
    """Bring the index into line with what the claim currently is.

    Returns True when the claim was queued for embedding, False when it was retracted or
    was never indexable. Called from the two places that change whether a claim is
    servable, and safe to call from anywhere else — it derives everything from the row.
    """
    row = (await session.execute(text(_CLAIM_ROW_SQL), {"claim_id": claim_id})).mappings().first()
    if row is None:
        return False

    servable = (
        row["owning_tenant_id"] is not None
        and row["consolidated_at"] is not None
        and row["status"] in _SERVABLE_STATUSES
        and row["t_invalidated_at"] is None
    )

    if not servable:
        if row["owning_tenant_id"] is not None:
            await retract(
                session,
                tenant_id=row["owning_tenant_id"],
                target_type=TARGET_CLAIM,
                target_id=claim_id,
            )
        return False

    body = index_text(str(row["predicate"]), row["value"])
    await enqueue(
        session,
        tenant_id=row["owning_tenant_id"],
        target_type=TARGET_CLAIM,
        target_id=claim_id,
        text_to_embed=body,
        # One chunk, always. A claim is a single typed assertion: splitting it would make
        # it compete against itself in a ranking, and a claim's fused score would then
        # depend on how many chunks its value happened to tokenise into.
        chunk_plan=[{"index": 0, "text": body, "start": 0, "end": len(body.split())}],
        now=now,
    )
    return True
