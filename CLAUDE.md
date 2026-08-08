# Project conventions — `contextplane/`

This is the shipping product repo. **Remote: `roughcompass/registry`.** Everything in this directory is part of the application; the test pyramid, gates, and runbooks all live here.

Planning artifacts (PRDs, TDDs, ADRs, dev plans) live in a **separate** repo at `../.context/`. That repo has no remote by default. Code in here must never reference paths or labels from there — see the doc-refs rule below.

---

## No external-doc references in shipped code

**The rule.** Files in this repo must not contain references to documents that live in `../.context/` — those are internal planning docs not visible to a future engineer reading this repo in isolation.

**What "shipped" means in this repo:**

| Path | Shipped? | Rule applies? |
|---|---|---|
| `contextplane/**/*.py` | yes | yes |
| `**/*.md` (operator-facing docs, runbooks) | yes | yes |
| `eval/EVAL.md` | yes | yes — *except* task IDs of any prefix are allowed in a "Commits" column as git-history anchors |
| `.env.example` | yes | yes |
| `README.md` | yes | yes |
| Past git commit messages | yes, but immutable | no — historical record; rule applies to new commits going forward |

**Forbidden patterns.** Any of these in a tracked file is a violation:

```
\bADR-\d+\b                           # e.g. ADR-024
\bF\d+\.\d+\b                         # e.g. F7.12 (PRD feature numbers)
\bOQ-[A-Za-z0-9-]+                    # e.g. OQ-P7-3 (open-question labels)
\b[A-Z]{2,5}(-P\d+R?)?-T\d+[a-z]?\b   # e.g. CAP-P7-T20, PP-T03, AAS-T14 (dev-plan task IDs of any prefix, except EVAL.md)
\bAQ\d+\b                             # e.g. AQ7 (architecture-quality labels)
PRD §                                 # explicit doc citation
TDD §                                 # explicit doc citation
(interfaces|flows|data-model)\.md §   # explicit doc/section citation
\bPhase \d+\b                         # bare phase labels — say what the change is, not which phase
\b[A-Z]{2,}-phase\b                   # e.g. ENC-phase — same rule as bare phase labels, spelled as a compound word
```

This is a shape match, not a fixed list of known prefixes: any two-to-five-letter prefix followed by an optional `-P<phase>` segment and a `-T<number>` gets caught, including a prefix that has never been seen before. A per-project task-ID scheme only has to be invented once for its references to start accumulating in shipped files; matching prefixes one at a time meant a project whose prefix nobody remembered to register slipped past this gate for its entire life, no matter how many references it left behind.

**The principle.** Comments should explain *the rule*, in the code's own vocabulary. Not where the rule was decided.

**Why this rule exists.** Planning docs live in a different repo. A future engineer (or agent) reading the code in isolation cannot resolve `ADR-024` or `F7.12` — those references imply context that has gone missing. The code must carry the rationale itself.

### Before / after

**Before (banned):**

```python
# Visibility filter is the ADR-024 chokepoint — every cross-tenant
# query path must call filter_entities() per F7.1 / TDD §2.1.
```

**After (acceptable):**

```python
# Every cross-tenant query path funnels through filter_entities() so
# tenant-isolation enforcement lives at one layer. Bypassing this
# function is how data leaks between tenants happen.
```

### Intentional bypass

If citing an external resource is genuinely the right thing (a stable public URL, an RFC, a Postgres docs page), end the line with the marker:

```python
# Postgres ROW SECURITY isn't used; we enforce at the service layer.  # doc-ref: intentional
```

The validation gate ignores lines tagged this way. Use sparingly.

### Validation

The gate is `scripts/check_no_doc_refs.py`, exposed as `make doc-refs`. It walks the in-scope paths, applies the forbidden-pattern regex set, ignores `# doc-ref: intentional` lines, and exits non-zero with a `file:line` list on any hit.

```
python scripts/check_no_doc_refs.py            # full repo
python scripts/check_no_doc_refs.py --explain  # one line per pattern + fix guidance
python scripts/check_no_doc_refs.py --paths contextplane/service
```

### Task IDs are commit-history anchors, not doc refs

Task IDs of any prefix (`CAP-P7-T20`, `PP-T03`, ...) appear in git commit message subjects (`git log --grep=CAP-P7-T20`). Because git history ships with the repo, these IDs remain resolvable forever.

- In code comments: **do not include them**. Anyone can `git blame` to find the commit.
- In `eval/EVAL.md` only: allowed as commit anchors in a dedicated "Commits" column.

---

## Repo navigation — mental model

| Path | What lives there |
|---|---|
| `contextplane/api/routers/` | HTTP surface — one router per concern. Thin adapters over services. |
| `contextplane/api/middleware/` | Tenant resolution, rate-limit, HTTP-methods router factory. |
| `contextplane/api/mcp/` | The MCP tool surface — one FastMCP server mounted in-process on the same FastAPI app (no sidecar, no separate transport process). Each tool call re-resolves the caller's `TenantContext` the same way the REST middleware does. |
| `contextplane/service/` | Business logic, organized by subdomain (`catalog/`, `memory/`, `retrieval/`, `workspace/`, `governance/`, `platform/`). **Every cross-tenant query MUST funnel through `service/governance/visibility.py`** — bypassing it is how leaks between tenants happen. |
| `contextplane/arc/` | Agent Readiness Context: attested governance artifacts, resolution, receipts, and the authoring surface (source admission, artifact proposals, approval, observation). `contextplane/arc/sandbox/` runs the parser and drafter as OS-isolated subprocesses of the API container itself — no sidecar, no separate pod — under a dedicated `arc-sandbox` group (GID 1500 in the shipped image) so their local sockets come up group-owned and non-world-reachable rather than needing a second container boundary for that alone. |
| `contextplane/workers/` | Background jobs — webhook delivery, closure-cache refresh, the memory-curation sweeps (consolidation, extraction drain, promotion), usage/workspace expiry, and the ARC source-status refresh / checkpoint export workers. |
| `contextplane/wiring/` | The composition root's building blocks — `container` (the per-request `Services` accessor), `services` (constructs every service), `jobs` (the scheduler and its registered jobs), `routes` (every router plus the MCP surface), `openapi`, `tracing`, `http_app` (middleware stack, error envelope, health/readiness probes). `contextplane/main.py::create_app` is the only place they're assembled together. |
| `contextplane/storage/` | SQLAlchemy models + Alembic migrations under `migrations/versions/`. |
| `contextplane/security/` | PII scanner (built-in pattern modules + per-tenant policy resolver). |
| `contextplane/ingest/` | External-source ingest connectors. Credentials resolve dynamically from env (`contextplane/ingest/connector.py::resolve_credential`); they don't live in `Settings`. |
| `scripts/` | Operational CLIs. Config mostly flows through `get_settings()`; a handful of scripts read specific env vars directly (each tagged `# config: intentional`) only where the read must happen before `Settings` can construct, e.g. a pre-flight `DATABASE_URL` presence check. |
| `tests/{unit,integration,conformance,perf}/` | Test pyramid (see below). `tests/airgap/` is a separate boot-check script run inside an isolated, no-egress Docker network by `make test-airgap`; it isn't part of the pytest pyramid or `make all`. |
| `deploy/` | Deployment examples — one Kubernetes Helm chart ships under `deploy/helm/`. The product is deployment-target-agnostic: it only reads `Settings`/env vars, so any other target (ECS, Lambda, systemd, Cloud Run, …) works the same way. |
| `.env.example` | Canonical env-var inventory. The example helm chart in `deploy/helm/` mirrors it; other deployment targets do the same. |

The single most important architectural rule: **`service/governance/visibility.py` is the one chokepoint for cross-tenant queries**. If you're writing a new service that returns entity rows, you must funnel through `filter_entities()` or `assert_visible()`. `make test-hygiene` (`scripts/check_visibility_chokepoint.py`) fails CI when a module reads the `entities` table without importing this module and isn't named in that script's allowlist with a reason.

---

## Testing

Four test buckets with very different purposes — pick the right one when adding a test:

- `tests/unit/` — pure Python, no DB, fast (~1.5s for 1100+ tests). Use SQL-string-keyed `AsyncMock` routers to fake the DB layer. **Default home for new tests.**
- `tests/integration/` — testcontainers Postgres + live FastAPI app via `httpx.ASGITransport`. Use when behaviour spans more than one service or you need real SQL (triggers, constraints, partitioning).
- `tests/conformance/` — contract drift gates (openapi.json snapshot, MCP tool catalog, cross-tenant isolation invariants). Run before tagging a release.
- `tests/perf/` — SLO verification (latency p95s, webhook fan-out). Marked `@pytest.mark.perf @pytest.mark.slow`; excluded from `make test` and reserved for the release pipeline.

When in doubt: write a unit test first. Promote to integration only when the unit version can't exercise the real code path.

**The gates are Make targets.** `make lint`, `make typecheck`, `make doc-refs`, `make test-hygiene`, `make test-unit`, `make test-conformance` are the contract. CI platforms (GitHub Actions, GitLab CI, Jenkins, Buildkite, …) wire these targets — they don't redefine the commands. See `docs/07-contributing/02-ci.md`.

**Coverage ratchet.** `make test-unit` runs with `--cov=registry --cov-report=term-missing:skip-covered --cov-fail-under=80`, printed straight to the CI job log (no separate artifact or upload step — the existing `make test-unit` CI step already carries it). The floor is a ratchet, not a target: it only ever rises. A PR that would lower unit-scope coverage below the current floor must either add coverage to stay above it, or lower the floor deliberately in the same commit with a stated reason — never silently. Raising the floor after adding coverage is the normal, expected way this number moves; re-measure with the command above and set `--cov-fail-under` to the true value, not a number rounded up "for safety margin" — coverage.py's own displayed total is rounded to the nearest integer for the per-file table, but pytest-cov's pass/fail line at the bottom compares against the unrounded float, so pinning the floor to the *displayed* rounded percentage can print a misleading red "FAIL ... not reached" banner on an otherwise-green run. Pin to the integer floor of the true percentage (e.g. a measured 80.90% pins to `80`, not `81`) so the gate's own log output stays unambiguous. Unit-scope only — `tests/integration`, `tests/conformance`, and `tests/perf` have no coverage floor of their own (D4's own scope), and this gate does not chase the number up by writing coverage-only tests with no behavioral assertion; `make test-hygiene`'s assertion-less-test checker is the check against exactly that failure mode.

**Test naming rule.** Test files must describe present-tense system behavior, not delivery history. Phase-named test files and stale phase-marker comments are forbidden in `tests/`. The gate is `make test-hygiene` (`scripts/check_no_phase_named_tests.py`). It runs on every commit alongside `make doc-refs`. If a test legitimately uses "phase" as a domain term unrelated to delivery milestones, end the relevant comment line with `# test-hygiene: intentional`.

**File-size ceiling.** No shipped module under `contextplane/` or `scripts/` may reach 800 lines. The gate is `scripts/check_file_sizes.py`, wired into `make lint`, and it scans both roots — not only whichever subtree a given change touches. A file already at or over the ceiling needs either a cohesion-based split (proven move-only if it touches a route: symbol inventory unchanged, `openapi.json` byte-identical) or a reasoned, named entry in that script's own `ALLOWLIST`/`PERMANENT_EXEMPTIONS`; a bare path with no reason is rejected structurally, and every allowlist entry is independently re-checked so one that is no longer needed fails the gate until removed. `make lint` also runs `scripts/check_arc_approval_writers.py`, an AST-based gate (not a text search — it inspects call sites, not comments) restricting which module may write `artifact_activation` approval evidence to `arc_approval_evidence`; its allowlist starts empty and grows only by a deliberate, reviewed addition.

**Node-only vector gate.** `make arc-vectors` re-verifies the ARC authoring-surface canonical fixtures under `tests/fixtures/arc_authoring/` against a from-scratch Node reference implementation (stdlib-only) that shares no code with the Python canonicalizer. It needs `node` on `PATH` and is deliberately not part of `make all` — nothing else in the gate list depends on Node — so run it directly whenever touching those fixtures or the profile canonicalizer; a CI job wired specifically to `tools/**` and the fixtures runs it on every commit regardless.

---

## Secrets and config

- Every env var the app reads lives in `Settings` (`contextplane/config.py`) and is documented in `.env.example`. The Helm chart under `deploy/helm/` mirrors the same inventory for its one supported deployment wiring.
- **Never commit secrets.** Webhook secrets, OIDC discovery URLs, database passwords, and the extraction provider's API key are operator-provided at deploy time (Kubernetes Secret, ECS task-definition secret refs, systemd EnvironmentFile, etc.).
- Any new env-var read outside `Settings` must carry a same-line `# config: intentional` marker **and** a matching, reasoned entry in `ALLOWLIST` in `scripts/check_config_consolidation.py` (`make test-hygiene`). The marker alone is not the mechanism — a marked read whose file is not registered there fails the gate, and a registered file with no marked read left in it fails as a stale entry. This is the consolidation gate the rest of this section describes; before it existed, this sentence named a check nothing enforced. Seven files are registered today:
  1. `contextplane/ingest/webhook.py` reads `{GITHUB,GITLAB}_WEBHOOK_SECRET` directly to support per-instance secret rotation without an app restart — `Settings` is a frozen snapshot taken once at startup, and a rotated-out secret would keep serving under it.
  2. `contextplane/ingest/connector.py::resolve_credential` resolves connector credentials by dynamic ref string — the set is not fixed, so it cannot live in `Settings`.
  3. `contextplane/api/middleware/http_methods.py::get_mode_settings` reads `REGISTRY_HTTP_METHODS_MODE`/`REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR` directly because routers register their routes at import time, before any `Settings` instance exists to read from. The defaults are deliberately duplicated in both places (the module's own docstring says so); `scripts/export_openapi.py` pins the same variable into the process environment before importing the app for the same reason, which is a write for this reader to see, not a read of its own.
  4. `contextplane/storage/migrations/versions/0001_baseline_schema.py` reads `EMBEDDING_DIM`/`EMBEDDINGS_PARTITION_COUNT` directly at `CREATE TABLE` time — both need integer/positivity validation with an error actionable from a bare `alembic upgrade head` failure, and `EMBEDDINGS_PARTITION_COUNT` has no `Settings` field at all because nothing at application runtime ever needs the partition count after the table exists.
  5. `scripts/bootstrap_dev_tenant.py` — a local-dev-only bootstrap script that never constructs `Settings`; it reads `DATABASE_URL` (presence-check-and-default, before `Settings` could exist regardless) and `OIDC_DISCOVERY_URL`/`ENTITLEMENT_SERVICE_URL` (computed as argparse defaults, evaluated even earlier).
  6. `scripts/seed.py` has the same `DATABASE_URL` presence-check-and-default shape as `bootstrap_dev_tenant.py`, immediately followed by a normal `get_settings()` call that picks up whatever the check just wrote.
  7. `scripts/prove_quickstart.py::baseline_env` builds a deliberately minimal, sanitized subprocess environment for the clean-clone proof's child processes, forwarding only `HOME`/`USER`/`TMPDIR`/`DOCKER_HOST` from this process — the same process-environment-plumbing role as `scripts/devstack/`, just at a single top-level script rather than a whole subtree.
- `scripts/devstack/` and `scripts/load_test/` are out of scope for the consolidation gate entirely (not merely allowlisted): they are local tooling that manages its own process environment — ports, mock-server settings, a Postgres binary directory — to stand up dependencies on a developer's machine, not the shipped app reading its own configuration. `scripts/check_doc_env_mentions.py`'s own allowlist draws the same line for `REGISTRY_PG_BINDIR`.

---

## Agent vs direct edit

When working with this repo via Claude (or any AI agent), the heuristic for whether to spawn a sub-agent or edit inline:

| Situation | Approach |
|---|---|
| Targeted change in a known file (≤ 5 edits) | Direct `Edit`/`Write` |
| Broad codebase exploration / "where is X?" | `Explore` agent (read-only, fast) |
| Bulk sweep across many files / independent scopes | Parallel `backend` agents, one per non-overlapping scope |
| Planning + design | `architect` / `designer` / `product` (writes to `../.context/` only — different repo) |
| Reviewing existing artefacts | `reviewer` (read + score) |

When delegating, the agent will not see this conversation. Brief it like a colleague who just walked in: include the file paths, what you've already tried, and what you expect back.

---

## Commit boundary

This repo and `../.context/` are **independent**. Never `git add` paths outside this directory — `../.context/...`, `../CLAUDE.md`, `../README.md`, and everything else outside `contextplane/` belongs to other repos (or nowhere). The planning workspace commits to its own `.git`; nothing else upstream of this directory is tracked.

---

## Commit messages

- Subject line prefix with the task ID when applicable: `PP-T03: expand CLAUDE.md`, `CAP-P7-T20: ...`.
- Body explains **why**, not what. The diff already shows what.
- Reference resolved test counts or perf numbers at the end of the body when relevant ("1133 unit tests pass; gate exits 0").
- Task IDs in commit subjects are intentional — `git log --grep=PP-T03` is how `EVAL.md`'s commit-anchor column resolves.

---

## Other conventions

(add new project-wide conventions here as they emerge.)
