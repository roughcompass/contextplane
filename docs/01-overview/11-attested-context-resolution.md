<!--
  title: Attested context resolution
  audience: evaluator, agent builder, operator, auditor
  archetype: explanation (mental model)
  summary: What Agent Readiness Context resolves, why it produces receipts, and how it differs from catalog and memory retrieval.
-->

# Attested context resolution

Agent Readiness Context (ARC) gives an agent a deterministic answer to two
questions:

1. What approved governance context applies before this task acts?
2. Can the agent later prove what context it received and why?

ARC is a subsystem inside the registry application. It shares the FastAPI
process, database, migrations, scheduler, authentication foundation, and MCP
server. It uses separate artifact audience rules because governance context and
catalog visibility solve different authorization problems.

This page explains the model. Operators managing keys, review expiry, audit
outbox failures, or downgrade guards should use the
[ARC operator runbook](../06-operations/03-arc-runbook.md).

---

## Search alone cannot satisfy mandatory context

A ranked search answers what is relevant. Governance resolution must also
answer what is mandatory, what conflicts, what no longer has valid approval,
and what fits inside a context budget without dropping an obligation.

ARC treats governance as versioned artifacts with structured applicability.
An active revision contains directives, audience rules, and review state.
Mandatory obligations persist even when their satisfying revision expires or
is revoked. The missing obligation blocks matching resolutions instead of
silently disappearing.

## The resolution flow binds identity, task, and result

The flow has six steps:

1. Authenticate the caller and execution host.
2. Issue a single-use challenge.
3. Attest the task manifest and challenge.
4. Resolve applicable revisions, directives, exceptions, and obligations.
5. Return an allowed or blocked policy result with a bounded context bundle
  and immutable receipt.
6. Retrieve authorized detail or explain the receipt later.

### MCP preflight fixes the connection identity

ARC MCP tools require a completed preflight. The preflight binds the connection
to the validated credential context. If the credential changes, ARC refuses
later calls until preflight runs again.

Preflight does not currently provide the authenticated host ID required to
issue a challenge. Challenge issuance and context resolution use the REST
surface with the `X-ARC-Host-ID` header. Resolution then verifies the host
attestation. MCP supports receipt retrieval and explanation after preflight.
See the [MCP tools reference](../05-reference/02-mcp-tools.md#arc-connection-and-receipt-tools)
for the current operation boundary.

### A challenge prevents replay

The caller requests a single-use challenge bound to its tenant, host, session,
and manifest-claims digest. The challenge expires and cannot back two receipts.
The host attestation proves that the task manifest and challenge belong to an
approved execution host.

Failure at this trust boundary returns an authentication-style refusal and no
policy receipt. The system has not accepted the manifest as an input to policy
resolution.

### Resolution is deterministic

Resolution selects active, applicable revisions; evaluates exceptions and
obligations; resolves directive conflicts; and assembles content under the
request budget. The same committed inputs produce the same selection.

Mandatory content is not discarded merely to meet a byte limit. When the
required context cannot fit or a mandatory obligation lacks a valid satisfying
revision, the resolution blocks and records the reason.

### The receipt is part of the result

A receipt records the manifest identity, selected revisions and directives,
resolution status, budget outcome, key identity, and supporting digests. Receipt
events form an append-only signed chain.

The response can report `resolution_status: blocked` with HTTP 200. Transport
succeeded, identity was accepted, policy ran, and the receipt explains why the
task may not proceed. Returning only HTTP 403 would lose that evidence.

## Artifacts carry lifecycle and approval state

Governance content moves through draft, approval, activation, review expiry,
revocation, and invalidation. These states have different meanings:

- **Review expiry** means the content has not been renewed in time.
- **Revocation** means the rule no longer has force.
- **Invalidation** means the content or source is no longer trustworthy.
- **Approval-evidence revocation** withdraws trust in the evidence used to
  activate content.

ARC rechecks current state at consequential boundaries. A stale draft cannot be
activated after its review date. Revoked or invalid content cannot remain valid
merely because an earlier request saw it.

Approval evidence has its own trust roots. Verifiers bind to principals and
credential fingerprints. Activation verifies the evidence against current
verifier state rather than treating feature enablement as approval.

## Budgets change presentation, not obligations

The caller supplies a maximum context size and a supported content profile. ARC
uses deterministic selection and rendering to fit the bundle. Optional material
can be omitted according to the selection rules. Mandatory directives remain
mandatory.

Authorized just-in-time detail retrieval lets the agent fetch selected content
without placing every body in the initial response. Retrieval rechecks audience
and artifact state. A context handle is not permanent permission to read content
that has since been revoked.

## Receipts remain externally verifiable

The public metadata endpoint publishes receipt-signing key history and supported
profiles. Retired and compromised public keys remain listed so old receipts can
still be checked and a compromise is not hidden by deleting its key record.

Private key material stays with its configured signing provider. Receipt signing,
host attestation, approval verification, challenge derivation, content
encryption, response replay encryption, and continuation tokens use distinct key
purposes.

## ARC is separate from catalog and memory retrieval

| Surface | Question answered | Evidence model |
|---|---|---|
| Canonical catalog | What approved capability state exists? | Bi-temporal entity, attribute, fact, and edge records |
| Living Memory | What has been observed about a subject? | Cited, scored, untrusted claims |
| Workspaces and sessions | What did this actor or team record? | Owner-scoped notes or event sequence |
| ARC | What approved governance context applied to this attested task? | Bounded bundle plus signed receipt and event chain |

An agent can use all four. It should preserve their boundaries in its output.
For example, an ARC directive may require the agent to consult a canonical
interface before acting. A remembered claim can provide a lead, but it cannot
satisfy a mandatory ARC obligation unless approved governance says it can.

## When to use ARC

Use ARC when at least one condition holds:

- A task must receive mandatory policy, security, legal, or operational context.
- The context depends on task-manifest attributes and audience rules.
- Conflicting directives need deterministic resolution.
- A later auditor must prove what the agent received.
- Blocking needs an explainable policy result rather than a generic permission
  error.

Use ordinary lookup, search, and claim recall when the task needs knowledge
discovery without an attested governance decision.

## Read next

- [Retrieval and context](10-retrieval-and-context.md)
- [ARC operator runbook](../06-operations/03-arc-runbook.md)
- [Architecture reference](../05-reference/04-architecture.md)
- [Authentication](04-authentication.md)
- [Authorization](05-authorization.md)
