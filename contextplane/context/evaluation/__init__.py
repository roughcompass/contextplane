"""A controlled evaluation of whether workspace recall earns its place.

Six modules, each owning one thing that could otherwise be quietly adjusted
after the numbers arrive:

- `protocol` holds the thresholds and freezes them, digest and all.
- `judge` is the scorer, and the only place the rubric is implemented.
- `scenarios` loads the frozen corpus and refuses one that changed size.
- `treatments` builds the ablation and the two treatments over injected reads.
- `harness` runs every configuration over every scenario, unconditionally.
- `evidence` signs the result and records no decision.

Nothing here is on a request path. The package is a measuring instrument: it
imports the assembler it measures, takes every read as an injected callable, and
adds no production surface, no table, and no job.
"""

from __future__ import annotations

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
    "JUDGE_VERSION",
    "PROTOCOL_VERSION",
    "AuthorizationFacts",
    "BatchInvalidated",
    "BatchResult",
    "Candidate",
    "ConfigurationResult",
    "Corpus",
    "CorpusInvalid",
    "Embedder",
    "FrozenProtocol",
    "InfrastructureError",
    "ProtocolInvalidated",
    "Scenario",
    "ScenarioScore",
    "SignedEvidence",
    "WorkspaceSource",
    "build",
    "exact_scan",
    "freeze",
    "load_corpus",
    "run_batch",
    "score",
]
