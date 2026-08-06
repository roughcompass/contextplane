# How to configure PII scanning policies

<!--
  title: Configure PII scanning policies
  audience: operator
  status: current
-->

The personally identifiable information (PII) scanner runs at write time on
selected text fields. Its `advisory`, `warn`, or `block` behavior is
configurable per tenant through two endpoint families: one for pattern
registration and one for per-field policy overrides. This guide covers setting
up and tuning those policies. Read [Data governance and PII](../01-overview/09-data-governance.md)
first for the control's scope and limitations.

**Preconditions:**

- A bearer token with the `admin` role for the tenant you are configuring. In local dev, run `make dev-jwt` to mint a short-lived token.
- Understanding of which data categories your deployment must protect (e.g., `CONTACT`, `FINANCIAL`, `GOVERNMENT_ID`).

**What this guide covers:**

- [How the scanner runs](#how-the-scanner-runs)
- [View current patterns and policies](#view-current-patterns-and-policies)
- [Set a block policy](#set-a-block-policy)
- [Set a warn policy](#set-a-warn-policy)
- [Understand PII categories](#understand-pii-categories)
- [Understand the custom-pattern limitation](#understand-the-custom-pattern-limitation)
- [Test a policy without writing data permanently](#test-a-policy-without-writing-data-permanently)

---

## How the scanner runs

The scanner runs on these current write fields:

| Write path | Field type |
|---|---|
| Workspace entry `body_md` | `workspace_entry.body` |
| Workspace entry `references_jsonb` rendered as text | `workspace_entry.references` |
| REST session-event body | `memory_session_event.body` |
| Direct or extracted claim string value and evidence excerpt | `claim_value` |
| Artifact body | `artifact.body` |
| String claim value crossing the promotion boundary | `memory_claim.<predicate>` |

The scanner does not run on reads or every free-text field. It does not scan
structured identifiers, workspace `reference_ids`, session metadata, generic
fact writes, or generic attribute writes. The MCP `record_session_event` tool
does not run the REST session-event adapter's scan.

> **Warning:** Never put sensitive content in session metadata. The registry
> indexes metadata but does not scan, redact, or encrypt it. MCP callers must
> also treat the event body as unscanned until the MCP adapter gains the REST
> route's PII control.

Three policy levels control what happens when a pattern matches:

| Policy | Result | Effect |
|---|---|---|
| `block` | Request fails | The target write is rejected. REST adapters commonly return HTTP 422; MCP direct claims return a structured `ToolError`. |
| `warn` | Request succeeds | The write proceeds. Adapters that support warnings include matched categories in the response. |
| `advisory` | Request succeeds | The write proceeds. The match is recorded internally without a required caller warning. |

The effective policy for a given `(field_type, pattern)` pair is resolved in this order:

1. A field-and-pattern policy override for the exact pair.
2. A field-wide policy override whose `pattern_id` is null.
3. The `policy_override` on the pattern itself.
4. The runtime default, `advisory`.

The scanner resolves each matched pattern separately. It then uses the most
restrictive resolved action across the matches in that scan.

---

## View current patterns and policies

**List all patterns** — both built-in (seeded at tenant creation) and any custom patterns your team has added:

```bash
curl -s https://api.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" | jq '.[] | {pattern_id, name, category, is_system, policy_override, is_enabled}'
```

Built-in patterns have `is_system: true`. They cannot be changed or deleted
through the API. Any PATCH or DELETE request against a system row returns HTTP
403. Use a field policy that names the built-in pattern ID to change how that
pattern acts on a field.

**List all per-field policy overrides:**

```bash
curl -s https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" | jq .
```

---

## Set a block policy

A block policy rejects writes when a matching pattern fires on the target field. Use it for categories where no free-text occurrence is acceptable in the database.

Block one built-in pattern on one field type:

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "field_type": "workspace_entry.body",
    "pattern_id": "<built-in-ssn-pattern-id>",
    "policy": "block"
  }' | jq .
```

When a block fires, the write endpoint returns:

```json
{
  "detail": {
    "code": "pii_detected",
    "field": "workspace_entry.body",
    "categories": ["GOVERNMENT_ID"]
  }
}
```

The write is not persisted. The caller must sanitize the input before retrying.

---

## Set a warn policy

A warn policy allows the write to proceed but signals to the caller that PII was detected. Use it when you want visibility into PII without hard-blocking writes — useful during a rollout period before enforcing `block`.

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "field_type": "workspace_entry.references",
    "policy": "warn"
  }' | jq .
```

Omitting `pattern_id` creates a catch-all override for that field type — any pattern match on `workspace_entry.references` will warn.

When a warn fires, the response body includes:

```json
{
  "entry_id": "...",
  "warnings": [
    {
      "field": "references_jsonb",
      "categories": ["CONTACT"]
    }
  ]
}
```

The write is committed. Callers that do not inspect `warnings[]` will not notice — make sure your client handles this field.

---

## Understand PII categories

Built-in patterns and their categories:

| Pattern name | Category | What it detects |
|---|---|---|
| `email` | `CONTACT` | RFC 5322-lite e-mail addresses |
| `phone` | `CONTACT` | Common North American and international phone number formats |
| `ssn` | `GOVERNMENT_ID` | US Social Security Numbers with separator and validity checks |
| `credit_card` | `FINANCIAL` | Visa, Mastercard, Amex, Discover card numbers (Luhn-checked) |
| `aws_access_key` | `CREDENTIALS` | AWS access key IDs (`AKIA...`) |
| `aws_secret_key` | `CREDENTIALS` | High-entropy AWS secret access key candidates |
| `jwt_token` | `CREDENTIALS` | JSON Web Tokens (`eyJ...`) |

To determine which category fired from a workspace response, inspect
`warnings[].categories` for a warn policy or `detail.categories` for a block
policy.

---

## Understand the custom-pattern limitation

The admin API accepts and validates tenant-owned regex pattern rows. The current
write-time runtime does not instantiate those regexes. It builds the active
scanner from the seven built-in detector modules listed above.

> **Do not rely on a tenant-defined regex for enforcement.** Registering an
> enabled custom pattern and assigning `policy_override: "block"` stores the
> configuration, but the regex does not generate matches on current write
> paths.

The API contract for registering pattern metadata is:

```bash
curl -s -X POST https://api.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "name": "acme_employee_id",
    "category": "EMPLOYEE_ID",
    "regex": "\\bEMP-[0-9]{6}\\b",
    "policy_override": "block",
    "is_enabled": true
  }' | jq .
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique name within the tenant |
| `category` | yes | Category label stored with the row |
| `regex` | yes | Python-compatible regex; validated server-side (`422` on invalid pattern) |
| `policy_override` | no | Stored `advisory`, `warn`, or `block` policy metadata |
| `is_enabled` | no | Whether policy loading treats the row as enabled; defaults to `true` |

Custom patterns have `is_system: false` and can be updated with `PATCH /v1/admin/pii-patterns/{pattern_id}` or deleted with `DELETE /v1/admin/pii-patterns/{pattern_id}`.

**Disable a pattern** without deleting it:

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/admin/pii-patterns/<pattern_id>" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"is_enabled": false}' | jq .is_enabled
```

---

## Test a policy without writing data permanently

There is no dry-run endpoint. Write a test workspace entry in a staging tenant
with the same policy configuration. Inspect the response, and then delete the
entry.

```bash
# 1. Write a test workspace entry with known PII content on the staging tenant.
ENTRY_ID=$(curl -s -X POST \
  "https://staging.example.com/v1/workspaces/$WS_ID/entries" \
  -H "Authorization: Bearer <staging-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "body_md": "Contact the owner at test.user@example.com for escalations.",
    "kind": "note"
  }' | jq -r '.entry_id')

# 2. Inspect the response for pii_detected (block) or warnings[] (warn).
#    If the call returned 422, the body contains code: pii_detected.
#    If it returned 200, check for a warnings[] field.

# 3. Delete the entry so staging data stays clean.
curl -s -X DELETE \
  "https://staging.example.com/v1/workspaces/$WS_ID/entries/$ENTRY_ID" \
  -H "Authorization: Bearer <staging-token>"
```

If you need to test on production (e.g., to validate a newly added custom pattern), use a dedicated test workspace, and delete the entry immediately after confirming the response.

---

**See also:**

- [`overview/data-governance.md`](../01-overview/09-data-governance.md): field coverage, tenant boundaries, retention, erasure, and control limitations
- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — PII scanner concept, warn vs block behavior, `warnings[]` response field
- [`reference/api.md`](../05-reference/01-api.md) — PII pattern and field-policy admin endpoints; workspace endpoint error codes
