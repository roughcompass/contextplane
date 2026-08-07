<!--
  title: Retrieval and context
  audience: evaluator, integrator, agent builder
  archetype: explanation (mental model)
  summary: How callers retrieve approved Registry records, observed knowledge, task memory, session history, and attested governance context without flattening trust.
-->

# Retrieval and context

The registry provides several read surfaces because “find relevant context” can
mean different things. A caller may need an approved Registry record, ranked
discovery, observed knowledge, task memory, an exact session turn, or a
governance bundle that proves what rules applied.

Choosing the wrong surface creates avoidable risk. Search is not a substitute
for direct lookup. A high-confidence claim is not canonical state. A workspace
note is not evidence about a capability. An ARC receipt is not a catalog search
result.

---

## Choose by question

| Question | Use | Why |
|---|---|---|
| “What is this approved capability?” | `get_capability` | Deterministic canonical lookup by UUID or slug |
| “Which capability owns this package or repository?” | `lookup_by_external_id` | Deterministic mapping from an upstream identifier |
| “What capabilities match this description?” | `search_capabilities` | Ranked semantic, lexical, and graph discovery |
| “What depends on this capability?” | Graph traversal tools | Deterministic dependency edges and blast radius |
| “What has been observed about this subject?” | `query_claims` | Exact structural lookup over cited, untrusted Living Memory claims |
| “What observed claim is similar to this question?” | `search_claims` | Semantic and lexical observed-knowledge recall |
| “What checkpoint or decision did this actor or tenant record?” | Workspace search | Lexical and reference lookup over mutable task memory |
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

## Claim retrieval searches observed knowledge, not approved state

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

## Workspace search retrieves task memory

Workspace entries are actor-owned or tenant-owned Markdown. Search is full-text
and can filter by entry kind and referenced entity IDs. It does not use the
claim embedding index and does not calculate claim confidence.

Use workspaces for checkpoints, decisions, open questions, saved queries, and
notes that an actor or tenant intentionally recorded. A workspace reference
anchors task memory to a capability but does not turn the note into evidence or
a catalog fact. Current actor-owned workspaces are personal. Current
tenant-owned workspaces are readable by every role holder in the tenant, so
they are not a selected task-participant boundary.

Workspace search is lexical and reference-based today. Use known work,
repository, session, or capability references for deterministic resume. Do not
assume semantic similarity search is available.

Any future semantic workspace path must first show that task memory improves
continuity over no workspace memory, then show material improvement over
lexical and reference search. The comparison must use pre-registered scenarios,
metrics, thresholds, and a judge rubric, with human review of a risk-based
sample and every safety failure. It must also satisfy the visibility and
derivative protections described in
[Workspaces as task memory](../03-use-cases/08-workspaces.md#searching-across-workspaces).

## Combined context must preserve its source blocks

An application may need approved catalog records, observed claims, and task
memory in one decision. Keep them in separate labelled blocks. Preserve source,
trust, citations, freshness, mutability, and exclusions for each item. Do not
concatenate all text into an unlabeled context field or let ranking turn a
workspace note into an approved fact.

## Feedback must identify the served context

Every feedback, outcome, judge label, or learning record used to evaluate
context or derive a claim must identify the receipt for the served context that
informed the decision. Item-specific feedback must also identify the exact
served item. An observation without that linkage is diagnostic input. It cannot
be presented as context feedback or used to derive learning.

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
5. Add observed-knowledge claim recall only when cited observations may help.
6. Add workspace task memory or session context only within its owner boundary.
7. Resolve ARC when governed directives apply.
8. Keep approved records, observed knowledge, and task memory in labelled
   blocks.
9. Preserve citations, trust labels, and receipt IDs in the generated answer or
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
