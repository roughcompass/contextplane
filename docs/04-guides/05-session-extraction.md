# Session extraction

Session extraction turns captured agent conversations into typed claims about your
capabilities. It is the registry's only use of an LLM, and it is entirely optional:
a deployment that never configures a provider captures and replays sessions
normally and produces no session-derived claims.

Extracted claims are **staged**. They are not served through any capability read
path, and nothing promotes them into the catalog. They exist to be scored,
corroborated, and — in a later release — reviewed by whoever owns the capability
they describe.

## Choosing a provider

`EXTRACTION_PROVIDER` selects one of three:

| Value | Needs | What it does |
|---|---|---|
| `noop` | nothing | The default. Extraction pauses. Nothing else changes. |
| `local` | nothing | Deterministic pattern rules. No API key, no network, no model. |
| `anthropic` | an API key | A real model. |

An unrecognized value stops the app at startup rather than falling back to `noop`.
That is deliberate: a deployment producing no claims because of a typo looks
exactly like one whose sessions contain nothing extractable, and you would go
looking in the wrong place.

### `local` — what it is and is not

`local` exists so that nothing downstream of extraction needs a credential to work
on. It is what `make dev-up` runs, and it drives the whole pipeline end to end:
an event lands, the outbox enqueues it, the provider extracts, the conformance gate
validates, and the write path stages a claim.

It is a small set of regular expressions. It recognizes a handful of phrasings —
"times out after 900 seconds", "owned by the platform team", "deployed in staging"
— and finds nothing in anything else. Real transcripts do not talk like that, so
**do not measure extraction quality against it**: a benchmark run on `local`
measures the regexes.

It identifies itself accordingly. Claims record `local-rules-v1` as the model, and
token usage is reported as estimated rather than measured.

The local dev stack stays on `local` even when an API key is present in the
environment. `make dev-up` behaves the same for every developer, and a stack that
starts spending money because a shell had a variable set is not that. Opt in
explicitly:

```bash
EXTRACTION_PROVIDER=anthropic CLAUDE_API_KEY=sk-ant-... make dev-up
```

### `anthropic`

Set `EXTRACTION_PROVIDER=anthropic` and supply `CLAUDE_API_KEY` (or
`ANTHROPIC_API_KEY` — both are accepted). `EXTRACTION_MODEL` selects the model and
`EXTRACTION_TIMEOUT_S` bounds a single call.

Selecting `anthropic` without a key fails at startup, and the error names the
key-free alternative. There is no silent fallback: a deployment that asked for a
model and got nothing would report healthy while producing nothing.

## Strategies

Three run independently and in parallel:

| Strategy | Subject of its claims | Looks for |
|---|---|---|
| `capability_observation` | a capability | assertions that hold outside the conversation — dependencies, ownership, interface promises, operational behaviour, decisions |
| `actor_preference` | the actor | standing ways of working, not one-off remarks |
| `session_summary` | the session | a prose narrative — the one category permitted a prose value |

Each strategy owns its prompt, its output schema, the predicates it may emit, and
the namespace its output lands in.

## Per-tenant configuration

`GET /v1/admin/extraction-strategies` returns every strategy as it will actually
run for your tenant, including disabled ones. A strategy you cannot see is
indistinguishable from one that does not exist in the build.

`PATCH /v1/admin/extraction-strategies/{strategy_id}` changes:

- `is_enabled` — whether the strategy runs at all
- `confidence_floor` — below this, a candidate is not staged
- `prompt_override` — replaces the shipped prompt entirely
- `model_override` — the model this strategy requests

Omitting a field leaves it unchanged. Because `null` already means "leave alone",
removing an override needs its own flag:

```bash
curl -X PATCH .../v1/admin/extraction-strategies/capability_observation \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clear_prompt_override": true}'
```

### What an override cannot change

An override changes **how well** claims are found. It never changes **what they
are allowed to mean**. These are not editable and are not exposed on the admin
surface at all:

- the **output schema** — model output is schema-constrained so that free-form text
  is refused rather than parsed
- the **permitted predicate set** — a tenant that could widen its own set would be
  redefining the shared vocabulary locally, and the point of a deployment-wide
  ontology is that a predicate means the same thing everywhere
- the **namespace template**

Writing "you may use the predicate `anything_i_like`" into a prompt override does
not make it legal. The claim is refused at the conformance gate and counted.

An empty prompt override is rejected rather than stored. It would leave the model
with no instructions while extraction kept running, which is worse than switching
the strategy off: output keeps arriving and it is nonsense.

## Why a claim did not appear

Extraction refuses far more than it stages, by design. Every refusal is counted by
reason, so start with the metrics:

```
registry_extraction_candidates_total{strategy}      what the provider proposed
registry_extraction_staged_total{strategy}          what became a claim
registry_extraction_rejected_total{strategy,reason} what was refused, and why
```

Common reasons:

| Reason | Meaning |
|---|---|
| `predicate_not_in_strategy` | The model used a predicate this strategy may not emit. |
| `value_type_mismatch` | The value is not what the predicate declares — prose where seconds were expected, an offset timestamp, a boolean where an integer belongs. |
| `unknown_predicate` | Not in the ontology at all. |
| `no_evidence_cited` | The candidate cited no source event, or cited one that was not in the batch. |
| `directive_content` | The value instructs a reader rather than describing a capability. See below. |
| `pii_blocked` | The value matched a blocking PII policy for the `claim_value` field type. |
| `below_confidence_floor` | Your configured floor rejected it. |

An **empty result is normal**. Most conversations contain nothing extractable, and
a strategy that always finds something is one that invents.

### Unresolvable subjects are not refusals

A claim whose subject does not match a catalog entity is stored `unlinked`, not
dropped and not guessed. It is excluded from scoring and serving and waits for a
person to link or discard it. Guessing would attach an assertion to the wrong
entity, where it then looks corroborated by something unrelated.

```sql
SELECT subject_reference, predicate, value_jsonb, created_at
FROM memory_claims
WHERE author_tenant_id = '<tenant_uuid>' AND status = 'unlinked'
ORDER BY created_at DESC
LIMIT 50;
```

A rising share of unlinked claims means extraction is drifting off your entity
model — usually because the transcripts name things the catalog does not have.

```
registry_claim_unresolved_subject_total
```

## Injection containment

An event body is text an agent produced or observed, so it can contain anything —
including text written to be read by a *later* agent. Because the registry serves
claims to agents, a claim carrying instruction text is an injection delivered with
the platform's authority behind it.

Three layers, none sufficient alone:

1. **Bodies are passed to the model as delimited data**, inside a per-request
   unguessable boundary, never concatenated into the instruction channel. Event
   metadata is never shown to the model at all.
2. **Output is a forced tool call.** Prose is not an available answer, so there is
   nothing to best-effort parse.
3. **A value that instructs rather than describes is refused**, even when the model
   extracted it faithfully. A correct reading of a hostile input is still a hostile
   output.

Refusals are counted by what triggered them:

```
registry_extraction_candidate_refused_total{trigger}
```

`trigger` is one of `directive_content`, `role_redefinition`,
`tool_invocation_directive`, `boundary_forgery`, `no_evidence_cited`.

A rising count on the first three is worth looking at: something in a transcript is
addressed to a future reader. The detector is deliberately biased toward refusal,
so occasional false positives are expected — they cost one candidate, and the
alternative costs a stored instruction that an agent reads as fact.

## A defective prompt, versus a broken system

A strategy whose output is mostly refused has a prompt problem. Retrying does not
help: the output is wrong the same way every time and each attempt costs a provider
call.

`GET /v1/admin/extraction-strategies/conformance-policy` returns the threshold and
what it means. Below it, over a sufficient sample, the strategy is reported as
defective rather than retried:

```
registry_extraction_strategy_conformance_ratio{strategy}
registry_extraction_strategy_defective_total{strategy}
```

Below the minimum sample no verdict is issued at all. A handful of refusals is not
evidence, and judging it would mark every strategy defective on its first quiet
hour — after which nobody would believe the signal.

## Cost

Every provider call reports its token usage, split by kind:

```
registry_extraction_tokens_total{kind}   kind = prompt | completion | cached_prompt
registry_extraction_provider_calls_total{outcome}
registry_extraction_provider_duration_seconds
```

`cached_prompt` is part of the prompt count as the API reports it, not an addition
to it — do not sum them.

A provider that cannot report usage reports it as unknown rather than as zero, and
unknown usage increments nothing. A counter moved by zero would say "no calls"
while calls were happening.

## Related

- [Sync connectors](03-sync-connectors.md) — the other way claims arrive, without a model
- [PII policies](04-pii-policies.md) — generated values are scanned under the `claim_value` field type
- [Operations](../06-operations/01-ops.md) — draining a stuck extraction queue
- [Configuration](../05-reference/03-configuration.md) — every environment variable
