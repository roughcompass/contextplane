# 0004 — Scoring configuration: committed core, tenant override by binding

**Status:** Accepted 2026-08-19

## Context

Three scoring quantities (see ADR-0002) carry numbers that decide what is
remembered, what is believed, and what is promoted. Those numbers must be
configurable per tenant — a tenant whose corpus, risk appetite or storage
economics differ needs different weights — without becoming an untracked
runtime dial.

Two mechanisms already exist and are not alternatives to each other:

- `contextplane/ranking_registry.json`, read by `contextplane/ranking.py`,
  holds committed magnitudes with a mandatory written reason each, refuses an
  unknown id, a form/payload mismatch and an empty population.
- The profile system publishes a **core revision** and lets a tenant publish an
  **extension** of it, bound through `plan → validate → activate → rollback`
  with a temporal exclusion constraint admitting one active binding per tenant.

## Decision

The registry holds the **core default** for every scoring magnitude: the value
that governs a tenant which has published no extension. A tenant overrides by
publishing a profile **extension** carrying its own values, activated through
the existing binding lifecycle.

This is the composition the profile system was built for — core plus extension,
never replacement — applied to scoring rather than to entity schemas. Resolution
is: active binding's extension if one exists, else the committed core.

An override is a governed publication, not a setting. It plans, validates,
activates, and can be rolled back onto its recorded target, and while it is
merely planned it governs nothing.

No environment variable and no `Settings` field may set any of these values. A
weight deciding what an agent remembers is not deployment configuration.

## Assumptions

- Tenants will actually want different weights. If none ever does, the core
  registry alone would have sufficed and the extension path is unused
  machinery — but unused, not wrong, since the registry half carries the load.
- Calibration is per-tenant once weights are. A tenant on its own weights needs
  its own reliability check; a global calibration curve would describe a
  population no tenant matches. This is the largest hidden cost of this
  decision and is accepted deliberately.
- The binding lifecycle's latency is acceptable for a weight change. Weights
  are not an incident lever, so a governed publication is the right speed.

## Alternatives rejected

- **Registry only, no tenant override.** Simpler, and the recommendation before
  this decision. Rejected by the operator: multi-tenant deployments need
  per-tenant weights, and retrofitting that later means migrating live values.
- **Environment variables or `Settings`.** Fast and untraceable. An unreviewed
  number that decides what is remembered is exactly what the registry exists to
  prevent.
- **A database table with an admin endpoint.** Gives runtime tuning at the cost
  of the review trail; a weight could change with no PR, no reason recorded and
  no rollback target.

## Consequences

Two resolution paths for every scoring magnitude, which means every consumer
resolves through one accessor rather than reading the registry directly — the
accessor is where tenant resolution happens, and a consumer that skips it
silently ignores tenant overrides.

Per-tenant calibration follows, with the ground-truth volume that implies.
Deployments with one tenant pay none of this: no extension, no separate curve.

`contextplane/ranking.py` sits at the bottom import layer and cannot reach the
profile system, which is far above it. The tenant-resolving accessor therefore
cannot live in `ranking.py`; it belongs beside the profile services, with
`ranking.py` remaining the core-default reader. This is a real constraint of the
layering, discovered while writing this ADR, and it shapes the implementation.

## Dissent

I recommended registry-only and the operator chose tenant-scoped, on the
grounds that multi-tenant weight variation is a requirement rather than a
possibility. The disagreement is recorded because the cost it buys —
per-tenant calibration — lands later than the decision does, and whoever meets
that cost should be able to find the reasoning.
