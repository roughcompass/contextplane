"""The rows a governed relationship assertion writes, and the row a supersession ends.

Split out of `service.py` rather than living beside the checks because the two
answer different questions. `service.py` decides *whether* an assertion may be
written — the binding, the definition, the endpoints, the aggregate rules under
the lock. This module knows only *what* is written once that has been decided,
which is four rows that share one transaction: the edge, its provenance, its
governed metadata, its audit entry, plus the closure-cache outbox entry that
tells the derived graph an edge moved.

Every function here takes the caller's session and writes on it. None opens a
transaction, none commits, and none catches: a provenance row that survived a
rolled-back edge would describe an assertion that does not exist, and an outbox
entry that did not would leave the closure cache permanently wrong with nothing
recording that it is.

The refusal codes and the ordering discipline stay in `service.py`. What is here
is deliberately dumb.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.entities import assertions
from contextplane.relationships.definitions import RelationshipConstraints
from contextplane.relationships.refusals import (
    SUPERSEDED_IDENTITY_DIFFERS,
    SUPERSEDED_NOT_IN_FORCE,
    RelationshipWriteRefused,
)


@dataclasses.dataclass(frozen=True)
class Binding:
    """The active profile binding every governed row names."""

    binding_id: uuid.UUID
    profile_revision_id: uuid.UUID
    extension_set_digest: str


async def write_edge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relationship_type: str,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    started_at: datetime.datetime,
    now: datetime.datetime,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """The physical edge. The governed row shares its id rather than minting one."""
    edge_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO edges (edge_id, tenant_id, src_entity_id, rel, dst_entity_id,"
            "                   t_valid_from, t_valid_to, t_ingested_at, created_by)"
            " VALUES (:eid, :tid, :src, :rel, :dst, :vf, NULL, :now, :actor)"
        ),
        {
            "eid": edge_id,
            "tid": tenant_id,
            "src": source_entity_id,
            "rel": relationship_type,
            "dst": destination_entity_id,
            "vf": started_at,
            "now": now,
            "actor": actor_id,
        },
    )
    return edge_id


async def write_provenance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    constraints: RelationshipConstraints,
    binding: Binding,
    now: datetime.datetime,
    produced_by: str,
    source_system: str,
    source_namespace: str,
) -> uuid.UUID:
    """The record of who asserted this and which revision validated it.

    Routed through `contextplane.entities.assertions`, which is the one module
    permitted to write `assertion_provenance` — a second writer could produce
    evidence that says something the assertion path never observed, and the
    assertion itself would look untouched.

    `authority` comes from the definition rather than the caller: how much
    weight an assertion carries is a property of the relationship type, and a
    caller that could name its own authority could promote its own guesses.
    """
    return await assertions.record(
        session,
        assertions.for_governed_write(
            tenant_id=tenant_id,
            validating_profile_revision_id=binding.profile_revision_id,
            authority=constraints.authority,
            ingested_at=now,
            produced_by=produced_by,
            source_system=source_system,
            source_namespace=source_namespace,
            extension_set_digest=binding.extension_set_digest,
        ),
    )


async def write_metadata(
    session: AsyncSession,
    *,
    edge_id: uuid.UUID,
    tenant_id: uuid.UUID,
    definition_id: uuid.UUID,
    constraints: RelationshipConstraints,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    properties: Mapping[str, Any],
    started_at: datetime.datetime,
    state: str,
    provenance_id: uuid.UUID,
    binding: Binding,
    now: datetime.datetime,
) -> None:
    """The governed row. `relationship_type` and `cardinality_scope` are copied
    rather than read through the definition, so a later definition change shows
    up as a difference instead of rewriting what this assertion meant."""
    await session.execute(
        text(
            "INSERT INTO relationship_metadata ("
            "  relationship_id, tenant_id, relationship_type_definition_id,"
            "  source_entity_id, destination_entity_id, relationship_type, cardinality_scope,"
            "  properties, effective_from, effective_to, readiness_state,"
            "  provenance_id, profile_binding_id, recorded_at"
            ") VALUES (:rid, :tid, :did, :src, :dst, :rtype, :scope,"
            "          CAST(:props AS JSONB), :vf, NULL, :state, :pid, :bid, :now)"
        ),
        {
            "rid": edge_id,
            "tid": tenant_id,
            "did": definition_id,
            "src": source_entity_id,
            "dst": destination_entity_id,
            "rtype": constraints.relationship_type,
            "scope": constraints.cardinality_scope,
            "props": json.dumps(dict(properties), sort_keys=True, separators=(",", ":")),
            "vf": started_at,
            "state": state,
            "pid": provenance_id,
            "bid": binding.binding_id,
            "now": now,
        },
    )


async def close_superseded(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relationship_id: uuid.UUID,
    constraints: RelationshipConstraints,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    at: datetime.datetime,
) -> None:
    """End the named row at `at`, having checked it is the row the body describes.

    Read and closed under the lock the caller already holds, and in that
    order: a row read before the lock could be superseded by a concurrent
    writer between the read and the close, and the second close would end an
    interval that had already ended.

    The identity check is against the row, not against the type definition.
    A body naming a different type or different endpoints is refused rather
    than applied — see `supersede_relationship` for why moving an edge is two
    acts rather than one.
    """
    row = (
        await session.execute(
            text(
                "SELECT relationship_type, source_entity_id, destination_entity_id"
                "  FROM relationship_metadata"
                " WHERE tenant_id = :tenant AND relationship_id = :rid"
                "   AND effective_from <= :at AND (effective_to IS NULL OR effective_to > :at)"
                "   FOR UPDATE"
            ),
            {"tenant": tenant_id, "rid": relationship_id, "at": at},
        )
    ).first()

    if row is None:
        raise RelationshipWriteRefused(
            SUPERSEDED_NOT_IN_FORCE,
            (
                f"no assertion {relationship_id} is in force at {at.isoformat()}; a supersession has to name a "
                "row that is currently in force, and one already ended cannot be ended again"
            ),
        )

    held_type, held_source, held_destination = row
    if (
        held_type != constraints.relationship_type
        or held_source != source_entity_id
        or held_destination != destination_entity_id
    ):
        raise RelationshipWriteRefused(
            SUPERSEDED_IDENTITY_DIFFERS,
            (
                f"{relationship_id} holds {held_type!r} from {held_source} to {held_destination}, and the body "
                f"describes {constraints.relationship_type!r} from {source_entity_id} to {destination_entity_id}; "
                "type and endpoints are the assertion's identity, so changing them is retiring one edge and "
                "asserting another rather than amending this one"
            ),
        )

    await session.execute(
        text(
            "UPDATE relationship_metadata SET effective_to = :at"
            " WHERE tenant_id = :tenant AND relationship_id = :rid"
        ),
        {"tenant": tenant_id, "rid": relationship_id, "at": at},
    )
    await session.execute(
        text("UPDATE edges SET t_valid_to = :at WHERE tenant_id = :tenant AND edge_id = :rid"),
        {"tenant": tenant_id, "rid": relationship_id, "at": at},
    )


async def enqueue_closure(
    session: AsyncSession, *, tenant_id: uuid.UUID, edge_id: uuid.UUID, now: datetime.datetime
) -> None:
    """Tell the closure cache an edge moved, in this transaction.

    Enqueued rather than refreshed inline because the refresh walks a graph
    this transaction is still changing; enqueued *here* rather than after the
    commit because a queue write that can be lost is a cache that can be
    permanently wrong with nothing recording that it is.
    """
    await session.execute(
        text(
            "INSERT INTO closure_outbox (outbox_id, tenant_id, edge_id, enqueued_at)" " VALUES (:oid, :tid, :eid, :now)"
        ),
        {"oid": uuid.uuid4(), "tid": tenant_id, "eid": edge_id, "now": now},
    )


async def write_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    edge_id: uuid.UUID,
    constraints: RelationshipConstraints,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    state: str,
    now: datetime.datetime,
    action: str,
) -> None:
    """The audit row, on this session rather than through the fire-and-forget emitter.

    `contextplane.audit.emit` writes in its own transaction and swallows its
    own failures, which is right for a request that has already happened and
    wrong here: a governed assertion and the record that it was made have to
    share one fate, or the graph can hold rows no audit trail admits to.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log (audit_id, tenant_id, actor_id, action, target_type, target_id,"
            "                       after_jsonb, ts)"
            " VALUES (:aid, :tid, :actor, :action, 'relationship', :target, CAST(:after AS JSONB), :now)"
        ),
        {
            "aid": uuid.uuid4(),
            "tid": tenant_id,
            "actor": actor_id,
            "action": action,
            "target": edge_id,
            "after": json.dumps(
                {
                    "relationship_type": constraints.relationship_type,
                    "source_entity_id": str(source_entity_id),
                    "destination_entity_id": str(destination_entity_id),
                    "readiness_state": state,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "now": now,
        },
    )


__all__ = [
    "Binding",
    "close_superseded",
    "enqueue_closure",
    "write_audit",
    "write_edge",
    "write_metadata",
    "write_provenance",
]
