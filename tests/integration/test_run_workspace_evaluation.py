"""The evaluation runner refuses before it observes, and stamps what it ran.

A campaign runner is mostly gates, and a gate that has never been driven is a
comment. Each test below drives one refusal against a real database, plus the
success path end to end: three configurations over the frozen corpus, a signed
artefact on disk, and the resolved embedder named inside the signature.

The world these tests run against is **seeded here, by the test**, from the
frozen corpus's own declarations. That is legitimate for proving the runner
works and is *not* a campaign: the numbers it produces measure a world this file
invented, so nothing here writes an artefact anybody should read as a result.
The runner's corpus-world preflight is precisely what stops a real run being
taken against a world nobody stood up.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.config import Settings
from contextplane.context.evaluation import protocol, scenarios
from scripts import run_workspace_evaluation as runner

pytestmark = pytest.mark.asyncio

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)
_KEY = "integration-signing-key"

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def corpus() -> scenarios.Corpus:
    return scenarios.load_corpus(scenarios.corpus_path(_REPO_ROOT))


@pytest.fixture
def world() -> scenarios.World:
    return scenarios.load_world(scenarios.world_path(_REPO_ROOT))


async def _seed_corpus_world(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus, *, limit: int | None = None
) -> None:
    """Materialise the pinned world through the runner's own writer.

    The test invents nothing any more. The world is a committed, digested
    fixture, so the honest way to stand it up is the path a campaign uses --
    which also means a defect in the materialiser fails here rather than during
    collection. `limit` trims the corpus to prove the partial-world refusal.
    """
    trimmed = corpus if limit is None else dataclasses.replace(corpus, scenarios=corpus.scenarios[:limit])
    world = scenarios.load_world(scenarios.world_path(_REPO_ROOT))
    await runner.materialise_world(factory, trimmed, world, moment=_NOW)


async def _granted_tasks(factory: async_sessionmaker[AsyncSession], tenant_id: str, actor_id: str) -> set[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT task_id FROM task_participant_grants " "WHERE tenant_id = :t AND actor_id = :a"),
                {"t": uuid.UUID(tenant_id), "a": actor_id},
            )
        ).scalars()
    return {str(r) for r in rows}


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        pgbouncer_url=database_url,
        scheduler_jobstore_url=database_url,
        embedding_provider="stub",
    )


# --- the refusals --------------------------------------------------------------


def test_the_frozen_digests_match_the_tree_being_run() -> None:
    """The runner's pinned values are the ones this tree actually produces.

    If this fails, either the protocol moved or the pins are stale — and the two
    need opposite responses, so the runner must never quietly accept whichever
    it finds.
    """
    frozen = protocol.freeze()
    assert frozen.judge_digest == runner.FROZEN_JUDGE_DIGEST
    assert frozen.protocol_digest == runner.FROZEN_PROTOCOL_DIGEST
    assert frozen.freeze_digest() == runner.FROZEN_FREEZE_DIGEST
    assert runner.assert_freeze_unmoved().freeze_digest() == frozen.freeze_digest()


def test_a_moved_protocol_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "FROZEN_JUDGE_DIGEST", "0" * 64)
    with pytest.raises(runner.RunRefused, match="judge_digest"):
        runner.assert_freeze_unmoved()


def test_a_moved_threshold_is_refused_even_though_the_scorer_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case a scorer-only digest would miss: `judge.py` is byte-identical
    and the numbers still mean something different."""
    monkeypatch.setattr(runner, "FROZEN_PROTOCOL_DIGEST", "0" * 64)
    with pytest.raises(runner.RunRefused, match="protocol_digest"):
        runner.assert_freeze_unmoved()


def test_an_absent_signing_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(runner.SIGNING_KEY_ENV, raising=False)
    with pytest.raises(runner.RunRefused, match="not evidence"):
        runner.signing_key_from_env()


def test_a_blank_signing_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(runner.SIGNING_KEY_ENV, "   ")
    with pytest.raises(runner.RunRefused, match="not evidence"):
        runner.signing_key_from_env()


async def test_an_empty_database_is_refused_rather_than_scored_as_a_finding(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus
) -> None:
    """The refusal that matters most.

    Against a database without the corpus, all three configurations score zero,
    which is indistinguishable in the output from "task memory does not help" —
    the one conclusion that stops the work.
    """
    with pytest.raises(runner.RunRefused, match="indistinguishable"):
        await runner.assert_corpus_world_present(factory, corpus)


async def test_a_partially_seeded_database_is_refused_and_says_how_short_it_is(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus
) -> None:
    """A half-present world is the more dangerous shape: it produces a
    plausible mid-range number rather than an obviously wrong zero."""
    await _seed_corpus_world(factory, corpus, limit=5)
    with pytest.raises(runner.RunRefused, match="missing"):
        await runner.assert_corpus_world_present(factory, corpus)


async def test_the_preflight_passes_once_the_world_is_present(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus
) -> None:
    await _seed_corpus_world(factory, corpus)
    counted = await runner.assert_corpus_world_present(factory, corpus)
    assert counted["present"] == counted["required_checkpoints"]
    assert counted["required_checkpoints"] > 0


# --- the authorized-set boundary, over the real database -----------------------


async def test_the_runners_source_resolves_the_audience_before_generating_candidates(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus, world: scenarios.World
) -> None:
    """Same guarantee the harness's own suite pins, re-checked through the
    runner's source: a checkpoint outside the caller's tasks is not a candidate,
    so it is never scored."""
    await _seed_corpus_world(factory, corpus, limit=2)
    scenario = corpus.scenarios[0]
    source = runner.DatabaseWorkspaceSource(factory, world, moment=_NOW)

    mine = await source.authorized_candidates(scenario)
    assert mine, "the seeded world should give this caller candidates"

    # The boundary is the actor's grants, and the actor is the world's, not the
    # corpus's. The corpus names one actor for all forty scenarios; the world
    # gives each its own, which is the whole reason it exists — asking as the
    # shared actor would hand every scenario every other scenario's items and
    # make the audience check decorative. So the grants read here are the ones
    # the source itself resolves against.
    granted = await _granted_tasks(factory, scenario.tenant_id, world.entries[scenario.scenario_id].actor_id)
    for candidate in mine:
        assert str(candidate.item.payload["task_id"]) in granted

    # An actor with no grants sees nothing. Substituted through the world rather
    # than the scenario, because the world is where the source reads the asker
    # from — patching the corpus's actor would leave the read unchanged and the
    # test passing for no reason.
    nobody = dataclasses.replace(
        world,
        entries={
            **world.entries,
            scenario.scenario_id: dataclasses.replace(world.entries[scenario.scenario_id], actor_id="agent-nobody"),
        },
    )
    stranger = runner.DatabaseWorkspaceSource(factory, nobody, moment=_NOW)
    assert await stranger.authorized_candidates(scenario) == ()

    # And a scenario the world never covered refuses rather than guessing one.
    unpinned = dataclasses.replace(scenario, scenario_id="OUTSIDER")
    with pytest.raises(runner.RunRefused, match="OUTSIDER"):
        await source.authorized_candidates(unpinned)


# --- the success path ----------------------------------------------------------


async def test_a_complete_run_writes_one_signed_artefact_naming_its_embedder(
    factory: async_sessionmaker[AsyncSession],
    corpus: scenarios.Corpus,
    world: scenarios.World,
    pg_container: str,
    tmp_path: Path,
) -> None:
    """End to end: three configurations, the full corpus, one sealed file.

    `repeats=1` here rather than the protocol's 5 — this proves the runner, and
    timing five resolutions per scenario would quintuple the test's cost to
    measure a fixture. A real campaign uses the protocol value, which is the
    argument's default.
    """
    await _seed_corpus_world(factory, corpus)
    settings = _settings(pg_container)

    batch, present = await runner.collect(settings=settings, corpus=corpus, world=world, moment=_NOW, repeats=1)

    assert [r.configuration for r in batch.results] == list(protocol.CONFIGURATIONS)
    for result in batch.results:
        assert len(result.runs) == len(corpus.scenarios)
    assert present["present"] == present["required_checkpoints"]

    from contextplane.context.evaluation import evidence

    sealed = evidence.build(batch, signing_key=_KEY.encode())
    stamped = runner._stamp_provenance(
        sealed.document, settings=settings, present=present, corpus=corpus, world=world
    )
    out = runner.write_artefact(stamped, signing_key=_KEY.encode(), out=tmp_path / "evidence.json")

    written = json.loads(out.read_text(encoding="utf-8"))
    provenance = written["evidence"]["run_provenance"]
    assert provenance["embedding_provider"] == "stub"
    assert provenance["embedding_model"] == settings.embedding_model
    assert provenance["corpus_world"]["present"] == present["required_checkpoints"]
    assert provenance["corpus_digest"] == protocol.FROZEN_CORPUS_DIGEST
    assert provenance["world_digest"] == protocol.FROZEN_WORLD_DIGEST


async def test_the_written_artefact_verifies_and_detects_an_edit(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus, tmp_path: Path
) -> None:
    """The seal covers the provenance block too, which is the whole reason the
    runner re-signs rather than reusing the library's signature."""
    import hashlib
    import hmac

    document = {"evidence_version": 1, "run_provenance": {"embedding_provider": "stub"}}
    out = runner.write_artefact(document, signing_key=_KEY.encode(), out=tmp_path / "e.json")
    written = json.loads(out.read_text(encoding="utf-8"))

    canonical = json.dumps(written["evidence"], sort_keys=True, separators=(",", ":"), default=str)
    assert hashlib.sha256(canonical.encode()).hexdigest() == written["digest"]
    assert hmac.new(_KEY.encode(), written["digest"].encode(), hashlib.sha256).hexdigest() == written["signature"]

    written["evidence"]["run_provenance"]["embedding_provider"] = "onnx"
    edited = json.dumps(written["evidence"], sort_keys=True, separators=(",", ":"), default=str)
    assert hashlib.sha256(edited.encode()).hexdigest() != written["digest"]


async def test_the_run_records_the_human_risk_sample_for_later_review(
    factory: async_sessionmaker[AsyncSession],
    corpus: scenarios.Corpus,
    world: scenarios.World,
    pg_container: str,
) -> None:
    """Human review has no owner in this work, so the sample is captured now and
    the gap is recorded rather than silent."""
    await _seed_corpus_world(factory, corpus)
    batch, _ = await runner.collect(
        settings=_settings(pg_container), corpus=corpus, world=world, moment=_NOW, repeats=1
    )
    assert len(batch.human_risk_sample) == protocol.HUMAN_RISK_SAMPLE_SIZE
    known = {s.scenario_id for s in corpus.scenarios}
    assert set(batch.human_risk_sample) <= known


def test_the_default_output_path_is_inside_the_ignored_run_directory() -> None:
    """The artefact must not land in the planning workspace, and must not be
    committed to the product repository either."""
    out = runner._default_out(_NOW)
    assert runner.DEFAULT_OUT_DIR in out.parents
    assert ".context" not in str(out)
    ignored = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "\nrun/\n" in ignored
