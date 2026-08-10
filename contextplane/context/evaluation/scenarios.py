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
from contextplane.context.evaluation.protocol import (
    FROZEN_CORPUS_DIGEST,
    FROZEN_WORLD_DIGEST,
    SCENARIO_COUNTS,
)

#: Where the frozen corpus lives, relative to the repository root.
CORPUS_RELATIVE_PATH = Path("tests") / "fixtures" / "workspace_evaluation" / "scenarios.json"

#: Where the world the corpus is evaluated against lives. A companion file
#: rather than more fields in the corpus: the corpus's own rule is that future
#: runs extend it with new files and never edit these, and keeping
#: `scenarios.json` byte-identical is what keeps its expectations provably
#: older than any observation.
WORLD_RELATIVE_PATH = Path("tests") / "fixtures" / "workspace_evaluation" / "world.json"

#: The corpus file's own schema version, so a reader can tell a shape change
#: from a content change.
CORPUS_VERSION = 1

#: The world file's schema version, versioned separately because the two files
#: change for different reasons.
WORLD_VERSION = 1


class CorpusInvalid(Exception):
    """The corpus on disk is not the frozen corpus the protocol committed to."""


class WorldInvalid(Exception):
    """The world on disk is not the world the protocol committed to, or does not pair."""


@dataclasses.dataclass(frozen=True)
class WorldCheckpoint:
    """One checkpoint the world places, and the content it carries.

    The corpus names required facts by key and says nothing about what they
    contain or where they sit. Those are the two things that decide whether a
    retrieval arm can find them, so they live here -- committed and digested
    before anyone observes, rather than invented by whoever runs the campaign.
    """

    item_key: str
    task_id: str
    sequence: int
    goal: str
    author: str


@dataclasses.dataclass(frozen=True)
class WorldEntry:
    """The world for one scenario: who is asking, and what exists to be found."""

    scenario_id: str
    #: Distinct per scenario. One shared actor across forty scenarios makes each
    #: scenario's grants reach every other scenario's items, so every envelope
    #: serves material outside its own declared audience and the judge records a
    #: violation that says nothing about the system.
    actor_id: str
    checkpoints: tuple[WorldCheckpoint, ...]

    def keys(self) -> frozenset[str]:
        """Every item key this scenario's world places."""
        return frozenset(c.item_key for c in self.checkpoints)


@dataclasses.dataclass(frozen=True)
class World:
    """Every scenario's world, and the digest it is pinned by."""

    version: int
    entries: dict[str, WorldEntry]
    digest: str


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


def world_path(repo_root: Path) -> Path:
    """Where the frozen world lives under *repo_root*."""
    return repo_root / WORLD_RELATIVE_PATH


def _refuse_on_digest(kind: str, path: Path, actual: str, expected: str | None) -> None:
    """Refuse a pinned input whose bytes are not the pinned bytes.

    Refusing rather than recording. The previous shape stamped the observed
    digest into the result and continued, which meant a swapped file produced a
    document that looked internally consistent -- it faithfully reported the
    digest of whatever it had actually read, and nothing anywhere compared that
    to what was pre-registered.
    """
    if expected is None:
        return
    if actual != expected:
        raise CorpusInvalid(
            f"the {kind} at {path} is not the pinned {kind} (pinned {expected[:12]}, found {actual[:12]}). "
            "A run against un-pinned inputs is not this protocol's run: re-pre-register and re-pin, or restore "
            "the frozen file."
        )


def load_corpus(path: Path, *, expected_digest: str | None = FROZEN_CORPUS_DIGEST) -> Corpus:
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
    _refuse_on_digest("corpus", path, digest, expected_digest)

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


def load_world(path: Path, *, expected_digest: str | None = FROZEN_WORLD_DIGEST) -> World:
    """Read and validate the world the corpus is evaluated against.

    Same order as the corpus: digest the raw bytes, refuse an un-pinned file,
    then trust the content. The digest is taken before parsing so a file that is
    the wrong file fails as the wrong file rather than as bad JSON.
    """
    if not path.is_file():
        raise WorldInvalid(
            f"the world is not committed at {path}; the corpus names required facts but nothing says what they "
            "contain or where they sit, so every instantiation of it would be authored at run time"
        )
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    _refuse_on_digest("world", path, digest, expected_digest)

    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise WorldInvalid(f"{path}: the world is not readable JSON: {exc}") from exc

    if document.get("world_version") != WORLD_VERSION:
        raise WorldInvalid(f"{path}: world_version {document.get('world_version')!r} is not {WORLD_VERSION}")

    raw_entries = document.get("scenarios")
    if not isinstance(raw_entries, dict):
        raise WorldInvalid(f"{path}: the world has no 'scenarios' object")

    entries: dict[str, WorldEntry] = {}
    for scenario_id, raw in raw_entries.items():
        checkpoints = tuple(
            WorldCheckpoint(
                item_key=str(c["item_key"]),
                task_id=str(c["task_id"]),
                sequence=int(c["sequence"]),
                goal=str(c["goal"]),
                author=str(c["author"]),
            )
            for c in raw["checkpoints"]
        )
        if not checkpoints:
            raise WorldInvalid(f"{scenario_id}: the world places no checkpoints, so nothing can be recalled")
        entries[str(scenario_id)] = WorldEntry(
            scenario_id=str(scenario_id), actor_id=str(raw["actor_id"]), checkpoints=checkpoints
        )

    return World(version=WORLD_VERSION, entries=entries, digest=digest)


def assert_pairs(corpus: Corpus, world: World) -> None:
    """Refuse a corpus and world that do not describe the same evaluation.

    Four checks, and each one is a way the pair silently produces a number about
    something other than the system:

    - **Every scenario present exactly once.** A scenario with no world has
      nothing to find and scores zero; a scenario named twice would have the
      later entry win, silently.
    - **Every required fact resolvable.** A required key the world never places
      is unreachable by construction, so the configuration is scored against an
      answer that does not exist.
    - **Every placement inside the scenario's own declared audience.** The world
      supplies content and position; it must never widen an audience the corpus
      already fixed, or the judge records a violation the world caused.
    - **Actors distinct across scenarios.** The failure this whole file exists
      to fix: one shared actor makes each scenario's grants reach every other
      scenario's items.
    """
    corpus_ids = [s.scenario_id for s in corpus.scenarios]
    missing = [i for i in corpus_ids if i not in world.entries]
    extra = [i for i in world.entries if i not in set(corpus_ids)]
    if missing or extra:
        raise WorldInvalid(
            f"the world does not pair with the corpus: {len(missing)} scenario(s) without a world "
            f"({missing[:3]}), {len(extra)} world entr(ies) without a scenario ({extra[:3]})"
        )

    seen_actors: dict[str, str] = {}
    for scenario in corpus.scenarios:
        entry = world.entries[scenario.scenario_id]

        unresolved = sorted(set(scenario.required_item_keys) - entry.keys())
        if unresolved:
            raise WorldInvalid(
                f"{scenario.scenario_id}: required fact(s) {unresolved} are not placed by the world, so no "
                "configuration could ever surface them and the scenario scores zero for a reason that is not "
                "about the system"
            )

        permitted = scenario.facts.permitted_task_ids
        if permitted is not None:
            outside = sorted({c.task_id for c in entry.checkpoints} - permitted)
            if outside:
                raise WorldInvalid(
                    f"{scenario.scenario_id}: the world places checkpoints on task(s) {outside}, outside the "
                    "audience the corpus declared. The world supplies content and position; widening an "
                    "audience would manufacture the safety violation it is supposed to avoid"
                )

        if entry.actor_id in seen_actors:
            raise WorldInvalid(
                f"{scenario.scenario_id} and {seen_actors[entry.actor_id]} share actor {entry.actor_id!r}; "
                "a shared actor holds both scenarios' grants, so each serves the other's items and the judge "
                "records an audience violation that says nothing about the system"
            )
        seen_actors[entry.actor_id] = scenario.scenario_id


def load_evaluation_inputs(repo_root: Path) -> tuple[Corpus, World]:
    """Both pinned inputs, validated together. The only supported entry point."""
    corpus = load_corpus(corpus_path(repo_root))
    world = load_world(world_path(repo_root))
    assert_pairs(corpus, world)
    return corpus, world


__all__ = [
    "CORPUS_RELATIVE_PATH",
    "WORLD_RELATIVE_PATH",
    "WORLD_VERSION",
    "CORPUS_VERSION",
    "Corpus",
    "CorpusInvalid",
    "Scenario",
    "World",
    "WorldCheckpoint",
    "WorldEntry",
    "WorldInvalid",
    "corpus_path",
    "load_evaluation_inputs",
    "load_world",
    "world_path",
    "load_corpus",
]
