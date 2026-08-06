<!--
  title: Retrieval and context
  audience: evaluator, integrator, agent builder
  archetype: explanation (mental model)
  summary: How callers choose canonical lookup, hybrid search, graph traversal, claim recall, workspace search, session replay, or attested context resolution.
-->

# Retrieval and context

The registry provides several read surfaces because “find relevant context” can
mean different things. A caller may need an authoritative record, a ranked
discovery result, a remembered observation, a prior decision, an exact session
turn, or a governance bundle that proves what rules applied.

Choosing the wrong surface creates avoidable risk. Search is not a substitute
for direct lookup. A high-confidence claim is not canonical state. A workspace
note is not evidence about a capability. An ARC receipt is not a catalog search
result.

---

## Choose by question

| Question | Use | Why |
|---|---|---|
| “What is this known capability?” | `get_capability` | Deterministic canonical lookup by UUID or slug |
| “Which capability owns this package or repository?” | `lookup_by_external_id` | Deterministic mapping from an upstream identifier |
| “What capabilities match this description?” | `search_capabilities` | Ranked semantic, lexical, and graph discovery |
| “What depends on this capability?” | Graph traversal tools | Deterministic dependency edges and blast radius |
| “What has been observed about this subject?” | `query_claims` | Exact structural lookup over cited Living Memory claims |
| “What remembered claim is similar to this question?” | `search_claims` | Semantic and lexical claim recall |
| “What decision did this actor or team record?” | Workspace search | Deliberate Markdown entries under actor or tenant ownership |
| “What happened in my previous conversation?” | Session replay | Exact actor-scoped event sequence |
| “What governance context applied before this action?” | ARC resolution and receipt tools | Deterministic selected directives with attestable evidence |

Call `whoami` before other MCP tools. Tenant selection and roles affect every
result, and a multi-grant token may need `X-Tenant-ID` on REST requests.

## Direct lookup is the preferred known-item path

Use a UUID when a previous response supplied one. Use an external-ID lookup when
the caller has a package name, repository slug, or another registered upstream
identifier. Use a capability slug for interactive lookup when rename risk is
acceptable.

Direct lookup avoids ranking uncertainty. It returns the canonical entity and
its selected subresources after visibility checks. It should be the default for
stored references and repeatable automation.

## Capability search discovers canonical catalog content

Capability search runs three arms concurrently:

- The **semantic arm** compares the query embedding with indexed fact content.
- The **lexical arm** ranks exact and token-level matches from fact text.
- The **graph arm** expands from matching entity names over selected
  relationships.

The service fuses ranks with stable weights, drops invisible entities, and
returns the best canonical capability matches. A failed arm is excluded and its
weight is redistributed. An empty arm remains a valid empty result.

Staged claims do not change capability search ranking or canonical capability
reads. A claim affects these paths only after an accepted promotion writes a
canonical attribute or edge.

Use search to discover unknown capabilities. After selecting a result, switch
to direct lookup or graph traversal for deterministic follow-up work.

## Graph traversal answers relationship questions

`get_dependencies`, `get_dependents`, and `get_blast_radius` follow canonical
edges. They answer topology questions that text search cannot answer reliably.

Use a bounded dependency traversal for nearby relationships. Use blast radius
when the complete transitive impact set matters. Historical traversals accept
an `as_of` instant and can fall back to recursive queries when a cache cannot
serve that time slice.

## Claim retrieval searches observations, not catalog truth

`query_claims` filters by subject, predicate, category, namespace, confidence,
and time. It is the preferred claim path when the caller knows the structure of
the question.

`search_claims` accepts prose and fuses semantic and lexical rankings. It helps
when the caller remembers the meaning but not the predicate or subject.

Every returned claim includes citations, confidence, authority, time basis,
and an untrusted recall label. Persona selection changes which categories and
citation excerpts are returned. It does not change the claim.

Do not merge a claim value into an authoritative answer without preserving its
trust label and citations. Read [Trust, authority, and confidence](08-trust-and-confidence.md)
for consumption guidance.

## Workspace search retrieves deliberate notes

Workspace entries are actor-owned or tenant-owned Markdown. Search is full-text
and can filter by entry kind and referenced entity IDs. It does not use the
claim embedding index and does not calculate claim confidence.

Use workspaces for decisions, open questions, saved queries, and notes that the
actor or team intentionally recorded. A workspace reference anchors a note to a
capability but does not turn the note into a catalog fact.

## Session replay restores exact conversation state

Session events are ordered by sequence number and scoped to the calling actor.
Reverse replay with a small limit retrieves the latest turns after a process
restart. This is exact recall, not semantic search.

Session extraction can create claims from selected events, but the event remains
separate from the claim. Deleting an event from replay does not imply that every
non-personal assertion derived from mixed evidence disappears.

## ARC selects governed context and records the decision

Agent Readiness Context (ARC) answers a different question from search: “Which
approved directives and obligations apply to this attested task, under this
budget, and what proves they were selected?”

ARC resolves an authenticated and attested manifest against active governance
artifacts. It returns a bounded context bundle and an immutable receipt. A
blocked policy resolution still returns a successful transport response with a
receipt explaining the block.

Use ARC when context is mandatory, audience-scoped, conflict-resolved, and
needs later verification. Use catalog and claim retrieval for discovery and
knowledge recall. Read [Attested context resolution](11-attested-context-resolution.md)
for the model.

## The registry is a context layer, not a text generator

In a retrieval-augmented generation workflow, the registry owns retrieval and
context packaging. The calling agent or application owns final generation.

A safe agent flow often looks like this:

1. Resolve identity with `whoami`.
2. Use direct lookup when the subject is known.
3. Use capability search when the subject is unknown.
4. Traverse canonical edges for dependencies and impact.
5. Add claim recall only when observations may help.
6. Add workspace or session context only within its owner boundary.
7. Resolve ARC when governed directives apply.
8. Preserve citations, trust labels, and receipt IDs in the generated answer or
   action record.

The registry does not decide how a model synthesizes the returned context. It
provides scoped records, rankings, evidence, and governance signals so the
caller can make that decision explicitly.

## Read next

- [Living Memory and claims](07-living-memory.md)
- [Trust, authority, and confidence](08-trust-and-confidence.md)
- [Attested context resolution](11-attested-context-resolution.md)
- [AI agent capability discovery](../03-use-cases/01-ai-agent-capability-discovery.md)
- [MCP tools reference](../05-reference/02-mcp-tools.md)
