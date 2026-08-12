# =============================================================================
# registry — canonical command surface
# =============================================================================
#
# This file is the SPEC for every gate the project enforces. Local dev,
# pre-commit hooks, and CI platforms all invoke these targets — they do
# not redefine the commands themselves. Wire your CI of choice (GitHub
# Actions, GitLab CI, Jenkins, Buildkite, CircleCI, Bitbucket Pipelines,
# Azure DevOps, an air-gapped on-prem runner, or a plain `bash` script)
# to invoke `make <target>`.
#
# One example wiring ships with the project: .github/workflows/ (GitHub
# Actions). It calls the targets defined below; it is not required and can
# be deleted without affecting the gates.
#
# See `docs/07-contributing/02-ci.md` for the architecture rationale.
#
# Conventions:
#   - All commands run from the repo root (this directory).
#   - All Python invocations assume the dev extras are installed
#     (`make install-dev`).
#   - Secrets and configuration come from environment variables — set
#     them however your platform sets env vars; the targets don't care.
#   - Each target is a single command or short pipeline. No logic in
#     Make beyond invocation.

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Prefer the project's venv at the conventional path; otherwise fall back
# to python3 (universally available on modern macOS / Linux). Operators
# can still override: `PYTHON=python3.13 make dev-token`.
#
# Resolved as an absolute path so recipes that `cd` still find the binary.
PYTHON      ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)
PIP         ?= $(PYTHON) -m pip
PYTEST      ?= $(PYTHON) -m pytest
RUFF        ?= $(PYTHON) -m ruff
MYPY        ?= $(PYTHON) -m mypy
ALEMBIC     ?= $(PYTHON) -m alembic

# import-linter ships no runnable module (`python -m importlinter` fails), so
# this is the one gate that has to name a console script. Resolved the same way
# as PYTHON above: the project venv when it exists, otherwise whatever is on
# PATH.
#
# The `lint` recipe prefixes it with PYTHONPATH=$(CURDIR), which is not
# decoration. import-linter builds its graph from the *installed* package, and
# an editable install resolves `contextplane` through a finder that points at
# whichever checkout ran `pip install -e .` — not necessarily this one. In a
# linked git worktree, or after a checkout is moved or renamed, the contract
# would then pass or fail against a tree nobody is looking at, which is the
# same class of silent-wrong-answer the repo-root anchoring in scripts/checklib.py
# exists to prevent. Putting the working directory first makes it lint the
# source it was invoked on.
LINT_IMPORTS ?= $(if $(wildcard .venv/bin/lint-imports),$(CURDIR)/.venv/bin/lint-imports,lint-imports)

# Source roots that ruff/mypy/pytest care about.
SRC_ROOTS   := contextplane scripts
TEST_ROOT   := tests

# Default target — print help.
.DEFAULT_GOAL := help

.PHONY: help install-dev lint format format-check typecheck doc-refs doc-links test-hygiene \
        privileged-writes usage-boundary env-documented helm-env calibration-report \
		auth-consolidation-gate reachability-audit \
        test-unit test-integration test-conformance test-native-provider arc-vectors test-perf test-airgap test-smoke test all \
        migrate openapi-export dev-token dev-jwt dev-seed seeds-validate clean \
        build-docker helm-package \
        dev-up dev-down dev-status dev-reset dev-logs dev-url

# `make dev-up` writes .devstack/env with the database URL and the OIDC /
# entitlement wiring for the native stack. Targets that talk to those
# services source it so the developer does not have to export anything.
#
# Two conditions, both learned the hard way:
#
# 1. Only when the native stack is actually running. .devstack/env outlives
#    `make dev-down` — state.json does not, because the supervisor unlinks it
#    on stop, and only once it has confirmed the children are gone. Without
#    this check, a developer who once ran `make dev-up` and later followed the
#    compose path got `make migrate` pointed at the dead native cluster's port,
#    and a connection error with no clue why.
#
# 2. An exported DATABASE_URL wins. The file supplies defaults for a stack
#    this Makefile manages; it has no business overriding a database the
#    caller named explicitly.
DEVSTACK_ENV   := .devstack/env
DEVSTACK_STATE := .devstack/state.json

# Host-side URL for the Postgres that docker-compose publishes, used as the
# last fallback. The Compose quickstart is `docker compose up -d` then
# `make migrate`, and nothing in that sequence puts DATABASE_URL in the
# environment — so on a fresh clone it died with `KeyError: 'DATABASE_URL'`.
# It only ever appeared to work for developers who had also run `make dev-up`
# at some point and were unknowingly borrowing that stack's URL.
#
# Safe to hardcode because it is not a secret and not a choice: the port and
# credentials are fixed in docker-compose.yml, which is local-dev-only. It
# applies only when neither the caller nor a running native stack said
# otherwise, so it can never override a real database.
COMPOSE_DATABASE_URL := postgresql+asyncpg://postgres:password@localhost:5544/contextplane

define with_devstack_env
	@set -e; \
	if [ -f $(DEVSTACK_ENV) ] && [ -f $(DEVSTACK_STATE) ]; then \
	  _caller_db="$${DATABASE_URL:-}"; \
	  set -a; . ./$(DEVSTACK_ENV); set +a; \
	  if [ -n "$$_caller_db" ]; then export DATABASE_URL="$$_caller_db"; fi; \
	fi; \
	if [ -z "$${DATABASE_URL:-}" ]; then export DATABASE_URL="$(COMPOSE_DATABASE_URL)"; fi;
endef

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

help: ## Print this help.
	@printf "registry — Make targets\n\n"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\nThe gates a PR must pass: lint + format-check + typecheck + doc-refs + privileged-writes + usage-boundary + reachability-audit + test-unit.\n"
	@printf "Run them in one shot with: make all\n"

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------

install-dev: ## Install the project + dev extras into the current Python env.
	$(PIP) install -e ".[dev]"

# -----------------------------------------------------------------------------
# Lint, format, type-check, doc-refs (PR gates — fast)
# -----------------------------------------------------------------------------

lint: ## Run ruff, the file-size and approval-writer guards, and the module-boundary contract.
	$(RUFF) check .
	$(PYTHON) scripts/check_file_sizes.py
	$(PYTHON) scripts/check_arc_approval_writers.py
	PYTHONPATH=$(CURDIR) $(LINT_IMPORTS)

format: ## Apply ruff format to the whole tree (writes).
	$(RUFF) format .

format-check: ## Verify the whole tree is formatted (read-only).
	$(RUFF) format --check .

typecheck: ## Run mypy --strict on the source tree.
	$(MYPY) --strict $(SRC_ROOTS)

doc-refs: ## Verify no internal-doc references in shipped code (see CLAUDE.md).
	$(PYTHON) scripts/check_no_doc_refs.py

doc-links: ## Verify every relative link and anchor in README.md and docs/**/*.md resolves.
	$(PYTHON) scripts/check_doc_links.py

test-hygiene: ## Verify no phase-named test files, stale phase comments, ungated entity reads, raw state access, unregistered config bypasses, or assertion-less tests.
	$(PYTHON) scripts/check_no_phase_named_tests.py
	$(PYTHON) scripts/check_import_direction.py
	$(PYTHON) scripts/check_migration_naming.py
	$(PYTHON) scripts/check_visibility_chokepoint.py
	$(PYTHON) scripts/check_state_access.py
	$(PYTHON) scripts/check_config_consolidation.py
	$(PYTHON) scripts/check_test_assertions.py

privileged-writes: ## Verify privileged tables are written only through their one module.
	$(PYTHON) scripts/check_privileged_writes.py

usage-boundary: ## Verify usage data stays non-authoritative (no service decides from it).
	$(PYTHON) scripts/check_usage_boundary.py

reachability-audit: ## Verify every quarantined memory service has a production caller (route, MCP tool, or job).
	$(PYTHON) scripts/check_memory_reachability.py

env-documented: ## Verify .env.example, the configuration reference, and every other shipped doc agree with Settings.
	$(PYTHON) scripts/check_env_documented.py
	$(PYTHON) scripts/check_doc_env_mentions.py

helm-env: ## Verify deploy/helm/values.yaml, templates/secret.yaml, and the canonical env set agree.
	$(PYTHON) scripts/check_helm_env.py

calibration-report: ## Report confidence-calibration state; non-zero if a fit misses its bound.
	$(PYTHON) scripts/calibration_report.py

auth-consolidation-gate: ## Fail if any auth-path discriminator / api_token symbol survives outside migrations.
	@# Pattern set is narrower than the original spec: it covers names
	@# that are unambiguously dead (auth_mode discriminator, RSAM/rsam
	@# legacy naming, the api_token validator + hasher + admin path).
	@# `actor_roles`/`api_token` table-name strings still appear in
	@# workspace.py + ratelimit.py SQL queries — those are tracked as
	@# their own follow-ups since the workspace permission model and
	@# the ratelimit api_token fallback are not part of this auth
	@# consolidation. The gate exists to prevent re-introduction of
	@# the deleted symbols, not to police every legacy name.
	@if grep -rn 'auth_mode\|AUTH_MODE\|\bRSAM\b\|\brsam\b\|validate_token\|hash_token\|upsert_rsam\|admin_tokens\.' \
	    contextplane/ --include='*.py' --exclude-dir=migrations 2>/dev/null; then \
	  echo "auth-consolidation-gate: FAIL — legacy auth names found in contextplane/"; \
	  exit 1; \
	else \
	  echo "auth-consolidation-gate: PASS"; \
	fi

# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

test-unit: ## Run unit tests (no DB; ~2s) with the coverage ratchet (see CLAUDE.md Testing).
	$(PYTEST) $(TEST_ROOT)/unit -q --timeout=60 --cov=contextplane --cov-report=term-missing:skip-covered --cov-fail-under=80

# The whole tier, always. No marker filter and no file list: this target is what
# `release-gate` reads, and a gate that names files only ever covers the files
# somebody remembered. The tier ran red for a full milestone because every gate
# that touched it enumerated specific paths, so 17 failures and 6 errors crossed
# a release boundary while `make all` stayed green.
#
# Some files here are opt-in on an environment (a provider credential, a running
# compose stack) and skip themselves when it is absent. A skip is a reported
# result; a file nobody runs is not.
test-integration: ## Run every integration test (testcontainers Postgres; slow).
	$(PYTEST) $(TEST_ROOT)/integration -q --timeout=180

test-conformance: ## Run conformance suite (openapi drift, tenant isolation, MCP).
	$(PYTEST) $(TEST_ROOT)/conformance -v --timeout=60

# The focused provider lifecycle contract, run under a question the ordinary
# suite does not ask: does *this* provider actually work here?
#
# Under that question a skip is the worst outcome available. The contract file
# skips honestly when a host cannot supply a server, and inside the full tier
# that is right. Here it is not: a skip is indistinguishable from a pass in
# every summary line, and what it stopped checking is the whole reason somebody
# ran this. The runner therefore fails on zero collection, on any skip, and on
# any error — including a teardown error, which matters because this contract
# creates and drops real databases and one that leaks them poisons every later
# run on the host.
#
# Select the provider with CONTEXTPLANE_TEST_PG, e.g.
# `CONTEXTPLANE_TEST_PG=devstack make test-native-provider`.
test-native-provider: ## Run the focused provider lifecycle contract; a skip counts as a failure.
	$(PYTHON) scripts/run_native_provider_contract.py

# The delivery-lifecycle exit gate as one command: the frozen scenario corpus,
# and the integration module that replays those scenarios against the shipped
# surfaces.
#
# This does not replace either tier and must not be read as covering them. Both
# files already run inside `test-conformance` and `test-integration`, which is
# where their coverage comes from — the lesson recorded above about gates that
# name files applies here too. What this target adds is the ability to run the
# exit gate on its own while the pilot's evidence is being reviewed, without
# waiting for the whole integration tier.
#
# Needs Docker: the exit half stands up a real Postgres, because what it proves
# is a join and a tenant predicate, and neither survives being faked.
test-lifecycle-pilot: ## Run the delivery-lifecycle pilot corpus and exit gates together (needs Docker).
	$(PYTEST) $(TEST_ROOT)/conformance/test_lifecycle_pilot_corpus.py \
		$(TEST_ROOT)/integration/test_lifecycle_pilot_exit.py -q --timeout=180

# The ARC authoring-surface canonical vectors under tests/fixtures/arc_authoring/
# are checked by two implementations that share no code: this Node reference
# verifier (stdlib-only, no package.json) and, once it exists, the production
# Python canonicalizer's own conformance suite. Requires `node` on PATH;
# nothing else in `make all` depends on Node, so this target is separate
# rather than folded into `test-conformance`.
arc-vectors: ## Verify the ARC authoring-surface canonical vectors against the Node reference implementation.
	node tools/arc-reference-verifier/verify.mjs tests/fixtures/arc_authoring

test-perf: ## Run perf tests (SLO p95 verification; marked @slow).
	$(PYTEST) $(TEST_ROOT)/perf -q --timeout=300 -m perf

# The full-stack auth smoke test, against whichever stack is running.
#
# A skip counts as a failure here. The test decides for itself whether a stack
# is reachable and skips when it is not — sensible when a developer runs the
# whole suite with nothing up, useless as a gate, because "no stack" and "auth
# is broken" both come out green. Running it through this target says "I expect
# a stack", so the absence of one is a real result. That distinction is why the
# test had never once run: it was gated on an env var nobody set.
test-smoke: ## Full-stack auth smoke test; requires a running stack (dev-up or compose).
	@set -e; \
	out=$$($(PYTEST) $(TEST_ROOT)/integration/test_auth_compose_smoke.py -m compose -q -rs 2>&1); \
	echo "$$out"; \
	if ! echo "$$out" | grep -qE '[0-9]+ passed'; then \
	  echo ""; \
	  echo "test-smoke: the smoke test did not run. Start a stack first:"; \
	  echo "    make dev-up && make dev-token      # or: docker compose up -d && make migrate && make dev-token"; \
	  exit 1; \
	fi

# Proves the image needs no network to embed. Everything else about the
# embedding path is checked with the model on the host filesystem, which cannot
# distinguish "the artifact is baked into the image" from "the artifact happens
# to be lying around". This runs the built image on a Docker network created
# with --internal — no default route, no egress — and drives ingest, drain, and
# an ANN search against a Postgres on that same isolated network.
#
# Needs Docker, and builds the image, so it is not part of `make all`.
test-airgap: ## Prove the image embeds and searches with no network egress.
	@set -e; \
	NET=contextplane-airgap-net; PG=contextplane-airgap-pg; IMG=contextplane:airgap-check; \
	DBURL="postgresql+asyncpg://postgres:password@$$PG:5432/contextplane"; \
	cleanup() { docker rm -f $$PG >/dev/null 2>&1 || true; docker network rm $$NET >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT; cleanup; \
	echo "==> building image"; \
	docker build -q -t $$IMG $${EMBEDDING_MODEL_SOURCE:+--build-arg EMBEDDING_MODEL_SOURCE="$$EMBEDDING_MODEL_SOURCE"} . >/dev/null; \
	echo "==> creating isolated network"; \
	docker network create --internal $$NET >/dev/null; \
	docker run -d --name $$PG --network $$NET -e POSTGRES_PASSWORD=password \
		-e POSTGRES_DB=contextplane pgvector/pgvector:pg16 >/dev/null; \
	for i in $$(seq 1 60); do \
		docker exec $$PG pg_isready -U postgres -d registry >/dev/null 2>&1 && break; sleep 1; done; \
	echo "==> asserting the network really is isolated"; \
	docker run --rm --network $$NET --entrypoint python $$IMG -c \
		"import socket; socket.setdefaulttimeout(5); \
		 exec('try:\n socket.create_connection((\"huggingface.co\", 443))\n raise SystemExit(\"egress is available - not an air-gap test\")\nexcept OSError:\n pass')"; \
	echo "==> migrating"; \
	docker run --rm --network $$NET -e DATABASE_URL="$$DBURL" \
		--entrypoint alembic $$IMG upgrade head >/dev/null; \
	echo "==> boot check (EXTRACTION_PROVIDER=$${EXTRACTION_PROVIDER:-noop})"; \
	docker run --rm --network $$NET -e DATABASE_URL="$$DBURL" \
		-e EXTRACTION_PROVIDER="$${EXTRACTION_PROVIDER:-noop}" \
		-e EXTRACTION_API_KEY="$${EXTRACTION_API_KEY:-airgap-dummy-key}" \
		-v "$$PWD/tests:/app/tests:ro" \
		--entrypoint python $$IMG tests/airgap/airgap_boot_check.py

test: test-unit test-conformance ## Run the fast test gates (unit + conformance).

all: lint format-check typecheck doc-refs doc-links test-hygiene privileged-writes usage-boundary reachability-audit env-documented helm-env seeds-validate test ## Run every gate a PR must pass.

# The whole integration tier, end to end on the integrated tree, plus every gate
# `all` runs. Separate from `all` because the tier needs Docker and takes
# minutes: this is what you run before tagging, not on every commit.
#
# `test-integration` rather than the exit-criteria file alone. That file is part
# of the tier, so naming it separately ran it twice and — the reason this
# changed — left every other integration file ungated: the tier sat red for a
# whole milestone and this target passed, because the failures were in files it
# did not name. A release gate that reads a subset of a test tier reports the
# health of the subset.
#
# Perf budgets are deliberately NOT here. They need a quiet machine to mean
# anything, and a release gate that fails on a busy laptop is a release gate
# people learn to re-run until it passes. Run `make test-perf` separately.
.PHONY: release-gate
release-gate: all test-integration ## Run the full integration tier plus every PR gate.

# -----------------------------------------------------------------------------
# Local dev stack (no container runtime required)
#
# Brings up the same services as `docker compose up -d`, on the same
# ports, with the same environment — as ordinary local processes. Use
# this where a container runtime is unavailable; use compose where it is
# and you want the closer-to-production topology. The two cannot run at
# the same time: they publish the same ports, and `dev-up` says so
# rather than half-starting.
#
# Postgres comes from whichever of these the machine has: Postgres.app,
# an install on PATH, or the pgserver package (`pip install -e
# ".[devstack]"`). Point CONTEXTPLANE_PG_BINDIR at an install elsewhere, or
# set DATABASE_URL to use a database you do not want managed at all.
# -----------------------------------------------------------------------------

dev-up: ## Start the full local stack (Postgres, mocks, observability, API). RECLAIM=1 offers to free busy ports.
	$(PYTHON) -m scripts.devstack up $(if $(RECLAIM),--reclaim,)

dev-down: ## Stop the local stack. Database contents survive.
	$(PYTHON) -m scripts.devstack down

dev-status: ## Show what the local stack is running and where.
	$(PYTHON) -m scripts.devstack status

dev-reset: ## Destroy the local database and bring the stack back up clean.
	$(PYTHON) -m scripts.devstack reset

dev-url: ## Print the database URL the local stack uses.
	@$(PYTHON) -m scripts.devstack url

# Tail one service or all of them: `make dev-logs SVC=api`, add FOLLOW=1
# to stream.
SVC ?=
LINES ?= 50
dev-logs: ## Tail dev-stack logs. Overrides: SVC=<name>, LINES=<n>, FOLLOW=1.
	@$(PYTHON) -m scripts.devstack logs $(SVC) -n $(LINES) $(if $(FOLLOW),-f,)

# -----------------------------------------------------------------------------
# Operational helpers
# -----------------------------------------------------------------------------

migrate: ## Apply Alembic migrations to the database in $DATABASE_URL.
	$(call with_devstack_env) $(ALEMBIC) upgrade head

openapi-export: ## Regenerate the committed openapi.json from the live app.
	$(PYTHON) scripts/export_openapi.py

# Bootstrap a local-dev tenant + actor + mock-IDP/entitlement seed.
# Idempotent — re-running reuses the existing tenant + actor rows and
# re-registers the client + canned entitlements without minting new
# credentials. Writes DEV_TENANT_SLUG, DEV_TENANT_ID, DEV_ACTOR_ID,
# DEV_USER_ID, CLIENT_ID, and CLIENT_SECRET to .env.dev so the
# developer can fetch a JWT from the mock IDP and call the API.
# Requires $DATABASE_URL pointing at a migrated DB plus the mock OIDC
# + mock entitlement services running on their default compose ports
# (or pass --skip-mock-seed). Pass --env-file=PATH to write somewhere
# other than .env.dev. See docs/02-get-started/01-quickstart.md for
# the JWT-fetch step.
dev-token: ## Seed dev tenant + actor + mock-IDP/entitlement state. Writes .env.dev.
	$(call with_devstack_env) $(PYTHON) scripts/bootstrap_dev_tenant.py

# Mint a fresh JWT from the local mock IDP using the client credentials
# in .env.dev. Stdout is the bare access_token so it composes:
#   export TOKEN=$(make dev-jwt)
#   curl -H "Authorization: Bearer $(make dev-jwt)" http://localhost:8000/v1/whoami
# Requires `make dev-token` to have been run (for .env.dev) and the mock
# OIDC server reachable. The token endpoint is derived from
# OIDC_DISCOVERY_URL when the native stack has written one, so a
# non-default port is picked up automatically; otherwise it falls back to
# the shared default both providers publish. Token TTL is 3600s — re-run
# to refresh.
dev-jwt: ## Mint a fresh JWT from the local mock IDP. Stdout-only (pipe-friendly).
	@if [ ! -f .env.dev ]; then \
	  echo "error: .env.dev not found; run \`make dev-token\` first" >&2; exit 1; \
	fi; \
	if [ -f $(DEVSTACK_ENV) ] && [ -f $(DEVSTACK_STATE) ]; then set -a; . ./$(DEVSTACK_ENV); set +a; fi; \
	set -a; . ./.env.dev; set +a; \
	if [ -n "$$OIDC_DISCOVERY_URL" ]; then \
	  TOKEN_URL=$${OIDC_DISCOVERY_URL%/.well-known/openid-configuration}/token; \
	else \
	  TOKEN_URL=http://localhost:8090/default/token; \
	fi; \
	curl -fsS -X POST "$$TOKEN_URL" \
	  -d grant_type=client_credentials \
	  -d client_id=$$CLIENT_ID \
	  -d client_secret=$$CLIENT_SECRET \
	  -d scope=registry \
	| $(PYTHON) -c "import json,sys; t=json.load(sys.stdin).get('access_token'); sys.exit('error: no access_token in mock IDP response') if not t else print(t)"

# Follow-up to dev-token: seed the dev tenant from every numbered bundle
# directory under seeds/ (00-core, 01-capability, …). One command, full
# demo. Idempotent — re-running yields the same entity_ids.
dev-seed: ## Seed dev tenant from every bundle under seeds/. Idempotent.
	$(call with_devstack_env) $(PYTHON) scripts/seed.py

# Validate every capability entity in seeds/ against the capability JSON
# Schema (seeds/_templates/capability-schema.json). Operates on the merged
# attribute state across bundles — runs without a database so it can gate CI.
seeds-validate: ## Validate seeds/ capabilities against the capability JSON Schema.
	$(PYTHON) scripts/validate_seeds.py

# -----------------------------------------------------------------------------
# Release-side targets — the build/package commands. Image push and
# signing are platform-specific (each CI platform has its own login/secret
# story); they stay in the workflow YAML, not here. These targets give
# operators a portable starting point.
# -----------------------------------------------------------------------------

# Override these on the command line: `make build-docker IMAGE_TAG=v1.7.0`.
IMAGE_NAME ?= contextplane
IMAGE_TAG  ?= dev
HELM_VERSION ?= 0.0.1

build-docker: ## Build the application Docker image. Overrides: IMAGE_NAME, IMAGE_TAG, EMBEDDING_MODEL_SOURCE.
	docker build -t "$(IMAGE_NAME):$(IMAGE_TAG)" $(if $(EMBEDDING_MODEL_SOURCE),--build-arg EMBEDDING_MODEL_SOURCE="$(EMBEDDING_MODEL_SOURCE)",) .

helm-package: ## Package the Helm chart into /tmp/helm-pkg/. Overrides: HELM_VERSION.
	mkdir -p /tmp/helm-pkg
	helm package deploy/helm/ \
		--version "$(HELM_VERSION)" \
		--app-version "$(HELM_VERSION)" \
		--destination /tmp/helm-pkg

clean: ## Remove build artefacts + caches.
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -prune -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
