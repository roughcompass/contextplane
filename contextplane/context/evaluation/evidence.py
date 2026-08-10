"""Turning a batch into evidence: digested, signed, and deliberately undecided.

This module produces the artifact somebody else reads to make a decision. It
does not make one, and the refusal is enforced rather than merely intended --
`build` will not accept a branch name and `EVIDENCE_CARRIES_NO_DECISION` is
asserted by the conformance suite. The reason is the same one that makes the
protocol worth freezing: the party that runs the measurement and the party that
concludes from it should not be the same party, because a harness that reported
"semantic recall passes" would be grading its own homework in the one place
nobody re-checks.

**The freeze is re-verified at build time, not at collection time.** The
dangerous edit is the one made after the observations exist and before they are
written up, and a check performed while collecting cannot see it. `build` calls
`assert_unchanged` and refuses to emit anything if the protocol or the scorer
moved.

**Digest and signature answer different questions.** The digest says the content
has not changed since it was written; the signature says who wrote it. A result
file with only a digest is one an editor can re-digest after editing, which is
why the signature takes an operator-supplied key rather than a constant that
would ship in this repository and sign nothing.

**Everything that would let a reader re-derive the numbers travels with them.**
The corpus digest, the freeze digests, per-scenario recall counts, every safety
failure, the drawn review sample, and the latency medians with their tail rule.
A summary a reader cannot reproduce is a claim, and this file exists so the
result is not one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, Final

from contextplane.context.evaluation.judge import RUBRIC
from contextplane.context.evaluation.protocol import (
    LATENCY_TAIL_RULE,
    MARGINAL_BAND,
    SAFETY_REVIEW_IS_EXHAUSTIVE,
    TREATMENT_A_MARGIN,
    TREATMENT_B_MARGIN,
    assert_unchanged,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from contextplane.context.evaluation.harness import BatchResult, ConfigurationResult

#: The evidence document's schema version.
EVIDENCE_VERSION: Final = 1

#: Stated in the artifact itself, so a reader who has only the file knows the
#: file is not the decision. The conformance suite pins it.
EVIDENCE_CARRIES_NO_DECISION: Final = (
    "This document reports observations against a pre-registered protocol. It records no branch, "
    "no pass, and no recommendation. Reading these numbers against the protocol's branch table is a "
    "separate act, performed by the accountable party and recorded elsewhere."
)


class EvidenceUnsigned(Exception):
    """A signing key was not supplied, so the result cannot be attributed."""


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)


@dataclasses.dataclass(frozen=True)
class SignedEvidence:
    """The result document, its digest, and the signature over that digest."""

    document: dict[str, Any]
    digest: str
    signature: str

    def as_json(self) -> str:
        """The artifact as written to disk: content, then the seals over it."""
        return _canonical({"evidence": self.document, "digest": self.digest, "signature": self.signature})

    def verify(self, key: bytes) -> bool:
        """Whether this artifact is intact and was signed by the holder of *key*."""
        return hmac.compare_digest(self.digest, _digest(self.document)) and hmac.compare_digest(
            self.signature, _sign(self.digest, key)
        )


def _digest(document: object) -> str:
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _sign(digest: str, key: bytes) -> str:
    return hmac.new(key, digest.encode("utf-8"), hashlib.sha256).hexdigest()


def _configuration_document(result: ConfigurationResult) -> dict[str, Any]:
    """One configuration's observations, in re-derivable form.

    The per-scenario counts are carried, not only the means: a reader who
    disagrees with how a mean was taken can take their own, and a mean that
    cannot be re-derived is the number nobody can argue with for the wrong
    reason.
    """
    return {
        "configuration": result.configuration,
        "primary_metric": {
            "name": "required_fact_recall",
            "mean": result.mean_recall,
            "per_scenario": [
                {
                    "scenario_id": run.score.scenario_id,
                    "required_found": run.score.required_found,
                    "required_total": run.score.required_total,
                    "recall": run.score.recall,
                    "errored": run.score.errored,
                }
                for run in result.runs
            ],
        },
        "secondary_metrics": {
            "workspace_item_precision_mean": result.mean_precision,
            "resolution_latency_ms": {
                "median_of_scenario_medians": result.median_latency_ms,
                "slowest_scenario_median": result.slowest_scenario_median_ms,
                "repeats_per_scenario": [len(run.durations_ms) for run in result.runs],
                "tail_rule": LATENCY_TAIL_RULE,
            },
        },
        "errored_scenarios": list(result.errored_scenarios),
        "safety": {
            "review_is_exhaustive": SAFETY_REVIEW_IS_EXHAUSTIVE,
            "failure_count": len(result.safety_failures),
            "failures": [
                {
                    "scenario_id": score.scenario_id,
                    "violations": [
                        {"item_key": v.item_key, "kind": v.kind, "detail": v.detail} for v in score.violations
                    ],
                }
                for score in result.safety_failures
            ],
        },
    }


def build(batch: BatchResult, *, signing_key: bytes, judge_source: Path | None = None) -> SignedEvidence:
    """Write the evidence for one batch, or refuse to.

    Refuses on two grounds, both before any number is emitted: the protocol or
    scorer moved since collection began, or no signing key was supplied. Both
    produce an exception rather than an unsigned or caveated document, because a
    result file that says "unverified" in a field somewhere is a result file
    that gets quoted without the field.
    """
    if not signing_key:
        raise EvidenceUnsigned(
            "no signing key was supplied; an unsigned result cannot be attributed, and a constant key "
            "committed to this repository would sign nothing"
        )
    assert_unchanged(batch.collected_under, judge_source=judge_source)

    document: dict[str, Any] = {
        "evidence_version": EVIDENCE_VERSION,
        "disclaimer": EVIDENCE_CARRIES_NO_DECISION,
        "freeze": batch.collected_under.as_json(),
        "corpus_digest": batch.corpus_digest,
        "judge": {"version": batch.collected_under.judge_version, "rubric": RUBRIC},
        "thresholds": {
            "treatment_a_margin_over_baseline": TREATMENT_A_MARGIN,
            "treatment_b_margin_over_treatment_a": TREATMENT_B_MARGIN,
            "marginal_band": MARGINAL_BAND,
        },
        "human_risk_sample": list(batch.human_risk_sample),
        "configurations": [_configuration_document(result) for result in batch.results],
    }
    digest = _digest(document)
    return SignedEvidence(document=document, digest=digest, signature=_sign(digest, signing_key))


__all__ = [
    "EVIDENCE_CARRIES_NO_DECISION",
    "EVIDENCE_VERSION",
    "EvidenceUnsigned",
    "SignedEvidence",
    "build",
]
