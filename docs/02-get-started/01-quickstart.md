# Quickstart

Get from zero to an authenticated API call in under five minutes.

**Prerequisites:** Python 3.13, `make`, `curl`, and a PostgreSQL 16 with pgvector. No container runtime is needed — see [where Postgres comes from](../../.develop/local-dev.md#where-postgres-comes-from) for the options, the simplest being `pip install -e ".[devstack]"`, which brings its own.

Prefer containers? [Use Docker Compose instead](#alternative--docker-compose) — same ports, same commands from Step 2 onward.

---

## Step 1 — Clone and start

```bash
git clone <repo-url>
cd contextplane
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,devstack]"
make dev-up
```

`make dev-up` starts every dependency, applies migrations, and waits for each service to report healthy. First run initialises the database and takes 30–60 seconds; later runs are quicker.

Verify it started:

```bash
curl http://localhost:8000/healthz
```

Expected output:

```json
{"status":"ok"}
```

What is now running:

| Service | Port | URL |
|---|---|---|
| Context Plane API | 8000 | http://localhost:8000 |
| Swagger UI | 8000 | http://localhost:8000/docs |
| ReDoc | 8000 | http://localhost:8000/redoc |
| Postgres (pgvector) | 5544 | `postgresql://postgres:password@localhost:5544/registry` |
| Mock OIDC provider | 8090 | http://localhost:8090/default |
| Mock entitlement service | 8091 | http://localhost:8091 |
| Observability viewer | 16686 | http://localhost:16686 |

`make dev-status` shows the same list with health, and `make dev-down` stops it all.

---

## Step 2 — Bootstrap the dev tenant + fetch a JWT

```bash
make dev-token
```

This seeds a `dev` tenant + a `dev-admin` actor in Postgres, registers a `registry-dev` client in the local mock OIDC provider (port 8090), and seeds canned entitlements for that user in the mock entitlement service. The IDs and mock-client credentials land in `.env.dev`:

```
DEV_TENANT_SLUG=dev
DEV_TENANT_ID=<uuid>
DEV_ACTOR_ID=<uuid>
DEV_USER_ID=dev-admin
CLIENT_ID=registry-dev
CLIENT_SECRET=dev-secret
```

`.env.dev` is git-ignored. `make dev-token` is idempotent — re-running reuses the same tenant + actor + client.

Exchange the dev credentials for a bearer JWT against the mock IDP:

```bash
export TOKEN=$(make dev-jwt)
```

`make dev-jwt` reads `.env.dev`, hits the mock IDP, and prints the JWT to stdout — composable with `$(make dev-jwt)` inside any curl command. TTL is 3600s; re-run to refresh. See [authentication.md](../01-overview/04-authentication.md#fetching-a-jwt) for the equivalent raw curl and why `scope=registry` matters.

---

## Step 3 — Seed demo data

```bash
make dev-seed
```

Seeds closed-vocabulary values (entity types, edge relationship types, lifecycle states) and every bundle under [`seeds/`](../../seeds/README.md) — the Salt Design System capability across several historical versions, an enterprise user-preference service, and the rest of the demo dataset the other walkthroughs in this repo reference (identity, notifications, web-sdk, the memory-curation loop, …). Without this, `POST /v1/capabilities` rejects with `unknown vocabulary value` and `GET /v1/capabilities` returns an empty list.

`make dev-seed` is idempotent — re-running produces the same entity UUIDs.

---

## Step 4 — Make an authenticated call

```bash
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/capabilities
```

Expected output: a paginated envelope — `{"items": [...], "next_cursor": ...}` —
listing every entity `make dev-seed` created: capabilities, but also the
concepts, integrations, and people the demo dataset cites. Add
`?entity_type=capability` to see only the capabilities, which includes
`salt-design-system`.

Try fetching one by name:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/capabilities/salt-design-system
```

Expected: a single capability record.

---

## Step 5 — Explore via Swagger

Open http://localhost:8000/docs, click **Authorize**, and paste the token into the **bearerAuth** field. Every endpoint's **Try it out** button then sends the bearer header automatically.

For full API reference including request/response schemas, see [reference/api.md](../05-reference/01-api.md) and the live OpenAPI spec at http://localhost:8000/openapi.json.

---

## Stopping the stack

```bash
make dev-down    # stop everything, keep the database
make dev-reset   # wipe the database and come back up clean
```

---

## Alternative — Docker Compose

Where a container runtime is available, Compose gives a topology closer to production: PgBouncer in front of Postgres, and the real Jaeger, Prometheus, and Grafana instead of the local viewer.

```bash
git clone <repo-url>
cd contextplane
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d
make migrate
```

The `pip install` is still needed even though Postgres, the API, and every
mock service run in containers: `make migrate`, `make dev-token`,
`make dev-jwt`, and `make dev-seed` below are host-side commands (Alembic
and a couple of plain Python scripts), and they need this project's
dependencies on whichever `python3` is first on `PATH` — there is no
container for them to run in. `.[devstack]` is not needed here; that extra
exists only to give the *native* path its own local Postgres.

The first `docker compose up` builds the API image, which downloads the ~90 MB
embedding model and bakes it in, so the running container needs no network
access to embed anything. Behind a proxy, or on a host that cannot reach the
model host, point the build at an internal mirror instead:

```bash
docker compose build --build-arg EMBEDDING_MODEL_SOURCE=https://artifacts.corp/minilm
```

Then continue from [Step 2](#step-2--bootstrap-the-dev-tenant--fetch-a-jwt) — the ports, credentials, and every `make` command are the same. Compose additionally publishes PgBouncer on 6432, Prometheus on 9090, and Grafana on 3000 (admin / admin), and is stopped with `docker compose down` (`-v` to wipe the database).

The two stacks publish the same ports and cannot run at once. Stop one before starting the other.

---

## Next steps

| I want to… | Go to |
|---|---|
| Understand tenants, entities, visibility | [overview/vocabulary.md](../01-overview/03-vocabulary.md) |
| Set up OIDC or production tokens | [overview/authentication.md](../01-overview/04-authentication.md) |
| Understand role grants, tenant selection, entitlements | [overview/authorization.md](../01-overview/05-authorization.md) |
| Configure env vars | [reference/configuration.md](../05-reference/03-configuration.md) |
| Call from an AI agent via MCP | [reference/mcp-tools.md](../05-reference/02-mcp-tools.md) |
