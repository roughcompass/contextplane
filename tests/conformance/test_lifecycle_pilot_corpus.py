"""The frozen pilot scenarios are a corpus, and a corpus that cannot be empty.

Six real delivery changes were preserved as fixtures so the lifecycle contract
is pinned by situations that occurred rather than by situations a test author
found convenient. This module checks the corpus is what it claims to be, and it
is deliberately structural: the scenarios are data, and data that nothing
validates decays into documentation.

**The floor is asserted separately from the per-scenario checks, and that
separation is the point.** A suite that only parametrizes over discovered files
reports success against no files at all. Move the directory, and every scenario
stops running while the gate stays green -- the failure announces itself as
nothing at all. So the count is its own assertion, the discovery helper refuses
an under-populated directory rather than returning a short list, and one test
proves that refusal against an empty directory instead of trusting that today's
directory happens to be full.

**Non-vacuity is checked at the corpus level, not per scenario.** Requiring
every scenario to carry a refusal would mean inventing refusals for the changes
that did not hit one. Requiring none would let six untroubled changes stand as a
corpus that pins nothing the happy path does not already pin. So an individual
scenario may record `"kind": "none"` -- explicitly, as a claim somebody made --
while the corpus as a whole must contain a real refusal and a real degradation.

**Trust labels are checked against the code's contract, not against
themselves.** Canonical items carry no trust metadata and every other block's
items must carry it. A scenario claiming a trust label on canonical, or omitting
one anywhere else, is describing a registry this one is not, and it fails here
rather than misleading whoever reads the corpus to learn what the blocks return.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

from contextplane.context.schemas.envelope import BLOCK_CANONICAL, BLOCK_NAMES, BLOCK_STATES
from contextplane.context.schemas.trust import TRUST_LEVELS

_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "lifecycle_context_pilot"

#: The contract's floor. Five is the number the pilot had to reach for the
#: corpus to be evidence at all; six were approved, so a corpus that has fallen
#: to five has lost one to a later admission review and is worth noticing even
#: though it still passes.
_MINIMUM_SCENARIOS = 5

#: The lifecycle points a change can cover. A closed set, because a scenario
#: that invented a sixth point would be describing a lifecycle the pilot did not
#: run, and the whole premise of the profile surface is that stage names come
#: from the caller's system rather than from here.
_STAGES = (
    "implementation",
    "review_test",
    "security_review",
    "deployment",
    "post_deployment",
)

#: A change has to use context before code exists and again where the change is
#: verified or observed. One point on each side of that line is what makes a
#: scenario evidence about a lifecycle rather than about a single request.
_PRE_CODE = frozenset({"implementation"})
_VERIFICATION_OR_LATER = frozenset({"review_test", "security_review", "deployment", "post_deployment"})

#: What a refusal-or-degradation entry may be. `none` is legal and explicit;
#: the rest name a way the run did not go smoothly. `refusal` and `degradation`
#: are the two the corpus is required to contain, because they are the two the
#: contract's own failure paths produce.
_EVENT_KINDS = frozenset({"none", "refusal", "degradation", "interruption", "incident", "usability_finding"})

#: Shapes that must never reach a fixture. Planning task identifiers of any
#: prefix, and the transcript-ish keys that would mean somebody pasted a session
#: in rather than recording what happened. Checked here as well as by the
#: repository's own reference gate because that gate reads shipped prose, and a
#: fixture's risk is re-identification rather than an unresolvable citation.
_FORBIDDEN_IN_FIXTURES = (
    re.compile(r"\b[A-Z]{2,5}(-P\d+R?)?-T\d+[a-z]?\b"),
    re.compile(r"\btranscript\b", re.IGNORECASE),
    re.compile(r"\bprompt\b", re.IGNORECASE),
)


def _scenario_paths(directory: pathlib.Path = _CORPUS) -> list[pathlib.Path]:
    """Every scenario file, refusing a corpus that has fallen below the floor.

    Raising rather than returning a short list is what keeps an empty directory
    from being indistinguishable from a directory nobody looked at. A caller
    that got `[]` back would parametrize over nothing and pass.

    The directory is a parameter so the refusal can be exercised against an
    empty one. A guard whose only evidence is that the real corpus happens to be
    full is a guard nobody has run.
    """
    paths = sorted(directory.glob("*.json"))
    if len(paths) < _MINIMUM_SCENARIOS:
        raise AssertionError(
            f"the pilot corpus holds {len(paths)} scenario file(s) at {directory}, "
            f"below the floor of {_MINIMUM_SCENARIOS}; a corpus this small is not evidence, "
            "and it must be reported short rather than topped up with an invented scenario"
        )
    return paths


def _load(path: pathlib.Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


_PATHS = _scenario_paths()
_SCENARIOS = [(path.stem, _load(path)) for path in _PATHS]
_IDS = [name for name, _ in _SCENARIOS]


def test_an_empty_corpus_directory_fails_rather_than_passing_vacuously(tmp_path: pathlib.Path) -> None:
    """The guard is proved against an empty directory, not assumed.

    Every other test here runs over the corpus that exists. This one runs over a
    corpus that does not, because "the fixtures are checked" and "the fixtures
    were found" are different claims and only the second one survives somebody
    renaming the directory.
    """
    empty = tmp_path / "lifecycle_context_pilot"
    empty.mkdir()

    with pytest.raises(AssertionError, match="below the floor"):
        _scenario_paths(empty)

    # And a directory holding some scenarios but not enough is refused too: the
    # failure mode this guards is a corpus that shrank, which looks exactly like
    # a healthy one to anything that only checks for emptiness.
    for index in range(_MINIMUM_SCENARIOS - 1):
        (empty / f"partial_{index}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError, match="below the floor"):
        _scenario_paths(empty)


def test_the_corpus_meets_its_floor() -> None:
    """The count itself, asserted independently of anything parametrized over it."""
    assert (
        len(_PATHS) >= _MINIMUM_SCENARIOS
    ), f"{len(_PATHS)} scenario file(s) under {_CORPUS}; the floor is {_MINIMUM_SCENARIOS}"


def test_every_discovered_file_is_actually_checked() -> None:
    """The parametrized checks cover the whole corpus, not a prefix of it.

    Discovery and parametrization are two lists that must agree. They are built
    from the same call today; this fails the day somebody filters one of them
    and leaves scenarios silently unchecked.
    """
    assert len(_SCENARIOS) == len(_PATHS)
    assert sorted(_IDS) == sorted(path.stem for path in _PATHS)
    assert len(set(_IDS)) == len(_IDS), "two scenarios share a name"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_records_all_five_required_sections(name: str, scenario: dict[str, Any]) -> None:
    """Each scenario carries coverage, trust, learning, refusal, and counts."""
    for field in (
        "scenario",
        "summary",
        "work",
        "stages_covered",
        "expected_source_coverage",
        "trust_labels",
        "prior_learning",
        "refusal_or_degradation",
        "cardinality",
    ):
        assert field in scenario, f"{name} is missing {field!r}"

    assert scenario["scenario"] == name, "the scenario's own name disagrees with its file name"
    assert scenario["summary"].strip(), f"{name} has an empty summary"

    work = scenario["work"]
    for field in ("repository", "team", "work_item", "work_type"):
        assert work.get(field), f"{name} records no {field}"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_covers_a_pre_code_and_a_later_point(name: str, scenario: dict[str, Any]) -> None:
    """Context was used before code existed and again where the change was checked."""
    stages = scenario["stages_covered"]
    assert stages, f"{name} covers no lifecycle point at all"
    unknown = set(stages) - set(_STAGES)
    assert not unknown, f"{name} names lifecycle point(s) the pilot did not run: {sorted(unknown)}"
    assert len(set(stages)) == len(stages), f"{name} lists a lifecycle point twice"

    assert _PRE_CODE & set(stages), f"{name} used no context before code existed"
    assert _VERIFICATION_OR_LATER & set(stages), f"{name} used no context at verification or later"

    # A point the change did not cover has to say why, in a key naming that
    # exact point. Absence with no reason is indistinguishable from a scenario
    # somebody trimmed to make a count work, and one blanket reason covering
    # several omissions lets the weakest of them ride on the strongest.
    for stage in sorted(set(_STAGES) - set(stages)):
        reason = scenario.get(f"no_{stage}_because", "")
        assert reason.strip(), f"{name} omits {stage} without recording why (expected a no_{stage}_because key)"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_expects_every_block_in_a_legal_state(name: str, scenario: dict[str, Any]) -> None:
    """Coverage is recorded for all four blocks, in states the envelope defines."""
    coverage = scenario["expected_source_coverage"]
    assert set(coverage) == set(
        BLOCK_NAMES
    ), f"{name} records coverage for {sorted(coverage)}, and the blocks are {sorted(BLOCK_NAMES)}"
    for block, state in coverage.items():
        assert state in BLOCK_STATES, f"{name} expects {block} in state {state!r}, which is not a block state"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_labels_trust_the_way_the_blocks_do(name: str, scenario: dict[str, Any]) -> None:
    """Canonical carries no trust; every other block carries a legal level.

    This is the corpus's tie to the code rather than to itself. Canonical items
    are built by a constructor that sets trust to `None` by contract, and every
    other block goes through one that refuses an item without it.
    """
    labels = scenario["trust_labels"]
    assert set(labels) == set(
        BLOCK_NAMES
    ), f"{name} labels trust for {sorted(labels)}, and the blocks are {sorted(BLOCK_NAMES)}"

    assert (
        labels[BLOCK_CANONICAL] is None
    ), f"{name} claims canonical items carry trust {labels[BLOCK_CANONICAL]!r}; they carry none by contract"
    for block, level in labels.items():
        if block == BLOCK_CANONICAL:
            continue
        assert level in TRUST_LEVELS, f"{name} labels {block} {level!r}, which is not a trust level"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_states_whether_it_reused_prior_learning(name: str, scenario: dict[str, Any]) -> None:
    """Reuse is claimed or denied explicitly, and a claim carries its instances."""
    learning = scenario["prior_learning"]
    assert isinstance(learning.get("retrieved"), bool), f"{name} does not say whether it retrieved prior learning"
    assert learning.get("why", "").strip(), f"{name} gives no reason for its retrieval state"

    instances = learning["instances"]
    if learning["retrieved"]:
        assert instances, f"{name} claims retrieval and lists no instance"
    else:
        assert not instances, f"{name} denies retrieval and lists one anyway"

    for instance in instances:
        assert (
            instance.get("from_scenario") in _IDS
        ), f"{name} retrieves learning from {instance.get('from_scenario')!r}, which is not in this corpus"
        assert instance.get("subject", "").strip(), f"{name} retrieves learning with no subject"
        assert instance.get("confirmation", "").strip(), f"{name} retrieves learning nobody confirmed"
        assert isinstance(instance.get("judged_useful"), bool), f"{name} does not say whether the learning helped"
        if not instance["judged_useful"]:
            assert instance.get("why_not_useful", "").strip(), (
                f"{name} judged retrieved learning unhelpful without saying why, "
                "which is the half of the record that would change the selection"
            )


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_accounts_for_refusals_explicitly(name: str, scenario: dict[str, Any]) -> None:
    """Every scenario says what went wrong, including the ones where nothing did."""
    events = scenario["refusal_or_degradation"]
    assert events, f"{name} records no refusal entry at all, not even the absent case"

    for event in events:
        kind = event.get("kind")
        assert kind in _EVENT_KINDS, f"{name} records event kind {kind!r}"
        assert event.get("what_happened", "").strip(), f"{name} records a {kind} with no account of it"
        if kind == "none":
            assert len(events) == 1, f"{name} records 'none' alongside a real event"
            assert event.get("surface") is None, f"{name} records 'none' against a surface"
        else:
            assert event.get("surface", "").strip(), f"{name} records a {kind} against no surface"
            assert event.get(
                "detected_by", ""
            ).strip(), f"{name} records a {kind} nothing detected; how it surfaced is the finding"


@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
def test_a_scenario_counts_what_it_produced(name: str, scenario: dict[str, Any]) -> None:
    """Cardinality is present, non-negative, and consistent with the narrative."""
    counts = scenario["cardinality"]
    for field in (
        "receipts",
        "handoffs",
        "commits",
        "workflow_runs",
        "work_items",
        "deployments",
        "incidents",
        "outcomes_joined",
        "outcomes_unjoined",
    ):
        value = counts.get(field)
        assert isinstance(value, int) and value >= 0, f"{name} counts {field} as {value!r}"

    assert counts["receipts"] >= 1, f"{name} used context and left no receipt"
    assert counts["work_items"] >= 1, f"{name} names no external work item"

    # A change that deployed has an outcome that joined; a change that recorded
    # unjoined outcomes has to have recorded the degradation that produced them.
    if counts["outcomes_unjoined"]:
        kinds = {event["kind"] for event in scenario["refusal_or_degradation"]}
        assert (
            "degradation" in kinds
        ), f"{name} counts {counts['outcomes_unjoined']} unjoined outcome(s) and records no degradation"


def test_the_corpus_spans_more_than_one_team() -> None:
    """Five changes on one team would not be evidence about handoff at all."""
    teams = {scenario["work"]["team"] for _, scenario in _SCENARIOS}
    assert len(teams) >= 2, f"the corpus covers one team ({sorted(teams)}); the contract needs two or more"


def test_the_corpus_is_not_a_parade_of_happy_paths() -> None:
    """A real refusal and a real degradation are both present.

    Checked across the corpus rather than within a scenario. This is what makes
    the fixtures non-vacuous: a corpus where nothing was ever refused pins only
    the paths the happy-path tests already cover, and would pass unchanged if
    every refusal in the system were deleted.
    """
    kinds = {event["kind"] for _, scenario in _SCENARIOS for event in scenario["refusal_or_degradation"]}
    assert "refusal" in kinds, "no scenario records a refusal; the corpus pins no failure path"
    assert (
        "degradation" in kinds
    ), "no scenario records a degradation; a degraded answer is not the same as an empty one"


def test_the_corpus_records_learning_that_did_not_help() -> None:
    """Reuse is recorded with its honest denominator.

    A corpus in which every retrieval was useful would report the selection as
    working, and the instance that mattered most in the pilot is the one where
    every dimension matched and the learning was still wrong.
    """
    judged = [instance for _, scenario in _SCENARIOS for instance in scenario["prior_learning"]["instances"]]
    assert judged, "no scenario retrieved prior learning; the reuse path is unpinned"
    assert any(instance["judged_useful"] for instance in judged), "no retrieval in the corpus ever helped"
    assert not all(instance["judged_useful"] for instance in judged), (
        "every retrieval in the corpus is recorded as useful, which is a corpus that cannot "
        "show the selection choosing wrongly"
    )


def test_reuse_points_backwards_to_a_scenario_that_could_have_produced_it() -> None:
    """No scenario retrieves learning from itself."""
    for name, scenario in _SCENARIOS:
        for instance in scenario["prior_learning"]["instances"]:
            assert instance["from_scenario"] != name, f"{name} retrieves learning from itself"


@pytest.mark.parametrize(("name", "path"), list(zip(_IDS, _PATHS, strict=True)), ids=_IDS)
def test_a_scenario_carries_no_planning_identifier_or_transcript(name: str, path: pathlib.Path) -> None:
    """Admission review, as an assertion rather than a step somebody remembers.

    The risk a fixture carries is re-identification: a corpus that names the
    pilot's own change identifiers can be mapped back to the participants it was
    anonymized to protect.
    """
    text = path.read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_IN_FIXTURES:
        found = pattern.search(text)
        assert found is None, f"{name} carries {found.group(0)!r}, which must not reach a frozen fixture"


def test_the_corpus_discloses_every_outcome_that_never_joined() -> None:
    """The unjoined envelopes are counted here rather than quietly dropped.

    They were left in the ledger rather than deleted, and a corpus that recorded
    only the joined ones would describe a pilot whose outcomes all arrived.
    """
    unjoined = sum(scenario["cardinality"]["outcomes_unjoined"] for _, scenario in _SCENARIOS)
    assert unjoined > 0, (
        "no scenario carries an unjoined outcome, though the join failure is the "
        "finding the pilot spent two days not noticing"
    )
