"""The pre-registered evaluation protocol, as values a program can check.

Whether task memory and semantic recall actually help is a question that stops
meaning anything the moment a threshold moves after the observations arrive.
The protocol is the defence: every number below is fixed in advance, and this
module exists so "in advance" is a property the test suite can verify rather
than a claim in a document nobody diffs.

**The freeze is a digest, not a date.** A timestamp says when someone intended
to stop editing. A digest over the protocol values and over the scorer's own
source says what was actually committed to, and it changes the instant either
one does. `freeze()` computes both, and a run whose freeze does not match the
one its results were collected under is invalid -- not adjusted, not annotated,
invalid, because a protocol that can be edited mid-run is a protocol that will
be edited to agree with whatever the data showed.

**Both treatments run unconditionally.** Running the semantic treatment only if
the lexical one passed would foreclose the branch where lexical fails and
semantic succeeds -- an outcome the branch table has to be able to return, and
one that is unobservable if the second treatment never ran. So the run order
here carries no gate.

**Nothing in this module concludes anything.** It holds the thresholds and the
branch evidence; deciding which branch the observations fall into is a separate,
human-owned act, and `evidence.py` deliberately refuses to name one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The protocol's own version. Any change to a value below changes the digest,
#: but a reader comparing two evidence files wants a name before they want a
#: hex string.
PROTOCOL_VERSION: Final = "workspace-value-evaluation-1.0.0"

#: The scorer this protocol pins. A run scored by any other version is invalid
#: and restarts; there is deliberately no discretion in that rule, because the
#: judge is the one component whose change can move every number at once.
JUDGE_VERSION: Final = "workspace-eval-judge v1.0.0"

#: The five-block scorer E24-T4 added. Selectable rather than default: the
#: workspace-value evaluation this protocol was written for holds four blocks
#: fixed and varies one, and scoring it with a scorer that reads all five would
#: measure arms the ablation deliberately does not move.
#:
#: It is registered here rather than left to each caller to digest for itself,
#: because the freeze's whole value is that one function answers "what was this
#: collected under" -- and a second scorer digested by a second mechanism is two
#: answers to that question.
ENVELOPE_JUDGE_VERSION: Final = "context-envelope-judge v2.0.0"

#: Which source file each scorer version is. Enumerated rather than derived from
#: the version string: a naming convention is a rule nobody checks, and a freeze
#: that digested the wrong file would be a freeze over a program that was not
#: running.
JUDGE_SOURCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        JUDGE_VERSION: "judge.py",
        ENVELOPE_JUDGE_VERSION: "envelope_judge.py",
    }
)

# -- the three configurations ------------------------------------------------

#: The no-memory baseline: the workspace arm disabled entirely, answering
#: truthfully-empty. The minimal ablation -- canonical, governance, claims and
#: resume paths are untouched -- so a difference measured against it is
#: attributable to workspace recall and not to a differently-configured system.
CONFIG_BASELINE: Final = "baseline-no-memory"

#: Lexical and reference recall: what workspace memory does today.
CONFIG_TREATMENT_A: Final = "treatment-a-lexical-reference"

#: The experimental semantic arm: an exact scan over the authorized candidate
#: set. Exact rather than approximate, and authorized-set-first rather than
#: filtered-after, because the question under test is whether semantic matching
#: adds recall -- not whether an index approximates it well.
CONFIG_TREATMENT_B: Final = "treatment-b-semantic-exact-scan"

#: Closed, and ordered as the run reports them. Every configuration runs on
#: every scenario; a configuration missing from a batch is a failed batch rather
#: than a smaller one.
CONFIGURATIONS: Final[tuple[str, ...]] = (CONFIG_BASELINE, CONFIG_TREATMENT_A, CONFIG_TREATMENT_B)

# -- the corpus ---------------------------------------------------------------

#: Scenarios that resume a task from its own recorded history.
KIND_TASK_RESUME: Final = "task_resume"

#: Scenarios that need material recorded on a *different* task the caller also
#: participates in. The case lexical recall is least likely to reach, and
#: therefore the one that decides whether semantic matching earns its place.
KIND_CROSS_TASK_RECALL: Final = "cross_task_recall"

#: How many of each kind the frozen corpus holds. Checked rather than assumed:
#: a corpus that quietly shrank would raise every mean it feeds without any
#: system having improved, and the shrinkage is invisible in the score.
SCENARIO_COUNTS: Final[dict[str, int]] = {KIND_TASK_RESUME: 20, KIND_CROSS_TASK_RECALL: 20}

#: The corpus size the protocol froze.
SCENARIO_COUNT: Final = sum(SCENARIO_COUNTS.values())

#: SHA-256 of the frozen scenario corpus, and of the world it is evaluated
#: against. Pinned because they are inputs the freeze otherwise cannot see:
#: `freeze()` digests the thresholds and the scorer, and neither of those moves
#: when a scenario's content changes. Before these existed the corpus digest was
#: stamped into a result and never compared, so a corpus swapped between the
#: freeze and the run passed every gate and the evidence recorded the swapped
#: one's digest as though it had always been the pinned value.
#:
#: Deliberately **not** part of `frozen_values()`. That set is what the protocol
#: thresholds commit to; folding two file digests into it would move
#: `protocol_digest` every time a scenario's wording changed, which conflates
#: "the rules moved" with "the inputs moved" — and those need opposite
#: responses. The corpus and world are pinned here and refused at load, which is
#: a stricter gate than being one term inside a hash nobody re-derives.
FROZEN_CORPUS_DIGEST: Final = "d4841c704f0a06c93f5bef9b51d14a2810bec34aa710b2e5a736ab86457504ac"
FROZEN_WORLD_DIGEST: Final = "62a58edcbd86bf559dd2f21681fbca8fcb81518dca7bdb00a88f1baebe50b1b7"

#: What the closed workspace-retrieval decision was taken under, recorded as
#: literals because it describes a run that happened rather than a rule that
#: holds. The intent nomenclature cut renamed one key the judge reads off a
#: served item and the same field throughout the world, which moved the judge's
#: source digest, the freeze that contains it, and the world's bytes. Nothing
#: about what was measured changed; the names it was measured under did.
#:
#: **These are deliberately not `freeze()`.** The decision is evidence, and
#: evidence is checked against the identity it names -- not against whatever the
#: tree computes today. A constant that followed the tree would let any later
#: edit present itself as the thing that was measured, which is the exact
#: failure the corpus and world pins above were added to stop. So the boundary
#: is marked instead: this is the pre-cut identity, it does not move again, and
#: a decision that does not match it is not the decision this module may act on.
#:
#: Re-measuring under the renamed judge is a real and separate piece of work. It
#: answers how the *current* protocol scores, which is a different question from
#: what the closed decision recorded, and it is not a correction to this.
V1_ERA_IDENTITY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "protocol_version": PROTOCOL_VERSION,
        "judge_version": JUDGE_VERSION,
        "protocol_digest": "1c182d21498fbe206fce6e1fa6e8b1e8517db8818c3f992a0489a5e74791686a",
        "judge_digest": "c8c4a56d7b7bfe724b89eaa3c5478cef77f1bb8076eeae866c307ae92a02e54e",
        "freeze_digest": "19e7e465f9afd71755f1559e238a00a9994a38bed18e8987b08b75ffd4a9e10f",
        "corpus_digest": FROZEN_CORPUS_DIGEST,
        "world_digest": "b00c8619acde5fb05706063948603f1b6c7336c708421d40daf5b996c5f93270",
    }
)

# -- decision thresholds ------------------------------------------------------

#: Absolute required-fact-recall margin over baseline for lexical+reference to
#: count as adding value.
TREATMENT_A_MARGIN: Final = 0.15

#: Absolute margin the semantic treatment must add *over the lexical treatment*,
#: not over baseline. Semantic recall that only reproduces what lexical already
#: found has not earned a vector store.
TREATMENT_B_MARGIN: Final = 0.10

#: A result this close to its threshold is labelled marginal rather than being
#: read as a clean pass. Deterministic system and frozen corpus, so the number
#: is exact for this corpus -- exactness is not the same as robustness, and a
#: margin of 0.005 should not read like a margin of 0.15.
MARGINAL_BAND: Final = 0.02

# -- no-harm ------------------------------------------------------------------

#: Serving a single item the caller was not entitled to disqualifies a
#: configuration whatever its recall. Zero, not a rate: the safety question is
#: not how often the system leaks.
SAFETY_TOLERANCE: Final = 0

#: Latency regression that disqualifies, measured as the ratio of treatment
#: median to baseline median over repeated runs.
LATENCY_MEDIAN_REGRESSION_LIMIT: Final = 0.25

#: How many times each scenario is timed per configuration. A median needs
#: repeats; a single reading per scenario measures the machine.
LATENCY_REPEATS: Final = 5

#: The documented tail rule. A percentile taken over a sample this small rests
#: on one or two observations -- a p95 over 40 samples is literally the
#: second-slowest reading -- so the tail is reported as the slowest observed
#: scenario median and is *descriptive*: it informs the reader and gates
#: nothing. Only the median ratio above disqualifies.
LATENCY_TAIL_RULE: Final = (
    "The tail is reported as the slowest per-scenario median and is descriptive only. "
    "It does not gate. A percentile over a corpus this small would rest on one or two "
    "observations, which is a property of the sample size rather than of the system."
)

# -- review -------------------------------------------------------------------

#: Scenarios drawn per configuration for human risk review. A sample, and
#: labelled one.
HUMAN_RISK_SAMPLE_SIZE: Final = 10

#: Safety failures are reviewed in full, never sampled. The distinction matters:
#: sampling a risk review is a resourcing decision, sampling safety failures is
#: deciding not to look at some of them.
SAFETY_REVIEW_IS_EXHAUSTIVE: Final = True

# -- the branch table ---------------------------------------------------------

#: The four branches, each with the evidence that establishes it, fixed before
#: anyone knows which one occurred. A protocol with no stated consequence for
#: "neither helps" is one that cannot return that answer, so the fourth entry
#: is the one that makes the other three worth anything.
BRANCHES: Final[tuple[str, ...]] = (
    "semantic-adds-value-and-passes-safety",
    "lexical-sufficient",
    "semantic-adds-value-but-fails-safety",
    "task-memory-fails-baseline",
)


@dataclasses.dataclass(frozen=True)
class FrozenProtocol:
    """The committed protocol: its values, and the scorer they are scored by.

    Both digests together are the freeze. The protocol digest alone would let
    the scorer be rewritten under a fixed set of thresholds, which changes every
    number without changing anything this module can see.
    """

    protocol_version: str
    judge_version: str
    #: Digest over the frozen values below, in a canonical encoding.
    protocol_digest: str
    #: Digest over the scorer's own source text.
    judge_digest: str

    def freeze_digest(self) -> str:
        """The single value a result is stamped with, over both halves."""
        return _digest({"protocol": self.protocol_digest, "judge": self.judge_digest})

    def as_json(self) -> dict[str, str]:
        """The freeze as it travels inside a result document."""
        return {
            "protocol_version": self.protocol_version,
            "judge_version": self.judge_version,
            "protocol_digest": self.protocol_digest,
            "judge_digest": self.judge_digest,
            "freeze_digest": self.freeze_digest(),
        }


class ProtocolInvalidated(Exception):
    """The protocol changed after data collection started.

    Raised rather than reported as a lower score, because the collected
    observations are not merely worse under the new protocol -- they are
    uninterpretable under either, having been produced under one and judged
    under the other.
    """


def _canonical(value: object) -> str:
    """One encoding, so two identical protocols cannot digest differently."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def frozen_values(*, judge_version: str = JUDGE_VERSION) -> dict[str, object]:
    """Exactly what the freeze commits to.

    Enumerated rather than swept from module globals: a sweep would silently
    absorb a new constant into the freeze, and silently drop one that was
    renamed, which is the opposite of what a freeze is for.

    `judge_version` defaults to the workspace-arm scorer, so the digest this
    returns for the closed decision is byte-identical to the one it always
    returned. Naming another scorer changes the digest, which is correct: which
    program produced the numbers is part of what a freeze commits to.
    """
    return {
        "protocol_version": PROTOCOL_VERSION,
        "judge_version": judge_version,
        "configurations": list(CONFIGURATIONS),
        "scenario_counts": dict(sorted(SCENARIO_COUNTS.items())),
        "treatment_a_margin": TREATMENT_A_MARGIN,
        "treatment_b_margin": TREATMENT_B_MARGIN,
        "marginal_band": MARGINAL_BAND,
        "safety_tolerance": SAFETY_TOLERANCE,
        "latency_median_regression_limit": LATENCY_MEDIAN_REGRESSION_LIMIT,
        "latency_repeats": LATENCY_REPEATS,
        "latency_tail_rule": LATENCY_TAIL_RULE,
        "human_risk_sample_size": HUMAN_RISK_SAMPLE_SIZE,
        "safety_review_is_exhaustive": SAFETY_REVIEW_IS_EXHAUSTIVE,
        "branches": list(BRANCHES),
    }


def judge_source_digest(source: Path | None = None, *, judge_version: str = JUDGE_VERSION) -> str:
    """The content digest of the committed scorer.

    Over the file's bytes rather than over its exported names: a rubric change
    that leaves the function signatures alone is exactly the change that must
    invalidate a run, and a signature-level digest would not see it.

    An unregistered `judge_version` raises rather than falling back to the
    default. A freeze that silently digested the wrong program is worse than no
    freeze, because it names a scorer and swears to another.
    """
    if judge_version not in JUDGE_SOURCES:
        raise ProtocolInvalidated(
            f"no scorer is registered under {judge_version!r}; the registered versions are " f"{sorted(JUDGE_SOURCES)}"
        )
    path = source if source is not None else Path(__file__).with_name(JUDGE_SOURCES[judge_version])
    if not path.is_file():
        raise ProtocolInvalidated(
            f"the scorer {judge_version} is not committed at {path}; "
            "a freeze cannot digest a program that does not exist"
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(*, judge_source: Path | None = None, judge_version: str = JUDGE_VERSION) -> FrozenProtocol:
    """Compute the freeze the current tree would collect data under.

    The default is the workspace-arm scorer, so this reproduces the closed
    decision's identity exactly. E24-T4's five-block scorer is asked for by name.
    """
    return FrozenProtocol(
        protocol_version=PROTOCOL_VERSION,
        judge_version=judge_version,
        protocol_digest=_digest(frozen_values(judge_version=judge_version)),
        judge_digest=judge_source_digest(judge_source, judge_version=judge_version),
    )


def assert_unchanged(
    collected_under: FrozenProtocol, *, judge_source: Path | None = None, judge_version: str | None = None
) -> None:
    """Refuse to report results produced under a protocol that has since moved.

    Called when results are turned into evidence rather than when they are
    collected: the change that matters is the one made *between* those two
    moments, and a check at collection time would miss it by construction.

    The scorer to re-digest is read off the collected freeze when the caller does
    not name one. Defaulting to the workspace-arm scorer instead would compare a
    five-block run against a four-block digest and report the scorer as changed
    on every call, which is a check that fails on everything -- the same defect
    as one that fails on nothing.
    """
    current = freeze(judge_source=judge_source, judge_version=judge_version or collected_under.judge_version)
    if current.freeze_digest() == collected_under.freeze_digest():
        return
    changed = "the protocol values" if current.protocol_digest != collected_under.protocol_digest else "the scorer"
    raise ProtocolInvalidated(
        f"{changed} changed after collection began "
        f"(collected under {collected_under.freeze_digest()[:12]}, now {current.freeze_digest()[:12]}); "
        "the collected observations are invalid and the run restarts"
    )


__all__ = [
    "BRANCHES",
    "CONFIGURATIONS",
    "CONFIG_BASELINE",
    "CONFIG_TREATMENT_A",
    "CONFIG_TREATMENT_B",
    "ENVELOPE_JUDGE_VERSION",
    "HUMAN_RISK_SAMPLE_SIZE",
    "JUDGE_SOURCES",
    "JUDGE_VERSION",
    "KIND_CROSS_TASK_RECALL",
    "KIND_TASK_RESUME",
    "LATENCY_MEDIAN_REGRESSION_LIMIT",
    "LATENCY_REPEATS",
    "LATENCY_TAIL_RULE",
    "MARGINAL_BAND",
    "PROTOCOL_VERSION",
    "SAFETY_REVIEW_IS_EXHAUSTIVE",
    "SAFETY_TOLERANCE",
    "SCENARIO_COUNT",
    "SCENARIO_COUNTS",
    "TREATMENT_A_MARGIN",
    "TREATMENT_B_MARGIN",
    "FROZEN_CORPUS_DIGEST",
    "FROZEN_WORLD_DIGEST",
    "FrozenProtocol",
    "ProtocolInvalidated",
    "assert_unchanged",
    "freeze",
    "frozen_values",
    "judge_source_digest",
]
