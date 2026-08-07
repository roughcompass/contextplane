<!--
  title: Use case: Living Memory turns working evidence into governed knowledge
  audience: integrator, agent builder, producer, operator, business user
  archetype: explanation (use-case scenarios)
  summary: Ten realistic scenarios show how developers, agents, operators, and business users reduce friction without confusing observations with approved catalog state.
-->

# Use case: Living Memory turns working evidence into governed knowledge

The registry is most useful when it makes the easier action the governed one.
A coding agent should find an approved capability before rebuilding it. A
business user should route a missing answer without finding the right team by
hand. A security platform should connect a central finding to internal owners
without declaring every dependent vulnerable.

These ten scenarios start with those common frictions. They then cover
design-system consistency, vendor risk, decisions, incidents, and migrations.
Each scenario uses services that ship with the registry and names any work that
remains outside it.

Read [Living Memory and claims](../01-overview/07-living-memory.md) for the
system model. Model Context Protocol (MCP) tool inputs and response fields
remain in the
[MCP tools reference](../05-reference/02-mcp-tools.md#memory-curation).

---

## Use the smallest durable surface that solves the problem

Not every useful interaction should create a claim or change the catalog. The
registry provides separate surfaces for approved state, observations, working
memory, requests, and dependency impact.

| Need | Use | Why |
|---|---|---|
| Find something already approved | Catalog search and capability lookup | Canonical state should answer before an agent invents new state |
| Checkpoint an evaluation or migration task | Actor- or tenant-owned workspace | Working decisions and handoffs remain scoped and do not claim universal truth |
| Ask the owner for an answer or change | Capability request | The request reaches the subject's owner and has a visible lifecycle |
| Record a cited assertion about a capability | Living Memory claim | The observation remains untrusted until consolidation and review |
| Understand who may be affected | Dependency or blast-radius traversal | The graph returns visible known paths without inferring unknown ones |

When an observation does become a claim, it follows a governed path. The
registry resolves the subject, records evidence, and preserves disagreement.
It routes review to the owner and writes or reverses one typed canonical target.

## The highest-impact examples address daily friction first

| Scenario | Role | Friction removed | Registry outcome |
|---|---|---|---|
| Reuse before rebuilding | Developer or agent | Duplicate implementation and tool sprawl | Finds an approved capability, records the decision, and declares adoption |
| Turn a missing answer into an owner-routed request | Any MCP user | Slack archaeology and support-ticket bouncing | Gives the owner a request with a trackable lifecycle |
| Keep product interfaces on the approved design-system path | Frontend developer or agent | Inconsistent interfaces and stale packages | Connects package-level drift to owners and migration guidance |
| Connect central security findings to internal topology | Application security | Manual owner and blast-radius mapping | Finds visible known dependency paths for assessment |
| Trace a vendor API sunset into products | Procurement or product | Vendor notices disconnected from engineering impact | Connects the notice to visible known consumers and owners |
| Keep agents on the current decision | Architect, product, or agent | Stale standards and contradictory guidance | Preserves a cited decision and its supersession chain |
| Detect a breaking interface before the catalog catches up | Consumer team or agent | Provider-consumer timing gaps | Routes the observed contract change to the provider |
| Turn incident evidence into missing topology | Reliability engineer or agent | Impact analysis based on an incomplete graph | Promotes a reviewed dependency edge |
| Reuse a proven migration pattern | Developer or migration agent | Every team rediscovering the same fix | Recalls scoped guidance linked to the affected capability |
| Correct stale response data after a failed page | Operator or support agent | Wrong runbook or paging destination | Reconciles conflicting operational claims |

## Scenario 1: Reuse an approved capability before rebuilding it

A coding agent receives a task to add enterprise file uploads. Before generating
a new service, it searches for capabilities that already provide malware
scanning, retention controls, and multipart upload support.

The agent uses `search_capabilities`, inspects candidates with `get_capability`,
and traverses dependencies before choosing one. It records its reasoning in an
actor-owned workspace so a later session can recover the evaluated alternatives.
If the team adopts the capability, the agent declares that adoption through the
adoption endpoint.

This flow removes work without creating a Living Memory claim. The catalog
already holds approved state, the workspace holds private reasoning, and the
adoption row tells the provider that another tenant depends on the capability.
An agent should not manufacture a claim when canonical discovery answers the
question.

The result is easier than rebuilding. The developer receives a supported
interface, the provider gains a consumer signal, and impact analysis includes
the adoption. The registry recommends and records. It does not invoke or
provision the selected capability.

## Scenario 2: Turn a missing answer into an owner-routed request

A sales engineer asks an MCP agent whether `invoice-api` supports regional data
residency for a regulated customer. The agent finds the capability but no
canonical attribute, claim, decision, or interface contract that answers the
question.

The agent does not guess or tell the user to find the owning team. It calls
`raise_capability_request` with the `documentation` category. The request routes
to the tenant that owns `invoice-api`. Its producer can
acknowledge, accept, decline, mark it duplicate, or resolve it with a reason.

The request does not become a claim merely because someone asked the question.
If the owner publishes a cited typed answer later, that answer follows the claim
and promotion path. The requester can track progress through
`list_capability_requests` without opening another chat thread.

This pattern also works for developers, support, product, and compliance users.
It turns “the agent does not know” into a governed feedback loop instead of a
hallucinated answer or untracked message.

## Scenario 3: Keep product interfaces on the approved design-system path

A frontend agent reviewing `customer-portal` sees imports from the retired
`legacy-ui-kit` package. The approved replacement is the `northstar-design-system`
capability, whose decision record explains the migration.

The code scanner or coding agent performs the source inspection. The registry
does not lint user interface code. Once observed, the agent checks whether the
graph already has that dependency. If not, it can stage a `depends_on` claim from
`customer-portal` to `legacy-ui-kit`. It can also stage `work_item_url` and
`decision_record_url` claims connecting the application to migration work and
approved guidance.

The application owner reviews the missing dependency and links. Acceptance
makes package-level drift visible in later dependency and deprecation queries.
The design-system owner can see registered adopters, while each consumer sees
only the adoption records allowed by tenant visibility.

This supports consistent user interfaces at the capability and package level.

### Design-system governance is not component-level enforcement

The registry does not provide component props, design tokens, composition
rules, visual regression testing, or automatic code replacement. A dedicated
design-system source can provide that detailed context. The registry supplies
ownership, lifecycle, dependencies, decisions, and governed feedback around it.

## Scenario 4: Connect central security findings to internal topology

The enterprise application-security platform finds that a vendor package
version is affected by a newly published vulnerability. Central software
composition analysis has already identified the package coordinates and the
repositories where that version appears.

A governed connector maps each repository and package coordinate to visible
registry capabilities. It stages `work_item_url` claims for remediation
records and missing `depends_on` claims where scanner evidence reveals catalog
gaps. If the organization's security process records the advisory as an
incident, the connector can stage an `incident_report_url` claim to that record.

The security workflow then starts from the registered vendor-package capability
and calls `get_blast_radius` in the reverse direction. The result expands the
centrally supplied repository list through visible canonical `depends_on` and
`composes` edges. Security can route assessments to the returned capability
owners instead of maintaining a second ownership spreadsheet.

Each proposal still belongs to its subject's owner. Cross-tenant observations
never auto-promote, regardless of confidence. Traversal never reveals private
capabilities the caller cannot see.

### Dependency reachability is not vulnerability confirmation

The registry does not perform software composition analysis or calculate
vulnerability applicability. The central security platform determines affected
packages and observed repositories. Registry traversal identifies additional
known dependency paths that require assessment. It does not prove that every
dependent loads vulnerable code or exposes a vulnerable configuration.

The shipped ontology also has no structured vulnerability identifier,
affected-version range, exploitability, or remediation-state predicate.
`incident_report_url` and `work_item_url` preserve links to the security system
that owns those records.

## Scenario 5: Trace a vendor API sunset into affected products

A procurement analyst receives a vendor notice that the `geo-verification-v1`
API will shut down at `2027-03-31T00:00:00Z`. The notice names a contract, not
the internal products that rely on it.

An MCP agent resolves the vendor API through its external identifier. It stages
`deprecated_after` with the cited notice and a `work_item_url` for the vendor
management record. Reverse traversal finds visible registered capabilities with
known dependency paths to the API.

The result gives procurement, product, and engineering a shared starting list.
Each team can raise or receive capability requests for migration planning. The
capability owner reviews the deprecation claim before it becomes canonical.

The result is not a complete enterprise exposure report. Unregistered
dependencies, private capabilities outside the caller's visibility, and
dependencies hidden in configuration remain absent. The registry connects a
vendor signal to governed known topology; connectors and inventory controls
must keep that topology complete.

## Scenario 6: Keep agents on the current decision

An architecture agent finds two documents about outbound event delivery. An old
decision allows unsigned callbacks. A newer decision requires signed webhooks
and explicitly replaces the earlier record.

A document connector or curator stages four claims on the affected capability:
`decision_record_url`, `decided_at`, `decision_status`, and
`supersedes_decision`. The claims cite immutable document revisions.
Consolidation preserves the records rather than rewriting the old decision out
of history.

After owner review, promotion makes the decision links and supersession chain
canonical attributes. An agent planning new work can retrieve the current
decision and follow the link to its source. It does not mistake search rank or
document recency for authority.

The registry does not parse every policy implication or prevent code that
violates the decision. It makes the governing record, status, and replacement
chain explicit. Enforcement remains with continuous integration, policy
engines, and review tools.

## Scenario 7: Detect a breaking interface before the catalog catches up

A consumer's contract-test pipeline fetches a provider's new interface
specification and detects version `3.0.0`. The provider's canonical catalog
entry still describes the `2.x` interface.

The consumer stages `interface_version` on the provider capability and cites
the specification revision or deterministic connector run. It may also stage
`interface_specification_url` when the canonical link is stale. Claim retrieval
lets the consumer warn its build immediately, but the warning must say
“observed version,” not “canonical version.”

The proposal shows the current canonical value beside `3.0.0`. Cross-tenant
origin requires provider review, and a large direct blast radius adds another
high-impact reason. The provider can accept the release, amend a scanner
mistake, or reject evidence from a preview specification.

This scenario does not use `depends_on_version`, whose current value cannot
identify which dependency the range constrains. Traversal can find visible
consumers, but it does not decide whether each consumer is compatible.

## Scenario 8: Turn incident evidence into missing topology

During a checkout outage, a trace shows that `checkout-api` calls
`risk-policy-service` on every order. The catalog does not contain that edge,
so earlier impact reviews missed the service.

An incident connector stages `incident_occurred_at` and
`incident_report_url` on `checkout-api`. An agent also stages a `depends_on`
claim whose value is the capability ID of `risk-policy-service`. The claim
cites the trace that exposed the call.

The owner decides whether the call is a required dependency, optional
integration, or incident-only fallback. Acceptance creates a canonical edge.
Rejection keeps the evidence in claim history without turning a transient call
into permanent topology.

## Scenario 9: Reuse a proven migration pattern

One team finishes a difficult migration from `legacy-auth` to `identity-v3`.
During the work, its agent writes workspace checkpoints linked to both
capability IDs. The entries record the rollout order, compatibility trap,
validation query, completed checks, unresolved questions, and rollback trigger.
Those entries are task memory. They are useful for resume and handoff, but they
are mutable and do not prove the migration succeeded.

The completed pull request, commit, continuous-integration run, deployment, and
post-deployment checks provide stable outcome evidence. If the result belongs
in a reusable runbook, the platform owner publishes a normalized procedure in
the organization's approved documentation system. A caller can then stage a
typed `runbook_url` claim citing the immutable document revision and relevant
outcome evidence. The claim contains the concise assertion and evidence
references, not a copy of the workspace body.

When another team begins the same migration, its agent searches visible
workspace entries by kind and referenced capability. It retrieves the cited
pattern before proposing a plan instead of reconstructing the solution from old
tickets and chat logs. It separately retrieves the runbook claim as observed
knowledge and follows the evidence before relying on it.

The workspace remains scoped task memory. The claim remains untrusted observed
knowledge. Owner review can promote an eligible runbook link to the approved
Registry record, but neither the workspace nor the successful deployment can
skip that review. The registry does not automatically derive this claim from a
completed workspace today.

## Scenario 10: Correct stale response data after a failed page

An incident page to `payments-primary` bounces. The current runbook names
`payments-edge` and links to a different response guide. An operations agent
records the failure and stages `on_call_rotation: payments-edge` plus the new
`runbook_url`.

`on_call_rotation` is single-valued. A live claim might still name
`payments-primary`. Consolidation treats the overlapping values as a conflict
instead of keeping two paging destinations. The owner checks the paging system
and cited runbook revision before accepting, amending, or rejecting the
observation.

This makes urgent evidence visible without letting the latest incident
transcript rewrite the on-call route. Acceptance closes the previous canonical
attribute; the failed-page evidence remains queryable.

## The scenarios preserve useful uncertainty

The first three scenarios remove daily friction: reuse before rebuilding,
owner-routed answers, and design-system alignment. The next three connect
enterprise signals to governed context. The final four keep contracts,
topology, migration knowledge, and response data current.

The same boundaries apply throughout:

- Canonical discovery wins when approved knowledge already exists.
- Workspaces keep scoped reasoning without publishing it as truth.
- Capability requests route needs without manufacturing answers.
- Claims remain untrusted recall, even when confidence is high.
- Graph traversal broadens investigation only across visible known edges.
- The subject's owner controls canonical changes.
- Promotion writes one typed target and remains reversible.

Living Memory makes evidence available at working speed without making arrival
order, scanner output, or model inference the source of truth.

## Read next

- [AI agent capability discovery](01-ai-agent-capability-discovery.md)
- [Workspaces and agent memory](08-workspaces.md)
- [Layered abstractions and design systems](05-layered-abstractions.md)
- [Trust, authority, and confidence](../01-overview/08-trust-and-confidence.md)
- [Retrieval and context](../01-overview/10-retrieval-and-context.md)
- [Memory-curation runbook](../06-operations/05-memory-curation.md)