"""Seeding the claim store to its stated scale point, fast enough to be a test.

The design point is a million claims in one tenant and ten thousand in one namespace.
Staging them through the write path would take hours and would measure the write path
rather than the read path, so the rows go in through `COPY` with the invariants the write
path enforces satisfied by construction.

**That is a deliberate trade and it has a cost.** These rows are shaped correctly but
were not validated by `ClaimService`, so a seeding bug could produce rows the product
cannot actually create. The mitigation is that the write path is measured separately, on
a small number of claims, through its real interface -- so the two together cover both
"can it write correctly" and "can it read at scale", and neither pretends to cover the
other.
"""

from __future__ import annotations

import datetime
import io
import json
import uuid
from typing import Final

import asyncpg


def b_(text: str) -> bytes:
    """COPY reads bytes. A tiny helper rather than encoding at each call site, so a
    forgotten encode is a type error instead of a runtime one buried in the driver."""
    return text.encode("utf-8")


_NOW: Final[datetime.datetime] = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)

# Spread across the ontology so a predicate filter is selective rather than matching
# everything, which is what makes the index measurement meaningful.
_PREDICATES: Final[tuple[tuple[str, str, str], ...]] = (
    ("owned_by_team", "string", "ownership_stewardship"),
    ("runbook_url", "url", "operational_lifecycle"),
    ("escalation_contact", "string", "ownership_stewardship"),
    ("interface_specification_url", "url", "interface_contract"),
    ("deployment_environment", "string", "operational_lifecycle"),
)


async def raw_connection(dsn: str) -> asyncpg.Connection:
    """A driver-level connection, because `COPY` is not worth doing through the ORM."""
    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


async def seed_scale_point(
    conn: asyncpg.Connection,
    *,
    total_claims: int,
    entity_count: int,
    namespace_claims: int,
    namespace: str = "perf/hot",
) -> dict[str, uuid.UUID]:
    """Seed one tenant to the scale point. Returns the ids a test needs to query.

    `entity_count` matters as much as the total: a million claims spread over ten
    subjects would make a subject filter useless and the measurement meaningless,
    because every query would touch a tenth of the table.
    """
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) " "VALUES ($1, $2, $2, $3, TRUE)",
        tenant_id,
        f"perf-{tenant_id.hex[:8]}",
        _NOW,
    )
    await conn.execute(
        "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, actor_kind, "
        "                    created_at) "
        "VALUES ($1, $2, 'perf', $3, 'agent', $4)",
        actor_id,
        tenant_id,
        f"perf-{actor_id.hex[:8]}",
        _NOW,
    )

    entities = [uuid.uuid4() for _ in range(entity_count)]
    entity_rows = io.BytesIO()
    for index, eid in enumerate(entities):
        entity_rows.write(b_(f"{eid}\t{tenant_id}\tcapability\tperf-cap-{index}\tpublic\tt\t{_NOW.isoformat()}\n"))
    entity_rows.seek(0)
    await conn.copy_to_table(
        "entities",
        source=entity_rows,
        columns=["entity_id", "tenant_id", "entity_type", "name", "visibility", "is_active", "created_at"],
    )

    # The subject a test filters on, guaranteed to exist and to hold a known predicate.
    probe_entity = entities[0]

    claim_rows = io.BytesIO()
    inputs = json.dumps({"base": 0.45, "authority": "owner_extraction"}, sort_keys=True)
    for index in range(total_claims):
        claim_id = uuid.uuid4()
        entity = entities[index % entity_count]
        predicate, value_type, category = _PREDICATES[index % len(_PREDICATES)]
        # A namespace on the first slice only, so a prefix filter is selective.
        in_namespace = index < namespace_claims
        ns = namespace if in_namespace else "\\N"
        strategy = "perf-strategy" if in_namespace else "\\N"
        value = json.dumps(f"value-{index}")
        claim_rows.write(
            b_(
                f"{claim_id}\t{tenant_id}\t{tenant_id}\t{actor_id}\t{entity}\t{entity}\t"
                f"{predicate}\t{value_type}\t{category}\t{value}\t{_NOW.isoformat()}\t"
                f"staged\tpublic\towner_extraction\t20\t{_NOW.isoformat()}\t"
                f"{_NOW.isoformat()}\t0.450\t{_NOW.isoformat()}\t{inputs}\tconfidence-v1\t"
                f"uncalibrated\t270.00\tmulti\tf\t{ns}\t{strategy}\n"
            )
        )
    claim_rows.seek(0)
    await conn.copy_to_table(
        "lmm_claims",
        source=claim_rows,
        columns=[
            "claim_id",
            "owning_tenant_id",
            "author_tenant_id",
            "author_actor_id",
            "subject_entity_id",
            "subject_reference",
            "predicate",
            "value_type",
            "claim_category",
            "value_jsonb",
            "asserted_valid_from",
            "status",
            "visibility",
            "source_authority",
            "size_bytes",
            "created_at",
            "consolidated_at",
            "confidence",
            "confidence_scored_at",
            "confidence_inputs",
            "scorer_version",
            "calibration_version",
            "decay_half_life_days",
            "value_cardinality",
            "is_contested",
            "namespace",
            "strategy_id",
        ],
    )

    # Every claim gets provenance, because the write path requires it and a claim
    # without it cannot be served at all -- the serving type refuses to construct one.
    # Seeding only a slice produced rows the product cannot create, which the citation
    # check caught on the first run. It cost about thirty seconds to fix and would have
    # made every read measurement below a measurement of a shape that never occurs.
    prov_rows = io.BytesIO()
    for record in await conn.fetch("SELECT claim_id FROM lmm_claims WHERE owning_tenant_id = $1", tenant_id):
        prov_rows.write(
            b_(
                f"{record['claim_id']}\tsession_event\tperf-event\tperf excerpt\t"
                f"{_NOW.isoformat()}\tinference\tdigest\tsession\n"
            )
        )
    prov_rows.seek(0)
    await conn.copy_to_table(
        "lmm_claim_provenance",
        source=prov_rows,
        columns=[
            "claim_id",
            "evidence_kind",
            "evidence_ref",
            "evidence_excerpt",
            "recorded_at",
            "derivation",
            "independence_key",
            "independence_group",
        ],
    )

    # Planner statistics. Without this the planner has no idea of the table's shape and
    # may sequential-scan a million rows -- which would measure a cold optimiser rather
    # than the index the requirement is about.
    await conn.execute("ANALYZE lmm_claims")
    await conn.execute("ANALYZE lmm_claim_provenance")

    return {"tenant_id": tenant_id, "actor_id": actor_id, "probe_entity": probe_entity}
