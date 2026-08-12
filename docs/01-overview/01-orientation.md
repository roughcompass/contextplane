<!--
  title: Orientation
  audience: evaluator, new reader
  archetype: explanation (orientation)
   summary: What DE Context Plane for Agents is, why it exists, and the scenarios it enables.
-->

# Orientation

## What this is

**In one sentence:** DE Context Plane for Agents is a multi-tenant capability catalog and governed context layer. Platform teams publish capabilities so consumers and AI agents can find, evaluate, adopt, and track them. Agents can also recall cited observations and resolve the approved governance context for a task.

Context Plane holds three layers side by side. The **canonical catalog** records approved capability declarations and relationships. **Living Memory** stores cited observations without treating them as canonical. **Agent Readiness Context (ARC)** selects approved governance context for an attested task and records the result in a receipt. Context Plane also maintains a read-optimized projection of content that upstream systems own, such as documentation, release notes, schemas, changelogs, and runtime configuration. Every layer applies an explicit tenant or audience boundary.

REST at `/v1/` and the Model Context Protocol (MCP) surface at `/mcp/sse` expose these capabilities to applications and agents. Both surfaces use the same identity and service foundations. Each surface still documents its own available operations and adapter-level validation.

## Why it exists

Platform teams produce shared foundations — APIs, libraries, design systems, data pipelines — that many other teams depend on. As the number of producers and consumers grows, a recurring set of problems appears: consumers can't easily find what exists, producers have no reliable view of who depends on what, breaking changes land without warning, and compliance questions ("who was using this capability in Q3, and was it audited?") require manual investigation.

Context Plane addresses these problems by giving the catalog a single coherent surface with machine-readable contracts. Producers register their capability's declaration in one place — lifecycle stage, interface contract, ownership — and Context Plane projects in the surrounding context (docs, release notes, schemas, runtime config) from the systems that own those facts. Consumers declare adoptions so producers have a real impact list before shipping a breaking change. Lifecycle events are pushed to subscribers so no team needs to poll or check release notes by hand. And every mutation to declaration-layer data carries a bi-temporal timestamp, so the historical record is always queryable without reconstructing it from logs.

The MCP surface extends the same model to AI agents. An agent planning a build can look up capabilities, traverse dependencies, recall cited claims, resume personal session context, and submit feedback. ARC adds task-bound governance context and receipts when retrieval alone is not enough.

## The core concepts have separate trust boundaries

Start with the question you need to answer:

| Need | Concept |
|---|---|
| Approved capability state and relationships | The canonical catalog described in [How it is structured](02-how-its-structured.md) |
| Cited observations that may change or conflict | [Living Memory and claims](07-living-memory.md) |
| Source standing, score meaning, and safe consumption | [Trust, authority, and confidence](08-trust-and-confidence.md) |
| Tenant isolation, PII controls, history, and erasure | [Data governance and PII](09-data-governance.md) |
| The right lookup, search, graph, memory, or workspace surface | [Retrieval and context](10-retrieval-and-context.md) |
| Mandatory task context with a verifiable decision record | [Attested context resolution](11-attested-context-resolution.md) |

These concepts work together, but they do not collapse into one trust level. A
recalled claim remains untrusted evidence. A workspace note remains scoped to
its owner. An ARC receipt proves what governance context resolution selected.
Only the canonical graph represents approved catalog state.

ARC currently spans both transports. REST requests supply `X-ARC-Host-ID` for
challenge issuance and attested context resolution. MCP supports connection
preflight and receipt reads, but not end-to-end resolution. See
[Attested context resolution](11-attested-context-resolution.md) for this
boundary.

## Where Context Plane sits in the broader platform

Context Plane is one node in a larger ecosystem of platform systems. The three diagrams below focus on capability declarations and configuration ownership. Living Memory and ARC remain inside the Context Plane boundary but are omitted from these diagrams. Their trust flows are documented in the concept pages above.

**1. Where capability facts live, and how they reach Context Plane.**

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
   │  canonical doc URL     │  │   scheduled +    ──►│ Context Plane        │
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
   │ Context Plane        │                tools, dashboards, search)
   │ (Knowledge Graph)    │
   │                      │ ◄──── Agents / copilots  (Context Plane MCP)
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
                  │        Context Plane         │
                  └──────────────────────────────┘
```

Reading the three diagrams together:

- **Each fact has one home.** Capability repositories own code, schemas, changelogs, and doc markdown. The documentation portal owns rendered, published docs and the canonical URL humans read. The release-communications platform owns release notes. The control plane owns runtime configuration and governance enforcement. Context Plane owns the *declaration layer* sitting on top of these — what each capability is, its lifecycle stage, the interface contracts it publishes, who depends on it, who has adopted it, and what feedback it has accumulated.

- **The documentation chain is three layers deep.** Repositories hold the source of doc markdown. The portal renders and publishes that markdown under a canonical URL. Context Plane indexes the portal so agents and other clients can search across all capability docs and cite back to the portal's URLs.

- **Connectors are one-way.** Every arrow into Context Plane is read-only ingestion, scheduled or event-driven. Context Plane never writes back to a source — no doc edits, no release-note mutations, no control-plane changes proposed via Context Plane.

- **There are two distinct MCP surfaces.** The **Context Plane MCP** is for reading platform knowledge — discovering capabilities, traversing the dependency graph, evaluating contracts, declaring adoptions, submitting feedback. The **Control-plane MCP** is for making configuration changes — adjusting capability config under governance. The same agent can call both; they are different interfaces because they serve different concerns.

- **Governance lives in the control plane, not in any UI.** Every config-change path — DE Console (humans), Control-plane MCP (agents), direct API (services and pipelines) — passes through the same enforcement. The console is not where policy lives; it is where compliant configuration is easier to author than non-compliant configuration.

## Use cases

The scenarios below show Context Plane applied to concrete situations. Each links to a full walkthrough.

**[AI agent capability discovery](../03-use-cases/01-ai-agent-capability-discovery.md)** — An AI agent uses the MCP surface to search the catalog, traverse the dependency graph, evaluate interface contracts, and declare adoptions — all within its tool-calling loop, without custom integration beyond a bearer token.

**[Platform team running a shared Context Plane](../03-use-cases/02-platform-team-shared-contextplane.md)** — A platform team provisions tenants, registers capabilities with progression governance, controls visibility, and notifies consumer teams of breaking changes before they land.

**[Mirroring an external source of truth](../03-use-cases/03-mirroring-external-sources.md)** — An operator configures sync connectors to ingest GitHub repositories, OpenAPI specs, npm manifests, or ADR corpora automatically, keeping the catalog current without manual entry.

**[Event-driven consumers](../03-use-cases/04-event-driven-consumers.md)** — A product team subscribes to lifecycle events on capabilities it depends on, receives signed webhook deliveries, verifies signatures, and replays missed notifications from the log.

**[Layered abstractions — consumers becoming producers](../03-use-cases/05-layered-abstractions.md)** — A tenant that adopts upstream primitives republishes higher-level abstractions to its own downstream consumers, forming a multi-layer dependency graph with lifecycle propagation at each level.

**[AISDLC pipeline](../03-use-cases/06-aisdlc-pipeline.md)** — Each stage of a multi-stage AI Software Development Lifecycle is registered as a capability; agents discover and invoke stages via MCP, and telemetry from observability and testing feeds back into Context Plane as artefacts and adoption events.

**[Compliance and audit over a regulated capability inventory](../03-use-cases/07-compliance-and-audit.md)** — Bi-temporal queries reconstruct historical states without touching current data; audit partitions are archived on a configurable schedule; PII scanning applies per-tenant field policies at write time.

**[Workspaces — private scratchpad and agent memory](../03-use-cases/08-workspaces.md)** — Tenant- or actor-owned notebooks of typed Markdown entries (notes, decisions, open questions, saved queries) that humans and agents use to keep shared context as capabilities move through adoption and lifecycle changes.

**[Living Memory turns working evidence into governed knowledge](../03-use-cases/09-living-memory-in-action.md):** Ten scenarios show how developers, agents, operators, and business users reduce duplicate work and route missing answers. They also align interfaces and govern security, vendor, decision, incident, and migration evidence.
