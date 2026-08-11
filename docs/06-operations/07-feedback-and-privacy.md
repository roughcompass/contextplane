<!--
  title: Feedback and privacy runbook
  audience: operator
  archetype: how-to (operator procedures)
  summary: Registering signal sources, reading floored aggregates, honouring erasure, and the configuration each of those needs.
-->

# Feedback and privacy runbook

Operator procedures for the surfaces that receive reported outcomes and serve
what is derived from them. For the model behind these procedures see
[Feedback and learning](../01-overview/12-feedback-and-learning.md); for the
curation pipeline the evidence feeds, see the
[memory-curation runbook](05-memory-curation.md).

---

## Register a source before it writes

An undeclared source is refused outright. Declaring one records what its claims
are worth and how much it may write:

```http
POST /v1/admin/memory-sources
{
  "source_id": "<uuid of an existing sync source>",
  "authority_tier": "observer_extraction",
  "ingest_ceiling": 1000,
  "window_seconds": 3600
}
```

The `source_id` must already exist as a sync source owned by your tenant. A
source owned by another tenant answers `404` — the same answer an unknown id
gets, deliberately, so a source id cannot be used to test whether another
tenant has one.

**Authority tier is not a formality.** It travels with every observation the
source writes and decides how much weight the curation pipeline gives what is
derived from it. A connector declared `owner_human` is asserting that its
reports carry the same authority as the owner saying it in person.

### When a source misbehaves

A source over its declared ceiling answers `429`, not `403` — nothing about the
submission is wrong and the same bytes will be accepted once the window rolls.
Repeated breaches trip a breaker; `POST /v1/admin/memory-sources/{id}:reset-breaker`
closes it again after you have dealt with the cause.

Raising a ceiling to stop a `429` is occasionally right and usually wrong. The
ceiling is what stops a looping connector from filling the ledger with one
event reported ten thousand times.

## Read the aggregates

```http
GET /v1/learning/metrics      # the closed metric set this deployment serves
GET /v1/learning/aggregates?window_days=30
```

Both are admin-only. Seven metrics are served: `context_quality`, `reuse`,
`handoff_success`, `adequacy` on the feedback side, and `claim_aging`,
`contradiction_backlog`, `promotion_yield` on the learning side.

Every cell is floored before it is served. A cell that does not clear **five
distinct actors** and **five events** is suppressed and carries no value; the
floors in force are returned alongside the numbers so a suppressed cell is
legible rather than mysterious.

The whole metric set is served over one window rather than one metric per
request, so a caller cannot repeatedly ask for the one metric whose cells are
thin and bracket a suppressed value across windows.

A total that had cells withheld from it is labelled **partial**, so a number is
never quietly smaller than the thing it claims to count. A partial total is a
real answer about the rows that cleared the floor, not an estimate of the rows
that did not.

**Do not treat this as a personnel surface.** It reports cohorts. If somebody
asks you to break a cell down until it identifies a person, the request is
asking you to defeat the control, whatever the intent behind it.

## Honour an erasure request

```http
DELETE /v1/admin/actors/{actor_id}/personal-data
```

Returns `200` with per-subsystem counts rather than `204`, so you can confirm
what was actually reached. The operation is idempotent — a second call returns
zeros, which is what makes a timed-out request safe to retry.

Erasure propagates to what was derived from the erased records, not only the
records themselves: staged claims, evidence links, receipts, checkpoints, and
the derivative records that point at any of them.

### What each record class is held for, and what erasure does to it

Twelve record classes carry a disposition. "Retention" is how long the record
lives; "payload" is the earlier clock on the part of it that is content rather
than structure, and the two are separate because an envelope can stay useful
long after what it carried should be gone.

| Record class | Legal basis | Retention | Payload | On erasure |
|---|---|---|---|---|
| `task_checkpoint` | contract performance | — | — | minimize + tombstone: body fields cleared, id/sequence/predecessor/digest kept |
| `context_receipt` | legitimate interest (verification) | 730d | — | minimize + tombstone: items and exclusions minimized, envelope kept |
| `receipt_item` | legitimate interest (verification) | — | — | minimize: item key replaced with a tenant-keyed erased marker |
| `receipt_exclusion` | legitimate interest (verification) | — | — | minimize: item key replaced, block and withholding reason kept |
| `external_signal` | legitimate interest | 730d | 180d | delete + tombstone, so dependents can be invalidated by cause |
| `context_feedback` | contract performance | 730d | 365d | minimize: free-text note cleared; discriminant, rating and linkage survive |
| `memory_claim` | legitimate interest | — | 180d | minimize: excerpts minimized, claim invalidated, shell kept for audit and served nowhere |
| `derivative` | inherited from every source | — | — | redact where the kind supports it, delete where it does not |
| `audit_log` | legitimate interest (accountability) | 1095d | — | **exempt** |
| `pii_detection_log` | legitimate interest | 730d | — | **exempt** |
| `export` | contract performance | 30d | — | delete |
| `workspace_entry` | contract performance | — | — | delete + tombstone, so the deletion is accountable |

The two exemptions are the ones to be able to explain: an accountability log
that an erasure could edit would stop being able to evidence the erasure.

### What a verifier may learn after an erasure

A tombstone says that a record existed, that it occupied a position, and that
it was erased on a date under a policy version. It has to say that much — the
chain is append-only and successors point at the erased revision, so the hole
is detectable anyway, and claiming non-existence would be dishonest rather than
private.

What a verifier gets is **structural integrity plus HMAC-keyed tombstone
metadata, and nothing else**. The proof is keyed to a per-tenant secret rather
than being a bare content digest, because erased content is routinely guessable
— a task goal, a source system's name, an item key naming a document. A bare
hash lets anyone who can guess the content confirm the guess, and equal digests
would reveal that two erased records were equal. The raw content digest stays
internal to chain verification and never appears on a tombstone or in a
disclosure.

### Erasure needs a retention key configured

Erasure mints a keyed tombstone so that "this was erased" survives without
retaining what was erased. **The shipped default configures no key, and an
erasure that cannot key its proof fails loudly rather than falling back to an
unkeyed one.** Configure both:

```sh
RETENTION_KEYS=<key-id>:<hex-material>[,<older-key-id>:<hex-material>]
RETENTION_ACTIVE_KEY_ID=<key-id>
```

Keep retired keys listed after the active one. A tombstone minted under a key
you have dropped can no longer be verified, and the verification is the whole
value of having minted it.

### Tenant offboarding destroys the salt, and that is itself an erasure

A tenant's salt is derived from operator root key material rather than stored
per tenant, so offboarding a tenant means naming it destroyed — one action,
nothing to hunt down and shred.

Once it is gone, **no proof for that tenant can be derived by anybody,
including this system.** The tombstones stay readable as structure — the record
existed, at this position, and was erased — while their keyed metadata stops
being derivable, and disclosure says so explicitly rather than emitting a value
it can no longer stand behind.

Treat this as irreversible and sequence it accordingly. Anything you will need
to evidence about that tenant must be evidenced before the salt goes, because
afterwards the answer is permanently "this cannot be derived" rather than "this
did not happen".

### Legal holds, and the report that keeps them visible

An actor's records under a legal hold are partitioned out of an erasure rather
than silently deleted or silently kept. A deployment with no hold storage
configured answers "nothing is held" truthfully and refuses to place or renew a
hold loudly, rather than accepting one it cannot honour.

**A hold is an exception to a deletion policy, so it has to be visible.** The
held-overdue report lists records a hold is keeping past the retention their
class would otherwise have applied. Read it on a schedule: a hold that outlives
its reason turns into indefinite retention that nobody chose, and the report is
the only place that becomes apparent. Renewing a hold requires re-justifying
it, for the same reason.

## What to check after an incident

| Question | Where to look |
|---|---|
| Did a connector loop? | Ceiling breaches and breaker state on the source's governance record. |
| Did an erasure complete? | The per-subsystem counts in the `DELETE` response; a subsystem absent from the response was not reached. |
| Is a disagreement unresolved? | The `contradiction_backlog` metric, and the curation queue behind it. |
| Did retries create duplicates? | They should not — one idempotency key is one row. A count above one for a single key is worth reporting. |
