# Migrating from the Task domain to the Intent domain

The concept a workspace accumulates around is an **intent**, and every surface now
says so. What was `task` is `intent` across REST, MCP, JSON, profiles, telemetry,
audit events, the database, and Python imports.

**This is a hard cutover.** There is no compatibility window, no redirect, no
alias, and no dual-emitted metric. A client that sends `task_id` gets a `422`; a
caller that requests `/v1/tasks/{task_id}` gets a `404`. That is deliberate: an
alias layer would have to be removed later, and a dual-write period is a second
source of truth for as long as it lasts.

One exception, and it is not a compatibility shim: **V1 artifacts keep their V1
field names forever**, because their digests and signatures identify those exact
bytes. See [V1 verification, V2 authoring](#v1-verification-v2-authoring).

---

## The replacement map

Generated from the manifest that drove the cutover — one row per rule it applied,
not a list written from memory. That manifest and the engine that read it were
retired once the cutover completed and nothing was left to apply, so **this table
is now the record**. Anything below that disagrees with the code is a bug in this
document; the code is the authority.

### REST

| Before | After |
|---|---|
| `/v1/tasks/{task_id}` | `/v1/intents/{intent_id}` |

Every path under it moves with it, so `/v1/tasks/{task_id}/participants` becomes
`/v1/intents/{intent_id}/participants`, and the same for `/checkpoints` and
`/checkpoints/{checkpoint_id}`. `GET /v1/checkpoints/by-digest/{digest}` never
carried the word and is unchanged.

### MCP tools

| Before | After |
|---|---|
| `list_task_participants` | `list_intent_participants` |
| `grant_task_participation` | `grant_intent_participation` |
| `revoke_task_participation` | `revoke_intent_participation` |
| `append_task_checkpoint` | `append_intent_checkpoint` |
| `get_task_checkpoint` | `get_intent_checkpoint` |
| `get_task_checkpoint_by_digest` | `get_intent_checkpoint_by_digest` |

### JSON fields and request parameters

| Before | After |
|---|---|
| `task_id` | `intent_id` |
| `task_ids` | `intent_ids` |
| `ambiguous_task_ids` | `ambiguous_intent_ids` |

### Profile and ARC vocabulary

| Before | After |
|---|---|
| `task_kind` | `intent_kind` |
| `task_kinds` | `intent_kinds` |
| `task_summary` | `intent_summary` |
| `task_summary_template` | `intent_summary_template` |
| `lower_scope_task_kind` | `lower_scope_intent_kind` |
| `parse_task_kind` | `parse_intent_kind` |
| `AuthorityScope.TASK` | `AuthorityScope.INTENT` |
| `TaskManifest` | `IntentManifest` |
| `TaskKind` | `IntentKind` |
| scope literal `"task"` | scope literal `"intent"` |

### Telemetry

| Before | After |
|---|---|
| metric prefix `contextplane_task_` | `contextplane_intent_` |

Dashboards and alert rules matching the old prefix match nothing after the
cutover. There is no dual emission to bridge them — rewrite the queries.

### Audit and log events

| Before | After |
|---|---|
| `task.checkpoint.appended` | `intent.checkpoint.appended` |
| `task.head.summary_set` | `intent.head.summary_set` |

Reference-binding subject values move with them: a binding recorded against
`task_checkpoint` reads `intent_checkpoint`.

### Database

Renamed **in place** with `ALTER TABLE ... RENAME`, never dropped and recreated,
so every row, index and grant survives.

| Before | After |
|---|---|
| table `task_participant_grants` | `intent_participant_grants` |
| table `task_checkpoints` | `intent_checkpoints` |
| table `task_heads` | `intent_heads` |
| column `task_id` on each of those three | `intent_id` |
| column `context_receipts.task_id` | `intent_id` |
| function `task_checkpoints_are_immutable()` | `intent_checkpoints_are_immutable()` |
| column `arc_applicability_rules.task_kinds` | `intent_kinds` |
| column `arc_approved_exceptions.lower_scope_task_kind` | `lower_scope_intent_kind` |

Some index and constraint names are deliberately left spelled the old way where
the manifest declares it; a name is not a contract and renaming one costs a lock
for no benefit.

### Python imports

| Before | After |
|---|---|
| `contextplane.api.routers.task_memory` | `...routers.intent_memory` |
| `contextplane.api.mcp.tools.task_memory` | `...mcp.tools.intent_memory` |
| `contextplane.api.schemas.task_memory` | `...schemas.intent_memory` |
| `contextplane.workspaces.schemas.task_memory` | `...schemas.intent_memory` |

Class names move with them: `TaskCheckpoint`, `TaskCheckpointV1`,
`TaskCheckpointService`, `TaskParticipantGrant`, `TaskParticipantGrantV1`,
`TaskGrantService` and `TaskHead` all take an `Intent` prefix.

---

## Order of operations

The two migrations rename in place, so the window where the schema and the code
disagree is the window between them. Keep it short and keep writers out of it.

1. **Pause writers** to intent memory and to the ARC authoring surface. Reads may
   continue. Nothing here rewrites data, so a reader sees consistent rows
   throughout; a *writer* mid-migration is what fails, because it holds a
   statement naming a column that is being renamed under it.
2. **Apply `0048_intent_memory_nomenclature`** — the intent-memory tables,
   columns, trigger and function, plus the receipt column and the
   reference-binding subject values.
3. **Apply `0049_arc_intent_nomenclature`** — the ARC selector vocabulary:
   applicability-rule and approved-exception columns with their indexes and
   check constraints.
4. **Deploy the application** built from the same commit as those revisions. The
   code does not tolerate the old schema and is not meant to: a build that
   queried `task_id` against a renamed column would fail loudly, which is the
   correct outcome.
5. **Resume writers.**
6. **Regenerate clients** — see below.
7. **Run the negative probes** — see below.

Both revisions have real `downgrade()` implementations that reverse the renames in
the opposite order. Rollback is therefore possible, with one boundary.

### The rollback boundary

**Roll both revisions back together, or neither.** They are independent in what
they touch and coupled in what the application expects: a build that speaks the
Intent vocabulary needs both, and a build that speaks the Task vocabulary needs
neither. Downgrading only `0049` leaves a tree whose intent-memory surface is
renamed and whose ARC selector surface is not, which no released build matches.

Rolling back after writers have resumed is safe for data — the renames carry no
information — but any audit event, metric sample or receipt written in the
meantime carries the Intent spelling and will not be rewritten by a downgrade.
Treat those as a permanent record of the window, not as something to reconcile.

---

## Regenerating clients

The committed contract is regenerated, never hand-edited:

```sh
make openapi-export      # writes openapi.json from the live app
```

It is deterministic — running it twice produces byte-identical output — so a
diff after regeneration is a real change, not noise. Point your generator at the
regenerated `openapi.json`, then look for these in the diff:

- every `/v1/tasks/...` operation replaced by `/v1/intents/...`;
- `task_id` path parameters and body fields replaced by `intent_id`;
- the six renamed MCP tools, if your client binds the tool catalogue.

Search your own tree for the left-hand column of every table above. A client that
still sends `task_id` will be refused by schema validation rather than silently
ignored, which is the intended failure.

---

## V1 verification, V2 authoring

The product **authors V2 profiles** and **still verifies V1 artifacts**. So the V1
field names remain in active code on purpose, and they are not a migration
leftover:

- V1 profile schemas keep `task_kind`, `task_kinds`, `task_summary` and
  `task_summary_template`. Those names are part of what was signed; renaming them
  would redefine what V1 means and invalidate every existing digest.
- The V2 profiles use the Intent spellings.
- `canonical.py` holds `task_summary` and `intent_summary` in one optional-field
  set precisely because both versions are live at once.

**What this means for you.** If you verify an artifact published before the
cutover, expect V1 field names and verify against the V1 schema. If you author a
new artifact, use the Intent spellings. Do not "upgrade" a stored V1 artifact by
renaming its fields — that changes its bytes and breaks its signature.

The frozen V1 fixture vectors under `tests/fixtures/arc_authoring/*_v1/` are the
executable statement of this rule; regenerating them must leave those subtrees
byte-identical.

---

## Negative probes: proving the old surfaces are gone

Absence is what a cutover claims, and absence is what usually goes unchecked. Run
these against a deployed instance.

```sh
BASE=http://localhost:8000
TOKEN=... # a token your tenant accepts

# 1. The old collection is gone, not merely undocumented.
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/tasks/00000000-0000-0000-0000-000000000000/participants"
# expect 404

# 2. The new one is served.
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/intents/00000000-0000-0000-0000-000000000000/participants"
# expect 200 or 404-for-unknown-intent, never 404-for-unknown-route

# 3. The old field name is refused rather than ignored.
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"task_id":"00000000-0000-0000-0000-000000000000","goal":"probe"}' \
  "$BASE/v1/intents/00000000-0000-0000-0000-000000000000/checkpoints"
# expect 422 -- a 2xx here means an alias survived
```

Probe 3 is the one worth keeping. A `404` on the old path is easy to achieve by
accident; a `422` on the old *field* is what proves no alias is quietly accepting
it.

In the database:

```sql
-- expect zero rows: the old tables are gone, not emptied
SELECT tablename FROM pg_tables
 WHERE tablename IN ('task_participant_grants','task_checkpoints','task_heads');

-- expect zero rows: no Task-spelled column survives on the renamed tables
SELECT table_name, column_name FROM information_schema.columns
 WHERE column_name LIKE 'task\_%'
   AND table_name IN ('intent_participant_grants','intent_checkpoints',
                      'intent_heads','context_receipts','arc_applicability_rules',
                      'arc_approved_exceptions');
```

### The gate that keeps them gone

Probes you have to remember to run are probes that stop being run.
`tests/conformance/test_rest_mcp_parity.py` fails if the Task domain reappears on
any published surface — a route, a path parameter, a schema field, or an MCP tool
name. It reads the generated contract rather than the routers, because the
contract is what a client generates from.

It is scoped to the published surface on purpose. Internal identifiers are a
separate and larger question: compound spellings like `permitted_task_ids` and
`_authorized_task_ids` still exist in internal code — 137 occurrences across 40
files at the time of writing. **None is reachable by a caller**, which is why the
gate above draws its line where it does.

They were also invisible to the residue scanner used during the cutover, because
its probes matched on word boundaries and a compound *embeds* the token rather
than equalling it. Worth knowing if you go looking: a search for `task_id` will
not find `permitted_task_ids`, so match on the substring rather than the word.
