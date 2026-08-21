# Ordering-site review

The governed-magnitude registry closes over the *parameters* that decide an
order. It deliberately does not close over the *act* of ordering: three designs
for that were attempted and each was defeated by separating the arithmetic from
the sort — the score computed in one function, the ordering done elsewhere on a
bare attribute. A gate believed exhaustive and quietly defeated is worse than
none.

This file is the honest substitute. Each review sweeps the tree for numbers that
decide an order and records what was registered **and what was not, with the
reason**. The non-findings are the more valuable half: the next reviewer starts
from them instead of re-deciding the same borderline cases from scratch.

A scheduled workflow opens an issue each quarter pointing here.
`scripts/check_governed_magnitudes.py` is not that workflow and never will be —
it checks the artifact's internal coherence, not the world.

---

## 2026-08-21 — first review

Swept: every module-level constant in the tree matching a weight, threshold,
floor, ceiling, scale, decay, penalty or cutoff shape, plus inline float
literals in the scoring, ranking, confidence, salience and calibration paths.

### Registered

| Magnitude | Where it was | Why it qualifies |
|---|---|---|
| `confidence-decay-floor@1` | `service/memory/confidence.py` **and** `service/memory/confidence_decay.py`, both `0.10` | Declared twice, with two justifying comments and two tests importing their own copy. Changing one would have left the two paths flooring differently with both suites green. |
| `salience-entity-density-ceiling@1` | `extraction/salience.py`, `3.0` | The registered salience weights are applied to a value this normalises, so moving it reorders every episode while every weight stays put. |
| `salience-tool-diversity-ceiling@1` | `extraction/salience.py`, `4.0` | Same, and its offset from the entity ceiling was reasoned but unrecorded. |

The decay floor is the review's best argument for itself. Neither copy was
wrong, neither test was weak, and nothing in the tree connected them — which is
exactly the "no way for a reviewer to find its siblings" failure the registry's
own docstring names.

### Considered and not registered

**`service/memory/confidence.py` `CONTRADICTION_PENALTY = 0.25`.** Qualifies on
the merits — it changes a confidence, and confidence orders claims. Not
registered because none of the three forms describes it. It is not a threshold,
not a ladder, and calling a single multiplicative coefficient a "weights" map
with one key is the vocabulary abuse this registry exists to prevent. Adding a
`coefficient` form is a deliberate change to `ranking.py`, which that module's
own docstring says is not something a branch does in passing. **Left for whoever
adds the form; this is the entry that justifies it.**

**`service/memory/salience_reliability.py` `BUCKET_COUNT = 10`,
`MIN_BUCKET_OBSERVATIONS = 20`.** These shape a reliability curve rather than an
order. They change how confidently the calibration reports, not which episode
outranks which. Methodology, not magnitude — but the boundary is thin, and if
calibration output ever feeds a ranking directly they cross it.

**`context/evaluation/treatments.py` `SEMANTIC_FLOOR = 0.30`.** A similarity
cutoff deciding what an evaluation arm serves. Genuinely decides inclusion, but
it belongs to an experiment definition rather than to production serving: a
treatment's parameters are the thing being varied, and freezing them in a
registry that changes by PR would make an arm harder to vary than the experiment
needs. Revisit if a treatment is ever promoted to a default.

**`context/resume.py` `DEFAULT_CHECKPOINT_BOUND = 5`,
`DEFAULT_FEEDBACK_BOUND = 20`.** Truncation, not ordering. They decide how much
of an already-ordered list is returned. The same reasoning excludes
`usage/reads.py`'s `DEFAULT_RANKING_LIMIT` and `MAX_RANKING_LIMIT`.

**`context/evaluation/protocol.py` `HUMAN_RISK_SAMPLE_SIZE = 10`.** A sample
size. It decides how much is checked, not what outranks what.

**Everything resource-shaped.** Chunk sizes, key lengths, socket modes,
timeouts, backoff bases, idempotency-key lengths, rollback windows, breaker
cooldowns. None of them reorders anything. Listed here only so the next review
does not re-derive the exclusion: the sweep's regex finds them every time.

### Gaps closed along the way

**`_FORMS` admitted `threshold` and nothing could read one.** The form had been
legal since the module was written, with no accessor — an entry declaring it
would load and then be unreachable through `weights()` or `ladder()`. Registering
the three entries above needed it, which is how the gap surfaced. `threshold()`
now exists and refuses a payload that disagrees with its form tag, like its two
siblings.

### Not swept

The UI repository. E9's boundary statement names UI-side reordering as
uncoverable by the closure, and it is equally uncovered by this review — a sweep
of `contextplane-ui` is a separate exercise against a different vocabulary, and
claiming this review covered it would be the overclaim the whole approach is
written against.
