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
**Hotspot:** yes | no
**Repo:** contextplane | contextplane-ui

Goal: one paragraph.

Acceptance (tasks only — runnable commands, no placeholders):
    <commands>
```

Rules: an **epic** is not claimable and carries no acceptance commands — it is
decomposed into tasks by PR before work starts. A **task** with a `TBD` or
`DECISION REQUIRED` in its block is not claimable; record the decision (ADR if
it outlives the task) first. Safety-relevant orderings must ALSO be encoded as
required checks per the delivery process — `Blocked by:` alone is advisory.
