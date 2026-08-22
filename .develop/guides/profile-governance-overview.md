# Profile governance

The catalog knows what a capability is. A **profile** is how a deployment says what
*else* exists — the entity types, relationship types and interfaces its
organization actually works in — and has the platform enforce them.

## Why it is not just a schema

A schema validates a payload. A profile answers questions a payload cannot:

- which types exist, and which properties each carries;
- what a relationship joins, in which direction, and how many are allowed;
- which relationships must be present before something is ready;
- who is accountable for a thing, separately from who may change it;
- what may cross an organization boundary, and under what approval.

Those are graph-shaped, so they are enforced where the graph is written rather
than at the edge of a request.

## The three intents

Every write on the generic surface states its intent, and there is no default —
a default routes somebody's write somewhere they did not choose.

| Intent | Reaches | Meaning |
|---|---|---|
| `observation` | a staged claim | "I saw this" |
| `request` | the owner's queue | "I would like this" |
| `authorized_approval` | the canonical validators | "this was reviewed" |

An ordinary agent can use the whole surface and never write canonical data. That
is the property that makes it safe to expose generically, and it is enforced by
resolving authority from the credential — never from the request body.

## Enforcement follows the binding

A profile is published once and bound per tenant. The binding state decides
whether violations are refused or merely reported, so a deployment can measure
itself against a candidate profile before committing to it, and enforcement begins
at one recorded transition rather than at a deploy.

## What this does not do

It does not decide permissions. Ownership records accountability and grants record
sharing; neither is consulted by the authorization layer, and that separation is
enforced by a gate rather than by convention.

For operating it, see [profile governance operations](../operations/profile-governance.md).
