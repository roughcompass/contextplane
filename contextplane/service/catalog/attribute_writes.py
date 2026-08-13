"""The one path that writes a promoted claim into the canonical graph.

A promotion's canonical write looks like an ordinary catalog write -- a row
lands in ``attributes`` or ``edges`` either way -- but it is answering a
different question: not "what does this entity's owner declare", but "does
what a claim asserted still hold, checked against the same ontology that
accepted it when the claim was staged". That check has to run again here, not
only at staging time, because the vocabulary can change in between. A
predicate valid when a claim was staged can be deprecated by the time somebody
reviews and accepts the proposal it produced, and nothing upstream of this
module re-reads the vocabulary to notice -- so without this check here, a
claim staged against a since-deprecated predicate would land in the canonical
graph unchecked.

**Every write closes what it replaces.** A promotion never leaves two live
rows for one key or one relationship; the row it supersedes is closed to the
moment the new one starts, and its id is handed back so the caller's reversal
journal can find and restore it later. Reversal is what makes an
auto-promotion path defensible at all, and reversal only works if every write
here remembers what it overwrote.

**The caller owns the transaction.** Both functions run inside whatever
transaction the caller already opened -- the same shape as ``ClaimService``'s
own ``stage_confirmation``/``merge_provenance`` -- so a promotion's canonical
write, its journal row, its audit row, and its claim-state update commit
together or not at all.
"""

from __future__ import annotations

import datetime
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.entities.validation import validate_entity_write
from contextplane.exceptions import ValidationError
from contextplane.storage.models import CLAIM_PREDICATE_KIND
from contextplane.types import JSONValue


async def _assert_current(session: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> None:
    """The same check staging ran, run again at the moment of the canonical write.

    Mirrors ``ClaimService._resolve_predicate``'s query: a global vocabulary
    entry wins over a tenant's own, and a deprecated entry refuses regardless of
    which one matched. Kept as its own query rather than reused from claims.py
    because this module has no `TenantContext` to hand it -- only the resolved
    tenant id a proposal already carries.
    """
    row = (
        await session.execute(
            text(
                "SELECT deprecated_at FROM vocabulary_values "
                "WHERE kind = :kind AND value = :value "
                "  AND (tenant_id IS NULL OR tenant_id = :tid) "
                "ORDER BY tenant_id NULLS FIRST LIMIT 1"
            ),
            {"kind": CLAIM_PREDICATE_KIND, "value": key, "tid": tenant_id},
        )
    ).one_or_none()
    if row is None:
        raise ValidationError(f"{key!r} is not in the ontology and may not be written to the canonical graph")
    if row.deprecated_at is not None:
        raise ValidationError(f"{key!r} is deprecated and accepts no new canonical writes")


async def _assert_profile_permits(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    key: str,
    value: JSONValue,
) -> None:
    """Check one promoted attribute against the profile the tenant is bound to.

    A promotion is the path that most needs this and least looks like it. The
    vocabulary check above asks whether the *predicate* is still current; it says
    nothing about whether the entity's type declares that property, so a claim
    about a property the profile never granted lands in the canonical graph
    looking exactly like one that was granted.

    The entity's type is read here rather than passed in because a promotion
    carries a claim's subject id and nothing else -- the caller genuinely does
    not know the type. This is the same single-row lookup ``write_edge`` already
    does for the destination's tenant, on a row this function is about to write
    to and whose tenant it re-checks.
    """
    row = (
        await session.execute(
            text("SELECT entity_type, tenant_id FROM entities WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
    ).first()
    if row is None:
        raise ValidationError("the attribute's entity does not exist")
    if row[1] != tenant_id:
        raise PermissionError("an attribute may not be written across a tenant boundary")

    result = await validate_entity_write(session, tenant_id=tenant_id, entity_type=row[0], attributes={key: value})
    # Only the supplied key is being written, so a required property missing from
    # this one-key view is not this write's problem -- a promotion writes one
    # attribute at a time and cannot be asked to complete the entity.
    relevant = [v for v in result.violations if v.code != "missing_required_property"]
    if relevant and result.enforced:
        detail = "; ".join(f"{v.code}: {v.detail}" for v in relevant)
        raise ValidationError(f"the promoted attribute violates the profile this tenant is bound to: {detail}")


async def write_attribute(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    key: str,
    value: JSONValue,
    valid_from: datetime.datetime,
    valid_to: datetime.datetime | None,
    actor_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
    """Write one canonical attribute row, closing whatever it replaces.

    Returns ``(new_row_id, superseded_row_id, superseded_valid_to)`` -- the
    triple a promotion's reversal journal needs to find and restore the row
    this write displaced.

    ``now`` defaults to ``valid_from`` when omitted: the two coincide for an
    ordinary promotion (asserted now, written now), and a caller promoting a
    backdated interval can still pass the real moment of write separately so
    ``t_ingested_at`` keeps recording when the row actually entered the graph.
    """
    await _assert_current(session, tenant_id=tenant_id, key=key)
    await _assert_profile_permits(session, tenant_id=tenant_id, entity_id=entity_id, key=key, value=value)

    prior = (
        (
            await session.execute(
                text(
                    "SELECT attr_id, t_valid_to FROM attributes "
                    " WHERE entity_id = :eid AND key = CAST(:key AS TEXT) "
                    "   AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1 FOR UPDATE"
                ),
                {"eid": entity_id, "key": key},
            )
        )
        .mappings()
        .first()
    )

    superseded_id: uuid.UUID | None = None
    superseded_valid_to: datetime.datetime | None = None
    if prior is not None:
        superseded_id = prior["attr_id"]
        superseded_valid_to = prior["t_valid_to"]
        await session.execute(
            text("UPDATE attributes SET t_valid_to = :vf WHERE attr_id = :aid"),
            {"vf": valid_from, "aid": superseded_id},
        )

    attr_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO attributes "
            "  (attr_id, tenant_id, entity_id, key, value, t_valid_from, "
            "   t_valid_to, t_ingested_at, created_by) "
            "VALUES (:aid, :tid, :eid, :key, CAST(:val AS JSONB), :vf, :vt, :now, :actor)"
        ),
        {
            "aid": attr_id,
            "tid": tenant_id,
            "eid": entity_id,
            "key": key,
            "val": json.dumps(value),
            "vf": valid_from,
            "vt": valid_to,
            "now": now if now is not None else valid_from,
            "actor": actor_id,
        },
    )
    return attr_id, superseded_id, superseded_valid_to


async def write_edge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    src_entity_id: uuid.UUID,
    rel: str,
    dst_entity_id: uuid.UUID,
    valid_from: datetime.datetime,
    valid_to: datetime.datetime | None,
    actor_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None, datetime.datetime | None]:
    """Write one canonical edge row, closing whatever it replaces.

    Refuses a destination that does not exist, and refuses one that belongs to
    another tenant: the tenant that accepted this write consented to a claim
    about their own capability, and cannot consent on behalf of the tenant at
    the other end of the edge.

    Returns the same ``(new_row_id, superseded_row_id, superseded_valid_to)``
    triple ``write_attribute`` does. ``now`` behaves the same way too.
    """
    await _assert_current(session, tenant_id=tenant_id, key=rel)

    dst_tenant = (
        await session.execute(text("SELECT tenant_id FROM entities WHERE entity_id = :eid"), {"eid": dst_entity_id})
    ).scalar_one_or_none()
    if dst_tenant is None:
        raise ValidationError("the edge destination does not exist")
    if dst_tenant != tenant_id:
        raise PermissionError("an edge may not be written across a tenant boundary")

    prior = (
        (
            await session.execute(
                text(
                    "SELECT edge_id, t_valid_to FROM edges "
                    " WHERE src_entity_id = :eid AND rel = CAST(:rel AS TEXT) "
                    "   AND dst_entity_id = :dst AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1 FOR UPDATE"
                ),
                {"eid": src_entity_id, "rel": rel, "dst": dst_entity_id},
            )
        )
        .mappings()
        .first()
    )

    superseded_id: uuid.UUID | None = None
    superseded_valid_to: datetime.datetime | None = None
    if prior is not None:
        superseded_id = prior["edge_id"]
        superseded_valid_to = prior["t_valid_to"]
        await session.execute(
            text("UPDATE edges SET t_valid_to = :vf WHERE edge_id = :eid"),
            {"vf": valid_from, "eid": superseded_id},
        )

    edge_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO edges "
            "  (edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
            "   t_valid_from, t_valid_to, t_ingested_at, created_by) "
            "VALUES (:eid, :tid, :src, :rel, :dst, :vf, :vt, :now, :actor)"
        ),
        {
            "eid": edge_id,
            "tid": tenant_id,
            "src": src_entity_id,
            "rel": rel,
            "dst": dst_entity_id,
            "vf": valid_from,
            "vt": valid_to,
            "now": now if now is not None else valid_from,
            "actor": actor_id,
        },
    )
    return edge_id, superseded_id, superseded_valid_to


__all__ = ["write_attribute", "write_edge"]
