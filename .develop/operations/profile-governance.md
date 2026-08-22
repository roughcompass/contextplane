# Operating profile governance

This describes what the platform enforces today. Where a capability is partial, it
says so — an operations document that describes an intended end state is one that
sends somebody to production expecting a guarantee that is not there yet.

## What a profile is, and what binds a tenant to it

A **profile revision** is an immutable published document: entity types,
relationship types and interfaces, compiled together and digested. Publishing is
one-way. A revision cannot be edited or deleted, and a correction is a new
revision — every validator downstream resolves against a published revision, and
the guarantee they depend on is not "the document was right when written" but
"the document is still the one that was written".

A **binding** joins a tenant to a revision. It moves through
`planned → validating → active`, with `rollback_pending → rolled_back` available
and `retired` terminal. Exactly one binding is active per tenant.

The binding state decides enforcement, and nothing else does:

| Binding state | Entity writes are |
|---|---|
| `active` | **refused** on violation |
| `validating` | **reported** on violation; the write proceeds |
| neither | unconstrained — the tenant has adopted no profile |

A tenant validating its next profile is still governed by its current one. If both
exist, `active` wins; the validation window measures the candidate without leaving
the live profile unenforced.

## Adopting a profile

1. Publish the revision. It compiles before it is stored; a document that does not
    compile is never written.
2. Plan a binding for the tenant and move it to `validating`.
3. Read the violations. Every entity write during this window reports what the
    candidate profile would have refused.
4. Resolve them, then activate. Enforcement begins at that transition.

Rollback is available from `active` via `rollback_pending`. It requires the
rollback target to be recorded when the binding is planned — a rollback with no
target is not a rollback.

## What is enforced on a write

**Entities.** Type must be declared; properties must be declared or fall under a
declared extension point; required properties must be present; values must match
their declared type. Violations are bounded at twenty per write, and the response
says when it truncated.

**Relationships.** Endpoint types, tenancy, declared properties, duplicate policy,
symmetry, direction and maximum cardinality — all decided in one transaction, under
an advisory lock on `(binding, relationship_type, cardinality_scope)`. Minimum
cardinality does not refuse a write; it records `draft`, and readiness gates
activation instead.

**Provenance.** Every governed assertion records its source, times, authority,
freshness and the profile revision that validated it. Only a derived assertion
carries a confidence. Provenance is immutable — a correction is a new record
superseding the old one.

## Cross-organization sharing

Nothing crosses a tenant boundary without an active grant. Grants are proposed and
then activated with recorded approval evidence; a proposal permits nothing.

Omission denies, everywhere. A grant that does not name an operation does not
permit it; an empty type list reaches no types; a classification the platform does
not recognise is refused rather than ranked. Classification is a **ceiling**, not a
filter — content above it is not shared at all, rather than shared redacted.

Revocation is immediate. Derived artefacts (caches, indexes, closures) are keyed to
the grant set that produced them, so a revocation changes the key and the old
artefact becomes unreachable at once. Reads against a stale artefact fail closed;
they are not silently recomputed.

A denial never distinguishes "does not exist" from "not shared with you". That is
deliberate, and it means an operator debugging a denial needs the audit record
rather than the response.

## Ownership is not authorization

An ownership assignment records who is accountable for something. It grants
nothing. No authorization decision consults ownership, and a conformance gate
inspects the authorization modules to keep it that way — if the two were joined,
assigning an owner would become a way to acquire permissions.

Assignments start in `draft`, reach `validated` through `proposed`, and carry a
reason on every transition.

## Current limits

These are the boundaries of what is enforced today.

- **Cross-organization grants deny by default and there is no grant-approval
  workflow in the product.** Grants are recorded and enforced; proposing and
  approving them is an operator action against the service, not a self-service
  surface.
- **The generic write surface routes by intent, and only the approval route
  reaches canonical data.** Observation and request routes record the intent and
  return what they did; the staged-claim and owner-queue paths they name are
  recorded, and the downstream review workflow is the memory-curation surface.
- **Ownership targets are checked for entities only.** An assignment against
  another target kind is accepted without a subject check, because what may be
  owned is the profile's to define and this layer does not gatekeep it.
- **The legacy interface surface is still in use.** Interfaces are modelled as
  governed entities, and the retirement gate exists, but its first condition —
  zero consumers — is not yet met. Run
  `python scripts/interface_consumer_inventory.py` for the current list.
- **Entity-level provenance is not yet carried on the entity itself.** Assertion
  provenance reaches attributes and relationships; an entity read reports the
  provenance of its governed relationships.

## Operator commands

```sh
# Who still uses the legacy interface surface
python scripts/interface_consumer_inventory.py
python scripts/interface_consumer_inventory.py --check   # inventory completeness

# What a profile migration must account for
python scripts/profile_migration_inventory.py
python scripts/profile_migration_execute.py --direction forward    # dry run
python scripts/profile_migration_execute.py --direction rollback   # dry run

# Every entity writer resolves through profile validation
python scripts/check_profile_write_coverage.py
python scripts/check_profile_write_coverage.py --explain
```

`profile_migration_execute.py` is dry-run only. Executing a migration is an
operator action taken with a plan in hand; a script that could do it with one flag
is one that eventually does it with one typo.

## Migration dispositions

Every finding a migration reports needs a disposition, and a disposition is four
things: owner, reason, expiry, action. The actions are `migrate`, `grandfather`,
`quarantine`, `remove`.

Expiry is enforced. A grandfathered finding whose expiry has passed blocks
activation exactly as an undecided one does — a grandfather that never expires is
a `remove` nobody admitted to.
