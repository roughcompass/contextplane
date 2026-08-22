<!--
  title: Approval Request Control (ARC)
  audience: product, operator, auditor, agent builder
  archetype: explanation (product overview)
  summary: What Approval Request Control is, how approvals work, and how external systems integrate.
-->

# Approval Request Control (ARC)

**Approval Request Control** (ARC) is a policy engine that lets your organization define, enforce, and audit governance rules without code changes.

It centrally manages:
- **Approval workflows** — Multi-step authorization gates (deploy to prod, rotate secrets, export data)
- **Rate limits** — Prevent data connectors and services from misbehaving (circuit breakers auto-pause runaway ingest)
- **Prohibition gates** — Hard stops (no direct database access, no unrestricted API calls)
- **Escalation rules** — Auto-route sensitive changes (crypto code changes → security team)

Rules are **enforced at the boundary** — when someone tries to deploy, rotate a secret, or export data, ARC checks: "Is this allowed? Does it need approval? Who must sign off?" All logged. All auditable.

---

## What Problem Does It Solve?

**Before ARC:**
- Rules are scattered: CI/CD code, policy docs, spreadsheets, Slack messages
- Changing a rule requires code review, deployment, rollout
- Non-engineers can't update governance without developer help
- Audit requires digging through 5 different systems to answer "who approved this?"

**With ARC:**
- One system for all governance rules
- Rules deploy instantly without code changes
- Security/ops teams can update rules themselves
- Approval is cryptographically proven (not hope)
- Complete audit trail, automatically

---

## How It Works (High-Level)

### 1. Define Rules (No Code)

Your ops/security team writes rules:
```
Rule: Production Deployments
Applies to: All payment service deployments to production
Requires: Approval from {payments-lead, security-officer}
Approval must be: Cryptographically signed
Approval lasts: 1 hour
Exception: Can be delegated to on-call SRE
```

This is not code. It's structured data. The system validates it.

### 2. Request Approval

When someone tries to deploy:
```
Engineer: "Deploy payment service to prod"
    ↓
ARC checks: "Does a rule apply?"
    ↓
ARC says: "Yes. This requires approval from payments-lead AND security-officer"
    ↓
System generates: Approval challenge + bytes to sign
```

### 3. Approvers Sign Off

The integration layer sends the challenge to approvers (the integration decides the channel: email, Slack, approval portal, etc.):
```
External integration:
1. Receives the challenge from ARC API
2. Delivers it to the approver (through their preferred channel)
3. Collects the signature from the approver
4. Submits the signed proof back to ARC

Approver sees: "Approve deployment to production"
  - Policy: Payment service deployment
  - Requested by: engineering@acme.com
  - Changes: [diff shown]
  - Expires: 1 hour

Approver signs: (with their private key, hardware key, or attestation)
    ↓
Integration submits signature to ARC
    ↓
System records: Evidence (permanent, cryptographically verified)
```

### 4. Enforce

Once all required approvals are collected:
```
ARC verifies:
  ✓ Signature is valid (cryptographic proof)
  ✓ Approver was authorized
  ✓ Approver's credential not revoked
  ✓ Approval not revoked
  
Result: DEPLOY APPROVED
    ↓
Deployment proceeds
```

All logged. All auditable.

---

## Key Features

### Approval Types

1. **Detached Signature** — Approver signs with their private key (Ed25519)
   - Cryptographically strongest
   - Can be done offline (sign locally, submit later)
   - Used for high-security approvals (secret rotation, critical deploys)

2. **Verifier Attestation** — Call a trusted third-party (identity provider, approval system)
   - Approver approves in their own system
   - ARC calls the verifier: "Did Bob approve this?"
   - Used for integrations with enterprise approval systems

### Rate Limiting

```
Connector writes 500 items per hour
    ↓
After 1 hour: Window resets
    ↓
If ceiling exceeded: Circuit breaker trips (auto-pauses connector)
    ↓
Operator investigates (at their pace)
    ↓
Operator re-enables (flips a switch, no code deploy)
```

**No paging. No SSH. No manual fixes. Auto-safety.**

### Prohibition Gates

```
Rule: "Direct database access is prohibited unless API layer is used"
    ↓
Engineer tries: Direct SQL query
    ↓
ARC says: BLOCKED
    ↓
System logs: Who tried what when
```

### Escalation Rules

```
Rule: "Changes to crypto code must go to security team"
    ↓
Engineer opens PR with crypto changes
    ↓
System: Auto-routes to security team
    ↓
Security team: Reviews before merge
    ↓
Merge only after security approval
```

---

## How External Systems Integrate

ARC doesn't implement the approval source. It provides APIs, and external systems call them.

### The Two Required Calls

**Call 1: Request Approval**
```
POST /v1/arc/proposals/{id}/versions/{v}/approval-challenges

Request:
{
    "approval_verifier_id": "alice@acme.com",
    "idempotency_key": "slack-msg-12345"
}

Response:
{
    "challenge_id": "c9e5f8b2-...",
    "canonical_bytes": "3a7f92b1c5...",  # Bytes to sign
    "expires_at": "2026-08-22T14:40:00Z"
}
```

External system: Send this to the approver (email, Slack, etc.)

**Call 2: Submit Approval**
```
POST /v1/arc/proposals/{id}/versions/{v}/approval-challenges/{id}:complete

Request:
{
    "verification_method": "detached_signature",
    "proof_bytes": "3a7f92b1c5...",  # Their signature
    "signature_algorithm": "ed25519"
}

Response (200 OK):
{
    "evidence_id": "e7d2f9a1-...",
    "verified_at": "2026-08-22T13:45:00Z"
}
```

### Integration Patterns

**Email:**
1. Request challenge
2. Send email link: "Click to approve"
3. Approver clicks → signs in browser
4. Browser submits signature
5. Email service receives: "Approved"

**Slack:**
1. Request challenge
2. Post in Slack: "Click to approve" button
3. Approver clicks → redirects to approval portal
4. Portal collects signature
5. Slack message updates: "✓ Approved"

**GitHub Actions (GitOps):**
1. Workflow parses new rule from commit
2. Workflow requests challenge
3. Workflow signs with GitHub Actions OIDC token
4. Workflow submits signature
5. Rule activated automatically

**Enterprise Approval System:**
1. Request challenge
2. Call out to your approval system: "Approval needed"
3. Approvers approve in your system
4. Your system calls back to ARC: "Here's the proof"
5. ARC records evidence

---

## What You Get

### For Security Teams
- Rules enforced automatically at request time
- Complete audit trail (who, what, when, how)
- Compliance reports generated automatically
- No more "hope-based" compliance

### For Ops/SRE Teams
- Approval workflows centralized (not scattered across email + Slack + Jira)
- Bottlenecks visible in metrics
- Rules can change same-day without deploys
- Auto-safety (circuit breakers, rate limits)

### For Compliance/Legal
- Single source of truth for all governance
- Every rule change versioned and audited
- Audit finishes in 30 minutes, not 3 weeks
- Proof is cryptographic, not manual

### For Everyone
- **Speed:** Rules that took weeks can be deployed in days
- **Safety:** Rules are validated before they take effect; conflicts caught early
- **Auditability:** Every decision logged with context
- **Flexibility:** Rules apply to deploy, database, API, data export, anything
- **Control:** Non-engineers can define and update rules

---

## ARC vs. Catalog vs. Memory

| System | Question | Evidence |
|--------|----------|----------|
| **Catalog** | What approved capability state exists? | Entity records + audit trail |
| **Memory** | What has been observed about a subject? | Cited claims + confidence scores |
| **ARC** | What approved governance applies to this task? | Signed approval + receipt chain |

An agent uses all three. Keep their boundaries clear.

**Example:**
- An ARC directive says: "Before deploying payment code, read the current production interface"
- Agent reads the interface from Catalog
- Agent finds a stale claim in Memory ("This API has a performance bug")
- But that claim cannot satisfy a mandatory ARC obligation unless governance approves it

---

## Approval Trust Model

### How Do You Know Alice Really Approved?

The system stores **cryptographic proof**:

1. **Challenge** (what was presented)
   ```
   canonical_bytes = hash(policy + review + target)
   expires_at = now + 1 hour
   nonce = single-use (prevents replay)
   ```

2. **Evidence** (what Alice proved)
   ```
   signature = sign(canonical_bytes, alice_private_key)
   verified_at = 2026-08-22T13:45:00Z
   credential_fingerprint = which_key_alice_used
   ```

3. **Verification** (proof that it was Alice)
   ```
   verify_signature(canonical_bytes, signature, alice_public_key) → TRUE
   check_credential_revocation(credential_fingerprint, verified_at) → NOT_REVOKED
   check_approval_revocation(evidence) → NOT_REVOKED
   ```

**Result:** You can cryptographically prove Alice approved, when, and what.

### What If Alice's Key Gets Revoked?

The system knows the approval was **before** or **after** revocation:
- Approval at 2:45pm, key revoked at 3:00pm → Approval is valid (before revocation)
- Approval at 2:45pm, key revoked at 2:30pm → Approval is invalid (after revocation)

Immutable timestamps prove it.

---

## Operator Responsibilities

Operators manage:
- **Approval verifier enrollment** — Who can approve (identity verification)
- **Verifier revocation** — Revoke trust if a key is compromised
- **Review expiry** — Old approvals must be renewed
- **Approval evidence revocation** — Withdraw an approval if policy changed

See the [ARC operator runbook](../06-operations/03-arc-runbook.md) for procedures.

---

## Audit & Compliance

### Audit Trail

Every lifecycle event is logged:
- Proposal created
- Proposal submitted
- Challenge issued
- Approval signed
- Revision activated
- Approval revoked
- Resolution blocked (and why)

Query example:
```sql
SELECT
    evidence_id,
    approval_verifier_id,
    approving_principal_subject,
    verified_at,
    revoked_at
FROM arc_projection_approval_evidence
WHERE verified_at >= NOW() - INTERVAL '30 days'
ORDER BY verified_at DESC;
```

### Compliance Reports

Answer audit questions:
- "Who approved what changes in the last year?" — Query evidence table
- "Has this rule been revoked?" — Check revision state
- "Was this approval valid when made?" — Verify signature + credential state
- "What governance applied when this deployment happened?" — Read receipt from that time

---

## Getting Started

1. **Define a rule** — Work with your ops/security team
2. **Create a policy artifact** — Use the ARC authoring surface
3. **Submit for approval** — Generate challenge, collect approvals
4. **Activate the policy** — Move from draft to active
5. **Integrate your approval source** — Wire up email, Slack, or enterprise system
6. **Monitor** — Watch approval metrics, audit logs, blocked requests

See the [Approval Request Control integration guide](./arc-integration-points.md) for concrete examples.

---

## Key Terms

- **Artifact** — A policy (e.g., "Q3 Security Policy")
- **Revision** — A version of a policy (v1, v2, v3)
- **Directive** — An individual rule (e.g., "require approval", "prohibit access")
- **Applicability Rule** — When a directive applies (scope, intent kind, action class)
- **Challenge** — A request for approval (generated, sent to approver)
- **Evidence** — Proof that approval happened (cryptographically signed)
- **Obligation** — What must be satisfied (mandatory directive that must have an active satisfying revision)
- **Receipt** — Record of a resolution decision (what rules applied, were they satisfied)

---

## More Information

- **How approvals work:** See [Approval mechanics](./arc-approvals.md)
- **How external systems integrate:** See [Integration points](./arc-integration-points.md)
- **Operator procedures:** See [ARC operator runbook](../06-operations/03-arc-runbook.md)
- **Detailed resolution model:** See [Attested context resolution](./11-attested-context-resolution.md)
