"""Evaluation: the controlled workspace study, and the scorer a surface uses.

The original six modules are a measuring instrument for one question -- whether
workspace recall earns its place -- and each owns one thing that could otherwise
be quietly adjusted after the numbers arrive:

- `protocol` holds the thresholds and freezes them, digest and all.
- `judge` is the workspace-arm scorer, and the only place its rubric is written.
- `scenarios` loads the frozen corpus and refuses one that changed size.
- `treatments` builds the ablation and the two treatments over injected reads.
- `harness` runs every configuration over every scenario, unconditionally.
- `evidence` signs the result and records no decision.

Beside them, and on a request path rather than beside one:

- `runs` holds prompt sets, runs over them and persisted verdicts (E22-T15).
- `envelope_judge` scores all five blocks under its own rubric version (E24-T4).

**The two scorers are versions, not a migration.** `judge` measures an ablation
that holds four blocks fixed; `envelope_judge` measures a live resolution where
none is fixed. `protocol.freeze()` still digests `judge` by default, so every run
collected under the closed workspace-retrieval decision stays valid, and the
five-block scorer is asked for by name.
"""

from __future__ import annotations

from contextplane.context.evaluation.envelope_judge import (
    ENVELOPE_JUDGE_VERSION,
    BlockTally,
    EnvelopeScore,
    UncheckedDimension,
)
from contextplane.context.evaluation.envelope_judge import (
    AuthorizationFacts as EnvelopeAuthorizationFacts,
)
from contextplane.context.evaluation.envelope_judge import (
    score as score_envelope,
)
from contextplane.context.evaluation.evidence import SignedEvidence, build
from contextplane.context.evaluation.harness import (
    BatchInvalidated,
    BatchResult,
    ConfigurationResult,
    InfrastructureError,
    run_batch,
)
from contextplane.context.evaluation.judge import AuthorizationFacts, ScenarioScore, score
from contextplane.context.evaluation.protocol import (
    CONFIGURATIONS,
    JUDGE_SOURCES,
    JUDGE_VERSION,
    PROTOCOL_VERSION,
    FrozenProtocol,
    ProtocolInvalidated,
    freeze,
)
from contextplane.context.evaluation.scenarios import Corpus, CorpusInvalid, Scenario, load_corpus
from contextplane.context.evaluation.treatments import Candidate, Embedder, WorkspaceSource, exact_scan

__all__ = [
    "CONFIGURATIONS",
    "ENVELOPE_JUDGE_VERSION",
    "JUDGE_SOURCES",
    "JUDGE_VERSION",
    "PROTOCOL_VERSION",
    "AuthorizationFacts",
    "BlockTally",
    "BatchInvalidated",
    "BatchResult",
    "Candidate",
    "ConfigurationResult",
    "Corpus",
    "CorpusInvalid",
    "Embedder",
    "EnvelopeAuthorizationFacts",
    "EnvelopeScore",
    "FrozenProtocol",
    "InfrastructureError",
    "ProtocolInvalidated",
    "Scenario",
    "ScenarioScore",
    "SignedEvidence",
    "UncheckedDimension",
    "WorkspaceSource",
    "build",
    "exact_scan",
    "freeze",
    "load_corpus",
    "run_batch",
    "score",
    "score_envelope",
]
