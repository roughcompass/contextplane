"""The frozen scenario corpus, and the checks that keep it frozen.

Forty scenarios, each naming in advance the facts a correct envelope must
surface. Naming them in advance is the entire mechanism: a scenario whose
required facts were written after seeing what the system returned would be
satisfied by whatever the system returned.

**Cardinality is checked before content.** A corpus that quietly lost half its
scenarios raises every mean it feeds without any system having improved, and the
shrinkage is invisible in the score -- the remaining scenarios all still pass.
So `load_corpus` counts first and refuses a corpus of the wrong size before it
looks at a single required fact.

**The corpus is frozen, not versioned in place.** Future runs extend it with new
files; they never edit these. An edited scenario changes the corpus digest, and
a run whose results were collected under a different digest is invalid.

**Every scenario carries its own authorization facts.** The judge needs to know
what the answer should have been without asking the system under test, and the
scenario is the only place that knowledge can live and still predate the
observation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from contextplane.context.evaluation.judge import CLASSIFICATION_ORDER, AuthorizationFacts
from contextplane.context.evaluation.protocol import SCENARIO_COUNTS

#: Where the frozen corpus lives, relative to the repository root.
CORPUS_RELATIVE_PATH = Path("tests") / "fixtures" / "workspace_evaluation" / "scenarios.json"

#: The corpus file's own schema version, so a reader can tell a shape change
#: from a content change.
CORPUS_VERSION = 1


class CorpusInvalid(Exception):
    """The corpus on disk is not the frozen corpus the protocol committed to."""


@dataclasses.dataclass(frozen=True)
class Scenario:
    """One evaluation case, complete before any observation exists.

    `term` and `reference` are the request the harness issues; they are part of
    the scenario rather than derived from it, because deriving a query from the
    required facts would hand the treatment the answer.
    """

    scenario_id: str
    kind: str
    description: str
    tenant_id: str
    actor_id: str
    term: str
    reference: dict[str, str] | None
    #: What a correct envelope must surface, by receipt item key or content
    #: digest. Never empty: a scenario requiring nothing is passed by a system
    #: that returns nothing.
    required_item_keys: tuple[str, ...]
    #: The superset used for the secondary precision metric. Always contains the
    #: required facts -- a fact that is required and not relevant would make the
    #: two metrics contradict each other.
    relevant_item_keys: tuple[str, ...]
    facts: AuthorizationFacts

    def __post_init__(self) -> None:
        if not self.required_item_keys:
            raise CorpusInvalid(
                f"{self.scenario_id}: no required facts; a scenario that requires nothing is passed by a "
                "system that returns nothing"
            )
        missing = set(self.required_item_keys) - set(self.relevant_item_keys)
        if missing:
            raise CorpusInvalid(
                f"{self.scenario_id}: required fact(s) {sorted(missing)} are not in the relevant set, so recall "
                "and precision would disagree about the same item"
            )


def _facts_from(raw: dict[str, Any], scenario_id: str) -> AuthorizationFacts:
    ceiling = raw.get("max_classification", "confidential")
    if ceiling not in CLASSIFICATION_ORDER:
        raise CorpusInvalid(f"{scenario_id}: max_classification {ceiling!r} is not one of {list(CLASSIFICATION_ORDER)}")
    tenants = raw.get("permitted_tenant_ids")
    tasks = raw.get("permitted_task_ids")
    return AuthorizationFacts(
        permitted_tenant_ids=None if tenants is None else frozenset(str(t) for t in tenants),
        permitted_task_ids=None if tasks is None else frozenset(str(t) for t in tasks),
        max_classification=ceiling,
        withdrawn_item_keys=frozenset(str(k) for k in raw.get("withdrawn_item_keys", ())),
    )


def _scenario_from(raw: dict[str, Any]) -> Scenario:
    scenario_id = str(raw["scenario_id"])
    kind = str(raw["kind"])
    if kind not in SCENARIO_COUNTS:
        raise CorpusInvalid(f"{scenario_id}: kind {kind!r} is not one of {sorted(SCENARIO_COUNTS)}")
    reference = raw.get("reference")
    return Scenario(
        scenario_id=scenario_id,
        kind=kind,
        description=str(raw["description"]),
        tenant_id=str(raw["tenant_id"]),
        actor_id=str(raw["actor_id"]),
        term=str(raw["term"]),
        reference=None if reference is None else {str(k): str(v) for k, v in reference.items()},
        required_item_keys=tuple(str(k) for k in raw["required_item_keys"]),
        relevant_item_keys=tuple(str(k) for k in raw.get("relevant_item_keys", raw["required_item_keys"])),
        facts=_facts_from(raw.get("authorization", {}), scenario_id),
    )


@dataclasses.dataclass(frozen=True)
class Corpus:
    """The whole frozen corpus, with the digest a result is stamped against."""

    version: int
    scenarios: tuple[Scenario, ...]
    digest: str

    def by_kind(self, kind: str) -> tuple[Scenario, ...]:
        """Every scenario of one kind, in corpus order."""
        return tuple(s for s in self.scenarios if s.kind == kind)


def corpus_path(repo_root: Path) -> Path:
    """Where the frozen corpus lives under *repo_root*."""
    return repo_root / CORPUS_RELATIVE_PATH


def load_corpus(path: Path) -> Corpus:
    """Read and validate the frozen corpus.

    Order matters here. The file is digested from its raw bytes first, then the
    counts are checked, and only then is any scenario's content trusted. A
    truncated corpus fails on the count rather than by scoring a clean sweep
    over whatever survived.
    """
    if not path.is_file():
        raise CorpusInvalid(f"the frozen corpus is not committed at {path}")
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CorpusInvalid(f"{path}: the corpus is not readable JSON: {exc}") from exc

    version = document.get("corpus_version")
    if version != CORPUS_VERSION:
        raise CorpusInvalid(f"{path}: corpus_version {version!r} is not the expected {CORPUS_VERSION}")

    entries = document.get("scenarios")
    if not isinstance(entries, list):
        raise CorpusInvalid(f"{path}: the corpus has no 'scenarios' list")

    counted: dict[str, int] = {kind: 0 for kind in SCENARIO_COUNTS}
    for entry in entries:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind in counted:
            counted[kind] += 1
    if counted != SCENARIO_COUNTS:
        raise CorpusInvalid(
            f"{path}: the corpus holds {counted} scenario(s) but the frozen protocol committed to "
            f"{dict(SCENARIO_COUNTS)}; a corpus that changed size raises every mean it feeds without any "
            "system having improved"
        )

    scenarios = tuple(_scenario_from(entry) for entry in entries)
    ids = [s.scenario_id for s in scenarios]
    if len(set(ids)) != len(ids):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise CorpusInvalid(f"{path}: duplicate scenario_id(s) {duplicated}")

    return Corpus(version=CORPUS_VERSION, scenarios=scenarios, digest=digest)


__all__ = [
    "CORPUS_RELATIVE_PATH",
    "CORPUS_VERSION",
    "Corpus",
    "CorpusInvalid",
    "Scenario",
    "corpus_path",
    "load_corpus",
]
