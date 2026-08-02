# ARC operator runbook

Agent Readiness Context (ARC) gives AI agents attested, deterministic
governance context and records what they were given. That evidential purpose
shapes every procedure below: several things an operator would normally expect
to be able to fix quickly are deliberately hard, because doing them casually
would destroy the record that makes ARC worth having.

Read the failure modes before the procedures. Most ARC incidents are cases
where the system is correctly refusing something.

---

## Operator allowlist

Deployment-wide governance writes — global artifacts, approval-verifier
revocation, approval-evidence revocation — authorize on an exact
`(issuer, subject)` pair from `ARC_GLOBAL_OPERATOR_ALLOWLIST`, never on a role.

Every role here is tenant-scoped. If `admin` sufficed, an admin of any tenant
could edit policy binding every other tenant.

### Granting operator access

1. Add `https://issuer.example/realms/ops|svc-arc-operator` to
   `ARC_GLOBAL_OPERATOR_ALLOWLIST` (comma-separated for several).
2. Restart. The list is read at startup and frozen — an allowlist that could be
   appended to at runtime is one that can be appended to by a bug.
3. Confirm with `GET /v1/arc/admin/operator-identity`, which reports whether the
   *calling* credential holds operator identity. It never returns the list.

### Symptoms

| Symptom | Cause | Action |
|---|---|---|
| Startup fails with `ARC_GLOBAL_OPERATOR_ALLOWLIST entry ... missing the '\|' delimiter` | Malformed entry | Fix the entry. This is deliberate — skipping it would leave someone believing they have access when they do not. |
| Every global write returns `403`, allowlist looks populated | Issuer mismatch | The pair is matched whole. A subject under an unexpected issuer is a different principal. Compare the `iss` claim on the actual token. |
| Audit shows an unfamiliar `allowlist_fingerprint` | The list changed | The fingerprint is over the sorted list, so reordering is not a change. Someone edited membership. |

---

## Key custody and rotation

ARC uses seven distinct key purposes, each with its own provider class. Sharing
a key between any two is a real vulnerability, not untidiness: a receipt-signing
key that also verified host attestations would let a host mint receipts.

| Purpose | Rotation effect |
|---|---|
| Receipt event signing | Old public keys stay published for the full receipt retention period. |
| Host attestation verification | An old key stays valid for attestations already issued under it, until its own expiry. |
| Approval evidence verification | Revocation is **immediate** — unlike signing keys, a revoked approval verifier stops verifying at once. |
| Challenge nonce derivation | A retired key must be held for the challenge window plus skew, or in-flight retries break. |
| Content encryption | Re-keying re-wraps DEKs; content ciphertext is untouched. |
| Response replay encryption | Retained receipts become unreplayable without their key. |
| Continuation token | Tokens sealed under a dropped key stop opening; clients restart paging. |

### Rotating a receipt-signing key

1. Add the new key and make it active. New events sign under it immediately.
2. **Do not remove the old public key.** A receipt signed years ago must stay
   verifiable, and `GET /v1/arc/metadata` publishes retired and compromised keys
   for exactly that reason. Removing one both breaks verification and hides the
   compromise.
3. Verify `GET /v1/arc/metadata` lists both.

### Rotating content encryption

Re-key a revision **and its directives together**. Directive prose uses its
parent revision's content-protection mode, so re-keying a revision alone leaves
directive envelopes naming a key nobody looks up — undecryptable content that
looks fine until someone reads it.

---

## The reserved `_deployment` tenant

`ffffffff-ffff-ffff-ffff-ffffffffffff`, slug `_deployment`.

It exists so deployment-scope audit rows have a foreign-key target. It is **not
an identity anything authenticates as** — requests arriving under it are
rejected on every path, including ones otherwise open to every tenant.

### Why an auditor sees it

Deployment-wide events (global artifact registration, operator actions) are
filed against it rather than against whichever tenant happened to trigger them.
Filing them under a real tenant would both mislead that tenant's auditor and
leak that deployment-wide activity occurred.

An auditor querying audit rows for `_deployment` is seeing deployment
governance, not a tenant's. It is intentionally disabled (`disabled_at` set) so
no ordinary path can use it.

> **Do not delete or re-enable it.** It is not the all-zero UUID — that is the
> seed `default` tenant. These were the same value at one point, which made the
> reserved-tenant insert a silent no-op and the downgrade a deletion of a real
> tenant.

---

## Review expiry and renewal

Every active revision carries `review_expires_at`. Governance nobody has
re-confirmed is stale, and stale governance is worse than absent because agents
still obey it.

The review-expiry worker transitions expired revisions to `expired` and advances
any mandatory obligation they satisfied to `missing_review_expired`.

### Renewing before expiry

1. Register a new revision of the artifact with a fresh `review_expires_at`.
2. Attach its approval evidence.
3. Activate it. The successor takes over the predecessor's obligation rather
   than adding a second, so there is no window of double-blocking.

### After expiry

Resolutions matching the obligation **block** with a review-expiry reason. That
is the intended behaviour: the obligation is still known to apply, and nothing
current satisfies it. Register and activate an approved successor.

Expiry is also re-checked at activation, so a revision that sat in draft past its
review date cannot be activated.

---

## Audit outbox

ARC does not write `audit_log` inline. It writes an outbox row in the same
transaction as the state change, and the drain worker is ARC's **only** writer to
`audit_log`. A resolution is a retryable transaction: inline writes would either
roll back with the attempt or record attempts that never committed.

### The stuck-row gauge

Undrained depth is exposed as a gauge. A rising floor means rows are failing
repeatedly, not that traffic is high.

```sql
-- How many, how old, and why.
SELECT count(*), min(created_at), last_error_code
FROM arc_audit_outbox
WHERE drained_at IS NULL
GROUP BY last_error_code
ORDER BY count(*) DESC;
```

| `last_error_code` pattern | Likely cause | Action |
|---|---|---|
| Same code, `attempts` climbing | Sink rejecting the payload | Inspect one row's `event_payload`. Fix the sink or the emitting call site. |
| `NULL` with old `created_at` | Worker not running | Check the scheduler. Rows are safe; they drain when it returns. |
| Mixed codes, growing | Sink unavailable | Treat as a sink outage. Do not delete rows. |

> **Never delete undrained rows to clear the gauge.** They are the audit record
> for state changes that already committed. Deleting them destroys the evidence
> and leaves ARC unable to explain what it did.

---

## Downgrade guard

`alembic downgrade` past the ARC migration **refuses** when receipts or
legal-held revisions exist. Receipts are retained audit evidence, and a routine
downgrade would destroy them silently.

### Archive-first procedure

1. Confirm what would be lost:

   ```sql
   SELECT count(*) FROM arc_receipts;
   SELECT count(*) FROM arc_revisions WHERE legal_hold;
   ```

2. Archive receipts, their event chains, and their selected rows to durable
   storage outside the database. Verify the archive is readable before
   continuing.
3. Clear legal holds only if legally cleared to do so.
4. Downgrade with the per-session escape:

   ```sql
   SET arc.allow_destructive_downgrade = 'on';
   ```

   Per-session and deliberate, not a flag set once and forgotten. A development
   database with no receipts downgrades freely.

---

## Activation gates

Some ARC capability is present in code but not enabled, each behind a gate. Each
blocks a specific thing, and knowing which matters when triaging.

| Gate | Blocks | Why |
|---|---|---|
| Host attestation key registration | Any real agent host resolving context | Registering a host is a trust decision requiring out-of-band identity proof. |
| Production key custody (KMS/HSM) | Production receipt signing | Development signers hold raw private bytes in process. |
| Global-scope content activation | Deployment-wide encrypted content | Needs provider, recovery, and rotation approval — losing the key loses the governance. |
| Approval-verifier revocation cascade | `POST /v1/arc/admin/approval-verifiers/{id}/revoke` (returns `501`) | Revoking trust must also withdraw affected revisions and exceptions. Without the cascade, revisions stay active on withdrawn approval — worse than refusing. |
| Closed vocabularies for content classification and receipt event type | Nothing at runtime; both columns are length-bounded only | The vocabulary members are a product decision, not an implementation one. |

---

## Triage: the system is refusing something

| Response | Meaning | Usually |
|---|---|---|
| `403 blocked_manifest_unverified`, no receipt | No trusted attestation | Unregistered/revoked signer key, or an expired or already-consumed challenge. **Not** a policy refusal. |
| `200` with `resolution_status: blocked` | Authenticated and refused by policy | Read the receipt: it explains why. Working as intended. |
| `403 detail_denied` | Detail unavailable | Revoked artifact, audience policy, or an invalid continuation token — deliberately indistinguishable. |
| `409 exception_not_permitted` | The directive forbids exceptions | The target is not delegable. Not a permissions problem. |
| `mcp_preflight_required` | Connection has no valid preflight | Call `arc_complete_preflight` first, or the credential changed mid-connection. |

A blocked resolution is a **success** at the transport layer. Returning `403`
would discard the receipt and make "you may not do this" indistinguishable from
"you are not who you say you are".
