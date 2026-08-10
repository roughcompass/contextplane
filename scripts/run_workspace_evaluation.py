"""Collect the pre-registered workspace-recall evaluation, or refuse to.

Runs the frozen scenario corpus through all three configurations against a real
database and the deployment's resolved embedder, and writes one signed evidence
document. It records no decision: which branch the numbers fall into is read by
the accountable party from the artefact, not asserted here.

Almost all of this file is refusals, and each guards a way a campaign produces a
number that looks fine and means something other than what it says.

**It refuses a protocol that moved.** The freeze is recomputed and compared
before the first observation. A run under a changed scorer or changed thresholds
is not this protocol's run, and finding that out after 600 resolutions is the
expensive way to learn it.

**It refuses to run against a database that does not contain the corpus.** This
is the one that would otherwise be silent. The corpus names the facts a correct
envelope must surface by item key; point the runner at a database where those
keys do not exist and every configuration scores exactly zero — which does not
read as "the fixture is missing", it reads as "task memory does not help", the
one conclusion that stops the work. The preflight turns that into a loud refusal
before any observation is taken.

**It refuses to write an unsigned artefact.** An unsigned result cannot be
attributed, and a key constant enough to commit here would sign nothing.

**It never writes into the planning workspace.** The evidence lands in this
repository's git-ignored run directory and crosses to the planning record by
handoff. A runner that wrote the decision document directly would have the
author of the measurement authoring the artefact that decides on it.

The embedder is stamped, not chosen: the protocol pins the scorer, not the
model, so the run records whichever provider the deployment resolved rather than
selecting one that flatters it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.config import Settings, get_settings
from contextplane.context.assembler import ArmOutcome
from contextplane.context.evaluation import evidence, harness, protocol, scenarios, treatments
from contextplane.context.schemas.envelope import BLOCK_ARC, BLOCK_CANONICAL, BLOCK_OBSERVED_CLAIMS
from contextplane.embedding import build_embedder
from contextplane.workspaces import queries_audience as audience_q
from contextplane.workspaces import recall as workspace_recall
from contextplane.workspaces.models import TaskCheckpoint
from contextplane.workspaces.recall import WorkspaceRecall

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextplane.context.assembler import ContextArm
    from contextplane.types import Embedder as ProductionEmbedder

_log = logging.getLogger(__name__)

#: The scorer digest the pre-registration froze. Hard-coded rather than read
#: from a file the runner could be pointed at: a comparison against a value the
#: caller supplies is a comparison the caller can win.
FROZEN_JUDGE_DIGEST = "c8c4a56d7b7bfe724b89eaa3c5478cef77f1bb8076eeae866c307ae92a02e54e"

#: The protocol and freeze digests as computed from the tree the freeze was
#: verified against. The published freeze records the scorer digest; these two
#: are the values that tree yields, pinned here so a change to the thresholds --
#: which would leave the scorer digest untouched -- is caught by this runner as
#: well.
FROZEN_PROTOCOL_DIGEST = "1c182d21498fbe206fce6e1fa6e8b1e8517db8818c3f992a0489a5e74791686a"
FROZEN_FREEZE_DIGEST = "19e7e465f9afd71755f1559e238a00a9994a38bed18e8987b08b75ffd4a9e10f"

#: Where the artefact lands by default. Git-ignored: an evidence document is a
#: run output, and committing it to the product repository would put a result in
#: the history of the thing it measures.
DEFAULT_OUT_DIR = Path("run") / "workspace_evaluation"

#: The environment variable carrying the signing key.
SIGNING_KEY_ENV = "WORKSPACE_EVALUATION_SIGNING_KEY"


class RunRefused(Exception):
    """A precondition failed, so no observation was taken."""


class _PlainVectors:
    """The deployment's embedder, returning plain floats.

    The production embedders answer with a numpy array; the evaluation's scan
    asks only for sequences of floats and does its own arithmetic. Converting
    here rather than widening the harness's protocol keeps the instrument free
    of a numpy dependency it does not otherwise need, and makes the one place
    the two shapes meet explicit.
    """

    def __init__(self, inner: ProductionEmbedder) -> None:
        self._inner = inner
        self.model_version: str = inner.model_version

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(value) for value in row] for row in self._inner.encode(texts)]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def assert_freeze_unmoved() -> protocol.FrozenProtocol:
    """Recompute the freeze and refuse unless it is the one that was published.

    All three digests, not just the scorer's. The scorer digest alone would miss
    a threshold edit, which changes what the numbers mean without touching a
    byte of `judge.py`.
    """
    frozen = protocol.freeze()
    mismatches = [
        (name, expected, actual)
        for name, expected, actual in (
            ("judge_digest", FROZEN_JUDGE_DIGEST, frozen.judge_digest),
            ("protocol_digest", FROZEN_PROTOCOL_DIGEST, frozen.protocol_digest),
            ("freeze_digest", FROZEN_FREEZE_DIGEST, frozen.freeze_digest()),
        )
        if expected != actual
    ]
    if mismatches:
        detail = "; ".join(f"{name}: frozen {exp[:12]}, now {act[:12]}" for name, exp, act in mismatches)
        raise RunRefused(
            f"the protocol has moved since it was frozen ({detail}). A run under a changed protocol is not "
            "this protocol's run. Restore the frozen state, or re-pre-register and re-freeze before collecting."
        )
    return frozen


def signing_key_from_env() -> bytes:
    key = os.environ.get(SIGNING_KEY_ENV, "")  # config: intentional
    if not key.strip():
        raise RunRefused(
            f"{SIGNING_KEY_ENV} is unset, so the result could not be attributed to whoever ran it. "
            "An unsigned artefact is not evidence; set the variable and re-run."
        )
    return key.encode("utf-8")


async def assert_corpus_world_present(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus
) -> dict[str, int]:
    """Refuse unless the database actually holds the checkpoints the corpus names.

    The failure this prevents is the quiet one. Every scenario names its required
    facts by checkpoint id; against a database that does not contain them, all
    three configurations score exactly 0.0 and the run reads as
    "task memory does not help" rather than as "you pointed this at the wrong
    database". Those two produce the same numbers and opposite decisions.
    """
    wanted = {key for scenario in corpus.scenarios for key in scenario.required_item_keys}
    as_uuids = []
    for key in wanted:
        try:
            as_uuids.append(uuid.UUID(key))
        except ValueError:
            # A corpus may name a fact by content digest rather than id; those
            # cannot be checked here and are not counted as missing.
            continue

    async with factory() as session:
        present = set(
            (
                await session.execute(
                    select(TaskCheckpoint.checkpoint_id).where(TaskCheckpoint.checkpoint_id.in_(as_uuids))
                )
            ).scalars()
        )

    missing = len(as_uuids) - len(present)
    if missing:
        raise RunRefused(
            f"the database holds {len(present)} of the {len(as_uuids)} checkpoint(s) the frozen corpus requires "
            f"({missing} missing). Every configuration would score zero, which is indistinguishable in the "
            "output from a finding that workspace recall does not help. Point the runner at the database the "
            "corpus was written against, or stand that world up first."
        )
    return {"required_checkpoints": len(as_uuids), "present": len(present)}


# ---------------------------------------------------------------------------
# The reads, over the real database
# ---------------------------------------------------------------------------


class DatabaseWorkspaceSource:
    """The three workspace reads a configuration is built from, over Postgres.

    `authorized_candidates` resolves the caller's audience first and builds
    candidates only from what came back, and it withholds restricted material
    exactly as the lexical arm does -- a semantic arm that served what another
    arm withholds would make the withholding decorative.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, moment: datetime.datetime) -> None:
        self._factory = factory
        self._moment = moment
        self._recall = WorkspaceRecall(session_factory=factory)

    async def lexical(self, scenario: scenarios.Scenario) -> ArmOutcome:
        return await self._recall.lexical_arm(
            tenant_id=uuid.UUID(scenario.tenant_id),
            actor_id=scenario.actor_id,
            term=scenario.term,
            moment=self._moment,
        )()

    async def reference(self, scenario: scenarios.Scenario) -> ArmOutcome:
        if scenario.reference is None:
            return ArmOutcome()
        reference = scenario.reference
        # Named explicitly rather than splatted: the arm also takes a `limit`,
        # so a corpus that grew a stray key would otherwise reach a parameter
        # the scenario never meant to set.
        return await self._recall.reference_arm(
            tenant_id=uuid.UUID(scenario.tenant_id),
            actor_id=scenario.actor_id,
            moment=self._moment,
            source_system=reference["source_system"],
            source_namespace=reference["source_namespace"],
            kind=reference["kind"],
            external_id=reference["external_id"],
        )()

    async def authorized_candidates(self, scenario: scenarios.Scenario) -> tuple[treatments.Candidate, ...]:
        tenant_id = uuid.UUID(scenario.tenant_id)
        async with self._factory() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(TaskCheckpoint).where(
                            TaskCheckpoint.tenant_id == tenant_id,
                            TaskCheckpoint.task_id.in_(
                                audience_q._authorized_task_ids(
                                    tenant_id=tenant_id, actor_id=scenario.actor_id, moment=self._moment
                                )
                            ),
                        )
                    )
                ).scalars()
            )
        return tuple(
            treatments.Candidate(item_key=str(row.checkpoint_id), text=row.goal, item=workspace_recall._item(row))
            for row in rows
            if workspace_recall.classification_for(row.evidence) != "restricted"
        )


def held_fixed_arms(scenario: scenarios.Scenario) -> dict[str, ContextArm]:
    """The three arms the evaluation holds constant across configurations.

    Truthfully empty rather than absent. An arm missing from the mapping is a
    *failed* block, which would degrade every envelope in all three
    configurations equally and measure a broken system three times over.
    """

    async def empty() -> ArmOutcome:
        return ArmOutcome()

    return {BLOCK_CANONICAL: empty, BLOCK_ARC: empty, BLOCK_OBSERVED_CLAIMS: empty}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _stamp_provenance(document: dict[str, Any], *, settings: Settings, world: dict[str, int]) -> dict[str, Any]:
    """Add what produced these vectors, and against what.

    The protocol pins the scorer and says nothing about the embedder, so the run
    has to name the model itself or the reader is left inferring it from the
    date. Carried inside the signed document rather than beside it: provenance a
    reader cannot verify is provenance somebody can edit.
    """
    return {
        **document,
        "run_provenance": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "corpus_world": world,
        },
    }


async def collect(
    *,
    settings: Settings,
    corpus: scenarios.Corpus,
    moment: datetime.datetime,
    repeats: int,
) -> tuple[harness.BatchResult, dict[str, int]]:
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        world = await assert_corpus_world_present(factory, corpus)
        embedder = _PlainVectors(build_embedder(settings))
        batch = await harness.run_batch(
            corpus=corpus,
            source=DatabaseWorkspaceSource(factory, moment=moment),
            other_arms=held_fixed_arms,
            embedder=embedder,
            now=moment,
            repeats=repeats,
        )
    finally:
        await engine.dispose()
    return batch, world


def write_artefact(document: dict[str, Any], *, signing_key: bytes, out: Path) -> Path:
    """Seal the stamped document and write it.

    Re-sealed here rather than reusing the library's seal, because the
    provenance block was added after `evidence.build` sealed its own document.
    A signature that covered only part of what the file says would be worse than
    none: it would look verified.
    """
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    signature = hmac.new(signing_key, digest.encode("utf-8"), hashlib.sha256).hexdigest()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"evidence": document, "digest": digest, "signature": signature}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect the pre-registered workspace-recall evaluation and write signed evidence."
    )
    parser.add_argument("--database-url", default=None, help="asyncpg database URL (overrides DATABASE_URL)")
    parser.add_argument("--out", default=None, help="Where to write the signed evidence JSON")
    parser.add_argument(
        "--repeats",
        type=int,
        default=protocol.LATENCY_REPEATS,
        help=f"Timed resolutions per scenario (protocol value: {protocol.LATENCY_REPEATS})",
    )
    return parser.parse_args(argv)


def _default_out(moment: datetime.datetime) -> Path:
    return DEFAULT_OUT_DIR / f"evidence-{moment.strftime('%Y%m%dT%H%M%SZ')}.json"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    moment = datetime.datetime.now(datetime.UTC)

    try:
        # Both refusals run before anything is read, let alone observed.
        assert_freeze_unmoved()
        signing_key = signing_key_from_env()

        settings = (
            Settings(
                database_url=args.database_url,
                pgbouncer_url=args.database_url,
                scheduler_jobstore_url=args.database_url,
            )
            if args.database_url
            else get_settings()
        )

        repo_root = Path(__file__).resolve().parents[1]
        corpus = scenarios.load_corpus(scenarios.corpus_path(repo_root))
        _log.info(
            "collecting %d scenario(s) x %d configuration(s) at %d repeat(s)",
            len(corpus.scenarios),
            len(protocol.CONFIGURATIONS),
            args.repeats,
        )

        batch, world = asyncio.run(collect(settings=settings, corpus=corpus, moment=moment, repeats=args.repeats))
        sealed = evidence.build(batch, signing_key=signing_key)
        out = write_artefact(
            _stamp_provenance(sealed.document, settings=settings, world=world),
            signing_key=signing_key,
            out=Path(args.out) if args.out else _default_out(moment),
        )
    except (RunRefused, scenarios.CorpusInvalid, protocol.ProtocolInvalidated, harness.BatchInvalidated) as exc:
        # Refusals are the normal way this exits when something is wrong, so they
        # print as one actionable line rather than a traceback.
        print(f"workspace evaluation refused: {exc}", file=sys.stderr)
        return 1

    for result in batch.results:
        _log.info(
            "%s: mean recall %.4f, %d safety failure(s), %d errored",
            result.configuration,
            result.mean_recall,
            len(result.safety_failures),
            len(result.errored_scenarios),
        )
    print(f"evidence written to {out}")
    print("This artefact records observations and no decision; interpreting it is a separate, accountable act.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
