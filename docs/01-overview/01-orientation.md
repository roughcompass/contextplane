<!--
  title: Orientation
  audience: evaluator, new reader
  archetype: explanation (orientation)
  summary: What the registry is, why it exists, and the scenarios it enables.
-->

# Orientation

## What this is

**In one sentence:** The registry is a multi-tenant capability catalog and knowledge graph: platform teams publish capability declarations into it so the artefacts they ship — shared services, libraries, agents — are discoverable, and consumer teams and AI agents use it to find, evaluate, adopt, and track those artefacts over time.

The registry holds two kinds of data side by side. It is the source of truth for the **capability-declaration layer**: what each capability is, where it sits in its lifecycle, what interface contracts it publishes, which capabilities depend on it, which consumers have adopted it, and the feedback they have submitted. It is also a **read-optimized projection** of content that lives upstream — doc markdown rendered by the documentation portal, release notes from the release-communications platform, schemas and changelogs from capability repositories, runtime configuration from the control plane. Every piece of data carries a tenant boundary and a full audit trail. Two parallel surfaces expose all of it — a REST API at `/v1/` and a Model Context Protocol surface at `/mcp/sse` — so both human developers and AI agents read through the same endpoints, and write to the declaration layer through those same endpoints, with no custom integration.

## Why it exists

Platform teams produce shared foundations — APIs, libraries, design systems, data pipelines — that many other teams depend on. As the number of producers and consumers grows, a recurring set of problems appears: consumers can't easily find what exists, producers have no reliable view of who depends on what, breaking changes land without warning, and compliance questions ("who was using this capability in Q3, and was it audited?") require manual investigation.

The registry addresses these problems by giving the catalog a single coherent surface with machine-readable contracts. Producers register their capability's declaration in one place — lifecycle stage, interface contract, ownership — and the registry projects in the surrounding context (docs, release notes, schemas, runtime config) from the systems that own those facts. Consumers declare adoptions so producers have a real impact list before shipping a breaking change. Lifecycle events are pushed to subscribers so no team needs to poll or check release notes by hand. And every mutation to declaration-layer data carries a bi-temporal timestamp, so the historical record is always queryable without reconstructing it from logs.

The MCP surface extends the same model to AI agents: an agent planning a build can look up what exists, check interface contracts, and submit feedback — with no custom integration beyond a bearer token.

## Where the registry sits in the broader platform

The registry is one node in a larger ecosystem of platform systems. The three diagrams below show, in turn: where capability knowledge lives and how it reaches the registry, how people and agents read that knowledge, and how configuration changes flow through the separate control-plane surface that enforces governance.

**1. Where capability facts live, and how they reach the registry.**

```
   ┌────────────────────────┐
   │  Capability repos      │ ─┐
   │  code, schemas,        │  │
   │  changelogs,           │  │
   │  doc markdown          │  │
   └────────────────────────┘  │
                               │
   ┌────────────────────────┐  │
   │  Documentation portal  │  │   connectors
   │  rendered docs,        │  ├─► (one-way,         ┌──────────────────────┐
   │  canonical doc URL     │  │   scheduled +    ──►│ Capability Registry  │
   └────────────────────────┘  │   event-driven)     │ (Knowledge Graph)    │
                               │                     └──────────────────────┘
   ┌────────────────────────┐  │
   │  Release-comms platform│  │
   │  release notes         │  │
   └────────────────────────┘  │
                               │
   ┌────────────────────────┐  │
   │  Control plane         │  │
   │  runtime config,       │  │
   │  governance state      │  │
   └────────────────────────┘ ─┘
```

**2. How people and agents read knowledge.**

```
   ┌──────────────────────┐
   │ Documentation portal │ ◄──── Humans  (canonical URL for
   │ canonical doc URL    │                reading docs in a browser)
   └──────────────────────┘


   ┌──────────────────────┐ ◄──── Humans  (REST API — for internal
   │ Capability Registry  │                tools, dashboards, search)
   │ (Knowledge Graph)    │
   │                      │ ◄──── Agents / copilots  (Registry MCP)
   └──────────────────────┘
```

**3. How configuration changes happen — the write path.**

```
       Humans                            Agents / copilots
         │                                       │
         │ web UI                                │ MCP
         ▼                                       ▼
   ┌───────────────────┐               ┌──────────────────────┐
   │  DE Console       │               │  Control-plane MCP   │
   │  (UI over the     │               │  (agent write        │
   │   control plane)  │               │   surface)           │
   └─────────┬─────────┘               └──────────┬───────────┘
             │                                    │
             │       every write goes through     │
             └────────────────┬───────────────────┘
                              ▼
                  ┌──────────────────────────────┐
                  │       Control Plane          │
                  │   ────────────────────────   │
                  │   Enforces governance rules  │
                  │   identically on every write │
                  │   path. Holds runtime config.│
                  └───────────────┬──────────────┘
                                  │
                                  │ connector (one-way) — the
                                  │ registry observes the new
                                  │ state but does not
                                  ▼ participate in the write.
                  ┌──────────────────────────────┐
                  │     Capability Registry      │
                  └──────────────────────────────┘
```

Reading the three diagrams together:

- **Each fact has one home.** Capability repositories own code, schemas, changelogs, and doc markdown. The documentation portal owns rendered, published docs and the canonical URL humans read. The release-communications platform owns release notes. The control plane owns runtime configuration and governance enforcement. The registry owns the *declaration layer* sitting on top of these — what each capability is, its lifecycle stage, the interface contracts it publishes, who depends on it, who has adopted it, and what feedback it has accumulated.

- **The documentation chain is three layers deep.** Repositories hold the source of doc markdown. The portal renders and publishes that markdown under a canonical URL. The registry indexes the portal so agents and other clients can search across all capability docs and cite back to the portal's URLs.

- **Connectors are one-way.** Every arrow into the registry is read-only ingestion, scheduled or event-driven. The registry never writes back to a source — no doc edits, no release-note mutations, no control-plane changes proposed via the registry.

- **There are two distinct MCP surfaces.** The **Registry MCP** is for reading platform knowledge — discovering capabilities, traversing the dependency graph, evaluating contracts, declaring adoptions, submitting feedback. The **Control-plane MCP** is for making configuration changes — adjusting capability config under governance. The same agent can call both; they are different interfaces because they serve different concerns.

- **Governance lives in the control plane, not in any UI.** Every config-change path — DE Console (humans), Control-plane MCP (agents), direct API (services and pipelines) — passes through the same enforcement. The console is not where policy lives; it is where compliant configuration is easier to author than non-compliant configuration.

## Use cases

The scenarios below show the registry applied to concrete situations. Each links to a full walkthrough.

**[AI agent capability discovery](../03-use-cases/01-ai-agent-capability-discovery.md)** — An AI agent uses the MCP surface to search the catalog, traverse the dependency graph, evaluate interface contracts, and declare adoptions — all within its tool-calling loop, without custom integration beyond a bearer token.

**[Platform team running a shared registry](../03-use-cases/02-platform-team-shared-registry.md)** — A platform team provisions tenants, registers capabilities with progression governance, controls visibility, and notifies consumer teams of breaking changes before they land.

**[Mirroring an external source of truth](../03-use-cases/03-mirroring-external-sources.md)** — An operator configures sync connectors to ingest GitHub repositories, OpenAPI specs, npm manifests, or ADR corpora automatically, keeping the catalog current without manual entry.

**[Event-driven consumers](../03-use-cases/04-event-driven-consumers.md)** — A product team subscribes to lifecycle events on capabilities it depends on, receives signed webhook deliveries, verifies signatures, and replays missed notifications from the log.

**[Layered abstractions — consumers becoming producers](../03-use-cases/06-layered-abstractions.md)** — A tenant that adopts upstream primitives republishes higher-level abstractions to its own downstream consumers, forming a multi-layer dependency graph with lifecycle propagation at each level.

**[AISDLC pipeline](../03-use-cases/07-aisdlc-pipeline.md)** — Each stage of a multi-stage AI Software Development Lifecycle is registered as a capability; agents discover and invoke stages via MCP, and telemetry from observability and testing feeds back into the registry as artefacts and adoption events.

**[Compliance and audit over a regulated capability inventory](../03-use-cases/08-compliance-and-audit.md)** — Bi-temporal queries reconstruct historical states without touching current data; audit partitions are archived on a configurable schedule; PII scanning applies per-tenant field policies at write time.
