<!--
  title: Trust, authority, and confidence
  audience: integrator, agent builder, operator, auditor
  archetype: explanation (mental model)
  summary: How the registry labels recalled claims, derives source authority, calculates confidence, and preserves evidence for review.
-->

# Trust, authority, and confidence

Every served claim is untrusted recall, even when its confidence is high. The
registry uses three separate signals so callers do not confuse provenance,
probability, and approval:

- **Trust** identifies the knowledge layer. Living Memory recall is always
  labelled `untrusted`; canonical graph state is the approved catalog layer.
- **Authority** describes where a claim came from and whether the author owns
  the subject.
- **Confidence** estimates how likely the assertion is to be correct over its
  effective interval.

These signals answer different questions. Independent agreement can raise
confidence, but it cannot turn an observer into an owner. Human confirmation
can raise standing, but it does not promote the value. Promotion alone changes
the canonical graph.

For the surrounding lifecycle, read
[Living Memory and claims](07-living-memory.md). For review procedures, use the
[memory-curation runbook](../06-operations/05-memory-curation.md).

---

## Authority comes from ownership and derivation

The registry derives authority from authenticated tenant ownership and
resolvable evidence. Callers do not choose their own tier.

| Authority tier | Meaning | Shipped base confidence |
|---|---|---:|
| `owner_human` | A human in the subject's owning tenant asserted or confirmed it | 0.80 |
| `owner_extraction` | A reproducible source owned by the subject's tenant produced it | 0.62 |
| `owner_inference` | The owner tenant produced it through an inference step | 0.45 |
| `observer_human` | A human outside the owning tenant asserted it | 0.42 |
| `observer_extraction` | A reproducible external source produced it | 0.32 |
| `observer_inference` | An outside inference produced it | 0.23 |
| `unattributed` | The subject did not resolve, so ownership cannot be determined | Not scored |

A shipped base combines two axes: the source's standing and the derivation's
reproducibility. The listed values are the stored defaults, not caller-supplied
scores. Tenant tuning may change weights but cannot reorder the authority
ladder.

A claim with several evidence items takes the weakest derivation needed to
produce it. Attaching one strong source cannot launder a weaker inference into
a higher tier. Agreement from other sources contributes to confidence instead.

Human standing is tied to a human principal. A service or worker credential
cannot create curator evidence or claim the authority of human review.

## Confidence is stored with its explanation

The score is calculated when a claim is written or rescored. The row stores the
inputs and scorer version beside the result. This allows an auditor to
reconstruct why the score had that value at that time.

The calculation uses:

1. The base value for the authority tier.
2. A calibrated provider score, when a valid calibration mapping exists.
3. Independent corroborating evidence.
4. Human confirmation, when present.
5. A disagreement penalty when the claim is contested.
6. A cap below certainty.

A provider's self-reported confidence contributes nothing until adjudicated
outcomes calibrate what that provider's numbers predict. This prevents an
unverified model score from appearing authoritative.

## Confidence buckets tell consumers what the number permits

Bucket boundaries are deployment-wide. A threshold means the same thing in
every tenant, even when a tenant adjusts permitted scoring weights.

| Bucket | Range | Consumption guidance |
|---|---:|---|
| `confirmed` | 0.85 ≤ c | A human with standing reviewed the claim within its confirmation window. Use it like canonical state only when the decision remains reversible. |
| `strong` | 0.70 ≤ c < 0.85 | Independent evidence supports it. Use without immediate re-verification when the action is reversible. |
| `moderate` | 0.45 ≤ c < 0.70 | One source with standing supports it. Use when being wrong is cheap. |
| `weak` | 0.20 ≤ c < 0.45 | Treat it as a lead that needs verification. |
| `unreliable` | c < 0.20 | Do not act on it. Its presence only records that someone asserted it. |

Ranges are lower-closed and upper-open. A score of exactly 0.85 is
`confirmed`; a score of 0.849 is `strong`.

The system caps confidence at 0.98. No observation is represented as certain.
The decay floor is 0.10, so old evidence remains visible as stale evidence
instead of becoming indistinguishable from no assertion.

The `confirmed` bucket does not remove the `trust: "untrusted"` label. It
records a human review inside Living Memory. Promotion remains the canonical
boundary.

## Corroboration rewards independence, not repetition

Equivalent claims collapse into one survivor. Their evidence can raise the
survivor's confidence when it comes from independent sources.

The scorer deduplicates repeated evidence from one conversation or connector
run. It also caps one actor's contribution across sessions. An agent repeating
the same observation cannot ratchet a claim to the ceiling by volume.

Corroboration closes only part of the gap above the claim's base score. This
keeps many weak sources from substituting for one source with standing.

## Contesting affects both score and eligibility

Single-valued predicates can conflict when two values cover overlapping time
intervals. The registry records the contest and applies a bounded score penalty.
The larger consequence is procedural: a contested claim cannot be promoted.

Authority decides which claim may supersede another. Recency breaks ties only
between comparable authority tiers. A new observer inference does not displace
an older owner assertion merely because it arrived later.

Multi-valued predicates do not turn every different value into a conflict. A
capability can depend on several entities, so two `depends_on` assertions can
both be true.

## Confidence decays when current-state claims age

Age changes what a stored score is worth at read time. The stored score remains
unchanged, and the effective value derives from the clock, its scoring time,
its half-life, and any confirmation hold.

| Claim category | Default half-life |
|---|---:|
| `interface_contract` | 90 days |
| `operational_lifecycle` | 120 days |
| `dependency` | 180 days |
| `ownership_stewardship` | 270 days |
| `decision_rationale` | 730 days |
| `session_summary` | Does not decay when stored as prose |
| `incident_history` | Historical category with an effectively long half-life |

Subject volatility can shorten or lengthen the category rate when enough change
history exists. A tenant multiplier can tune the rate without changing bucket
meaning.

Human confirmation pauses decay for a bounded period. The hold cannot exceed
the category's own horizon. When the hold ends, decay restarts from the
confirmed score rather than snapping back to the pre-confirmation age.

## Adjudication calibrates providers

Confirmation says a person vouches for a claim. Adjudication records whether a
claim later proved `correct`, `incorrect`, or `undecidable`. They serve different
purposes.

Calibration fits mappings per provider, model, and extraction strategy. It uses
only adjudicated extraction claims and refuses to fit below the required sample
size. Until then, the provider remains uncalibrated.

This separation prevents human confirmation from training a provider mapping
as though it were a model prediction. It also prevents one model's score from
being reused for another model or strategy.

## Match the signal to the decision

| Decision | Preferred source |
|---|---|
| Find a possible owner or dependency | Claim recall at a suitable confidence floor, followed by citation review |
| Make a cheap, reversible choice | `moderate` or stronger claim when evidence fits the task |
| Make an expensive or irreversible change | Canonical graph state plus source verification |
| Change a canonical attribute or edge | Owner-reviewed promotion |
| Skip human promotion review | Only an eligible, owner-originated, allowlisted, non-high-impact claim |

High impact never becomes safe merely because confidence is high. A certain
withdrawal of a widely used interface needs more review, not less.

## Read next

- [Living Memory and claims](07-living-memory.md)
- [Retrieval and context](10-retrieval-and-context.md)
- [Living Memory in action](../03-use-cases/09-living-memory-in-action.md)
- [Memory-curation runbook](../06-operations/05-memory-curation.md)
