# deploy/

Production deployment examples for registry. **One example
deployment ships here; substitute your own at any level.**

The product is deployment-target-agnostic — `Settings` reads env vars,
and every env var the app reads is documented in
[`../.env.example`](../.env.example). You can run registry on
Kubernetes, AWS ECS / Fargate, AWS Lambda, EC2 + systemd, Cloud Run,
Nomad, App Runner, or a plain `docker run` — the application doesn't
know or care.

## What's here

| Path | What it is |
|---|---|
| [`helm/`](helm/) | One example Kubernetes deployment chart (with PgBouncer, Grafana dashboards, optional Helm-managed Postgres-as-CRD pattern). Fork or substitute. |

## ARC parser/drafter sandbox knobs

The ARC parser and drafter run as isolated OS subprocesses of the API
container itself — not sidecars, not separate pods — so three settings in
[`helm/values.yaml`](helm/values.yaml) matter beyond their own defaults if
you fork this chart for a different target:

- **`resources.limits.memory` has to cover the base app plus one sandbox
  process at a time**, not the base app alone. See the comment above
  `resources:` in `values.yaml` for the current headroom calculation
  (measured against the shipped embedding model's own footprint).
- **`resources.limits.cpu` is a scheduling quota, not core pinning.** Real
  one-core affinity for a sandbox subprocess needs the cluster's own
  node-level CPU-manager configuration plus this pod in the Guaranteed QoS
  class (requests equal to limits) — a cluster operator's choice this
  chart does not make for you. Outside Kubernetes, `docker run
  --cpuset-cpus` achieves the same restriction directly.
- **`podSecurityContext.runAsGroup` / `fsGroup` must stay `1500`** — the
  `arc-sandbox` group the image's `Dockerfile` creates. The parser and
  drafter sockets under `/tmp` inherit this GID from the process that
  creates them; a different value here silently reopens the
  world-reachable-socket problem the dedicated group exists to close.

Two ARC settings — `ARC_GLOBAL_OPERATOR_ALLOWLIST` and
`ARC_DRAFTER_MODEL_ENABLED` (with `ARC_DRAFTER_MODEL_ARTIFACT_PATH`) — are
plain, non-secret env vars documented in [`../.env.example`](../.env.example)
and [`../docs/05-reference/03-configuration.md`](../docs/05-reference/03-configuration.md).
They are not pre-populated in `values.yaml`'s `env:` block today; set them
with `--set env.ARC_GLOBAL_OPERATOR_ALLOWLIST=...` or by editing that block
directly in a fork.

## What's deliberately not here

- **CI/CD configuration.** A `.github/workflows/` directory exists at
  the repo root — that's the maintainer's CI for releasing this
  product. Consumers wire CI in their own fork using their own
  platform's syntax; the `Makefile` at the repo root defines the
  gates so any CI invocation can stay thin.
- **Local-dev tooling** (`docker-compose.yml`, `prometheus.yml`).
  Those live at the product root because they're for contributors
  working on the code, not for production deployment.
- **Per-target wiring examples** for AWS, GCP, Azure, etc. Choosing
  one over another is operator policy; we don't.
