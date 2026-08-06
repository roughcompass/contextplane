<!--
  title: Use case: Living Memory turns operational evidence into governed catalog knowledge
  audience: integrator, agent builder, producer, operator
  archetype: explanation (use-case scenarios)
  summary: Six realistic scenarios show how security, incident, interface, lifecycle, and recovery evidence becomes cited recall and reviewed catalog knowledge.
-->

# Use case: Living Memory turns operational evidence into governed catalog knowledge

Security findings, incident traces, release changes, and recovery tests often
expose catalog gaps before a capability owner can update them. Writing each
observation directly to the catalog would let stale documents, scanner errors,
or another tenant change authoritative state.

Living Memory keeps the evidence useful without granting it authority on
arrival. These six scenarios show how the same control loop handles urgent and
conflicting observations. Each scenario uses predicates and controls that ship
with the registry.

Read [Living Memory and claims](../01-overview/07-living-memory.md) for the
system model. Tool inputs and response fields remain in the
[MCP tools reference](../05-reference/02-mcp-tools.md#memory-curation).

---

## Every scenario uses the same governed loop

1. An agent resolves the subject to a visible capability UUID.
2. It records immutable evidence and stages a typed claim.
3. Claim retrieval serves the observation with `trust: "untrusted"`, its
   authority, confidence, interval, and citations.
4. Consolidation collapses agreement or preserves disagreement.
5. An eligible claim becomes a proposal owned by the subject's tenant.
6. The owner accepts, amends, or rejects the proposal. Acceptance writes a
   typed canonical attribute or edge.
7. The owner can reverse an incorrect promotion unless a later promotion has
   already replaced it.

Not every observation reaches the final step. An unlinked, contested,
unattributed, or low-confidence claim may remain useful as cited recall while
it waits for more evidence or review.

## The scenarios cover different operational failures

| Scenario | Evidence that arrives first | Main claims | Governed outcome |
|---|---|---|---|
| A vulnerability may cross repository boundaries | Dependency scan and security advisory | `incident_report_url`, `work_item_url`, `depends_on` | Finds visible dependency paths that require security assessment |
| An incident reveals a hidden dependency | Trace and incident report | `incident_occurred_at`, `incident_report_url`, `depends_on` | Adds a reviewed dependency edge for future impact analysis |
| A consumer detects a breaking interface release | Interface specification and release evidence | `interface_version`, `interface_specification_url` | Routes an early cross-tenant observation to the provider |
| A deprecated capability still has consumers | Deprecation notice and migration issue | `deprecated_after`, `work_item_url` | Makes withdrawal explicit without silently stranding consumers |
| A failed page exposes stale response data | Paging failure and current runbook | `on_call_rotation`, `runbook_url` | Reconciles conflicting operational contacts |
| Recovery documents disagree | Recovery exercise and document revisions | `recovery_time_objective_seconds` | Preserves the conflict until an owner chooses the governing target |

## Scenario 1: A vulnerability may cross repository boundaries

A security agent scans the `invoice-worker` repository and finds a vulnerable
version of the shared `archive-parser` library. The finding matters beyond the
repository where the scanner found it. Other services and libraries may depend
on the same capability.

The agent resolves both repositories through their GitHub or package
identifiers. It then stages three kinds of evidence:

- An `incident_report_url` claim on `archive-parser` links to the advisory when
  the organization's security process treats that advisory as an incident
  record.
- A `work_item_url` claim links the library to its remediation issue.
- A `depends_on` claim on `invoice-worker` points to `archive-parser` if the
  catalog is missing that dependency edge.

For example, the advisory claim can cite a registered scanner run:

```text
assert_claim(
  subject_reference = "<archive-parser-uuid>",
  predicate         = "incident_report_url",
  value             = "https://security.example/advisories/ADV-2026-0417",
  evidence          = [{
    "kind": "connector_run",
    "ref": "<scanner-run-uuid>",
    "excerpt": "archive-parser versions before the patched release require review"
  }]
)
```

The claim is immediately queryable as untrusted recall. A security workflow can
follow the canonical graph in reverse to find visible dependency paths:

```text
get_blast_radius(
  entity_id  = "<archive-parser-uuid>",
  direction  = "reverse",
  edge_types = ["depends_on", "composes"],
  depth      = 5
)
```

The result may include `invoice-worker`, `claims-uploader`, and services that
depend on either one. Security teams can use that list to assign assessments
and attach capability-specific remediation work items. Traversal respects
tenant visibility, so it never reveals a private capability that the caller
cannot see.

Each proposal routes to the owner of its subject. The library owner reviews the
advisory and remediation links. The `invoice-worker` owner reviews the missing
dependency edge. Cross-tenant observations never auto-promote, regardless of
confidence.

### Dependency reachability is not vulnerability confirmation

The registry does not calculate Common Vulnerabilities and Exposures (CVE)
applicability. A dependency path identifies a capability that needs assessment.
It does not prove that the capability uses an affected version, loads the
vulnerable code, or exposes the vulnerable configuration.

The shipped ontology also has no structured vulnerability identifier,
affected-version range, exploitability, or remediation-state predicate.
`incident_report_url` and `work_item_url` preserve links to those records. A
deployment that needs typed vulnerability assertions must add that ontology and
its review semantics. The registry connects a security signal to governed
topology; it does not stamp every dependent as vulnerable.

## Scenario 2: An incident reveals a hidden runtime dependency

During a checkout outage, a trace shows that `checkout-api` calls
`risk-policy-service` on every order. The catalog does not contain that edge,
so earlier impact reviews missed the service.

An incident connector stages `incident_occurred_at` and
`incident_report_url` claims on `checkout-api`. An agent also stages a
`depends_on` claim whose value is the UUID of `risk-policy-service`. The claim
cites the trace or incident record that exposed the call.

The observation remains separate from the canonical graph while the
`checkout-api` owner decides whether the call is a required dependency, an
optional integration, or an incident-only fallback. If a central reliability
tenant authored the claim, cross-tenant routing sends the proposal to the
`checkout-api` owner and prevents automatic promotion.

Acceptance creates a canonical `depends_on` edge. Future dependency and blast
radius queries now include the relationship. Rejection keeps the trace in the
claim history without turning a transient call into permanent topology.

## Scenario 3: A consumer detects a breaking interface release first

A consumer's contract-test pipeline fetches a provider's new interface
specification and detects version `3.0.0`. The provider's canonical catalog
entry still describes the `2.x` interface.

The consumer stages an `interface_version` claim on the provider capability and
cites the specification revision or deterministic connector run. It may also
stage `interface_specification_url` when the canonical link is stale. Claim
retrieval lets the consumer warn its build as soon as the observation arrives,
but the warning must say “observed version,” not “canonical version.”

The provider receives the proposal because it owns the subject. The proposal
shows the current canonical value beside `3.0.0`. Cross-tenant origin requires
review, and a large direct blast radius adds another high-impact reason. The
provider can accept the release, amend a scanner mistake, or reject evidence
from a preview specification.

This scenario uses `interface_version` to describe the provider. It does not
use `depends_on_version`, whose current value cannot identify which dependency
the range constrains. Dependency traversal can find consumers, but it does not
decide whether each consumer is compatible with the new interface.

## Scenario 4: A deprecated capability still has active consumers

A release-note agent observes that `legacy-auth` will be deprecated at
`2026-11-30T00:00:00Z`. The catalog still marks it active, and reverse
traversal shows several visible consumers.

The agent stages `deprecated_after` on `legacy-auth` and cites the exact release
revision. It also stages one or more `work_item_url` claims for the provider's
migration program. Consumers can read the dated observation before approval
and begin checking their own exposure.

A deprecation date narrows a surface that other capabilities use, so it always
requires human review. A high direct-dependent count provides an additional
high-impact reason. Acceptance writes the canonical date and work-item links.
It does not migrate consumers, create their tickets, or claim that every
dependency path remains active.

The provider can reverse a mistaken date. If a later promotion has already
replaced it, the provider must reverse the later change first.

## Scenario 5: A failed page exposes stale response data

An incident page to `payments-primary` bounces. The current runbook names
`payments-edge` and links to a different response guide. An operations agent
records the paging result and stages `on_call_rotation: payments-edge` plus the
new `runbook_url`.

`on_call_rotation` is single-valued. If a live claim still names
`payments-primary`, consolidation treats the overlapping values as a conflict
rather than keeping two paging destinations. Authority and recency explain
which claim stands, but confidence alone does not decide where future pages go.

The capability owner checks the paging system and cited runbook revision. The
owner can accept the new rotation, amend its identifier, or reject a temporary
incident handoff. Acceptance closes the previous canonical attribute and
writes the reviewed value. The original failed-page evidence remains
queryable.

This example prevents a common failure mode: an agent can make urgent evidence
visible immediately without letting the latest incident transcript rewrite the
on-call route.

## Scenario 6: Recovery documents disagree on the required target

A disaster-recovery exercise finds two values for the same capability. The
business continuity plan requires recovery within 1,800 seconds. The service
runbook allows 14,400 seconds.

Document connectors stage two `recovery_time_objective_seconds` claims with
revision-specific citations. The predicate is single-valued, so overlapping
claims compete. Consolidation may preserve a stronger source or mark comparable
claims contested. It never averages the values into a target that no document
specified.

Operators can inspect both claims, authority tiers, confidence buckets, and
citations while the conflict remains open. The owner decides which document
governs, rejects or supersedes the other claim, and promotes the verified
number. If policy needs both a contractual target and an engineering estimate,
the ontology needs two distinct predicates rather than two meanings for one
field.

This scenario shows why confidence is not approval. A reproducible extraction
can prove what a document says. Only the owner can decide which document sets
the capability's canonical recovery objective.

## These scenarios preserve useful uncertainty

Living Memory adds value before promotion. A security team can see an advisory,
a consumer can react to an observed interface release, and an operator can find
a disputed recovery target without presenting any of them as approved truth.

The control boundary stays consistent across all six scenarios:

- Evidence remains cited and immutable.
- Claims remain untrusted recall, even when confidence is high.
- Consolidation makes agreement and conflict explicit.
- Graph traversal broadens investigation only across visible canonical edges.
- The subject's owner controls canonical changes.
- Promotion writes one typed target; it does not infer changes elsewhere.
- Reversal preserves a bounded path back to the previous canonical value.

This is the purpose of Living Memory: make new evidence available at operational
speed without making arrival order, scanner output, or model inference the
source of truth.

## Read next

- [Trust, authority, and confidence](../01-overview/08-trust-and-confidence.md)
- [Retrieval and context](../01-overview/10-retrieval-and-context.md)
- [Data governance and PII](../01-overview/09-data-governance.md)
- [Memory-curation runbook](../06-operations/05-memory-curation.md)
- [Session extraction](../04-guides/05-session-extraction.md)