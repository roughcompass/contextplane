# How the registry is structured

This page explains the shape of the system: the core objects, how isolation works, how the API surfaces are organized, and where the moving parts live. For what the registry is and why it exists, see [orientation.md](01-orientation.md).

---

## Tenants and isolation

Business data carries an explicit tenant or deployment scope. A caller
authenticated with tenant A cannot read or write tenant B's data merely by
changing a query. Canonical entity reads pass through the service-layer
visibility chokepoint in `service/governance/visibility.py`. Session, workspace,
claim, and ARC services add the actor, subject, owner, or audience checks their
records require. The conformance suite tests these isolation boundaries.

Tenants are provisioned by an operator with database access. Each tenant has a
`slug` and a `display_name`. Entities within a tenant can be shared with
specific other tenants (`tenant-shared` visibility) or opened to all tenants in
the deployment (`public`). The default is `private`.

---

## The data model

An **[entity](03-vocabulary.md#entity)** is the primary tracked object. It carries a small set of fixed columns (name, type, lifecycle, visibility, timestamps); richer data is attached via [attributes](03-vocabulary.md#attribute), [facts](03-vocabulary.md#fact), and [edges](03-vocabulary.md#edge) — see [vocabulary.md](03-vocabulary.md) for the definitions of each term.

[Bi-temporality](03-vocabulary.md#bi-temporal-time-travel) tracks two independent time axes on canonical attributes, facts, and edges: when the data was *valid in the world* and when the registry recorded it. This lets an authorized caller ask "what did this entity look like as of last quarter?" without changing current data. A transitive closure cache over edges enables fast blast-radius queries without recursive SQL on hot paths.

## The context layers stay distinct

The canonical catalog is not the only context surface:

- [Living Memory and claims](07-living-memory.md) stores cited observations in
  a staging layer before owner-controlled promotion.
- [Workspaces](../03-use-cases/08-workspaces.md) store deliberate actor- or
  tenant-owned notes, decisions, and saved queries.
- Session memory stores an actor's ordered conversation events for exact replay.
- [Agent Readiness Context](11-attested-context-resolution.md) resolves
  approved governance artifacts against an attested task and returns a receipt.

[Retrieval and context](10-retrieval-and-context.md) explains which read surface
answers each question. These layers share one application and database, but they
do not share one trust label or visibility rule.

---

## Progression and governance

Each entity type can have a progression definition: a state machine that
specifies valid lifecycle states (alpha → beta → ga → deprecated → retired),
allowed transitions, and attribute gates that must be satisfied before a
transition proceeds. Definitions are bi-temporal — you can update them without
losing the history of what was enforced before.

An override mechanism allows a single entity to bypass one specific gate within
a time window, for a stated reason. Each override is single-use and generates
an audit record before it is inserted, so the bypass is always traceable.

---

## API surfaces

The registry exposes two parallel surfaces:

- **REST API** at `/v1/` — resource-oriented HTTP endpoints for capabilities,
  attributes, facts, edges, adoption tracking, subscriptions,
  notifications, external ID mapping, interface/artifact/operation/concept
  management, breaking-change previews, and admin operations. Mutation verbs
  (PATCH, DELETE) can be run in POST-tunneled mode for environments that
  restrict non-GET/POST HTTP methods.
- **MCP surface** at `/mcp/sse` — Model Context Protocol tools for AI-agent
  callers. Tools cover catalog retrieval, graph traversal, notifications,
  workspaces, actor-scoped session replay, Living Memory, curation, capability
  requests, and selected ARC connection and receipt operations.
  Auth uses the same bearer token as the REST API.

Both surfaces are served by the same FastAPI process. The OpenAPI spec is live
at `/openapi.json`; the MCP tool catalog is validated by the conformance suite
on every PR.

---

## Ingest and sync

The `contextplane/ingest/` package ingests external sources — GitHub repositories, OpenAPI
specs, npm `package.json` files, markdown and ADR corpora, release notes — and
populates entity facts automatically. Each connector follows a two-step
pattern: `fetch` pulls raw data from the external source, `parse` is a pure
function that produces structured records with no I/O or side effects. Connector
credentials come exclusively from environment variables at runtime; they are
never stored in the database.

---

## Extension points

| Extension point | Where to look | What to implement |
|---|---|---|
| New sync connectors | `contextplane/ingest/connectors/` | Subclass `Connector`; implement `fetch` and `parse` |
| Custom PII patterns | `contextplane/security/pii_patterns/` | Add a pattern module; register in the scanner |
| Progression definitions | Admin API — `POST /v1/admin/progression-definitions` | JSON schema; no code change required |
| Custom vocabulary | Admin API — `POST /v1/admin/vocabulary` | Operator-provisioned; scoped per tenant |
| Additional MCP tools | `contextplane/api/mcp/tools/` | Add a module-level function to the matching domain module (or a new one) and call it from that module's `register()`; wire the module's `register()` into `contextplane/api/mcp/server.py::create_contextplane_mcp_server` |

For the API contract shapes, see [reference/api.md](../05-reference/01-api.md) and [reference/mcp-tools.md](../05-reference/02-mcp-tools.md).
