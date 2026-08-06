# Local development setup

<!--
  title: Local development setup
  audience: contributor
  status: complete
-->

This guide covers setting up a local development environment for the registry codebase — virtual environment, hot reload, pre-commit hooks, and pointing tests at a local database.

For the gate reference and CI platform wiring, see [`ci.md`](02-ci.md). For first-time setup that just gets you to an authenticated `GET /v1/whoami`, see [`quickstart.md`](../02-get-started/01-quickstart.md).

**Preconditions:**

- Python 3.13 (the project requires `>=3.12`; example CI wirings pin 3.13)
- A PostgreSQL 16 with pgvector — see [Where Postgres comes from](#where-postgres-comes-from). No container runtime required.
- `make`, `curl`, `jq` (or `python3 -m json.tool`)
- ~4 GB free RAM

**What this guide covers:**

- [Install dependencies](#install-dependencies)
- [Start the dev stack](#start-the-dev-stack)
- [Where Postgres comes from](#where-postgres-comes-from)
- [Run the app with hot reload](#run-the-app-with-hot-reload)
- [Embeddings in local dev](#embeddings-in-local-dev)
- [Install pre-commit hooks](#install-pre-commit-hooks)
- [Run the test gates](#run-the-test-gates)
- [Choosing where tests get their database](#choosing-where-tests-get-their-database)
- [Regenerate the OpenAPI snapshot](#regenerate-the-openapi-snapshot)
- [Using Docker Compose instead](#using-docker-compose-instead)

---

## Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev,devstack]"
```

`-e ".[dev]"` installs the `registry` package in editable mode plus the `dev` extras (pytest, ruff, mypy, pre-commit, testcontainers, mcp client SDK). Code changes are picked up immediately — no reinstall needed.

`devstack` adds `pgserver`, a Postgres-in-a-wheel used by `make dev-up` when no other Postgres is available. It only has wheels for CPython ≤ 3.12 on x86_64, so on Python 3.13 or Linux arm64 it silently does not install — `make dev-up` then falls back to Postgres.app, a `PATH` install, or a `DATABASE_URL` you supply, and tells you which options apply.

The Makefile resolves Python in this order: `registry/.venv/bin/python`, then the first `python3` on `PATH`. Override with `make PYTHON=python3.13 <target>`.

## Start the dev stack

```bash
make dev-up
```

This starts every service the app needs as an ordinary local process, applies migrations, and waits until each one answers a health check:

| Service | Port (host) | What it is |
|---|---|---|
| registry API | 8000 | The FastAPI app under uvicorn `--reload` |
| postgres | 5544 | Primary DB. Database: `registry`. User: `postgres`, password: `password`. |
| mock OIDC provider | 8090 | Local IdP (`tests/mocks/oidc_server`). Issues real RS256 JWTs. |
| mock entitlement service | 8091 | Local entitlement-service stand-in. Returns canned grants per `userId`. |
| observability viewer | 16686 | Traces from this session, plus scraped metrics |
| OTLP ingest | 4318 | Where the app exports spans |

The other lifecycle commands:

```bash
make dev-status              # what is running, and which Postgres it found
make dev-logs SVC=api        # tail one service (FOLLOW=1 to stream)
make dev-down                # stop everything; database contents survive
make dev-reset               # destroy the database and come back up clean
```

Bootstrap the dev tenant + mock-IDP credentials + mock entitlements (idempotent):

```bash
make dev-token       # seed tenant + actor + mock OIDC client + canned entitlements
make dev-seed        # (optional) seed the closed vocabulary + demo capabilities
```

Get a fresh JWT for any local request:

```bash
export TOKEN=$(make dev-jwt)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/whoami
```

`make dev-up` writes the database URL and the OIDC/entitlement wiring to `.devstack/env`, and the targets above source it automatically. Under Compose the same values arrive through the container environment instead, so these commands are identical either way.

Authentication is **not** bypassed locally. The app fetches the discovery document, fetches the JWKS, and verifies the RS256 signature exactly as it does against the enterprise IdP. A request without a token gets a 401 here too.

## Where Postgres comes from

`make dev-up` needs PostgreSQL 16 with the `vector` extension, and takes the first of these it finds:

| Source | How to get it |
|---|---|
| `DATABASE_URL` is set | Uses that database as-is and manages nothing. Shared team instance, CI service container, anything you run yourself. |
| `REGISTRY_PG_BINDIR` is set | An install somewhere the search would not look: `export REGISTRY_PG_BINDIR=/path/to/postgres/bin` |
| Postgres.app (macOS) | Install it. Recent versions bundle pgvector. Version 16 is preferred when several are installed. |
| `initdb` on `PATH` | Any system or distro install. On Debian/Ubuntu with PGDG: `apt-get install postgresql-16 postgresql-16-pgvector` |
| The `pgserver` package | `pip install -e ".[devstack]"` — ships Postgres 16 and pgvector inside a wheel. Wheels exist for macOS and Linux x86_64 on Python 3.9–3.12; there is none for 3.13 or Linux arm64, so use another source there. |

`make dev-status` reports which one was chosen. If none is usable, `make dev-up` lists every source it tried and what was wrong with each.

Except in the `DATABASE_URL` case, the cluster lives in `.devstack/pgdata` — git-ignored, owned entirely by the dev stack, and safe to delete. `make dev-reset` does exactly that.

Two things are checked before the stack starts, because both fail confusingly otherwise:

- **pgvector must actually be present.** Without it, migrations fail partway through `alembic upgrade head` with an error that says nothing about the real cause.
- **The ports must be free.** The native stack and Compose publish the same ports deliberately, so they cannot run at the same time. If the other one is up, `make dev-up` says so instead of half-starting. `DEVSTACK_PORT_OFFSET=100 make dev-up` shifts every port when you genuinely need both.

## Run the app with hot reload

`make dev-up` already runs the API under `uvicorn --reload`, so code changes under `registry/` are picked up automatically. Its output goes to `.devstack/logs/api.log` — `make dev-logs SVC=api FOLLOW=1` to watch it.

To run the API yourself instead (breakpoint debugging in an IDE, say), start everything else and then take over port 8000:

```bash
make dev-up
make dev-down            # or stop just the API and leave the rest up
set -a; . ./.devstack/env; set +a
uvicorn registry.main:create_app --factory --reload --port 8000
```

Sourcing `.devstack/env` is what supplies `DATABASE_URL`, the OIDC discovery URL, the entitlement-service URL, and the rest — the same values the supervised process gets. Under Compose, export them by hand or read them out of `docker-compose.yml`.

**Do not skip `EMBEDDING_PROVIDER` when exporting by hand.** It defaults to `onnx`, which loads a model from `EMBEDDING_MODEL_PATH` — a path that only exists inside the container image. Miss it and the app refuses to start, with an `ArtifactError` naming the file it wanted. That is deliberate: a missing model used to fall back to zero vectors silently, which produced an app that looked healthy and quietly filled the search index with unusable rows. See [Embeddings in local dev](#embeddings-in-local-dev).

Hot reload **does not** apply Alembic migrations. After changing a migration, run `make migrate` and then restart the process.

## Embeddings in local dev

`make dev-up` and `docker compose up` both run `EMBEDDING_PROVIDER=stub`, which returns zero vectors. Search still answers — the lexical and graph arms do the ranking — but semantic ranking is inert, because every vector distance is identical. That is the right default for the dev loop: no model to stage, nothing to download, fast startup.

To exercise real semantic retrieval, stage the artifact once and point the provider at it:

```bash
python scripts/fetch_embedding_model.py --out .devstack/models/all-MiniLM-L6-v2
EMBEDDING_PROVIDER=onnx \
EMBEDDING_MODEL_PATH=$PWD/.devstack/models/all-MiniLM-L6-v2 \
  uvicorn registry.main:create_app --factory --reload --port 8000
```

`.devstack/` is gitignored, and that path is also where the embedding tests look — `tests/unit/test_onnx_embedder.py` and the parity test skip when it is absent, so staging it turns those on.

Under Compose the artifact is already in the image, so nothing needs staging: `docker compose run -e EMBEDDING_PROVIDER=onnx api …` is enough.

The parity test additionally needs the reference implementation and the torch weights, which are not in the image:

```bash
pip install -e ".[dev,torch]"
python scripts/fetch_embedding_model.py --out .devstack/models/all-MiniLM-L6-v2 --with-torch-weights
pytest tests/unit/test_onnx_parity.py -q
```

## Install pre-commit hooks

One-time, after cloning:

```bash
pre-commit install
```

This installs hooks that run on every `git commit`:

- `make lint` — ruff lint
- `make format-check` — ruff format (read-only check)
- `make typecheck` — mypy `--strict`
- `make doc-refs` — gate against external-doc references in shipped code (`scripts/check_no_doc_refs.py`)
- `make test-hygiene` — gate against phase-named tests (`scripts/check_no_phase_named_tests.py`)

The hooks call the same Make targets CI calls — there is no separate hook config to drift from CI. Skipping a hook (`--no-verify`) is rarely justified; if you do, explain why in the commit body so reviewers can verify the bypass.

## Run the test gates

```bash
make test-unit          # ~2s, no DB, default home for new tests
make test-integration   # ~1-2 min, real Postgres, real SQL
make test-conformance   # contract drift gates (openapi snapshot, MCP catalog, cross-tenant)
make test-perf          # SLO verification (latency p95s, webhook fan-out) — release pipeline only
```

`make test` runs `test-unit` + `test-conformance`. `make all` adds the lint, format, typecheck, and doc-reference gates — that is the full set a PR has to pass, and none of it needs a container runtime.

For the rationale behind each tier and the gates wired into CI, see [`ci.md`](02-ci.md).

## Choosing where tests get their database

The integration and conformance suites need a real Postgres. `REGISTRY_TEST_PG` decides where it comes from:

| Value | Behaviour |
|---|---|
| `auto` (default) | `DATABASE_URL` if set; else testcontainers if a container runtime answers; else a locally managed cluster. |
| `external` | Use `DATABASE_URL`. The schema must already be at head (`make migrate` against the same URL). |
| `testcontainers` | Always testcontainers. |
| `devstack` | Always a locally managed cluster — the same Postgres sources listed [above](#where-postgres-comes-from), and usually the fastest option. |

```bash
REGISTRY_TEST_PG=devstack make test-integration
```

Whichever source is used, each session gets a **freshly created, freshly migrated database** that is dropped when the run ends. Worth knowing why: the per-test `db_session` fixture commits rather than rolling back, so isolation between runs comes from the database being new, not from transactions being undone. The test cluster lives in `.devstack/pgdata-test`, separate from the dev stack's, so a test run can never touch your dev data.

The compose-stack smoke test at `tests/integration/test_auth_compose_smoke.py` is gated by `COMPOSE_STACK_UP=1` instead — set that env var when the local mocks are reachable and you want the real-JWT round-trip exercised:

```bash
COMPOSE_STACK_UP=1 pytest tests/integration/test_auth_compose_smoke.py -m compose -q
```

## Regenerate the OpenAPI snapshot

The committed `openapi.json` is the conformance baseline. Any change to a router, response model, or security scheme requires regenerating it:

```bash
make openapi-export
```

The script writes to `openapi.json` at the repo root. Commit the diff alongside the code change; `make test-conformance` fails CI if the committed file is stale.

The spec is generated with `REGISTRY_HTTP_METHODS_MODE` pinned to `rest`, so the committed contract describes the default surface no matter what the exporting shell had set. The POST-alias modes add roughly thirty extra paths, and the committed file would otherwise depend on who ran the export.

## Using Docker Compose instead

Compose remains fully supported, and it is the closer-to-production topology: it adds PgBouncer in front of Postgres and runs the real Jaeger, Prometheus, and Grafana rather than the local sink.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # `make migrate`, `dev-token` and `dev-jwt` run on the host, not in a container
docker compose up -d
make migrate
make dev-token
```

The install step is easy to skip because the containers look self-sufficient, and
they are — for the API. But `make migrate`, `make dev-token` and `make dev-jwt`
all execute on the host against the container's Postgres, so a clone with no
virtualenv fails on the second command with a bare import error that says nothing
about the missing install.

Ports, credentials, database name, and environment variables are identical to the native stack, so every command in this guide works unchanged. The differences are:

| | `make dev-up` | `docker compose up -d` |
|---|---|---|
| Container runtime | not needed | required |
| PgBouncer (6432) | not run — the compose dev override already bypasses it | present, bypassed by the dev override |
| Observability | local sink on 16686 | Jaeger 16686, Prometheus 9090, Grafana 3000 |
| Postgres | `.devstack/pgdata` on the host | named volume `cap_postgres_data` |
| Reset | `make dev-reset` | `docker compose down -v` |

They cannot run simultaneously — same ports. `make dev-down` or `docker compose down` before switching.

---

**See also:**

- [`ci.md`](02-ci.md) — gate descriptions, make target reference, CI platform wiring
- [`../02-get-started/01-quickstart.md`](../02-get-started/01-quickstart.md) — five-minute path to an authenticated API call
- [`../05-reference/04-architecture.md`](../05-reference/04-architecture.md) — component map and request lifecycle
- the repository's root `CLAUDE.md` — project-wide conventions every contributor must follow
