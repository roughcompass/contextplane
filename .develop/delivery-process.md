# Delivery process

Canonical for both repositories: `contextplane` (service) and `contextplane-ui`
(admin dashboard). It governs humans and AI agents identically. Adopted
2026-08-18, replacing the retired planning-workspace/baton process.

## Principles

1. **GitHub is the only coordination state.** Issues are tasks, PRs are
   integration, required status checks are gates. No side-channel claim files,
   no separate planning repository, no coordination scripts. If it is not on
   the issue or PR, it did not happen.
2. **Trunk-based.** `main` is always releasable. Branches live days, not weeks.
   Incomplete features ship dark behind flags or the product's own
   advisory-mode patterns.
3. **Specs and decisions live in the repo they govern** and change only by PR.
4. **One path for humans and agents.** Every change is a PR passing the same
   required checks. Nobody pushes to `main` directly; agents never merge a PR
   whose checks are red.
5. **Resumable by construction.** Work state lives in the pushed branch and the
   draft PR description — never in a chat transcript or a working tree.

## Task lifecycle

**Plan → issues.** The implementation plan is a file, `.develop/plan/`, holding
structured task blocks: id, goal, acceptance criteria as runnable commands,
`Blocked by:` links, and a hotspot flag. Issues are a generated projection of
that file — an agent syncs them idempotently via `gh issue create/edit`.
Re-planning is a PR to the plan file, not hand-editing forty issues.

**Claim.** Claim = self-assign the issue **and immediately push a branch**
(`<issue>-<slug>`, an empty `wip:` commit is fine) **and open a draft PR** from
it. Assign only if the assignee list is empty; re-read after assigning to
detect a race. Only issues labeled `status:ready` may be claimed — automation
flips `status:blocked` → `status:ready` when the last `Blocked by:` issue
closes.

**Work.** Push at least every ~30 minutes and before starting any long-running
gate, regardless of test state — `wip:` commits on a draft PR are safe because
required checks gate *merge*, not pushes. Update the PR description's
`Remaining / How to verify` section at each push: the PR, not your working
tree or your memory, is the resume point.

**Interruption, staleness, takeover.** A claim is **stale** when its draft PR
has had no push or comment for 8 hours (a scheduled Action labels it
`stale-claim` and unassigns the issue; post a heartbeat comment if a long
local run is in progress). Any session may take over a stale claim by posting
a takeover comment on the PR and **continuing from the pushed branch tip** —
never force-pushing over unexamined WIP. Unpushed local work is forfeit by
rule; that is what the 30-minute push discipline is for. If the branch tip is
unusable, restart from `main` and say so in the takeover comment.

**Finish.** Mark ready, checks green, merge (see integration), `Fixes #n`
closes the issue. Enable "automatically delete head branches"; run
`git worktree prune` at session start and remove worktrees whose PR is merged.

## Parallelism

- **Footprints are derived, not declared.** Before claiming, list the changed
  paths of all open draft PRs (`gh pr list` + `git diff --name-only`); do not
  claim an issue whose work will overlap them. A short hand-maintained
  **hotspot list** is serialized — at minimum: `pnpm-lock.yaml`, the OpenAPI
  document and generated client, `storage/migrations/`, and any coverage/gate
  baseline files. Issues touching a hotspot run one at a time.
- **Contract-first, pinned.** The OpenAPI contract is the boundary between the
  repos. `contextplane-ui` vendors `openapi.json` as a **committed file**;
  `generate:api` reads only that file (never a sibling checkout), and UI CI
  fails if regeneration from the pinned contract is dirty. A contract change
  is: (1) server PR merges a backward-compatible contract; (2) one UI PR bumps
  the pin and regenerates the client together. No atomic cross-repo merge is
  ever assumed; the UI issue's `Blocked by:` names the server PR.
- **Local parallel sessions:** one git worktree per issue.
- **WIP limit:** at most 3 open non-draft PRs per repo; check before claiming.

## Integration

Auto-merge with **"require branches to be up to date before merging"** (strict
status checks) on `main`. On a "branch out of date" notification, the PR owner
updates the branch and lets gates re-run. Squash-only merges; one CI check
validates the PR title against conventional-commit format (per-commit message
rules and a separate linear-history setting are redundant and not used). A
merge queue may be added later if PR volume warrants it; it is not core.

## Quality gates (required status checks)

- **contextplane:** the existing `make` gate set — lint (ruff, import-linter,
  visibility-chokepoint, file-size), format-check, mypy strict, doc-refs,
  test-hygiene, coverage-ratcheted tests, conformance.
- **contextplane-ui:** `pnpm lint`, `type-check`, `test` (coverage ≥ 80%),
  `build`, bundle budgets, and the generated-client drift check. Standing this
  CI up is step zero for the UI repo.

**Require the `gate` job, not the tiers individually.** Naming tiers here and
requiring them by name is what let protection drift away from this list: most
tiers are conditional (`unit` skips on a docs-only PR, `perf` is nightly), and
GitHub reads a *skipped* required check as pending rather than satisfied, so
requiring one blocks exactly the PRs it was written not to run on. Protection
therefore drifted to the handful of unconditional jobs, and a PR with a red test
tier stayed mergeable — which is how #51 put a broken unit suite on `main` while
GitHub reported the merge as allowed. `ci.yml`'s `gate` job always runs, depends
on every other job, and fails unless each one succeeded or was deliberately
skipped. Requiring that one context covers the whole list above and keeps
covering it when a tier is added.

**Branch protection on `main`, both repos:** required checks strict, force
pushes disabled, deletions disabled, **"do not allow bypass" enabled for
admins**. This last is what makes the audit trail real for a sole-owner org —
and it is also why a new required context is pointed at a job only *after* that
job has reported once: with bypass off, a required check that never reports
locks the repository instead of protecting it.

**Review policy — honest for one human.** Branch protection carries **no
approval count**: with one human identity and agents running under it, an
enforced count makes PRs unmergeable. Instead: a non-blocking agent-review CI
job comments findings on every PR; changes touching the **risky-path list**
(migrations, `security/`, `service/governance/`, the OpenAPI contract, auth)
get the `needs-human` label and wait for explicit human sign-off in a comment.
Human-authored risky-path changes get two agent reviews from **fresh, isolated
sessions** (no shared context), each posting a standalone rationale, and merge
with the label `review-waiver:sole-human` so they are queryable in audit.

## Decisions (ADRs)

Decisions that outlive their PR are recorded in `.develop/adr/` (MADR-lite) and
merge by PR. The template mandates: context, decision, **assumptions**,
**rejected alternatives**, consequences, and dissent. For consequential ADRs,
two reviewers form views **independently**: each writes their full rationale
in an isolated session *without opening the PR conversation*, then posts it
whole. Disagreement is recorded in the ADR, not resolved away. Rationale
lives in the ADR file — never only in PR comments, which are not repo content.

## Hard gates are code, not metadata

`Blocked by:` links are advisory; GitHub does not enforce them. Any
safety-relevant sequencing constraint (e.g. model-governance validation before
a score-consuming feature activates) must be encoded as a **required check**:
the gate asserts the guarded code path is flag-off or absent until a committed
validation-evidence artifact (`docs/governance/validation/<name>.md` with a
machine-readable status field) exists and passes schema check.

## Secrets and data hygiene

Issues and PR comments are the coordination medium and agents will paste into
them. Rules: redact tokens, credentials, tenant identifiers, and personal data
before posting; post log *excerpts*, never dumps; link to local artifact paths
for anything sensitive. Secret scanning and push protection stay enabled; a
scheduled job scans issue/comment text, which push protection does not cover.

## Plan-tier reality

Both repos are currently **public**, so branch protection, rulesets, and
CODEOWNERS enforcement are free. If they go **private on the Free plan,
enforced branch protection disappears entirely** — at that point either pay
for Pro/Team (the cheapest real fix) or drop to convention: auto-merge
disabled, only the human merges, only on green CI. This cliff must be
consciously crossed, not discovered.

## Setup checklist

1. `contextplane-ui`: add CI (root commands + coverage + bundle budgets +
   client-drift check); vendor the pinned `openapi.json`; branch protection as
   above.
2. `contextplane`: branch protection as above; PR-title check; agent-review
   job; `stale-claim` and `status:ready` Actions; seed `.develop/adr/` and
   `.develop/plan/`; issue/PR templates with the redaction rules.
3. Sync the implementation plan into issues (one supervised agent task).
4. Ensure `gh` is installed on every machine agents run on.
