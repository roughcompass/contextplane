# Memory Curation Runbook

Living Memory lets agents, session transcripts, and governed connectors write
observations into the catalog without those observations becoming truth on
arrival. Every observation starts as a **staged claim** — readable, but not
part of the canonical graph — and only becomes canonical through a review a
person can see and undo. This runbook is for the operator or steward who
works the curation queue, reviews promotions, and configures how much of that
review a tenant is willing to automate.

Audience: operators and stewards with `producer` or `admin` access in the
target tenant. For what a claim, a promotion, and a capability request *are*,
see [Concepts](../01-overview/03-vocabulary.md#claim). For the exact request
and response shape of every route and MCP tool covered here, see
[MCP tools reference → Memory curation](../05-reference/02-mcp-tools.md#memory-curation)
— the agent-facing tools and the REST routes below are the same service
calls, so that section's field tables apply to both. This page does not
repeat those schemas; it covers what an operator does with them, and what to
check when something looks wrong.

**Before you act, know two things:**

1. **Nothing is canonical until it is promoted.** A claim search, a queue
   listing, or a claim history entry can all show a staged claim — that is
   not the same as it being true in the catalog's own graph. Search results
   drawn from staged claims carry `trust: "untrusted"` for exactly this
   reason (see [MCP tools reference → search_claims](../05-reference/02-mcp-tools.md#search_claims)).
2. **Auto-promotion is opt-in and empty by default.** A fresh tenant
   auto-promotes nothing. Turning it on for a predicate is a real,
   audited decision with real blast radius — see
   [Auto-promotion: enabling it safely](#auto-promotion-enabling-it-safely)
   before you touch it.

Every example below uses `<registry-base-url>` and `$TOKEN` the same way the
rest of the operations runbook does — see
[Operations Runbook → Getting a token for these examples](01-ops.md#getting-a-token-for-these-examples).
Every command on this page was run against a local dev stack seeded with
`make dev-seed`; the [seeded walkthrough](#walk-the-seeded-demo) at the
bottom reproduces the exact commands and real output.

---

## The loop, in one pass

```
stage → consolidate → propose → review → promote / reverse
```

- **Stage.** A claim enters through one of three doors: an agent asserting it
  directly (`POST /v1/memory/claims`), extraction turning session transcripts
  into candidates, or a governed connector admitted through source
  governance (see [Source governance for connectors](#source-governance-for-connectors)).
  All three land in the same place — staged, or `unlinked` if the subject
  reference does not yet resolve to an entity.
- **Consolidate.** A scheduled sweep (`consolidation_sweep`, every
  `CONSOLIDATION_SWEEP_INTERVAL_S`) reconciles a claim against others about
  the same subject and predicate: agreement scores it up, disagreement marks
  both sides contested. See [Configuration reference](../05-reference/03-configuration.md)
  for the interval settings.
- **Propose.** Once a claim is consolidated, subject-resolved, and not
  already promoted, the `promotion_sweep` worker proposes it for promotion —
  this is the step that turns a settled staged claim into something a review
  queue can act on.
- **Review.** A person (or, for allowlisted predicates, the system itself)
  accepts or rejects the proposal.
- **Promote / reverse.** Accepting writes the canonical attribute or edge;
  the write is reversible, exactly, by reference to that one promotion.

Curators intervene at **stage** (link, discard), **review** (accept, reject),
and after the fact (**reverse**, **confirm**, **adjudicate**). Everything
else is the scheduled sweeps working without a human unless a guardrail
routes something back to one.

---

## Is the curation backlog growing?

Two numbers answer this, both **cluster-scoped** (counted from the database
at read time, correct regardless of how many replicas are running) — see
[`GET /v1/admin/operational-health`](../06-operations/01-ops.md):

```bash
curl "<registry-base-url>/v1/admin/operational-health" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Look for two `queues` entries:

| Key | What it counts | Reads as a problem when |
|---|---|---|
| `curation_queue_backlog` | Every claim that is unlinked, contested, below the tenant's confidence floor, or waiting on a high-impact proposal's owner — the same rows [`GET /v1/memory/curation-queue`](#reading-and-working-the-queue) would list, counted cluster-wide rather than for one tenant | Rising release over release, or large relative to your claim volume — nobody is working the queue, or something is producing claims faster than curators can act |
| `oldest_open_proposal_age_seconds` | How long the longest-waiting **open** promotion proposal has been waiting; `0` when nothing is open | Growing without bound — proposals are piling up faster than reviewers decide them, or a specific proposal is stuck (check whether it is high-impact and who its owner is) |

A per-tenant view of the same backlog, with the reason breakdown, is the
queue's own counts endpoint:

```bash
curl "<registry-base-url>/v1/memory/curation-queue?counts=true" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{"counts":{"contested":2,"unlinked":1}}
```

**What to do when it is growing:**

1. Pull the reason breakdown above. `unlinked` growing means a source keeps
   naming subjects that never resolve — check whether the connector or agent
   producing them should get `may_provision_entities` (see
   [Source governance](#source-governance-for-connectors)), or whether the
   references are simply wrong. `contested` growing means two sources
   keep disagreeing about the same predicate — look at which sources, and
   whether one's authority tier is mis-declared. `awaiting_owner` (a
   high-impact proposal) growing means the owning tenant's reviewers are not
   keeping up with consequential changes specifically — that queue needs a
   person, not a wider allowlist.
2. If `oldest_open_proposal_age_seconds` is large but the backlog count is
   small, one proposal is stuck: list open proposals
   ([Reviewing a promotion proposal](#reviewing-and-deciding-a-promotion-proposal))
   and look at its `high_impact_reasons` — a proposal nobody realizes needs
   their attention is the most common cause.
3. Widening the allowlist is **not** the first response to a growing queue.
   It removes eligible, low-risk claims from the *human* queue, but it does
   nothing for `unlinked` or `contested` claims (never auto-promoted
   regardless of the allowlist) and nothing for high-impact proposals (never
   auto-promoted, full stop). See the guardrails below before reaching for it.

---

## Reading and working the queue

```bash
curl "<registry-base-url>/v1/memory/curation-queue" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Every item names a `reason` and the `available_actions` that reason permits —
offering an action that does not apply to a row is how a queue gets worked
incorrectly, so the item itself tells you what you may do:

| Reason | Meaning | Actions offered |
|---|---|---|
| `unlinked` | The claim's subject reference never resolved to an entity | `link`, `discard` |
| `contested` | This claim disagrees with another about the same subject and predicate | `confirm`, `discard`, `escalate` |
| `below_floor` | Consolidated, but its confidence sits below the tenant's promotion floor | `confirm`, `discard` |
| `awaiting_owner` | An open, **high-impact** promotion proposal — needs the subject's owner specifically, not just any curator | `escalate` |

**Link** a subjectless claim once you know what it is about. Curator-only
(`producer` or `admin`), and the reference must be an **entity id or a
`system:external-id` pair** — not the entity's slug name. This is a real
gotcha: the rest of the API resolves a capability's slug name in a path
(`GET /v1/capabilities/salt-design-system`), but claim subject resolution
does not, by design (see
[Troubleshooting](#troubleshooting) below) — resolve the name to an id first
if you don't already have one:

```bash
curl -X POST "<registry-base-url>/v1/memory/claims/<claim-id>:link" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"subject_reference": "<entity-uuid>"}'
```

**Discard** refuses a claim outright — staged or still unlinked — and it
never serves again. This is also the only way out of the queue for a
reference that will never resolve (a typo, a decommissioned system): it
cannot be linked, and it cannot be scored, so discarding is the terminal
action for it, not a special case:

```bash
curl -X POST "<registry-base-url>/v1/memory/claims/<claim-id>:discard" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "subject will never resolve; asserted by mistake"}'
```

**Escalate** on a contested or awaiting-owner item means raising a capability
request against the subject's owner — see
[Escalating and capability requests](#escalating-and-capability-requests).

---

## Confirming and judging a claim

**Confirm** is a human putting their name to a claim. It produces a *new*
claim that supersedes the original — the original keeps its machine-derived
score and provenance, and the confirmation carries the human authority tier,
a raised confidence, and a hold against decay. Human principals only: a
service or worker credential is refused, because the human tier records that
a person actually reviewed this.

```bash
curl -X POST "<registry-base-url>/v1/memory/claims/<claim-id>:confirm" \
  -H "Authorization: Bearer $TOKEN"
```

**Adjudicate** records whether a claim's assertion turned out to be correct —
the only input the calibration loop is ever fitted from. `verdict` is
`correct`, `incorrect`, or `undecidable`; `observed_confidence` is what the
reviewer saw at judgment time.

```bash
curl -X POST "<registry-base-url>/v1/memory/claims/<claim-id>:adjudicate" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"verdict": "correct", "observed_confidence": 0.92}'
```

See [Calibration](#calibration) for what happens to adjudications after
they're recorded.

---

## Reviewing and deciding a promotion proposal

```bash
curl "<registry-base-url>/v1/memory/promotion-proposals?state=open" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Each proposal carries `current_value` (what the canonical graph says now, or
`null`) alongside `proposed_value`, and `high_impact`/`high_impact_reasons` —
a proposal is high-impact when it narrows a dependency surface, crosses the
tenant's blast-radius threshold, would displace a human confirmation, comes
from a different tenant than the subject's owner, or names a predicate the
tenant has put on its own always-review list. High-impact proposals are
**never** eligible for auto-promotion regardless of the allowlist.

Accept (optionally amending the value) or reject:

```bash
curl -X PATCH "<registry-base-url>/v1/memory/promotion-proposals/<proposal-id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "accepted"}'
```

```bash
curl -X PATCH "<registry-base-url>/v1/memory/promotion-proposals/<proposal-id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "rejected", "reason": "incorrect"}'
```

`reason` must be one of `incorrect`, `already_known`, `not_actionable`,
`wrong_subject`, `superseded_by_other` — rejecting records the refused
value so the same assertion, from the same or a weaker source, cannot
silently re-queue after a sweep proposes it again. A stronger source (a
human overturning a machine's rejected claim, for instance) can still be
proposed and reviewed again.

Authority to decide is entirely the promotion service's own gate: `producer`
or `admin`, and **only in the tenant that owns the proposal's subject** — a
claim's *author* tenant has no say over whether it becomes canonical on
someone else's graph. That is the whole mechanism behind cross-tenant claims
routing to their owner rather than writing directly.

---

## Auto-promotion: enabling it safely

**The default is that nothing auto-promotes.** A fresh tenant's allowlist is
empty, and there is no wildcard entry — turning auto-promotion on for a
predicate is a deliberate, per-predicate, audited act, never a default an
operator has to remember to turn off.

Four conditions must **all** hold before the scheduled sweep accepts a
proposal without a person:

1. **Not high-impact.** Checked independent of confidence — being certain a
   capability is about to be withdrawn is a reason to make sure a person sees
   it, not a reason to skip them.
2. **Eligible** — consolidated, uncontested, subject-resolved, above the
   tenant's confidence floor, and not already promoted.
3. **Owner-originated** — the claim's author tenant is the subject's owner.
   A cross-tenant claim always waits for the owner to review it themselves.
4. **The predicate is on the tenant's own allowlist.**

**What "safe" means when you turn a predicate on:** every future eligible,
owner-originated, non-high-impact claim under that predicate skips human
review, for every subject in the tenant, until you revoke it. That is the
blast radius — not one claim, but a standing posture. Start with predicates
whose worst case is cheap to reverse and easy to notice (a runbook URL, a
work-item link) before ones that shape how other systems query the graph
(`lifecycle_state`, `depends_on_version`).

```bash
curl "<registry-base-url>/v1/admin/memory-autopromote-allowlist" \
  -H "Authorization: Bearer $TOKEN"
# {"predicates":["runbook_url"]}

curl -X POST "<registry-base-url>/v1/admin/memory-autopromote-allowlist:allow" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"predicate": "escalation_contact"}'
# {"predicates":["escalation_contact","runbook_url"]}

curl -X POST "<registry-base-url>/v1/admin/memory-autopromote-allowlist:revoke" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"predicate": "escalation_contact"}'
# {"predicates":["runbook_url"]}
```

Requires `admin`. Both calls return the allowlist's new state, so you can
confirm what's now auto-promoting without a second round trip.

**Auto-promotion always runs as a distinct system identity, never as you.**
The sweep accepts under a per-tenant `system-curator` actor — a separate
`actor_kind` from both human principals and the sync-worker identity
connectors use — and writes a second audit row naming the guardrail decision
and that system actor alongside the ordinary promotion audit entry. Filter
the audit log by that actor kind (or by `action = 'claim.auto_promoted'`) to
see everything that promoted itself versus everything a person decided.

**Everything else in the review posture** — the confidence floor, the
blast-radius threshold, and a predicate list that always forces review
regardless of the allowlist — is the tenant's promotion policy, separate from
the allowlist:

```bash
curl "<registry-base-url>/v1/admin/memory-promotion-policy" \
  -H "Authorization: Bearer $TOKEN"
# {"confidence_floor":0.0,"blast_radius_threshold":5,"always_review":[]}

curl -X PUT "<registry-base-url>/v1/admin/memory-promotion-policy" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"confidence_floor": 0.6, "blast_radius_threshold": 5, "always_review": ["lifecycle_state"]}'
```

`PUT` replaces the whole policy in one write: `confidence_floor` and
`blast_radius_threshold` are required on every call (an omitted one is a
`422`, not a silent keep-current-value), and an omitted `always_review`
reverts to an empty list rather than keeping whatever was there before — so
always send all three, every time, even to change just one.

---

## Reversing a promotion

Reversal restores exactly what the canonical graph said before the
promotion: the row the promotion created is closed (not deleted — an `as_of`
query spanning the promotion still sees that it happened), and the row it
superseded is reopened to its original interval.

```bash
curl -X POST "<registry-base-url>/v1/memory/promotions/<promotion-id>:reverse" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "value was wrong; the source had stale information"}'
```

`<promotion-id>` is the id `review_promotion_proposal`'s (or the sweep's)
accept call returned — not the proposal id and not the claim id. If you did
not capture it at accept time, the promotion review response
(`promotion_id`) or the audit log entry for that promotion is where to find
it; there is no separate REST listing of a claim's promotion journal today.

**Required:** `producer` or `admin`, in the tenant that owns the promoted
row — the same authority bar as accepting the proposal in the first place.

**Refuses (409)** when a later promotion has already built on the row this
one created — reversing the older change first would silently discard the
newer one. Reverse the later promotion first, then this one.

Requesting `reason` is required and audited; there is no dry run for a
reversal because the effect is idempotent to inspect after the fact (an
`as_of` query straddling the reversal shows exactly what changed) rather
than needing a preview before it happens.

---

## Escalating and capability requests

Escalating a contested or awaiting-owner queue item raises a capability
request against the subject's owner — the same mechanism a consumer uses to
ask an owning team for something directly:

```bash
curl -X POST "<registry-base-url>/v1/memory/capability-requests" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "subject_entity_id": "<entity-uuid>",
    "request_category": "documentation",
    "title": "Short summary",
    "body": "Full request text"
  }'
```

The owner reviews what's waiting on them:

```bash
curl "<registry-base-url>/v1/memory/capability-requests?role=owner" \
  -H "Authorization: Bearer $TOKEN"
```

...and moves it along its lifecycle — `acknowledged`, `accepted`, `declined`,
`duplicate`, or `resolved`:

```bash
curl -X PATCH "<registry-base-url>/v1/memory/capability-requests/<request-id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to_status": "acknowledged"}'
```

A request that produced a real canonical change can be linked to the
promotion that made it, closing the loop visibly for the requester
(`POST /v1/memory/capability-requests/{id}:link-promotion`).

---

## Calibration

Confidence scores start from a fixed table by authority tier. Calibration
refits that mapping against **judged outcomes** — the `adjudicate` calls
above — per `(provider, model, strategy)` triple, on a schedule
(`calibration_refit`, every `CALIBRATION_REFIT_INTERVAL_S`; see
[Configuration reference](../05-reference/03-configuration.md)).

```bash
curl "<registry-base-url>/v1/admin/memory-calibration" \
  -H "Authorization: Bearer $TOKEN"
```

Each row is one triple's most recent fit attempt: `status`, `n_adjudicated`,
`measured_error`. A triple with fewer judged outcomes than the evaluation
floor requires stays `uncalibrated` — the fit is refused, not silently
accepted at a lower quality bar. Run the same fit on demand, rather than
waiting for the next scheduled tick, with:

```bash
curl -X POST "<registry-base-url>/v1/admin/memory-calibration:refit" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"provider_id": "<provider>", "model_id": "<model>", "strategy_id": "<strategy>"}'
```

This calls the exact same `load_observations → fit → publish` sequence the
scheduled worker does — an on-demand refit and the periodic sweep can never
compute a fit two different ways. Only extraction-derived claims (ones
carrying a `strategy_id`) feed a calibration fit; a human confirmation has no
strategy of its own and is not itself calibration input, even when adjudicated.

---

## Source governance for connectors

A connector may not write a single claim until an admin has declared its
authority tier and a write ceiling. Registering the sync source itself is
covered in the [sync connectors guide](../04-guides/03-sync-connectors.md);
this section is the *claims* half of that source's configuration.

```bash
curl -X POST "<registry-base-url>/v1/admin/memory-sources" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "source_id": "<sync-source-id>",
    "authority_tier": "observer_extraction",
    "ingest_ceiling": 1000,
    "window_seconds": 3600
  }'
```

`authority_tier` is one of `owner_human`, `owner_inference`,
`owner_extraction`, `observer_human`, `observer_inference`,
`observer_extraction`, `unattributed` — declaring it does not change what
tier a claim actually carries (that is derived from the evidence each claim
cites, which a connector cannot forge); declaring only gates whether the
source may write at all.

`ingest_ceiling` per `window_seconds` is a circuit breaker, not a rate
shaper: crossing it opens the breaker for the *whole batch* rather than
admitting a partial one — a connector cut off mid-document would leave the
store holding an arbitrary prefix of it, which is harder for a curator to
reason about than not having ingested it. Check and clear a tripped breaker:

```bash
curl "<registry-base-url>/v1/admin/memory-sources" \
  -H "Authorization: Bearer $TOKEN"
# look for a non-null "breaker_open_until"

curl -X POST "<registry-base-url>/v1/admin/memory-sources/<source-id>:reset-breaker" \
  -H "Authorization: Bearer $TOKEN"
```

`may_provision_entities` (off by default, set via the same declare call or a
`PATCH`) is its own blast-radius decision: when on, a connector-declared
subject that resolves to nothing gets a **new entity created for it**,
through the catalog's own audited write path, before the claim links to it.
When off (the default), an unresolved subject just lands `unlinked` in the
curation queue — a human decides whether it's a new entity or a typo. Turn
this on only for a source you trust to name real, new things; it is the one
place claim ingestion can grow the catalog itself rather than merely
describing what's already in it.

---

## Metrics reference

Every counter below is exposed at `/metrics` (gated by `METRICS_BEARER_TOKEN`
— see [Configuration reference → Metrics exposition](../05-reference/03-configuration.md)).

| Metric | Kind | Labels | Meaning |
|---|---|---|---|
| `registry_claim_promotion_proposed_total` | counter | — | Proposals created from an eligible, consolidated claim |
| `registry_claim_promotion_accepted_total` | counter | `auto_promoted` | Proposals accepted and written to the canonical graph — split by whether a person reviewed it or the sweep auto-accepted it under an allowlisted guardrail |
| `registry_claim_promotion_rejected_total` | counter | — | Proposals refused by the tenant that owns the subject |
| `registry_claim_promotion_reversed_total` | counter | — | Promotions undone, restoring what the canonical graph said before them |
| `registry_promotion_sweep_total` | counter | `outcome` (`auto_promoted`, `awaiting_review`, `not_eligible`, `failed`) | Every claim the sweep considered on its most recent tick, by what happened to it |
| `registry_promotion_sweep_pending` | gauge | — | Consolidated, subject-resolved staged claims never yet proposed — a persistently nonzero value here means the sweep is falling behind claim volume |
| `registry_claim_consolidation_swept_total` | counter | `outcome` | Claims the consolidation sweep reconciled, by outcome |
| `registry_claim_consolidation_pending` | gauge | — | Live claims never reconciled, or reconciled before something newer arrived |
| `registry_claim_contest_detected_total` | counter | `predicate` | Disagreements detected between claims |
| `registry_claim_confirmed_total` | counter | `authority` | Claims a human confirmed, by the authority tier the confirmation carries |
| `registry_claim_adjudicated_total` | counter | `verdict` | Claims judged, by verdict |
| `registry_source_ingest_admitted_total` | counter | `tenant_id`, `source_id` | Claims a connector was permitted to write |
| `registry_source_ingest_breach_total` | counter | `tenant_id`, `source_id` | Ingest-ceiling breaches — the circuit opened and claims were refused rather than the store absorbing them |

Read `curation_queue_backlog` and `oldest_open_proposal_age_seconds` from
`/v1/admin/operational-health` instead of `/metrics` — those two are
cluster-scoped database counts, not per-process counters, and answer "is the
loop backing up right now" more directly than any counter above can (see
[Is the curation backlog growing?](#is-the-curation-backlog-growing)).

---

## Normal operating procedure

- Check `curation_queue_backlog` and `oldest_open_proposal_age_seconds`
  (`GET /v1/admin/operational-health`) on the same cadence you check any
  other queue depth.
- Work the queue oldest-first — it is returned that way on purpose, so an
  item nobody ever reaches is a sign it should not have been queued at all,
  not a sign the ordering is wrong.
- Review an allowlist change like the review-policy change it is: one
  predicate, one tenant, one reason, and confirm the new state in the
  response rather than assuming the call succeeded.
- When you reverse a promotion, capture the `promotion_id` at accept time (or
  from the audit log) before you need it — there is no listing endpoint for
  a claim's promotion history today.
- Prefer `escalate` over `discard` for a contested claim you can't resolve
  yourself; discarding throws away one side of a disagreement a human might
  actually be able to adjudicate.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `link` returns `422` "still does not resolve to any entity" for a name you know exists (e.g. `salt-design-system`) | Subject resolution for claims accepts only an **entity id** or a `system:external-id` pair — never a slug name, unlike most of the rest of this API | Resolve the slug to its `entity_id` first (`GET /v1/capabilities/<name>`), then link using the id |
| `PATCH` on a proposal returns `409` "proposal is already accepted/rejected" | The proposal was already decided — proposals do not support re-deciding | Nothing to fix; `GET` the proposal to see its current state and (if accepted) its promotion |
| `:reverse` returns `409` "the canonical row this promotion created is no longer live" | A later promotion has already superseded the one you're reversing | Reverse the later promotion first, then retry this one |
| `assert_claim` / `POST /v1/memory/claims` returns `422` `containment_refused` | The value or an evidence excerpt reads as an instruction rather than a description — refused before it is ever staged | Rephrase as an observation ("the team said X"), not a directive; this is not a false positive to work around |
| `assert_claim` returns `422` `pii_blocked` | The value or an excerpt matched a **block**-level PII policy | Only fires when the tenant's PII policy for this field resolves to `block`; the default tenant policy is `advisory`, which logs the match (`pii_detection_log`) and proceeds without surfacing anything in the response — configure the policy via the [PII policies guide](../04-guides/04-pii-policies.md) if you want this route to refuse instead |
| `confirm` returns `403` | The calling principal is not a human actor — a service or worker credential cannot confirm | Confirm as a human-authenticated caller |
| A predicate stays out of the review queue even though it's allowlisted | It is high-impact, not owner-originated, or not yet eligible (unconsolidated, contested, below floor, already promoted) — the allowlist is only one of four required conditions | Check `list_promotion_proposals` for `high_impact_reasons`; that list explains which gate is blocking auto-promotion |

---

## Walk the seeded demo

`make dev-seed` loads `seeds/09-memory-loop/`, which stages four claims about
one demo capability (`memory-loop-demo`) through the real services — not by
writing rows directly — so everything below is exactly what a curator would
see and do by hand: one claim already linked and proposed, one unlinked, a
contested pair, one open capability request, and `runbook_url` pre-allowlisted
for auto-promotion.

```bash
make dev-up
make dev-token
export TOKEN=$(make dev-jwt)
make dev-seed
```

Check the backlog:

```bash
curl "<registry-base-url>/v1/memory/curation-queue?counts=true" \
  -H "Authorization: Bearer $TOKEN"
# {"counts":{"contested":2,"unlinked":1}}

curl "<registry-base-url>/v1/admin/operational-health" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# curation_queue_backlog: 3.0
# oldest_open_proposal_age_seconds: (age of the seeded proposal — nonzero)
```

List the queue to get real ids, then link the unlinked claim to a real
capability (using its entity id, per the gotcha above) and confirm the
backlog drops:

```bash
curl "<registry-base-url>/v1/memory/curation-queue" -H "Authorization: Bearer $TOKEN"
# the "unlinked" item's subject_reference is a name that resolves to nothing;
# link it to a real entity:

curl -X POST "<registry-base-url>/v1/memory/claims/<claim-id>:link" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"subject_reference": "<some-other-entitys-uuid>"}'
# 200, status "staged"

curl "<registry-base-url>/v1/memory/curation-queue?counts=true" -H "Authorization: Bearer $TOKEN"
# {"counts":{"contested":2}}
```

List the one open proposal and accept it — it proposes `owned_by_team:
platform-team` on `memory-loop-demo`, which has no current value:

```bash
curl "<registry-base-url>/v1/memory/promotion-proposals?state=open" -H "Authorization: Bearer $TOKEN"
# one item, current_value: null, proposed_value: "platform-team"

curl -X PATCH "<registry-base-url>/v1/memory/promotion-proposals/<proposal-id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "accepted"}'
# 200, "promotion_id": "<promotion-id>"

curl "<registry-base-url>/v1/capabilities/memory-loop-demo" -H "Authorization: Bearer $TOKEN"
# attributes now include "owned_by_team": "platform-team"
```

Reverse it, and confirm the attribute is gone again — restored, not merely
overwritten:

```bash
curl -X POST "<registry-base-url>/v1/memory/promotions/<promotion-id>:reverse" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "demonstrating the reversal path"}'
# {"status":"reversed"}

curl "<registry-base-url>/v1/capabilities/memory-loop-demo" -H "Authorization: Bearer $TOKEN"
# attributes no longer include "owned_by_team" -- back to exactly what they
# were before the promotion, because there was nothing there to begin with
```

`oldest_open_proposal_age_seconds` reads `0` afterward — there is no
open proposal left. Trying the same `PATCH` again returns `409 conflict`
("proposal is already accepted"), which is the expected, safe refusal to
re-decide something already settled.

Confirm one side of the contested pair, and watch the backlog narrow from
two contested claims to one — the confirmed side is superseded and drops out
of the backlog, its counterpart is still there for a curator to resolve:

```bash
curl -X POST "<registry-base-url>/v1/memory/claims/<contested-claim-id>:confirm" \
  -H "Authorization: Bearer $TOKEN"

curl "<registry-base-url>/v1/memory/curation-queue?counts=true" -H "Authorization: Bearer $TOKEN"
# {"counts":{"contested":1}}
```

Finally, the seeded capability request against `memory-loop-demo`'s owner:

```bash
curl "<registry-base-url>/v1/memory/capability-requests?role=owner" \
  -H "Authorization: Bearer $TOKEN"
# one request, status "raised"
```

---

## Where this fits

- [Operations Runbook](01-ops.md) — the rest of the operator surface
  (backups, migrations, webhook rotation).
- [Concepts → Claim](../01-overview/03-vocabulary.md#claim) — what a claim,
  a curation queue, a promotion, and a capability request are.
- [MCP tools reference → Memory curation](../05-reference/02-mcp-tools.md#memory-curation) —
  the full field-level schema for every route and MCP tool on this page.
- [Sync connectors guide](../04-guides/03-sync-connectors.md) — registering
  the sync source a memory source policy governs.
- [PII policies guide](../04-guides/04-pii-policies.md) — configuring the
  policy that decides whether a directly-asserted claim's PII match blocks
  or merely logs.
- [Configuration reference](../05-reference/03-configuration.md) — every
  sweep's interval setting and the extraction provider that feeds
  calibration.
- [Architecture reference → Background workers](../05-reference/04-architecture.md#background-workers) —
  where `consolidation_sweep`, `promotion_sweep`, and `calibration_refit`
  run relative to the rest of the process.
