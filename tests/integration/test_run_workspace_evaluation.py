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
from contextplane.workspaces.audience import RESOLVER_EXPLICIT
from contextplane.workspaces.schemas.task_memory import ROLE_CONTRIBUTOR
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


async def _seed_corpus_world(
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus, *, limit: int | None = None
) -> None:
    """Stand up exactly the tasks, grants and checkpoints the corpus names.

    Content is the scenario's own description, which is the only prose the
    frozen corpus carries. That is enough to prove the runner end to end and is
    deliberately not enough to be a measurement — see this module's docstring.
    """
    entries = corpus.scenarios[:limit] if limit is not None else corpus.scenarios
    tenants: set[uuid.UUID] = set()
    seeded_tasks: set[tuple[uuid.UUID, uuid.UUID]] = set()
    # Sequence and predecessor together, because the chain is append-only and
    # the database enforces it: sequence 1 has no predecessor and every later
    # one must name the checkpoint before it.
    sequences: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    previous: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}

    for scenario in entries:
        tenant_id = uuid.UUID(scenario.tenant_id)
        if tenant_id not in tenants:
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n) "
                        "ON CONFLICT (tenant_id) DO NOTHING"
                    ),
                    {"t": tenant_id, "s": f"eval-{tenant_id.hex[:8]}", "n": "evaluation corpus"},
                )
            tenants.add(tenant_id)

        tasks = sorted(scenario.facts.permitted_task_ids or ())
        if not tasks:
            continue
        home = uuid.UUID(tasks[0])

        async with factory() as session, session.begin():
            for raw_task in tasks:
                task_id = uuid.UUID(raw_task)
                if (tenant_id, task_id) in seeded_tasks:
                    continue
                # Raw insert with a conflict clause rather than the service
                # helper: the container is shared across this module's tests, so
                # a second seed of an overlapping corpus slice must be a no-op
                # rather than a unique-violation.
                await session.execute(
                    text(
                        """
                        INSERT INTO task_participant_grants
                            (tenant_id, task_id, actor_id, role, granted_by, granted_at,
                             expires_at, resolver_version)
                        VALUES (:tid, :task, :actor, :role, 'agent-owner', :granted, NULL, :resolver)
                        ON CONFLICT (tenant_id, task_id, actor_id) DO NOTHING
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "task": task_id,
                        "actor": scenario.actor_id,
                        "role": ROLE_CONTRIBUTOR,
                        "granted": _NOW - _HOUR,
                        "resolver": RESOLVER_EXPLICIT,
                    },
                )
                seeded_tasks.add((tenant_id, task_id))

            for key in scenario.relevant_item_keys:
                try:
                    checkpoint_id = uuid.UUID(key)
                except ValueError:
                    continue
                # Required facts live on the last permitted task, everything
                # else on the first: the cross-task scenarios need their answer
                # somewhere other than where the caller is working.
                task_id = uuid.UUID(tasks[-1]) if key in scenario.required_item_keys else home
                slot = (tenant_id, task_id)
                sequences[slot] = sequences.get(slot, 0) + 1
                await session.execute(
                    text(
                        """
                        INSERT INTO task_checkpoints
                            (checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, evidence,
                             next_action, author, recorded_at, retention_policy, digest)
                        VALUES (:cid, :tid, :task, :seq, :prev, :goal, '[]'::jsonb,
                                'keep going', :author, :rec, 'standard', :digest)
                        ON CONFLICT (checkpoint_id) DO NOTHING
                        """
                    ),
                    {
                        "cid": checkpoint_id,
                        "tid": tenant_id,
                        "task": task_id,
                        "seq": sequences[slot],
                        "prev": previous.get(slot),
                        "goal": scenario.description,
                        "author": scenario.actor_id,
                        "rec": _NOW,
                        "digest": checkpoint_id.hex[:16],
                    },
                )
                previous[slot] = checkpoint_id


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
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus
) -> None:
    """Same guarantee the harness's own suite pins, re-checked through the
    runner's source: a checkpoint outside the caller's tasks is not a candidate,
    so it is never scored."""
    await _seed_corpus_world(factory, corpus, limit=2)
    scenario = corpus.scenarios[0]
    source = runner.DatabaseWorkspaceSource(factory, moment=_NOW)

    mine = await source.authorized_candidates(scenario)
    assert mine, "the seeded world should give this caller candidates"

    # The boundary is the actor's *grants*, not the scenario's declared
    # permitted set — and in a single seeded world those differ, because every
    # scenario in this corpus shares one tenant and one actor. That divergence
    # is a property of the corpus rather than of the reads, and it is recorded
    # in the run notes; what this test pins is the boundary the database
    # enforces.
    granted = await _granted_tasks(factory, scenario.tenant_id, scenario.actor_id)
    for candidate in mine:
        assert str(candidate.item.payload["task_id"]) in granted

    outsider = scenarios.Scenario(**{**scenario.__dict__, "actor_id": "agent-nobody", "scenario_id": "OUTSIDER"})
    assert await source.authorized_candidates(outsider) == ()


# --- the success path ----------------------------------------------------------


async def test_a_complete_run_writes_one_signed_artefact_naming_its_embedder(
    factory: async_sessionmaker[AsyncSession],
    corpus: scenarios.Corpus,
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

    batch, world = await runner.collect(settings=settings, corpus=corpus, moment=_NOW, repeats=1)

    assert [r.configuration for r in batch.results] == list(protocol.CONFIGURATIONS)
    for result in batch.results:
        assert len(result.runs) == len(corpus.scenarios)
    assert world["present"] == world["required_checkpoints"]

    from contextplane.context.evaluation import evidence

    sealed = evidence.build(batch, signing_key=_KEY.encode())
    stamped = runner._stamp_provenance(sealed.document, settings=settings, world=world)
    out = runner.write_artefact(stamped, signing_key=_KEY.encode(), out=tmp_path / "evidence.json")

    written = json.loads(out.read_text(encoding="utf-8"))
    provenance = written["evidence"]["run_provenance"]
    assert provenance["embedding_provider"] == "stub"
    assert provenance["embedding_model"] == settings.embedding_model
    assert provenance["corpus_world"]["present"] == world["required_checkpoints"]


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
    factory: async_sessionmaker[AsyncSession], corpus: scenarios.Corpus, pg_container: str
) -> None:
    """Human review has no owner in this work, so the sample is captured now and
    the gap is recorded rather than silent."""
    await _seed_corpus_world(factory, corpus)
    batch, _ = await runner.collect(settings=_settings(pg_container), corpus=corpus, moment=_NOW, repeats=1)
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
