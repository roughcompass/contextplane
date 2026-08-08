"""End-to-end proof that retrieval works with no route off the host.

Runs **inside** the container, on a Docker network created with `--internal`:
the container can reach Postgres and nothing else. No default route, no DNS
beyond the network's own resolver, no egress. If any part of the embedding path
still needed to reach a model host, it would fail here.

Not a pytest module — pytest runs on the host, and the whole point is to
exercise the shipped image from the inside. `make test-airgap` builds the image,
stands up the isolated network, and runs this. The filename has no `test_`
prefix so host-side collection ignores it.

Exercises the full round trip, because the individual pieces passing proves less
than the chain working:

    build the embedder from config (loads the baked artifact, no network)
      -> ingest a fact (enqueues to the outbox)
      -> drain (encodes and writes pgvector rows)
      -> semantic search (ANN scan over the HNSW index)

and then asserts the vectors are real. That last check is the one that matters:
the failure this whole change exists to prevent is an app that boots healthy and
serves zero vectors, which looks identical to success from the outside.

Exit 0 and prints `airgap boot check ok`, or exits 1 with the reason.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import socket
import sys
import uuid
from urllib.parse import urlsplit

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.embedding import build_embedder
from registry.embedding.stub import STUB_MODEL_VERSION
from registry.service.catalog.core import CatalogService
from registry.service.catalog.schema import SchemaService
from registry.service.catalog.vocabulary import VocabularyService
from registry.service.retrieval import RetrievalService
from registry.service.retrieval.embedding_drain import drain_outbox
from registry.storage.pg import create_engine, get_session_factory
from registry.types import SystemClock, TemporalFilter, TenantContext

_VOCAB_ROWS = [
    ("entity_type", "capability"),
    ("fact_category", "overview"),
]

# Two facts far enough apart in meaning that a working model has to rank them
# differently for a query aimed at one of them. With zero vectors every distance
# is identical and the ordering is arbitrary.
_TARGET_BODY = "Card payment authorisation, settlement, and chargeback handling for the retail ledger."
_DECOY_BODY = "Nightly rotation of TLS certificates for the ingress load balancer fleet."
_QUERY = "how do we settle credit card transactions"


def _check_provider_construction_makes_no_egress(settings: Settings) -> list[str]:
    """Build the configured extraction provider with the network booby-trapped.

    Two failures this catches that nothing else here would.

    A vendor SDK that phones home at import -- to fetch a token, check a version,
    resolve a region -- turns `import` into a network dependency, and the adapter
    that pulled it in would look fine in every unit test on a machine with
    egress. The imports therefore happen *inside* the guard rather than at module
    top, which is the only way the guard can see them.

    And a provider that resolves or dials during construction would make startup
    depend on reaching a vendor, so a deployment pointing at an internal gateway
    would fail at boot rather than at first use.

    The container has no route regardless, so a real attempt would fail anyway --
    but it would fail as a timeout somewhere deep in a library, or be swallowed
    by an SDK's own try/except and reported as nothing at all. Trapping the
    syscalls turns "it did not happen to need the network" into "it provably did
    not reach for it", and names what did.
    """
    failures: list[str] = []
    db_host = urlsplit(settings.database_url.replace("postgresql+asyncpg://", "https://")).hostname

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    reached_for: list[str] = []

    def _guard_connect(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host != db_host:
            reached_for.append(f"connect({host!r})")
            msg = f"extraction provider construction attempted egress to {host!r}"
            raise AssertionError(msg)
        return real_connect(self, address, *args, **kwargs)  # type: ignore[arg-type]

    def _guard_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host != db_host:
            reached_for.append(f"resolve({host!r})")
            msg = f"extraction provider construction attempted to resolve {host!r}"
            raise AssertionError(msg)
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    socket.socket.connect = _guard_connect  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = _guard_getaddrinfo  # type: ignore[assignment]
    try:
        # Imported here, under the guard, so import-time egress is caught too.
        from registry.extraction.factory import build_provider

        provider = build_provider(settings)
        print(f"  extraction provider {provider.provider_id!r} constructed with no egress")
    except AssertionError as exc:
        failures.append(str(exc))
    except Exception as exc:
        failures.append(f"building extraction provider {settings.extraction_provider!r} failed: {exc!r}")
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo

    if reached_for:
        failures.append(f"provider construction reached for the network: {', '.join(reached_for)}")
    return failures


async def _seed_tenant(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_async_engine(database_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": f"airgap-{actor_id.hex[:8]}", "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, 'airgap', :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": f"sub-{actor_id.hex[:8]}", "now": now},
            )
            for kind, value in _VOCAB_ROWS:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE)"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _run() -> list[str]:
    """Return a list of failures; empty means the check passed."""
    failures: list[str] = []
    database_url = os.environ["DATABASE_URL"]
    settings = Settings(
        database_url=database_url,
        pgbouncer_url=database_url,
        scheduler_jobstore_url=database_url,
    )

    print(f"  provider={settings.embedding_provider} path={settings.embedding_model_path}")
    print(f"  extraction_provider={settings.extraction_provider}")

    # 0. Build the configured extraction provider. A new adapter is exactly the
    #    change that quietly adds a vendor SDK or an import-time network call,
    #    and neither shows up in a unit test on a machine with egress.
    failures.extend(_check_provider_construction_makes_no_egress(settings))
    if failures:
        return failures

    # 1. Load the model. Nothing may reach the network here.
    embedder = build_embedder(settings)
    print(f"  embedder={type(embedder).__name__} model_id={embedder.model_version}")
    if embedder.model_version == STUB_MODEL_VERSION:
        failures.append("fell back to the stub embedder — the artifact did not load")
        return failures

    tenant_id, actor_id = await _seed_tenant(database_url)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    try:
        clock = SystemClock()
        catalog = CatalogService(
            session_factory,
            clock,
            VocabularyService(session_factory),
            SchemaService(session_factory, clock),
        )

        # 2. Ingest. Each create_fact enqueues to the embedding outbox.
        for name, body in (("payments-svc", _TARGET_BODY), ("cert-rotator", _DECOY_BODY)):
            entity = await catalog.create_entity(ctx, "capability", name)
            await catalog.create_fact(ctx, entity_id=entity.entity_id, category="overview", body=body)

        # 3. Drain — encodes and writes pgvector rows.
        await drain_outbox(session_factory, embedder, settings)

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text("SELECT model_id, vector FROM embeddings WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            ).all()

        if not rows:
            failures.append("the drain wrote no embedding rows")
            return failures
        print(f"  wrote {len(rows)} embedding row(s)")

        # 4. The vectors have to be real. An all-zero vector is what a silently
        #    degraded embedder produces, and it is indistinguishable from a good
        #    one at every layer above this.
        for model_id, raw in rows:
            vector = np.array([float(v) for v in str(raw).strip("[]").split(",")])
            if not np.any(vector):
                failures.append(f"stored an all-zero vector under model_id={model_id!r}")
                break
            if abs(float(np.linalg.norm(vector)) - 1.0) > 1e-3:
                failures.append(f"vector is not unit length (norm {np.linalg.norm(vector):.4f})")
                break
            if model_id != settings.embedding_model:
                failures.append(f"model_id is {model_id!r}, expected {settings.embedding_model!r}")
                break

        # 5. Semantic search, on its own — not fused. search() tolerates a failed
        #    arm by redistributing weight to the lexical and graph arms, so going
        #    through it could pass while the ANN scan was raising every time.
        retrieval = RetrievalService(session_factory, clock, embedder, settings)
        results = await retrieval._semantic_arm(ctx, _QUERY, 10, TemporalFilter(as_of=None), None)
        if not results:
            failures.append("the semantic arm returned nothing")
            return failures

        top_name = results[0][1].name
        print(f"  semantic arm returned {len(results)} result(s), top={top_name!r}")
        if top_name != "payments-svc":
            failures.append(f"ranked {top_name!r} above 'payments-svc' for a payments query")
    finally:
        await engine.dispose()

    return failures


def main() -> int:
    print("airgap boot check")
    try:
        failures = asyncio.run(_run())
    except Exception as exc:
        print(f"airgap boot check FAILED: {exc!r}", file=sys.stderr)
        return 1

    if failures:
        print("airgap boot check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("airgap boot check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
