# Plan as code

The implementation plan lives here as structured task blocks. GitHub issues
are a **generated projection** of these files — an agent syncs them
idempotently via `gh issue create/edit`. Re-planning is a PR to a plan file,
never hand-editing issues.

## Task block format

```markdown
### <ID> — <title>

**Kind:** epic | task
**Status:** pending | done | deferred
**Blocked by:** <IDs or issue #s, or none>
**Hotspot:** yes | no   (tasks only; when yes, name which:
                        openapi.json + generated client | migrations |
                        pnpm-lock.yaml | coverage or gate baseline)
**Repo:** contextplane | contextplane-ui   (a task names exactly one;
          an epic spanning both lists both, comma-separated)

Goal: one paragraph.

Acceptance (tasks only — runnable commands, no placeholders):
    <commands>
```

An epic carries no `Hotspot:` flag — the flag serializes *claims*, and an epic
is never claimed. Each task cut from an epic declares its own `Hotspot:` and
names which one, so contract-touching tasks separate from non-contract work in
the same epic. The flag supplements, never replaces, the derived-footprint
check in the delivery process. An epic may list both repositories; every task
decomposed from it names exactly one, because an issue, branch and PR live in a
single repository — cross-repo ordering is expressed in the dependent task's
`Blocked by:`, never by a shared task.

Rules: an **epic** is not claimable and carries no acceptance commands — it is
decomposed into tasks by PR before work starts. A **task** with a `TBD` or
`DECISION REQUIRED` in its block is not claimable; record the decision (ADR if
it outlives the task) first. Safety-relevant orderings must ALSO be encoded as
required checks per the delivery process — `Blocked by:` alone is advisory.
