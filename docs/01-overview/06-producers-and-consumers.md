<!--
  title: Producers, consumers, and workspaces
  audience: evaluator, integrator (producer), integrator (consumer), end-user agent
  archetype: explanation (mental model)
  summary: How capabilities flow from a platform team down through a stack of consuming teams, how feedback flows back as annotations, and how workspaces serve as shared memory for the humans and agents working across that stack.
-->

# Producers, consumers, and workspaces

The registry is built around one organizational pattern: some teams **publish** capabilities, other teams **adopt** them, and most teams do both. This page explains the pattern, how feedback flows in the opposite direction, and how [workspaces](../03-use-cases/09-workspaces.md) act as the shared memory layer that humans and agents use to keep their place as capabilities get implemented across a stack.

For the API vocabulary that backs these concepts, see [Concepts](03-vocabulary.md). For the architectural rationale, see [How the registry is structured](02-how-its-structured.md).

---

## The two roles, and why most teams hold both

Two of the four [roles](03-vocabulary.md#roles) the registry recognizes drive the producer/consumer pattern:

- **`producer`** — a team that publishes capabilities. They own the catalog entries, the interface schema, the lifecycle progression, and the responses to feedback.
- **`consumer`** — a team that depends on someone else's capability. They read the catalog, record [adoptions](03-vocabulary.md#adoption), and submit [annotations](03-vocabulary.md#annotation) when something needs to change.

The relationship is **many-to-many**, never 1:1:

- One producer publishes many capabilities.
- One capability is adopted by many consumer teams.
- A single team is usually both a consumer (of foundational capabilities lower in the stack) **and** a producer (of the capabilities they publish to the teams above them).

A platform team sits at the bottom of the stack, publishing foundational pieces — auth, design system, observability, an LLM gateway. Product teams adopt those, then publish their own domain capabilities, which leaf teams (mobile apps) adopt without producing anything downstream. A composite team like a portal sits in the same position but isn't strictly a leaf: it's a stack of sub-teams, and the common serviced storefront sub-team inside it publishes shared pieces (layout shell, checkout flow, cart-state library) to the other portal teams.

---

## The stack, in one picture

```
        ┌─────────────────────────────────────────────────────────┐
        │   capabilities flow ▼            ▲   feedback flows up  │
        └─────────────────────────────────────────────────────────┘

        ╔════════════════════════════════════════════════════════╗
        ║  PLATFORM TEAM    (producer)                           ║
        ║  publishes foundational capabilities:                  ║
        ║   · auth-sdk        · design-system   · metrics-pipe   ║
        ║   · llm-gateway     · feature-flags   · storage-kit    ║
        ╚═══┬═══════════════┬═════════════════┬══════════════┬═══╝
            │               │                 │              │
       auth │       design  │       metrics   │      llm-gw  │
       sdk  │       system  │       pipeline  │              │
            ▼               ▼                 ▼              ▼
        ┌─────────┐    ┌─────────┐       ┌─────────┐    ┌─────────┐
        │PAYMENTS │    │ SEARCH  │       │   WEB   │    │ MOBILE  │
        │  team   │    │  team   │       │  team   │    │  team   │
        │         │    │         │       │         │    │         │
        │consumer │    │consumer │       │consumer │    │consumer │
        │   +     │    │   +     │       │   +     │    │ (leaf:  │
        │producer │    │producer │       │producer │    │ no pub) │
        │         │    │         │       │         │    │         │
        │publishes│    │publishes│       │publishes│    │         │
        │ payments│    │ search- │       │ shell-  │    │         │
        │ -api,   │    │  api    │       │  app,   │    │         │
        │ refunds │    │         │       │ admin-  │    │         │
        │ -svc    │    │         │       │  ui     │    │         │
        └────┬────┘    └────┬────┘       └────┬────┘    └─────────┘
             │              │                 │
             │  payments-api, search-api, shell-app
             └──────────────┼─────────────────┘
                            ▼
                ┌───────────────────────────┐
                │ PORTAL teams              │
                │ (composite: consumers +   │
                │  internal producer)       │
                │                           │
                │ adopts (from upstream):   │
                │   design-system,          │
                │   payments-api,           │
                │   search-api,             │
                │   shell-app               │
                │                           │
                │ internally, a common      │
                │ serviced storefront team  │
                │ publishes shared pieces   │
                │ (layout shell, checkout,  │
                │ cart-state) to the other  │
                │ portal sub-teams          │
                └───────────────────────────┘

   ◀══ annotations: bug · doc_gap · suggestion · question · feedback ══
       Every consumer can write feedback against any capability it
       adopts. Producers see all annotations on their capabilities;
       consumers see only what they themselves authored.


   ┌────────────────────────────────────────────────────────────────┐
   │  WORKSPACES — shared memory for humans AND agents              │
   │                                                                │
   │  Each team (and each agent) owns workspaces holding typed      │
   │  entries:                                                      │
   │   · note            · decision         · open_question         │
   │   · saved_query     · saved_view       · private_annotation    │
   │                                                                │
   │  Humans use them as a private scratchpad anchored to catalog   │
   │  entities. Agents use them as persistent cross-session memory  │
   │  — decisions written at the end of one session retrieved at    │
   │  the start of the next. Workspaces never cross tenant lines.   │
   └────────────────────────────────────────────────────────────────┘
```

Read top-to-bottom for capability flow; bottom-to-top for feedback flow.

---

## How capabilities flow downstream

A producer team publishes a capability via the standard catalog write path — see [Publish a capability](../04-guides/01-publish-a-capability.md) for the step-by-step. The published capability becomes discoverable to consumer tenants through [visibility rules](03-vocabulary.md#visibility): `private` (default), `tenant-shared`, or `public`.

A consumer team adopts a capability by writing an [adoption](03-vocabulary.md#adoption) record. The adoption is visible to the producer, so they know who depends on them — important when planning a breaking change. The [`/v1/capabilities/{id}/preview-version`](03-vocabulary.md#breaking-change) endpoint then lets a producer see, before publishing, which adopters would be affected.

Most teams in the middle of the stack are doing both at once: adopting from the layer below them and publishing to the layer above.

---

## How feedback flows upstream

Capability flow has a counter-current: feedback. The carrier is the [annotation](03-vocabulary.md#annotation).

Any consumer that can see a capability can submit an annotation against it. The five categories — `bug`, `doc_gap`, `feedback`, `question`, `suggestion` — cover the everyday surface area of "something about your capability needs your attention." Producers triage through the `open → triaged → acknowledged → closed` lifecycle; the producer tenant sees every annotation on its capabilities, including those from other consumer tenants, while a consumer sees only what it has authored itself.

Annotation traffic is how a multi-team stack stays coherent without a parallel ticketing system: the feedback lives next to the capability it concerns, and a producer can answer "what are people running into with auth-sdk?" by reading the catalog they already own.

---

## Why workspaces are the missing piece

Capabilities and annotations cover the **published, shared** state of the catalog. They are not the right home for everything an actor — human or agent — needs to remember while working across the stack:

- A platform engineer evaluating two candidate replacements for `auth-sdk` needs to record what they tried, what they ruled out, and why. None of that belongs in a public capability description; an annotation against a competitor's capability is the wrong shape.
- An agent doing the same evaluation across sessions needs the *same* persistence — otherwise every session starts from scratch and reaches a different conclusion than the previous one.
- A consumer team running a migration to `payments-api v2` accumulates internal notes, saved queries ("show me every entity still tagged `payments-v1`"), and decisions ("we will skip the optional `refund_reason` field on legacy orders"). This is durable working memory that belongs to *that team*, not the producer.

A [workspace](../03-use-cases/09-workspaces.md) is exactly this: a tenant-scoped or actor-scoped container of typed Markdown entries, with optional references to capability UUIDs to anchor the entry to the catalog. Visibility is determined by `owner_kind`: an `actor`-owned workspace is private to that actor; a `tenant`-owned workspace is visible to every actor in the owning tenant. Workspaces never cross tenant boundaries.

The same primitive serves humans as a private scratchpad and agents as cross-session memory. As capabilities ripple through the stack — a new auth-sdk version, a deprecation of payments-v1 — the team and agent workspaces collect the in-flight context that makes the rollout coherent. For full scenarios and request examples, see [Use case: Workspaces](../03-use-cases/09-workspaces.md).

---

## Where to go next

| I want to… | Go to |
|---|---|
| Publish a capability as a producer | [guides/publish-a-capability.md](../04-guides/01-publish-a-capability.md) |
| Subscribe to changes on a capability I adopt | [guides/subscribe-to-events.md](../04-guides/02-subscribe-to-events.md) |
| See full workspace request examples | [use-cases/workspaces.md](../03-use-cases/09-workspaces.md) |
| Look up the exact API endpoints | [reference/api.md](../05-reference/01-api.md) |
| Call from an AI agent | [reference/mcp-tools.md](../05-reference/02-mcp-tools.md) |
| Understand the term *capability*, *annotation*, *adoption* | [overview/vocabulary.md](03-vocabulary.md) |
| See the full workspace primitive (entries, owner kinds, visibility) | [use-cases/workspaces.md](../03-use-cases/09-workspaces.md) |
