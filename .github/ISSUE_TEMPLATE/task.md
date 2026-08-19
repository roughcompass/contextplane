---
name: Task
about: A claimable unit of work (sized ≤ 1 day)
labels: status:ready
---

## Goal

## Acceptance criteria

<!-- Runnable commands, not prose. A task is done when these pass. -->

```sh
make lint format-check typecheck doc-refs test-hygiene test
```

## Blocked by

<!-- "Blocked by: #n" lines. Automation flips status:blocked → status:ready
     when the last blocker closes. Leave empty if unblocked. -->

## Hotspot

<!-- yes/no: does this touch pnpm-lock.yaml, contracts/openapi.json, the
     generated client, or budget/coverage baselines? Hotspot issues run one
     at a time. -->

<!-- REDACTION REMINDER: no tokens, credentials, tenant identifiers, or
     personal data. -->
