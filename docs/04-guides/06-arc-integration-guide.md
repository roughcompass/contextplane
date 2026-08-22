<!--
  title: ARC Integration Guide
  audience: integrator, ops, product
  archetype: how-to (integration patterns)
  summary: How to integrate Approval Request Control with email, Slack, GitHub Actions, OPA, and enterprise approval systems.
-->

# Approval Request Control Integration Guide

This guide shows how external systems integrate with ARC to request approvals and submit approval proof.

**Key principle:** ARC provides two REST endpoints. Any external system can call them. The system can be email, Slack, GitHub Actions, OPA, or custom approval system—ARC doesn't care. It just needs cryptographic proof that the approver approved.

---

## Architecture

```
External Approval Source          ARC API                    Database
(Email, Slack, GitHub, etc.)      (Two REST endpoints)       (Stores proof)

┌──────────────┐
│ Email        │  POST .../approval-challenges        ┌─────────────┐
│ Slack        │─────────────────────────────────────→│ Challenge   │
│ GitHub       │  approval_verifier_id                │ Store       │
│ OPA          │  idempotency_key                     └─────────────┘
│ Custom       │
└──────────────┘  Response: challenge_id, canonical_bytes, expires_at
                  
                  Approver sees what's being approved
                  Approver signs / attests
                  
                  POST .../approval-challenges/{id}:complete
                  proof_bytes, signature_algorithm
                                                       ┌──────────────┐
                                                       │ Evidence     │
                                                    ←──│ Store        │
                                                       │ (Permanent)  │
                                                       └──────────────┘
```

---

## The Two Required Calls

### Call 1: Create Challenge (Request Approval)

**Purpose:** Tell ARC "Someone needs to approve this", get back what to sign.

**API:**
```
POST /v1/arc/proposals/{proposal_id}/versions/{proposal_version}/approval-challenges

Headers:
  Authorization: Bearer <access-token>
  X-Tenant-ID: <tenant-id>  # If multi-tenant

Request body:
{
    "approval_verifier_id": "alice@acme.com",    # Who must approve
    "idempotency_key": "slack-msg-12345"         # Unique request ID (prevents duplicates)
}

Response (200 OK):
{
    "approval_challenge_id": "c9e5f8b2-...",
    "canonical_evidence_bytes": "3a7f92b1c5...",  # HEX-ENCODED BYTES
    "signing_domain": "acme.contextplane.io",
    "approval_nonce": "nonce-12345",
    "expires_at": "2026-08-22T14:40:00Z"
}
```

**What happens next:**
- Save the `approval_challenge_id` (you'll need it for call 2)
- Send `canonical_evidence_bytes` to the approver (they'll sign it)
- Show the approver what they're approving (get it from `/v1/arc/proposals/.../review-package`)
- The challenge expires at `expires_at` — after that, call 1 again to get a new challenge

---

### Call 2: Complete Challenge (Submit Approval Proof)

**Purpose:** Tell ARC "The approver signed the bytes", verify the signature, record the approval.

**API:**
```
POST /v1/arc/proposals/{proposal_id}/versions/{proposal_version}/approval-challenges/{challenge_id}:complete

Headers:
  Authorization: Bearer <access-token>
  X-Tenant-ID: <tenant-id>

Request body:
{
    "verification_method": "detached_signature",  # or "verifier_attestation"
    "proof_bytes": "3a7f92b1c5...",              # HEX-ENCODED SIGNATURE
    "signature_algorithm": "ed25519"              # Always this for detached_signature
}

Response (200 OK):
{
    "evidence_id": "e7d2f9a1-...",
    "approval_challenge_id": "c9e5f8b2-...",
    "proposal_id": "...",
    "proposal_version": 1,
    "revision_id": "...",
    "verified_at": "2026-08-22T13:45:00Z",
    "approval_verifier_id": "alice@acme.com",
    "approving_principal_subject": "alice@acme.com",
    "revoked_at": null
}
```

**What happens next:**
- Evidence is recorded permanently
- Proposal is marked "approved"
- Operator can now activate the policy
- The approval is immutable (can only be revoked, not edited)

---

## Error Handling

Every integration must handle these errors:

```
POST .../approval-challenges/{id}:complete

Response (400 Bad Request):
{
    "code": "arc_approval_verification_failed",
    "message": "Signature verification failed"
}
→ Action: "Invalid signature. Ask approver to try again."

Response (409 Conflict):
{
    "code": "arc_approval_challenge_expired",
    "message": "Challenge expired"
}
→ Action: "Challenge expired. Call create-challenge again."

Response (409 Conflict):
{
    "code": "arc_approval_challenge_failed",
    "message": "Too many failed attempts"
}
→ Action: "Too many invalid attempts. Call create-challenge again."

Response (429 Too Many Requests):
{
    "code": "arc_approval_challenge_limit_reached",
    "message": "Over limit"
}
→ Action: "Too many in-flight challenges. Wait and retry."

Response (409 Conflict):
{
    "code": "arc_approval_challenge_superseded",
    "message": "Another challenge already won"
}
→ Action: "Another approver beat you. Approval is done."

Response (409 Conflict):
{
    "code": "arc_approval_already_completed",
    "message": "This challenge already completed"
}
→ Action: "Different approver already completed this."
```

---

## Integration Patterns

### Pattern 1: Email Approval

**Flow:**
```
User submits policy
    ↓
External service calls: POST .../approval-challenges
    ↓
Challenge created, bytes returned
    ↓
Email service sends: "Click to approve" link with challenge_id
    ↓
Approver clicks email link
    ↓
Browser shows: What's being approved (fetched from review-package endpoint)
    ↓
Approver clicks "Approve"
    ↓
Browser collects signature (local crypto, hardware key, etc.)
    ↓
Browser calls: POST .../approval-challenges/{id}:complete
    ↓
ARC verifies signature, records evidence
    ↓
Email service notified: Approval done
```

**Code example:**
```python
# Step 1: Request challenge
challenge = requests.post(
    f"https://contextplane.acme.com/v1/arc/proposals/{pid}/versions/{pv}/approval-challenges",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "approval_verifier_id": "alice@acme.com",
        "idempotency_key": f"email-{message_id}"
    }
).json()

# Step 2: Send email
email_service.send(
    to="alice@acme.com",
    subject="Approve Policy: Q3 Security Policy",
    html=f"""
    <h2>Policy Approval Needed</h2>
    <p>Title: Q3 Security Policy</p>
    <p>Requested by: engineering@acme.com</p>
    <p>Expires: {challenge['expires_at']}</p>
    <a href="https://acme.contextplane.io/approve/{challenge['approval_challenge_id']}">
      Click to approve
    </a>
    """
)

# Step 3: Approver clicks link, signs in browser

# Step 4: Browser collects signature and calls:
approval = requests.post(
    f"https://contextplane.acme.com/v1/arc/proposals/{pid}/versions/{pv}/approval-challenges/{challenge['approval_challenge_id']}:complete",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "verification_method": "detached_signature",
        "proof_bytes": alice_signature_hex,
        "signature_algorithm": "ed25519"
    }
).json()

# Step 5: Show confirmation
print(f"✓ Approved by {approval['approving_principal_subject']} at {approval['verified_at']}")
```

---

### Pattern 2: Slack Bot

**Flow:**
```
Policy submitted
    ↓
CI webhook notifies Slack bot
    ↓
Bot calls: POST .../approval-challenges
    ↓
Bot posts in Slack: "Approval needed" with button
    ↓
Engineer clicks button
    ↓
Bot redirects to approval portal OR signs with bot key
    ↓
Portal/bot collects signature
    ↓
Portal/bot calls: POST .../approval-challenges/{id}:complete
    ↓
ARC records evidence
    ↓
Bot updates Slack message: "✓ Approved"
```

**Code example:**
```python
from slack_bolt import App

app = App(token=os.environ["SLACK_BOT_TOKEN"], signing_secret=os.environ["SLACK_SIGNING_SECRET"])

# Listen for "policy submitted" event
@app.event("policy_submitted")
def handle_policy_submitted(event, client):
    # Step 1: Request challenge
    challenge = requests.post(
        f"https://contextplane/v1/arc/proposals/{event['proposal_id']}/versions/{event['version']}/approval-challenges",
        json={
            "approval_verifier_id": event['approver_email'],
            "idempotency_key": f"slack-{event['proposal_id']}"
        }
    ).json()
    
    # Step 2: Post in Slack
    client.chat_postMessage(
        channel="#approval",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Approval Needed*\n*Policy:* {event['policy_title']}\n*Expires:* {challenge['expires_at']}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "action_id": f"approve_{challenge['approval_challenge_id']}"
                    }
                ]
            }
        ]
    )

# Listen for button click
@app.action(action_id="approve_*")
def handle_approve(ack, action, client):
    ack()
    
    challenge_id = action['action_id'].split('_', 1)[1]
    user = action['user']['id']
    
    # Step 3: Collect signature (could redirect to portal, or use stored key)
    signature = sign_with_slack_bot_key(canonical_bytes)
    
    # Step 4: Submit approval
    approval = requests.post(
        f"https://contextplane/v1/arc/proposals/{...}/versions/{...}/approval-challenges/{challenge_id}:complete",
        json={
            "verification_method": "detached_signature",
            "proof_bytes": signature.hex(),
            "signature_algorithm": "ed25519"
        }
    ).json()
    
    # Step 5: Update Slack message
    client.chat_update(
        channel=action['team']['id'],
        ts=action['container']['message_ts'],
        blocks=[{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✓ Approved by <@{user}> at {approval['verified_at']}"
            }
        }]
    )

app.start(port=int(os.environ.get("PORT", 3000)))
```

---

### Pattern 3: GitHub Actions (GitOps)

**Flow:**
```
Git push with new policy
    ↓
GitHub Actions workflow triggered
    ↓
Workflow parses policy from commit
    ↓
Workflow calls: POST .../approval-challenges
    ↓
Workflow signs canonical_bytes
    ↓
Workflow calls: POST .../approval-challenges/{id}:complete
    ↓
ARC records evidence
    ↓
Workflow activates policy (if authorized)
```

**Code example:**
```yaml
name: Deploy Policy

on:
  push:
    branches: [main]
    paths: ['policies/*.yaml']

jobs:
  approve-and-deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v3
      
      - name: Parse policy from git
        id: policy
        run: |
          # Extract policy ID and version from commit
          POLICY_ID=$(cat policies/new-policy.yaml | yq .metadata.id)
          VERSION=$(cat policies/new-policy.yaml | yq .metadata.version)
          echo "POLICY_ID=$POLICY_ID" >> $GITHUB_OUTPUT
          echo "VERSION=$VERSION" >> $GITHUB_OUTPUT
      
      - name: Get OIDC token
        id: oidc
        run: |
          TOKEN=$(curl -H "Authorization: bearer ${{ secrets.GITHUB_TOKEN }}" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=${{ secrets.ARC_AUDIENCE }}" \
            | jq -r .value)
          echo "TOKEN=$TOKEN" >> $GITHUB_OUTPUT
      
      - name: Request approval challenge
        id: challenge
        run: |
          RESPONSE=$(curl -X POST \
            https://contextplane/v1/arc/proposals/${{ steps.policy.outputs.POLICY_ID }}/versions/${{ steps.policy.outputs.VERSION }}/approval-challenges \
            -H "Authorization: Bearer ${{ steps.oidc.outputs.TOKEN }}" \
            -d '{
              "approval_verifier_id": "github-actions-ci",
              "idempotency_key": "${{ github.run_id }}"
            }')
          
          CHALLENGE_ID=$(echo $RESPONSE | jq -r .approval_challenge_id)
          BYTES=$(echo $RESPONSE | jq -r .canonical_evidence_bytes)
          
          echo "CHALLENGE_ID=$CHALLENGE_ID" >> $GITHUB_OUTPUT
          echo "BYTES=$BYTES" >> $GITHUB_OUTPUT
      
      - name: Sign with GitHub OIDC
        id: sign
        run: |
          # Example: using openssl to sign locally
          # In production, you'd use a signing service
          SIGNATURE=$(echo -n "${{ steps.challenge.outputs.BYTES }}" | \
            openssl dgst -sha256 -sign <(echo "${{ secrets.SIGNING_KEY }}") | \
            base64 -w0)
          
          echo "SIGNATURE=$SIGNATURE" >> $GITHUB_OUTPUT
      
      - name: Submit approval to ARC
        run: |
          curl -X POST \
            https://contextplane/v1/arc/proposals/${{ steps.policy.outputs.POLICY_ID }}/versions/${{ steps.policy.outputs.VERSION }}/approval-challenges/${{ steps.challenge.outputs.CHALLENGE_ID }}:complete \
            -H "Authorization: Bearer ${{ steps.oidc.outputs.TOKEN }}" \
            -d '{
              "verification_method": "detached_signature",
              "proof_bytes": "${{ steps.sign.outputs.SIGNATURE }}",
              "signature_algorithm": "ed25519"
            }'
      
      - name: Activate policy
        run: |
          curl -X POST \
            https://contextplane/v1/arc/revisions/${{ steps.policy.outputs.REVISION_ID }}/lifecycle:activate \
            -H "Authorization: Bearer ${{ steps.oidc.outputs.TOKEN }}"
      
      - name: Comment on PR
        run: |
          gh pr comment --body "Policy activated: ${{ steps.policy.outputs.POLICY_ID }}"
```

---

### Pattern 4: Enterprise Approval System

**Flow:**
```
User creates policy in ARC UI
    ↓
Policy submitted
    ↓
ARC calls out: webhook to your approval system
    ↓
Approval system: Routes to approvers
    ↓
Approvers approve in your system
    ↓
Your system calls back: POST .../approval-challenges/{id}:complete
    ↓
ARC records evidence
```

---

### Pattern 5: Open Policy Agent

**Flow:**
```
Policy submitted
    ↓
OPA evaluates: Is this semantically correct? Does it conflict?
    ↓
OPA returns: ALLOW or DENY
    ↓
If ALLOW:
  - OPA signs the canonical bytes
  - OPA calls: POST .../approval-challenges/{id}:complete
  - ARC records: "OPA Approved"
```

---

## Fetching What Approvers See

When you request a challenge, approvers need to know **what** they're approving.

**API:**
```
GET /v1/arc/proposals/{proposal_id}/versions/{proposal_version}/review-package

Response:
{
    "review_package_digest": "abc123...",
    "baseline_diff": {
        "changes": [
            {
                "field_path": "directives[0].statement",
                "change_kind": "modified",
                "before": "Old statement",
                "after": "New statement"
            }
        ]
    },
    "field_provenance": [...],  # Who wrote each field
    "citations": [...],          # Sources cited
    "semantic_tests": {...},     # Tests that passed
    "expected_impact_envelope": {...},
    "risk_classification": "medium"
}
```

Your approval UI should show:
- The diff (what changed)
- The risk classification
- Who wrote what
- What tests passed
- When the approval expires

---

## Idempotency

**Use `idempotency_key` to prevent duplicate challenges.**

```
POST .../approval-challenges
{
    "approval_verifier_id": "alice@acme.com",
    "idempotency_key": "slack-msg-12345"   # Same key = same challenge
}
```

If your service crashes after requesting a challenge but before getting the response, you can call again with the same key and get the same challenge back (not a new one).

---

## Summary

Every integration follows this pattern:

1. **Call 1:** Request challenge
   - ARC generates unique challenge + canonical bytes
   - You send to approver (email, Slack, etc.)
   - Approver sees what they're approving

2. **Approver signs**
   - Detached signature: Approver signs locally
   - Attestation: Approver approves in their own system

3. **Call 2:** Submit proof
   - You send the signature / attestation
   - ARC verifies, records evidence
   - Approval is permanent + immutable

4. **Auditor queries** (later)
   - See who approved, when, with what proof
   - Verify the signature cryptographically
   - Check credential revocation status

---

## Common Questions

**Q: What if the approver's key gets compromised?**
A: The key can be revoked. The system records whether the revocation happened before or after the approval. If before, the approval is invalid.

**Q: What if the policy changes after approval?**
A: The canonical bytes include a hash of the policy. If the policy changes, the hash changes. The signature no longer matches. ARC will refuse to activate unless a new approval is given.

**Q: What if the approver later denies they approved?**
A: You can prove they approved via the signature. It's cryptographically unforgeable (without their private key).

**Q: How long does approval last?**
A: Until you revoke it, or the verifier (approver's identity) is revoked, or the policy changes. You set the challenge expiration time.

**Q: Can approvals be delegated?**
A: Depends on the rule. The rule can specify `delegable_exception: true` to allow delegation. Otherwise, only the named approver can approve.

---

## See Also

- [Approval Request Control Overview](../01-overview/13-approval-request-control.md)
- [ARC Operator Runbook](../06-operations/03-arc-runbook.md)
- [API Reference](../05-reference/01-api.md)
