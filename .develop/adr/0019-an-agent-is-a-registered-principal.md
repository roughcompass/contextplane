# 0019 — An agent is a registered principal, not an inferred one

**Status:** Accepted 2026-08-24

## Context

E20 reached a finding and recorded it as Decision 2, and it is quoted in full
here because this decision reverses the outcome without disputing a word of it:

> **`actor_kind` classification (human vs. agent vs. degree of autonomy) has no
> reliable source signal today, and inventing one is out of scope for this
> epic.** Investigation (not the initial design) found that
> `upsert_entitlement_actor` — the one function both the REST and MCP auth paths
> actually call — takes only `(session, tenant_id, oidc_subject, display_name)`.
> There is no machine-identity signal anywhere in
> `contextplane/auth/entitlements/resolver.py` to source a kind from, and the
> `WorkloadIdentity` concept that looks like a fit lives entirely inside ARC's
> autonomy-envelope subsystem with no connection to this code path. Worse, a
> human driving Claude Code or VSCode Copilot connects through the *identical*
> MCP transport an unattended agent would use, so even a transport-based
> classification would misclassify human-in-the-loop coding sessions as
> autonomous.

Every sentence of that remains true. `upsert_entitlement_actor` still has that
signature (`auth/entitlements/actor_store.py:137`), and the transport still
cannot distinguish the two callers. **That is precisely why inference is refused
here rather than attempted more cleverly.**

What changed is not the evidence. It is that E22 asks for a screen — `/agents` —
whose entire subject is agents, and whose first field is `Agent actor UUID` with
the placeholder `00000000-0000-0000-0000-000000000000`. A roster of principals
that cannot say which of them are agents is not a roster of agents; it is a list
of everyone, mislabelled. The user's instruction resolves it by supplying the
signal that was missing rather than deriving one: **an agent registers.**

Three facts about the current state, checked rather than assumed:

- `actors` already carries the column. Migration 0001 declares
  `actor_kind TEXT NOT NULL DEFAULT 'human'`, and the table's spine —
  `(actor_id, tenant_id, display_name, oidc_subject, created_at)` — is already
  written by both auth paths.
- The column has **two values in use across the whole tree**: `'human'` and
  `'sync_worker'`. `ingest/runner.py:583` selects on `sync_worker` to find the
  worker principal it provisions, and one curation path uses a system-curator
  kind. Nothing reads `'human'` to make a decision; it is what a row gets for
  not being anything else.
- There is **no CHECK constraint** on the column. Its vocabulary is a
  convention, not a rule, which is how it came to mean "not a sync worker".

## Decision

**`actor_kind` is set by declaration at registration, never inferred.**
Registration is an act by the operator or the integration that already had to
provision the principal's credentials; it carries a name, an owner, and the
declaration that this principal is an agent. `actor_kind` is then read off a
record.

**An unregistered principal has kind `unknown`, not `human`.** This is the part
of the decision that costs something and it is the part that matters. Defaulting
to `human` would make every unregistered agent invisible on exactly the screens
built to watch agents — the roster would be quietly complete and quietly wrong,
and the failure would present as "we have no agents" rather than as "nobody has
declared any."

So the column's default changes from `'human'` to `'unknown'`. That is a
behaviour change to a shipped column and it is deliberate: a default that
asserts a fact nobody stated is the same defect this repository refuses in
`observed_at`, in `source_namespace`, and in the sampling policy's unknown
categories. The rule those share is stated once more here — **a value nobody
declared must not escape every rule that names one** — and `human` was an
escape.

`'sync_worker'` is untouched. It is a declaration too, made by the code that
provisions the principal, and `ingest/runner.py` continues to read it.

## Assumptions

1. **An operator running an agent can be asked to declare it**, because they
   already had to provision its credentials. Registration adds a step to a
   process that already has one; it does not create a new obligation for
   somebody who had none.
2. **`unknown` is a durable state, not a migration artefact.** Some principals
   will never be declared, and that is a fact about the deployment rather than a
   backlog. Every consumer renders it as unknown rather than collapsing it into
   a kind — the roster shows an unknown row, it does not omit it or guess.
3. **Registration is tenant-scoped and carries an owner**, so "who do I talk to
   about this agent" is answerable from the roster. An agent whose owner is
   unrecorded is a principal nobody is accountable for, which is the state the
   registration exists to end.

## Alternatives rejected

**Inference from `memory_session_events.kind` patterns.** An unattended agent
and a human in an IDE produce different event mixes on average and
indistinguishable ones on any given day. The output would be a confident label
on a guess, placed on the screen whose entire job is calibrating trust in an
agent — the one place a guess presented as a fact does the most damage.

**Transport-based classification.** Rejected on E20's own evidence, which this
ADR quotes rather than re-derives: the transports are identical.

**Leaving `actor_kind` unused forever.** Defensible while nothing needed it, and
no longer: it is the user's first named defect, and the roster is unusable
without names and kinds. "Unused" was never the state anyway — it defaults to
`human` and is therefore already asserting something, for every row, without
anybody deciding.

**A separate `agents` table rather than a column on `actors`.** Rejected because
an agent *is* a principal: it authenticates, it holds entitlements, it authors
claims, and every one of those already keys on `actor_id`. A second table would
make "is this actor an agent" a join that half the call sites would forget, and
would leave two answers to one question — which is the shape this plan's
supersession rule exists to prevent.

## Consequences

**E20-T4 through E20-T10 continue to key on `author_actor_id` and are not
rewritten to consult `actor_kind`.** Stated explicitly because the tempting
follow-on is to "improve" the accuracy services now that a kind exists, and that
would re-derive shipped numbers against a dimension they were deliberately built
without. The declaration adds a **display dimension and a filter**, not a new
join in the accuracy path. E20's out-of-scope line stays in that epic, amended
rather than deleted, so a reader of those tasks finds the reason they do not
consult the column.

**The default flips, and one existing behaviour has to be checked with it.**
Nothing reads `'human'` to make a decision today, which is what makes the flip
safe; that this is true was checked rather than assumed, and it is the fact the
change rests on. If a later reader finds a consumer branching on `'human'`, this
ADR is the thing that was wrong, not that consumer.

**The vocabulary gains a rule.** `unknown`, `human`, `agent`, `sync_worker` and
the system-curator kind become a closed set with a CHECK, because a column whose
values are a convention is a column that acquires a sixth spelling of one of
them. This is the third time this tree has closed a vocabulary after using it
loosely, and the reason is the same each time.

**A cost accepted:** every deployment starts with a roster of `unknown`
principals and stays that way until somebody declares. That looks like a broken
screen on day one, and the dissent below is about exactly that.

## Dissent

**Registration is a step integrators will skip.** The strongest objection, and
it is not answered by asserting that they should not. An operator wiring an
agent against an MCP endpoint at 6pm will provision a token and stop; the
declaration is one more form, it blocks nothing, and nothing breaks without it.
The predictable end state is a roster that is mostly `unknown`, which reads as a
product that does not work rather than as a deployment that has not been told
anything.

This is answered rather than dismissed, and the answer is a requirement rather
than a hope: **the roster renders `unknown` as a first-class row with a "declare
this" action.** Not a filtered-out row, not a greyed one, and not an empty state
that says "no agents registered" while eleven principals are actively calling
the API. The screen shows every principal it has, says what it does not know
about each, and offers the one action that changes it. A roster honest about its
own gaps is usable; one that hides them is the failure this objection predicts.

**A second, narrower.** `unknown` as a default means the first thing a new
deployment sees is a column full of a word that sounds like an error. The
counter — that `human` sounds like an answer and is a guess — is stronger, but
the cost is real and lands on first impressions rather than on correctness.
