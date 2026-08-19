# Architecture decision records

Decisions that outlive their PR are recorded here and change only by PR.
Numbered `NNNN-slug.md`, MADR-lite. Every ADR carries all six sections:

1. **Context** — the problem and the forces on it.
2. **Decision** — what was decided, concretely.
3. **Assumptions** — what the decision rests on. An ADR recorded without its
   assumptions cannot be revisited when one changes, which is the only reason
   to revisit a decision.
4. **Alternatives rejected** — and why.
5. **Consequences** — including costs accepted.
6. **Dissent** — disagreement is recorded, not resolved away.

For consequential ADRs, two reviewers form views independently: each writes a
full rationale in an isolated session *without opening the PR conversation*,
then posts it whole. Rationale lives in the ADR file, never only in PR
comments.
