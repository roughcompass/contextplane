<!--
  title: Data governance and PII
  audience: evaluator, integrator, operator, auditor
  archetype: explanation (mental model)
  summary: How tenant isolation, write-time PII controls, audit history, retention, and erasure apply across registry data surfaces.
-->

# Data governance and PII

Context Plane combines several controls. Tenant isolation decides who may see a
row. Personally identifiable information (PII) scanning checks selected text
before specific writes. Bi-temporal storage preserves state history. Audit logs
record governed actions. Retention and erasure control how long personal
content remains.

No single control substitutes for the others. A PII scan does not encrypt data.
An audit record does not authorize a read. A soft delete does not satisfy a
physical-erasure request.

This page explains the boundaries. Operators configuring patterns and policies
should use [Configure PII scanning policies](../04-guides/04-pii-policies.md).

---

## Start with the data surface

Context Plane stores several kinds of content with different ownership and
retention models.

| Surface | Typical content | Primary scope | History behavior |
|---|---|---|---|
| Canonical catalog | Entities, attributes, facts, interfaces, and edges | Owning tenant plus explicit visibility | Mutable rows use valid and transaction time |
| Living Memory claims | Typed observations with citations and confidence | Claim and subject visibility | Claims are immutable; supersession creates history |
| Session events | Conversation turns and tool summaries | Calling actor only | Ordered, retained for a tenant-defined period |
| Workspaces | Notes, decisions, open questions, and saved queries | Owning actor or tenant | Entries can expire; workspace deletion is normally soft |
| ARC receipts | Selected governance context and resolution evidence | ARC audience rules | Immutable receipt with append-only lifecycle events |
| Audit log | Who changed what and when | Tenant auditor or deployment governance scope | Monthly partitions with operator-managed archival |

Choose the surface before choosing a policy. A team decision belongs in a
workspace. An observation about a capability belongs in a claim. Approved
capability state belongs in the canonical graph.

## Tenant isolation is the first boundary

Every business-data read carries tenant context. Canonical entities use the
service-layer visibility chokepoint. Personal sessions add actor scoping because
same-tenant access would expose a colleague's conversation. Workspaces enforce
actor or tenant ownership. Claims check both their own visibility and the
visibility of their subject.

Invisible resources return not found rather than forbidden when distinguishing
the responses would reveal that the resource exists.

Cross-tenant observations do not grant cross-tenant write authority. A claim
about another tenant's visible capability can route a proposal to the owner,
but only the owner decides whether it becomes canonical.

## PII scanning is a write-time control

The current runtime matches its built-in detectors against text. Each match
resolves to one policy:

| Policy | Write behavior | Caller behavior |
|---|---|---|
| `advisory` | Write proceeds and the detection is logged | No warning is required in the response |
| `warn` | Write proceeds and the detection is logged | Surfaces that support warnings return matched categories |
| `block` | Write is refused before the target row is stored | The caller removes or sanitizes the text before retrying |

Policy resolution prefers a field-and-pattern override, then a field-wide
override, and then a pattern override. The current runtime falls back to
`advisory` when no override applies.

A detection-log write is best effort. Failure to write the log does not turn a
PII refusal into a server error. Operators must monitor logging separately if
the detection record is a compliance requirement.

## The scanner covers named fields, not all text

The field type is part of policy resolution. Configure the exact field used by
the write path.

| Write path | Scanned text | Field type |
|---|---|---|
| Workspace entry create or update | `body_md` | `workspace_entry.body` |
| Workspace entry create or update | `references_jsonb` rendered as text | `workspace_entry.references` |
| REST session-event append | Event body | `memory_session_event.body` |
| Direct REST or MCP claim assertion | String claim value and evidence excerpts | `claim_value` |
| Session extraction | Generated string value and excerpt | `claim_value` |
| Artifact write | Artifact body | `artifact.body` |
| Task checkpoint append (REST or MCP) | Every client-supplied content field, serialized canonically | `intent_checkpoint.body` |
| Task checkpoint append (REST or MCP) | The evidence array rendered as text, including `authorized_uri`, which the checkpoint digest omits | `intent_checkpoint.references` |
| External signal ingest | The observation, in whichever form it arrived | `external_signal.payload` |
| External signal ingest | The normalized references rendered as text | `external_signal.references` |
| Claim promotion | String value crossing into a canonical attribute or edge | `memory_claim.<predicate>`; the current application composition supplies the built-in advisory scanner |

Structured identifiers, workspace `reference_ids`, and session-event metadata
are not scanned. The generic fact and attribute write paths do not imply PII
scanning merely because they accept data. Clients must not assume that every
free-text field in Context Plane passes this control.

> **Warning:** Do not put sensitive content in session metadata on either
> transport. Metadata is indexed and filterable, but it is not scanned,
> redacted, or encrypted. Event *bodies* are scanned on both transports: the
> MCP tool runs the same admission the REST route does, so a tenant policy of
> `block` refuses the write whichever surface made it.

## Detection is not redaction or encryption

The scanner reports or refuses pattern matches. It does not rewrite text,
redact responses, classify arbitrary documents, or retrospectively scan stored
rows.

Pattern matching can produce false positives and false negatives. Use field
policies to narrow enforcement, test changes in a staging tenant, and inspect
matched categories before moving from warn to block.

> **Current custom-pattern limitation:** The admin API stores and validates
> tenant-defined regex pattern rows, but the write-time runtime builds its
> scanner from built-in detector modules only. A custom regex row does not
> generate matches on current write paths. Do not cite a registered custom
> pattern as an enforced control until the runtime loads it.

Workspace bodies are stored as plaintext in the current implementation. A
regulated tenant at encryption tier `none` cannot create workspaces. This guard
prevents a caller from mistaking PII scanning for encrypted storage. Use
external storage controls and deployment encryption for requirements that
exceed the application-level feature set.

## Bi-temporal history answers two questions

Canonical attributes, facts, and edges record:

- **Valid time**, which says when the value was true in the world.
- **Transaction time**, which says when Context Plane recorded or invalidated it.

An `as_of` read selects a valid-time slice. Transaction history preserves when
Context Plane learned or retired the row. This supports historical
reconstruction without overwriting current state.

Claims keep their own immutable history. Consolidation closes a superseded
claim and points to the survivor. Promotion journals the canonical row it
created and the row it closed. Reversal restores the earlier canonical interval.

## Retention, deletion, and erasure differ

| Action | Effect |
|---|---|
| Soft delete | Removes a row from default reads while preserving history |
| Expiry | Automatically invalidates content after its retention timestamp |
| Archive | Hides a workspace from normal listings without deleting it |
| Erasure | Physically deletes actor-scoped personal data through the governed erasure path |
| Audit archival | Detaches old audit partitions for operator-managed storage |

Session events receive an expiry timestamp from the tenant's memory-retention
setting. The expiry worker soft-invalidates expired events. An actor can also
remove one event from replay without physically erasing it.

A right-to-be-forgotten operation physically removes the target actor's session
events and actor-owned workspace data. Claims need additional care because
provenance can mix personal session evidence with non-personal connector,
document, commit, or curator evidence. The erasure service decides whether a
claim can be removed or must retain non-personal evidence without preserving
the actor's session provenance.

Use the [operations runbook](../06-operations/01-ops.md) for the current erasure
procedure and receipt fields.

## Auditability does not mean unlimited retention

Governed writes record actor, tenant, action, target, timestamp, and relevant
before or after state. ARC uses a transactional outbox before its events reach
the shared audit log. Promotion, reversal, PII policy changes, progression
overrides, and erasure actions each have their own audit semantics.

Audit partitions can leave the live database under the configured archival
policy. Historical data that must remain queryable needs an archive and restore
procedure. Legal holds and retained ARC receipts can also block destructive
schema downgrade.

## Data governance has explicit limits

Context Plane is not a general data-loss-prevention product. It does not scan
reads, inspect every field, execute tenant-defined regex detectors, or encrypt
every stored body. It also does not decide an organization's legal basis,
retention schedule, or access-review policy.

Context Plane provides enforceable boundaries and evidence. Operators still own
policy selection, key management outside the application, audit export, and
incident response.

## Read next

- [Configure PII scanning policies](../04-guides/04-pii-policies.md)
- [Compliance and audit use case](../03-use-cases/07-compliance-and-audit.md)
- [Living Memory and claims](07-living-memory.md)
- [Authentication](04-authentication.md)
- [Authorization](05-authorization.md)
- [Operations runbook](../06-operations/01-ops.md)
