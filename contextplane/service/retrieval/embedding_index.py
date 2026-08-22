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
proves it. `ck_memory_claims_owner` ties a null owning tenant to a null subject, and
`ck_memory_claims_unlinked` ties a null subject to `status = 'unlinked'` — which is not one of
the servable statuses. So the assertion here is a statement about the schema, and if it
ever fires the schema changed underneath it.

**The index registers what it writes.** An artefact nobody registered is an artefact no
erasure reaches and no expiry sweeps, and a vector is the densest surviving copy of a
person's own words — `text_chunk` holds the source text verbatim and the row's `ts_vector`
is generated from it. So the single writer is also the registrar: the addressing below is
this store's own, the registration is written in the same transaction as the artefact, and
`derivative_handlers.py` parses that addressing back when the propagation drain applies the
removal. Registration on the *enqueue* side matters as much as on the write side — a
pending outbox row carries `text_to_embed` verbatim, so a request queued and never drained
is a copy nothing would otherwise reach.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.embedding.targets import TARGET_CLAIM
from contextplane.retention import derivatives, policies
from contextplane.types import JSONValue, TenantContext

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
       c.quarantined_at,
       c.t_invalidated_at,
       c.created_at
  FROM memory_claims c
 WHERE c.claim_id = :claim_id
"""

# --- how this store addresses its artefacts in a derivative registration ----

#: The scheme every locator this module writes begins with. A registration's
#: locator is opaque to the registry and meaningful only to the handler that
#: parses it, so the store that writes the artefact owns the addressing.
_LOCATOR_SCHEME: Final[str] = "embeddings"

#: One locator covers a target's whole embedding footprint: every `embeddings`
#: row for it (the vector, the verbatim `text_chunk`, and the `ts_vector`
#: generated from that chunk), plus any pending or dead-lettered request in
#: `embedding_outbox`/`embedding_outbox_failed` still carrying the same text.
#: They are one artefact through one lifecycle, not three that can be removed
#: separately, which is why one registration describes all of it.
_LOCATOR_PARTS: Final[int] = 3

#: Who the artefact was built for. A vector is served to everyone in the tenant
#: and to nobody outside it, so the tenant is the whole audience — spelled as a
#: constant rather than the tenant id, which is already its own column and would
#: only make the uniqueness key say the same thing twice.
_ARTEFACT_AUDIENCE: Final[str] = "tenant"

#: `text_chunk` is the source text verbatim, including whatever a claim quoted
#: from a signal or a session, so the artefact is classified by what it holds
#: rather than by the derived-data label its kind might suggest.
_ARTEFACT_CLASSIFICATION: Final[str] = "confidential"

#: Recorded on every registration this module writes, so an artefact built by a
#: registrar that has since changed can be identified rather than assumed
#: rebuildable. Bump it when the locator scheme or the removal semantics change.
ARTEFACT_HANDLER_VERSION: Final[str] = "embeddings-1"


def artefact_locator(target_type: str, target_id: uuid.UUID) -> str:
    """Address one target's embedding artefacts for a derivative registration."""
    return f"{_LOCATOR_SCHEME}/{target_type}/{target_id}"


def parse_artefact_locator(locator: str) -> tuple[str, uuid.UUID] | None:
    """Read a locator back, or None when it does not address this store.

    Total rather than raising: the caller is a propagation handler deciding
    whether a work item is even its own, and it is the one that owes the refusal
    a message naming what it was handed.
    """
    parts = locator.split("/")
    if len(parts) != _LOCATOR_PARTS or parts[0] != _LOCATOR_SCHEME:
        return None
    try:
        target_id = uuid.UUID(parts[2])
    except ValueError:
        return None
    return parts[1], target_id


async def claim_registration_anchor(session: AsyncSession, claim_id: uuid.UUID) -> datetime.datetime | None:
    """When the claim's own content clock started, or None if the claim is gone.

    The claim's creation instant rather than the moment the vector is built. A
    derivative must not outlive its source, and anchoring on "now" would push the
    artefact's expiry past the source's by however old the claim already was —
    the one direction that leaves content readable after the record it came from
    was reduced.
    """
    row = (
        await session.execute(text("SELECT created_at FROM memory_claims WHERE claim_id = :cid"), {"cid": claim_id})
    ).first()
    if row is None:
        return None
    anchor: datetime.datetime = row[0]
    return anchor


async def register_claim_artefact(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    claim_id: uuid.UUID,
    anchor: datetime.datetime,
) -> uuid.UUID:
    """Register a claim's embedding artefacts against the claim they were built from.

    Runs in the caller's transaction, always: a registration that committed
    separately from the artefact would describe something that does not exist, or
    — the direction that matters — the artefact would exist for a window in which
    nothing knew to erase it.

    The expiry is the claim's *payload* clock, not the claim's own life. A claim
    is retained for the life of its tenant while the excerpt it quotes reduces on
    the shorter clock, and the embedded text is that excerpt.

    Only claims are registered here. A fact has no declared record class — the
    retention policy covers the twelve classes a person's records fall into, and
    a capability fact is not one of them — so a registration for a fact would have
    to invent the disposition its expiry is computed from. Fact vectors are
    reached by the actor-scoped erasure participant instead.
    """
    return await derivatives.register_derivative(
        session,
        tenant_id=tenant_id,
        kind=derivatives.KIND_VECTOR,
        storage_locator=artefact_locator(TARGET_CLAIM, claim_id),
        audience_partition=_ARTEFACT_AUDIENCE,
        classification=_ARTEFACT_CLASSIFICATION,
        handler_version=ARTEFACT_HANDLER_VERSION,
        sources=[
            derivatives.SourceRef(
                record_class=policies.RECORD_MEMORY_CLAIM,
                source_id=claim_id,
                expires_at=policies.payload_deadline(policies.RECORD_MEMORY_CLAIM, anchor),
            )
        ],
    )


def index_text(predicate: str, value: JSONValue) -> str:
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
    now: datetime.datetime,
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
    now: datetime.datetime,
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


async def project_claim(session: AsyncSession, *, claim_id: uuid.UUID, now: datetime.datetime) -> bool:
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
        and row["quarantined_at"] is None
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
    # The queued row holds the claim's text verbatim, so it is registered the moment
    # it exists rather than when the drain turns it into a vector. A request enqueued
    # and never drained would otherwise be a copy of the claim that no erasure and no
    # expiry could find.
    await register_claim_artefact(
        session,
        tenant_id=row["owning_tenant_id"],
        claim_id=claim_id,
        anchor=row["created_at"],
    )
    return True


class EmbeddingIndex:
    """The erasure-facing surface over the index.

    A class rather than a bare function because the erasure registry takes participants
    that hold their own dependencies, and because this is the only operation here that
    needs its own session rather than joining a caller's transaction.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Delete every vector and queued request derived from this actor's work.

        Closes a real hole rather than tidying one. Nothing in the product deleted from
        `embeddings` before this: a right-to-be-forgotten request reported success while
        the erased person's fact bodies and claim values sat in `text_chunk`, verbatim and
        still returned by the semantic arm.

        Scoped by tenant as well as actor. The actor id alone would find the rows; the
        tenant predicate is there so a request made in one tenant's context cannot reach
        another's, matching how session-memory erasure is scoped.

        Per-table counts rather than one number, for the reason the memory eraser gives:
        a receipt that says "12" cannot be checked against anything. Idempotent -- a
        second call deletes nothing and returns zeros.
        """
        async with self._factory() as session, session.begin():
            counts: dict[str, int] = {}
            for key, table, id_column in (
                ("fact_vectors", "embeddings", "embedding_id"),
                ("queued", "embedding_outbox", "outbox_id"),
                ("dead_lettered", "embedding_outbox_failed", "failed_id"),
            ):
                # Facts and claims are resolved separately because the actor column
                # differs: a fact records who created it, a claim records who authored it.
                result = await session.execute(
                    text(
                        f"DELETE FROM {table} "  # noqa: S608 - table/id_column iterate over the fixed 3-tuple literal above, not caller input; actual values are bound via :tid/:actor
                        " WHERE tenant_id = :tid "
                        "   AND ( (target_type = 'fact' AND target_id IN ("
                        "             SELECT fact_id FROM facts "
                        "              WHERE tenant_id = :tid AND created_by = :actor))"
                        "      OR (target_type = 'claim' AND target_id IN ("
                        "             SELECT claim_id FROM memory_claims "
                        "              WHERE author_actor_id = :actor)) ) "
                        f" RETURNING {id_column}"
                    ),
                    {"tid": ctx.tenant_id, "actor": target_actor_id},
                )
                counts[key] = len(result.fetchall())
            # The first key covers both kinds; name it for what it is now that it is
            # measured rather than for the kind it started with.
            counts["vectors"] = counts.pop("fact_vectors")
            return counts


# The tenant predicate is optional and defaults to "every tenant", because the exported
# gauge is a deployment-wide number -- a per-tenant label on a gauge grows without bound
# as tenants are added. Callers that want one tenant's coverage pass it explicitly.
_COVERAGE_SQL = """
SELECT 'fact' AS target_type,
       count(*) AS indexable,
       count(e.target_id) AS indexed
  FROM facts f
  LEFT JOIN (
       SELECT DISTINCT target_id FROM embeddings
        WHERE target_type = 'fact' AND model_id = :model
  ) e ON e.target_id = f.fact_id
 WHERE f.t_invalidated_at IS NULL
   AND (CAST(:tenant AS UUID) IS NULL OR f.tenant_id = CAST(:tenant AS UUID))
UNION ALL
SELECT 'claim' AS target_type,
       count(*) AS indexable,
       count(e.target_id) AS indexed
  FROM memory_claims c
  LEFT JOIN (
       SELECT DISTINCT target_id FROM embeddings
        WHERE target_type = 'claim' AND model_id = :model
  ) e ON e.target_id = c.claim_id
 WHERE c.consolidated_at IS NOT NULL
   AND c.status IN ('staged', 'superseded')
   AND c.t_invalidated_at IS NULL
   AND (CAST(:tenant AS UUID) IS NULL OR c.owning_tenant_id = CAST(:tenant AS UUID))
"""


async def index_coverage(
    factory: async_sessionmaker[AsyncSession], model_id: str, *, tenant_id: uuid.UUID | None = None
) -> dict[str, float]:
    """What fraction of each kind's indexable rows actually holds a vector.

    The number a memory steward is accountable for. Everything else the pipeline reports
    describes work in flight; this one describes whether the index reflects the store, and
    it is the number that would have shown an empty claim index on a dashboard instead of
    leaving it to be discovered by reading code.

    Scoped to one model, because a vector produced by a different model does not make a
    row retrievable by the running one.

    An empty store reports full coverage rather than zero: a fresh deployment and a broken
    pipeline should not look the same, and "nothing to index, all of it indexed" is the
    truthful reading of zero rows.
    """
    async with factory() as session:
        rows = (await session.execute(text(_COVERAGE_SQL), {"model": model_id, "tenant": tenant_id})).mappings().all()
    coverage: dict[str, float] = {}
    for row in rows:
        indexable = int(row["indexable"])
        coverage[str(row["target_type"])] = 1.0 if indexable == 0 else int(row["indexed"]) / indexable
    return coverage


#: The three tables one target's text can be sitting in, each spelled out in full
#: rather than assembled from a shared tail: these are erasure statements, and a
#: reader checking that the predicate really is what it looks like should not have
#: to reconstruct it from two places.
#:
#: A NULL `:tid` means "every tenant", which is what an actor-scoped erasure has
#: already resolved by selecting the target ids; a caller that knows the tenant
#: passes it and gets partition pruning as well as a predicate that cannot reach
#: another tenant's rows.
_DELETE_VECTORS = (
    "DELETE FROM embeddings WHERE target_type = :kind AND target_id = ANY(:ids) "
    "  AND (CAST(:tid AS UUID) IS NULL OR tenant_id = CAST(:tid AS UUID))"
)
_DELETE_QUEUED = (
    "DELETE FROM embedding_outbox WHERE target_type = :kind AND target_id = ANY(:ids) "
    "  AND (CAST(:tid AS UUID) IS NULL OR tenant_id = CAST(:tid AS UUID))"
)
_DELETE_DEAD_LETTERED = (
    "DELETE FROM embedding_outbox_failed WHERE target_type = :kind AND target_id = ANY(:ids) "
    "  AND (CAST(:tid AS UUID) IS NULL OR tenant_id = CAST(:tid AS UUID))"
)


async def erase_targets(
    session: AsyncSession,
    *,
    target_type: str,
    target_ids: list[uuid.UUID],
    tenant_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Physically delete every vector and queued embedding for these targets.

    The erasure counterpart to the single-writer discipline this module
    enforces: `text_chunk` holds the source text verbatim, so an erasure that
    removed the source rows but left the vectors would keep the erased
    person's words searchable through the semantic arm. Runs in the caller's
    transaction — erasure is all-or-nothing with the source-row deletes, and a
    partial commit would orphan vectors a retry can no longer find.

    Dead-lettered requests go with the rest. A row that failed to embed still
    holds the text it failed to embed, and a queue nobody drains is exactly
    where a copy survives unnoticed.
    """
    if not target_ids:
        return {"embeddings": 0, "outbox_rows": 0}
    params: dict[str, Any] = {"kind": target_type, "ids": target_ids, "tid": tenant_id}
    embeddings = await session.execute(text(_DELETE_VECTORS), params)
    outbox = await session.execute(text(_DELETE_QUEUED), params)
    failed = await session.execute(text(_DELETE_DEAD_LETTERED), params)
    return {
        "embeddings": embeddings.rowcount or 0,  # type: ignore[attr-defined]
        "outbox_rows": (outbox.rowcount or 0) + (failed.rowcount or 0),  # type: ignore[attr-defined]
    }
