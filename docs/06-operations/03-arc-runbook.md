# ARC operator runbook

Agent Readiness Context (ARC) gives AI agents attested, deterministic
governance context and records what they were given. That evidential purpose
shapes every procedure below: several things an operator would normally expect
to be able to fix quickly are deliberately hard, because doing them casually
would destroy the record that makes ARC worth having.

Read [Attested context resolution](../01-overview/11-attested-context-resolution.md)
before this runbook if artifacts, obligations, challenges, blocked receipts, or
approval trust are unfamiliar.

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

## Source admission: connectors and upload policies

A source enters ARC through exactly one of two closed authorities: a
**connector** the deployment registers to fetch from an allowlisted
location, or an **upload policy** an authenticated caller sends bytes
against directly. Both stream through a hard 10 MiB ceiling while this
deployment hashes the bytes itself with SHA-256 — a claim's own asserted
digest is checked *against* that computed value, never trusted in its
place.

### Registering an authority is operator-gated, admitting through one is not

Registering a connector or upload policy — `POST /v1/arc/admin/source-connectors`
and `POST /v1/arc/admin/source-upload-policies` — always requires deployment
operator identity (the `ARC_GLOBAL_OPERATOR_ALLOWLIST` pair from the
[Operator allowlist](#operator-allowlist) section above), even when the
authority being registered is scoped to one tenant. A tenant admin cannot
self-register a connector for their own tenant.

*Admitting* a source through an already-registered authority is different:
it authorizes on that authority's own declared scope, so a tenant-scoped
connector or policy admits under ordinary tenant-admin authorization, and
only a global-scoped one needs operator identity at admission time too.

A connector registration names the closed set the fetch path may use —
nothing a caller supplies later can widen it:

| Field | Meaning |
|---|---|
| `connector_id` | Operator-chosen identifier callers reference at admission time. |
| `owning_scope` / `target_tenant_id` | `global` or `tenant` (with the tenant id set exactly when `tenant`). |
| `allowed_schemes` / `allowed_hosts` | Re-validated on every redirect hop, not only the first request — a fetch that redirects off this list is refused mid-fetch. |
| `allowed_media_types`, `allowed_verifier_ids` | Closed sets an admission against this connector must land inside. |
| `max_bytes` | Capped at 10485760 (10 MiB) by both a database constraint and the streaming reader independently. |
| `credential_ref` | The **name** of an environment variable, not the credential itself — resolved at fetch time via the same dynamic-ref mechanism sync connectors already use (see `contextplane/ingest/connector.py::resolve_credential` in `CLAUDE.md`'s secrets section). Set the named variable in the deployment's own secret store; the fetch sends its value as a Bearer token. |

An upload policy is the same shape minus the fetch-side fields
(`allowed_schemes`, `allowed_hosts`, `credential_ref`) — an authorized
upload supplies no URL, so there is no host or redirect to validate.

### Idempotency and retries

Every admission call requires an `Idempotency-Key` header (not a body
field). An exact retry — same key, same scope, same payload — returns the
first evidence row unchanged. A retry under the same key with a *changed*
payload is refused with `arc_idempotency_conflict` (409): the key names one
admission attempt, not a slot to overwrite.

### The residual gap: proof is stored, not yet cryptographically verified

Today, `proof.signature_base64` / `proof.assertion_base64` on an admission
claim are validated for shape and stored, but this admission layer does not
yet verify either cryptographically against an enrolled key or attestation
provider — authorization at this layer rests on `verifier_id` allowlist
membership on the admitting connector or policy, not on a checked
signature. Do not treat a successful admission as cryptographic proof the
named verifier actually produced the claim; treat it as "an allowlisted
verifier id was named and the bytes match the claimed digest." A worked,
executable example of the full multipart/JSON request shape lives in
`tests/integration/test_arc_source_admission.py` — read it rather than
hand-constructing a request from this table, since the claim and proof
objects nest several closed sub-schemas this runbook does not restate.

---

## Source status: freshness, refresh, and today's revocation gap

Every admitted source's approval status is re-checked, never assumed
current: `checked_at`/`next_check_at` cap freshness at five minutes, and a
read past `next_check_at` fails closed with `arc_source_status_unavailable`
(409) — the same code covers overdue, expired, revoked, or unknown status,
because in every case the correct action is identical: do not trust this
source right now.

The `arc_source_status_refresh` job runs every 60 seconds and re-derives
status for every row past its `next_check_at`. Two transitions are
terminal and cascade atomically to every dependent `active` revision (flip
to `revoked`/`expired`, one audit row, one operational event per cascaded
revision, all in one transaction — never a partial cascade):

- **Expiry** fires automatically. A source's admission-time claim caps its
  own `next_check_at` at the claim's `expires_at`, so `check_status` starts
  refusing on that deadline even before the refresh job gets to writing it
  durably.
- **Revocation** does not fire automatically on any deployment today. The
  refresh job's remote-status check is a stub that always reports "not
  revoked" — there is no real connector/provider integration behind it yet
  to ask, and this is the honest, current state of every deployment rather
  than a corner case. Nothing in this deployment can currently detect that
  a source's approval was revoked upstream before its own claimed expiry.
  There is also no admin route that writes a revocation directly. If a
  source must be treated as untrustworthy before its recorded `expires_at`,
  no supported operator action exists yet — do not hand-edit
  `arc_source_approval_status` to work around this; a direct `UPDATE`
  bypasses the revocation cascade, the audit row, and the operational
  event this transition is supposed to produce atomically, leaving
  dependent revisions active on a status nothing else agrees with.

Find sources that are overdue, expired, or otherwise not `current`:

```sql
SELECT source_evidence_id, status, checked_at, next_check_at
FROM arc_source_approval_status
WHERE now() > next_check_at OR status <> 'current'
ORDER BY next_check_at
LIMIT 20;
```

A large, growing result here with `status = 'expired'` rows whose sources
are still needed means their claims need re-admission with a fresh
`expires_at` — there is no renewal call that extends an existing source's
expiry in place.

---

## Operational chain and checkpoint export

Every lifecycle transition on a revision that has one appends a signed,
hash-chained `arc_operational_events` row, advances a per-revision head, and
writes a pending checkpoint outbox row — all three in the same database
transaction as whatever domain change the event is evidence of. Two
production gaps apply to every deployment today; both are by design for now,
not silent defects, and an operator should know both before relying on this
chain for more than it currently provides.

### The signing key is per-process, in memory, and does not survive a restart

The chain signs each event under an Ed25519 key this process generates once
for itself the first time it needs one. Private key material never leaves
process memory and is never persisted to any table. Two consequences follow
directly:

- **No rotation, no HSM/KMS custody.** This signing purpose has not yet been
  wired to the same operator-configured key material the deployment's other
  signing purposes (receipt events, host attestation) already wait on.
- **Restarting a process, or running more than one API replica, produces
  more than one signing key.** Two replicas append to the same revision's
  chain under different keys; a restart orphans every prior signature's
  verifier for that process. This deployment's chain verification already
  accounts for this (every key a process has ever signed with stays
  resolvable, not only the current one), but it means the chain's signature
  is not yet a durable, deployment-wide guarantee — treat it as evidence a
  compromised database alone cannot forge, not as a portable, long-lived
  credential.

### `deployment_id` is a fixed literal, not a per-deployment setting

Every checkpoint's identity is `{deployment_id, revision_id, sequence}`, so
an external sink can tell more than one deployment's checkpoints apart. There
is no operator-configurable deployment-identity setting yet — every
deployment currently names itself the same literal string
(`registry-default-deployment`). This is harmless only because no real sink
exists yet to collide against (see below); the moment one is wired for more
than one deployment, that literal needs to become a real per-deployment
setting first.

### No append-only sink is configured on any deployment today

`CheckpointExportService` is an abstraction over an external, independently
held append-only acknowledgment store — the thing that lets this chain
detect a compromised database quietly shortening its own suffix. Every
deployment constructs this service with no sink at all. This is not a stub
that pretends to succeed: every pending checkpoint stays pending, safely and
visibly, until a real sink is wired. Nothing here silently marks a
checkpoint durable that no external store has actually acknowledged.

The `arc_checkpoint_exporter` job runs every 30 seconds and logs its result
at INFO whenever there is a pending checkpoint:

```
arc_checkpoint_exporter: due=<n> exported=<n> sink_unavailable=<n> integrity_failed=<n>
```

A steady, non-shrinking `sink_unavailable` count is expected on every
deployment shipped today — it means the pending checkpoints are safe and
waiting, not stuck. Find them directly:

```sql
SELECT count(*), min(created_at), last_export_error_code
FROM arc_operational_chain_checkpoints
WHERE exported_at IS NULL
GROUP BY last_export_error_code
ORDER BY count(*) DESC;
```

`integrity_failed` (nonzero `last_export_error_code`, `sink_mismatch` /
`suffix_rollback` / `missing_receipt`) is a different and more serious
signal than `sink_unavailable`: it means an actual external sink was reached
and it disagrees with this deployment's local chain. Recovery for that case
is refusing to trust the local chain for the affected revision, never
silently accepting whichever side looks newer — escalate rather than retry
in a loop.

---

## Verifier enrollment (replaces direct registration)

Registering an approval verifier that can bind to a principal is now a
two-call protocol, not a single request: `POST
/v1/arc/admin/approval-verifiers/enrollment-challenges` issues a nonce and
canonical enrollment bytes to sign or attest over, and `POST
/v1/arc/admin/approval-verifiers` completes enrollment only against a
challenge whose proof of possession verifies. **An older, single-call
registration attempt against `POST /v1/arc/admin/approval-verifiers` with no
prior challenge is refused with `arc_enrollment_challenge_required` (409).**
There is no direct-registration fallback for principal-bound verifiers to
retry into.

Two binding kinds exist; only one is reachable today:

- **`exact_principal`** — proof of possession is a detached signature over
  the challenge's own canonical bytes. This is the binding kind every
  deployment can complete today.
- **`provider_delegated`** — proof of possession is a trusted, in-process
  attestation provider's own assertion, not a detached signature. No
  deployment ships with an in-process attestation provider configured, so
  completing a `provider_delegated` enrollment refuses on every deployment
  today with a message naming the unconfigured provider id. Configuring one
  is a code-level wiring change (an `attestation_providers` mapping passed
  to the enrollment service), not an environment variable — there is
  nothing to set in `.env.example` to turn this on.

A challenge is single-use: the first completion consumes it, and a second
attempt against the same challenge id loses regardless of whether it would
otherwise have verified.

---

## Submission's prerequisite refusal

`POST` on a proposal version's submit route is fully implemented — the
freeze, the one draft revision it materialises, the bijection link, and the
audit event are all written in a single transaction — but it refuses before
opening that transaction on every deployment today, with
`arc_operational_integrity_pending` (409). The message names why: the
transaction requires an injected operational-chain appender **and** a
risk/envelope validator in the same call, and only the first exists yet.
Nothing is written on this refusal — a proposal version that fails to
submit today is byte-identical to one that was never submitted.

This is expected, current-state behaviour, not an incident. There is no
workaround, flag, or retry that makes submission succeed today; do not spend
triage time on a submit failure carrying this exact code beyond confirming
it is this one. It stops being the answer once risk classification and
expected-impact-envelope validation are wired into the same call.

---

## Drafter: the human_only verdict and the structured form

The committed drafter model decision artifact
(`contextplane/arc/drafter/model_decision.json`) records `outcome: human_only`
on this codebase: no model evaluation could be executed against a real
candidate, because the accepted drafter sandbox has no network route by
design and no deployment-local model artifact exists to evaluate. Every one
of the artifact's evaluation gates is recorded honestly as not evaluated,
naming the fixture corpus a real evaluation would use — none is fabricated
as passing.

Consequences for what an operator can rely on today:

- `POST {proposal_id}/versions/{proposal_version}/draft` refuses with
  `arc_drafter_model_disabled` (409) on every deployment, before it reads
  the proposal or spawns a sandbox process, regardless of
  `ARC_DRAFTER_MODEL_ENABLED`'s setting — the flag can never be more
  permissive than the committed artifact (see
  [the configuration reference](../05-reference/03-configuration.md#agent-readiness-context-arc)
  for the full startup-refusal contract if `ARC_DRAFTER_MODEL_ENABLED=true`
  is set anyway).
- The **human structured-form path is the one that works** and carries no
  weaker review, approval, or activation guarantee than a model-assisted
  draft would have: editing a proposal version directly (`PATCH`,
  `/validate`, `/semantic-tests`) and recording reach confirmations
  (`POST {proposal_id}/versions/{proposal_version}/reach-confirmations`)
  function independently of the drafter's own disabled state.

Attempting to enable the model path against this committed artifact is not
an operator action available today — there is no candidate model artifact
this deployment could point `ARC_DRAFTER_MODEL_ARTIFACT_PATH` at that would
pass the digest check, because none was ever evaluated.

---

## Parser and drafter sandboxes: isolation and its named gaps

The parser and drafter each run as an isolated OS subprocess of the API
container — no separate pod, no separate image — under a dedicated
`arc-sandbox` group (GID 1500 in the shipped image) so the local sockets
they create come up group-owned and mode `0660` rather than
world-reachable. Read [`deploy/README.md`](../../deploy/README.md) for the
container/Helm knobs this section assumes.

What is closed today, confirmed by running the shipped container rather
than by reading its manifest: a real kernel memory ceiling (the pod/container
memory limit is the actual outer bound; the sandbox's own in-process limit is
a second, inner one — size the outer limit for the base app plus one sandbox
process, not the base app alone), a real read-only root filesystem with a
writable `tmpfs` scratch mount, and a network guard that refuses outbound
sockets and DNS resolution from inside the sandbox process itself.

What is **not** closed today — named gaps, not omissions:

- **CPU is a scheduling quota, not core pinning.** A container or pod CPU
  limit bounds how much CPU time the sandbox may consume; it does not
  restrict it to one specific core the way the isolation goal describes.
  Real core-affinity pinning needs the cluster's own node-level CPU-manager
  configuration plus running this pod in the Guaranteed QoS class (requests
  equal to limits) — a cluster operator's choice no chart can force by
  itself. Outside Kubernetes, `docker run --cpuset-cpus` achieves the same
  restriction directly with no cluster cooperation needed.
- **Read-side filesystem confinement is not complete.** The sandbox cannot
  write outside its granted scratch path (the read-only root filesystem
  enforces that for real), but it is not confined to reading *only* its one
  granted content path — that needs a per-invocation mount namespace, which
  the container runtime's own default security policy refuses to create
  short of `--privileged`, a tradeoff that would remove more isolation than
  it adds.
- **The sandbox subprocess shares the API process's own OS identity.** There
  is one non-root user in the shipped image; the parser and drafter run as
  that same user, distinguished from the API process only by the dedicated
  socket group above. A separate sandbox OS identity — so a compromised
  sandbox subprocess cannot read anything the API process itself can read —
  needs a privilege-drop code change this deployment does not make today.

---

## Startup integrity checks and `/healthz` / `/readyz` semantics

Three checks run inside application startup, before the process can accept
any request at all — a failure here is not a degraded readiness state
visible at `/readyz`; it is a process that never comes up, restarting (or
crash-looping, depending on the orchestrator) until whatever it is refusing
about is fixed. Diagnose these from process logs, not from probe status:

1. **The embedding-dimension check** (unrelated to ARC, but ordered first).
2. **The legacy-activation-evidence check.** Counts
   `arc_approval_evidence` rows with `evidence_type = 'artifact_activation'`.
   No writer in this deployment produces that evidence type today — the
   direct activation-evidence route was removed, and its replacement is not
   live yet — so any such row can only predate a trusted writer and is
   refused rather than grandfathered in. Confirm the count directly:

   ```sql
   SELECT COUNT(*) FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation';
   ```

   A nonzero count refuses startup with a report naming the count. Recovery
   is revoking the dependent active revision(s), or an explicit, reviewed
   bootstrap migration that re-creates equivalent evidence through a
   trusted writer and records the bootstrap in the audit log — never
   deleting the rows silently, which would erase the fact that they existed
   outside any trusted writer.
3. **The drafter decision check.** Only runs at all when
   `ARC_DRAFTER_MODEL_ENABLED=true`; see
   [Drafter: the human_only verdict and the structured form](#drafter-the-human_only-verdict-and-the-structured-form)
   above for what it refuses on.

`/readyz` itself only ever checks database connectivity (`SELECT 1`); it
carries no ARC-specific state. That is deliberate, not an omission — the
three checks above already gate whether the process is running at all, so a
readiness probe checking them again could only ever report "ready," never
catch a failure the process itself would have refused to boot with.

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
