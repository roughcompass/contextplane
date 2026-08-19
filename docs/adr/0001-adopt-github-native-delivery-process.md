# 0001 — Adopt a GitHub-native delivery process

**Status:** Accepted 2026-08-18

## Context

The prior delivery process (a bespoke coordination baton in a separate planning
repository: machine-local claims, a leader lease, coordinator/worker roles) was
retired by operator decision on 2026-08-18. Its replacement must preserve three
properties for a sole human operator working with a fleet of interruptible AI
agent sessions: interruption/resume without lost or redone work, parallelism
where footprints permit, and enforced quality gates on both repositories.

## Decision

Adopt the process in
[docs/07-contributing/03-delivery-process.md](../07-contributing/03-delivery-process.md):
GitHub Issues/PRs as the only coordination state; trunk-based with short-lived
branches; claim = assign + pushed branch + draft PR; time-boxed WIP pushes
independent of test state; stale-claim takeover after 8h; auto-merge with
strict up-to-date checks and squash-only merges; footprints derived from open
PR diffs plus a serialized hotspot list; the OpenAPI contract vendored and
pinned in the UI repository; safety-relevant sequencing encoded as required
checks, never as issue metadata.

## Assumptions

- Both repositories remain public (enforced branch protection is free); going
  private on the Free plan removes enforcement and must be consciously crossed.
- One human identity exists and agent sessions run under it, so branch
  protection carries no approval count; review is a labeled convention
  (`needs-human`, `review-waiver:sole-human`).
- A ~30-minute push cadence is an acceptable durability bound for in-flight
  agent work.

## Alternatives rejected

- **Keep the baton.** Bespoke coordination state duplicated what the forge
  provides, required its own validation tooling, and its opt-in ceremony made
  every session start ambiguous.
- **Hosted PM tool (Jira/Linear).** A second state surface agents coordinate
  through worse than `gh`; violates "GitHub is the only coordination state."
- **Monorepo merge.** Orthogonal to coordination; not justified by this
  problem.

## Consequences

The setup checklist in the process doc must run (UI CI, contract pinning,
branch protection, claim automations, templates). Issue state becomes the
single source of delivery truth; the archived planning workspace stays
read-only history.

## Dissent

Three adversarial reviews of the draft found 18 defects; all material ones are
incorporated (durability decoupled from test-green, stale-claim takeover rule,
contract pinning, strict up-to-date merges, derived footprints, hard gates as
required checks, no approval count in protection). One tension is recorded
rather than resolved: takeover-from-branch-tip preserves work but can inherit a
misconceived approach; takeover-fresh-from-main discards both. The process
prefers branch-tip with an explicit escape hatch.
