"""Ground truth for extraction, and the arithmetic that scores against it.

Extraction has been tested for shape — the output validates, the containment
checks fire, the conformance gate rejects what it should — and never for whether
what it extracts is *right*. Those are different questions, and only the second
one tells an operator whether the claims arriving in their graph are worth
having.

`eval/fixtures/extraction_ground_truth.json` is thirty transcript excerpts with
the claims a correct extraction yields. It is frozen once measured, in the same
way `search_questions.json` is: a fixture edited after the fact measures nothing,
because the number it produces can always be improved by moving the target.

**Three things here, and they are deliberately separate.**

The *fixture contract* runs always and needs neither a database nor a provider.
It asserts the file is well formed against the shipped ontology — every predicate
permitted, every value the type its predicate declares, every cited event present
in its own case. A label naming a predicate that does not exist would make a
correct extraction score as an error forever, and nothing else would notice.

The *scoring arithmetic* also runs always, against synthetic extractions rather
than a model. Precision and recall are three lines each and both are easy to get
subtly wrong; measuring a real provider with arithmetic nobody checked produces a
number that is worse than no number, because it looks like a measurement.

The *measurement* is opt-in and needs a real provider, following the same rule as
`test_extraction_live_provider.py`: no key, no run. It is deliberately not run
against `local-rules`. That provider's own module says a benchmark against it
measures the regexes, and a precision figure derived from the demo patterns would
be a fact about this repository's regular expressions filed under a heading that
reads "extraction quality".

**Report first, threshold later.** The measurement asserts nothing about the
figures. What to demand of extraction is a decision somebody makes after seeing
what it currently does, and a threshold chosen at the same moment as the first
measurement is a threshold chosen to pass.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
from collections.abc import Iterable, Sequence
from typing import Any

import pytest

from contextplane.extraction.containment import CandidateRefused, assert_not_directive
from contextplane.extraction.provider import CandidateClaim
from contextplane.extraction.strategies import OBSERVATION
from contextplane.service.memory.claim_ontology import CARDINALITY_SINGLE, ONTOLOGY

_FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "extraction_ground_truth.json"

#: Asserted rather than derived from the file. A count read out of the fixture
#: agrees with it by construction and would keep agreeing while cases silently
#: disappeared.
_CASE_COUNT = 30

_VALUE_TYPES = {seed.value: seed.value_type for seed in ONTOLOGY}
_CARDINALITY = {seed.value: seed.value_cardinality for seed in ONTOLOGY}

_API_KEY = (os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _load() -> list[dict[str, Any]]:
    document = json.loads(_FIXTURE.read_text())
    cases: list[dict[str, Any]] = document["cases"]
    assert len(cases) == _CASE_COUNT, f"expected {_CASE_COUNT} ground-truth cases, found {len(cases)}"
    assert document["strategy_id"] == OBSERVATION.strategy_id
    return cases


# --- how a claim is compared ----------------------------------------------------
#
# The ontology types a value but does not constrain its shape, so two careful
# labellers can write `POST /v1/sends` and `/v1/sends` for one operation and
# disagree about nothing. That is a real limit of this fixture and it is stated
# rather than papered over: comparison normalises case and whitespace and nothing
# else, so a differently-shaped value counts as a miss and shows up in the report
# as one. Hiding it behind a clever normaliser would move the disagreement out of
# the number and into a function nobody reads.


def _normalise(value: object) -> object:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    return value


def _key(subject: str, predicate: str, value: object) -> tuple[str, str, object]:
    return (_normalise(subject), predicate, _normalise(value))  # type: ignore[return-value]


def _expected_keys(case: dict[str, Any]) -> set[tuple[str, str, object]]:
    return {_key(c["subject_reference"], c["predicate"], c["value"]) for c in case["expected_claims"]}


def _extracted_keys(claims: Iterable[CandidateClaim]) -> set[tuple[str, str, object]]:
    return {_key(c.subject_reference, c.predicate, c.value) for c in claims}


# --- precision and recall -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Score:
    """What a run found, and what it should have found.

    Counts rather than ratios, so several cases combine by adding and a
    per-predicate breakdown is the same arithmetic over a subset.
    """

    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def precision(self) -> float | None:
        """Of what it claimed, how much was right. `None` when it claimed nothing.

        Not 1.0. An extractor that returns nothing at all has made no incorrect
        claims, and scoring that as perfect precision means the highest score on
        this metric belongs to a provider that is switched off.
        """
        predicted = self.true_positive + self.false_positive
        return self.true_positive / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        """Of what was there, how much it found. `None` when there was nothing.

        The negative cases have no expected claims by design, so recall is
        undefined for them rather than zero — a case that could not be recalled
        must not drag the recall figure down.
        """
        available = self.true_positive + self.false_negative
        return self.true_positive / available if available else None

    def __add__(self, other: Score) -> Score:
        return Score(
            true_positive=self.true_positive + other.true_positive,
            false_positive=self.false_positive + other.false_positive,
            false_negative=self.false_negative + other.false_negative,
        )


def score_case(*, expected: set[tuple[str, str, object]], extracted: set[tuple[str, str, object]]) -> Score:
    return Score(
        true_positive=len(expected & extracted),
        false_positive=len(extracted - expected),
        false_negative=len(expected - extracted),
    )


def score_by_predicate(
    *, expected: set[tuple[str, str, object]], extracted: set[tuple[str, str, object]]
) -> dict[str, Score]:
    """The same arithmetic, split by the predicate each claim names.

    Per-predicate because an aggregate hides the shape of the failure: an
    extractor that is excellent at ownership and hopeless at durations reports
    the same overall number as one that is mediocre at both, and only the first
    is worth shipping behind a predicate filter.
    """
    out: dict[str, Score] = {}
    for predicate in {key[1] for key in expected | extracted}:
        subset_expected = {k for k in expected if k[1] == predicate}
        subset_extracted = {k for k in extracted if k[1] == predicate}
        out[predicate] = score_case(expected=subset_expected, extracted=subset_extracted)
    return out


# --- the fixture contract -------------------------------------------------------


def test_the_fixture_holds_exactly_thirty_cases() -> None:
    """Frozen size. A fixture that can grow can be grown until the number looks
    better, which is the failure this whole file exists to avoid."""
    assert len(_load()) == _CASE_COUNT


def test_every_case_id_is_unique() -> None:
    ids = [case["case_id"] for case in _load()]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_every_labelled_predicate_is_one_the_strategy_may_emit(case: dict[str, Any]) -> None:
    """A label naming a predicate the strategy cannot produce would score a
    correct extraction as a miss, permanently and invisibly."""
    for claim in case["expected_claims"]:
        assert claim["predicate"] in OBSERVATION.permitted_predicates


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_every_labelled_value_is_the_type_its_predicate_declares(case: dict[str, Any]) -> None:
    """Units live in the type, so `900` and `"15 minutes"` are not the same label.
    A fixture that disagrees with the ontology teaches an extractor to be wrong."""
    for claim in case["expected_claims"]:
        declared = _VALUE_TYPES[claim["predicate"]]
        value = claim["value"]
        if declared in ("duration_seconds", "bytes", "decimal"):
            assert isinstance(value, int | float) and not isinstance(value, bool), f"{declared} must be numeric"
        elif declared == "boolean":
            assert isinstance(value, bool)
        elif declared == "url":
            assert isinstance(value, str) and value.startswith(("http://", "https://")), "a url must be absolute"
        elif declared == "timestamp_utc":
            assert isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value)
        else:
            assert isinstance(value, str) and value.strip()


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_no_case_asserts_a_single_valued_predicate_twice(case: dict[str, Any]) -> None:
    """Two values for a single-valued predicate is a contradiction, not ground
    truth, and no extraction could satisfy both."""
    seen: set[tuple[str, str]] = set()
    for claim in case["expected_claims"]:
        pair = (claim["subject_reference"], claim["predicate"])
        if _CARDINALITY[claim["predicate"]] == CARDINALITY_SINGLE:
            assert pair not in seen, f"{pair} asserted twice on a single-valued predicate"
        seen.add(pair)


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_every_claim_cites_an_event_the_case_contains(case: dict[str, Any]) -> None:
    """A claim citing an event that is not in the excerpt cannot be checked by a
    reader, which is the same standard the write path holds extractions to."""
    known = {event["event_id"] for event in case["events"]}
    for claim in case["expected_claims"]:
        assert claim["event_ids"], "a claim must cite at least one event"
        assert set(claim["event_ids"]) <= known


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_every_subject_and_entity_reference_names_an_entity_in_the_case(case: dict[str, Any]) -> None:
    references = {entity["reference"] for entity in case["entities"]}
    for claim in case["expected_claims"]:
        assert claim["subject_reference"] in references
        if _VALUE_TYPES[claim["predicate"]] == "entity_ref":
            assert claim["value"] in references, "a relationship must point at an entity the excerpt names"


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_every_labelled_subject_appears_verbatim_in_its_excerpt(case: dict[str, Any]) -> None:
    """A subject the excerpt never spells is a label no correct extraction reaches.

    The strategy tells a provider to use the reference "exactly as it appeared in
    the data" and never to invent one. An excerpt discussing `ledger-sync` in
    prose while the label says `service:ledger-sync` therefore scores every claim
    on that case as both a miss and an invention — and the resulting figure reads
    as a model that cannot extract, rather than as a fixture that cannot be
    satisfied. Eighteen of these thirty cases were in that state on first
    measurement; the catalog lookup each case now opens with is what fixed it.
    """
    text = "\n".join(event["body"] for event in case["events"])
    for claim in case["expected_claims"]:
        assert claim["subject_reference"] in text, (
            f"{case['case_id']}: labelled subject {claim['subject_reference']!r} never appears in the "
            "excerpt, so no correct extraction could produce it"
        )
        if _VALUE_TYPES[claim["predicate"]] == "entity_ref":
            assert claim["value"] in text


def test_every_case_opens_with_the_catalog_lookup_that_names_its_entities() -> None:
    """Uniform across all thirty, including the negatives.

    Added to the twelve that did not need it as well, so its presence correlates
    with nothing a model could exploit. It reveals no predicate and no value: what
    it removes is the guessing of an identifier, which is a different question
    from whether a claim was found and should not be measured as part of it.
    """
    for case in _load():
        first = case["events"][0]
        assert first["event_id"] == "e0"
        assert first.get("tool_name") == "catalog_lookup"
        for entity in case["entities"]:
            assert entity["reference"] in first["body"]


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_no_claim_cites_only_the_catalog_lookup(case: dict[str, Any]) -> None:
    """The lookup names entities; it settles nothing about them. A claim resting
    on it alone would be one the fixture itself invented."""
    for claim in case["expected_claims"]:
        assert set(claim["event_ids"]) != {"e0"}


@pytest.mark.parametrize("case", _load(), ids=lambda c: str(c["case_id"]))
def test_no_excerpt_carries_directive_text(case: dict[str, Any]) -> None:
    """Containment refuses directive text, so an excerpt containing it would
    measure the containment check rather than extraction quality. Adversarial
    inputs are worth testing and belong in a fixture that says so."""
    for event in case["events"]:
        try:
            assert_not_directive(event["body"], field=f"{case['case_id']}.{event['event_id']}")
        except CandidateRefused as refusal:  # pragma: no cover - a failure prints the reason
            pytest.fail(f"{case['case_id']}: {refusal}")


def test_the_fixture_contains_cases_where_the_right_answer_is_silence() -> None:
    """Precision cannot be measured without them. A fixture where every excerpt
    yields something rewards an extractor that always yields something."""
    negatives = [case for case in _load() if not case["expected_claims"]]
    assert len(negatives) >= 5, "too few negative cases to measure precision honestly"
    assert all(case["difficulty"] == "negative" for case in negatives)


def test_the_fixture_covers_most_of_the_strategys_vocabulary() -> None:
    """A ground truth exercising six predicates measures six predicates. Stated as
    a floor with the gap named, rather than as a claim of completeness."""
    labelled = {claim["predicate"] for case in _load() for claim in case["expected_claims"]}
    permitted = set(OBSERVATION.permitted_predicates)
    missing = permitted - labelled
    assert len(labelled) >= len(permitted) - 1, f"uncovered predicates: {sorted(missing)}"


# --- the scoring arithmetic -----------------------------------------------------


def _keys(*triples: tuple[str, str, object]) -> set[tuple[str, str, object]]:
    return {_key(*t) for t in triples}


def test_a_perfect_extraction_scores_one_and_one() -> None:
    expected = _keys(("svc:a", "owned_by_team", "Platform"), ("svc:a", "deployment_environment", "production"))
    result = score_case(expected=expected, extracted=set(expected))
    assert result.precision == 1.0
    assert result.recall == 1.0


def test_an_invented_claim_costs_precision_and_not_recall() -> None:
    expected = _keys(("svc:a", "owned_by_team", "Platform"))
    extracted = _keys(("svc:a", "owned_by_team", "Platform"), ("svc:a", "deployment_environment", "production"))
    result = score_case(expected=expected, extracted=extracted)
    assert result.precision == 0.5
    assert result.recall == 1.0


def test_a_missed_claim_costs_recall_and_not_precision() -> None:
    expected = _keys(("svc:a", "owned_by_team", "Platform"), ("svc:a", "deployment_environment", "production"))
    extracted = _keys(("svc:a", "owned_by_team", "Platform"))
    result = score_case(expected=expected, extracted=extracted)
    assert result.precision == 1.0
    assert result.recall == 0.5


def test_an_extractor_that_says_nothing_has_no_precision_rather_than_perfect_precision() -> None:
    """The degenerate case that makes an unchecked implementation dangerous: if a
    silent extractor scored 1.0, the best score on this metric would belong to a
    provider that is switched off."""
    result = score_case(expected=_keys(("svc:a", "owned_by_team", "Platform")), extracted=set())
    assert result.precision is None
    assert result.recall == 0.0


def test_a_negative_case_has_no_recall_rather_than_zero_recall() -> None:
    """There was nothing to find. Scoring it as a total recall failure would let
    the negative cases, which exist to measure precision, destroy the recall
    figure instead."""
    clean = score_case(expected=set(), extracted=set())
    assert clean.recall is None
    assert clean.precision is None

    noisy = score_case(expected=set(), extracted=_keys(("svc:a", "owned_by_team", "Platform")))
    assert noisy.precision == 0.0
    assert noisy.recall is None


def test_a_differently_shaped_value_is_a_miss_and_not_a_match() -> None:
    """The fixture's stated limit, asserted so it stays stated: normalisation
    covers case and whitespace, and nothing else."""
    expected = _keys(("svc:a", "exposes_operation", "POST /v1/sends"))
    same = score_case(expected=expected, extracted=_keys(("svc:a", "exposes_operation", "post  /v1/sends ")))
    assert same.precision == 1.0

    reshaped = score_case(expected=expected, extracted=_keys(("svc:a", "exposes_operation", "/v1/sends")))
    assert reshaped.precision == 0.0
    assert reshaped.recall == 0.0


def test_scores_combine_by_adding() -> None:
    """What makes an overall figure and a per-predicate breakdown the same
    arithmetic rather than two implementations that can disagree."""
    first = Score(true_positive=3, false_positive=1, false_negative=2)
    second = Score(true_positive=1, false_positive=0, false_negative=1)
    assert first + second == Score(true_positive=4, false_positive=1, false_negative=3)


def test_the_breakdown_splits_by_predicate() -> None:
    expected = _keys(("svc:a", "owned_by_team", "Platform"), ("svc:a", "deployment_environment", "production"))
    extracted = _keys(("svc:a", "owned_by_team", "Payments"), ("svc:a", "deployment_environment", "production"))
    breakdown = score_by_predicate(expected=expected, extracted=extracted)
    assert breakdown["deployment_environment"].precision == 1.0
    assert breakdown["owned_by_team"].precision == 0.0
    assert breakdown["owned_by_team"].recall == 0.0


def test_the_breakdown_sums_to_the_overall_score() -> None:
    """A per-predicate table that does not add up to the headline figure means one
    of the two is being computed differently, and a reader cannot tell which."""
    cases = _load()
    expected: set[tuple[str, str, object]] = set()
    for case in cases:
        expected |= _expected_keys(case)
    # A deliberately imperfect stand-in extraction: everything except the first
    # claim of each case, plus one invention.
    extracted = set(sorted(expected)[1:]) | _keys(("svc:nothing", "owned_by_team", "Nobody"))

    overall = score_case(expected=expected, extracted=extracted)
    total = Score(0, 0, 0)
    for part in score_by_predicate(expected=expected, extracted=extracted).values():
        total = total + part
    assert total == overall


# --- the measurement ------------------------------------------------------------


def _report(rows: Sequence[tuple[str, Score]]) -> str:
    lines = [f"{'predicate':<34} {'precision':>10} {'recall':>10}  tp/fp/fn"]
    for name, s in rows:
        p = "n/a" if s.precision is None else f"{s.precision:.3f}"
        r = "n/a" if s.recall is None else f"{s.recall:.3f}"
        lines.append(f"{name:<34} {p:>10} {r:>10}  {s.true_positive}/{s.false_positive}/{s.false_negative}")
    return "\n".join(lines)


@pytest.mark.skipif(
    not _API_KEY,
    reason="no CLAUDE_API_KEY or ANTHROPIC_API_KEY; the extraction quality measurement is opt-in",
)
@pytest.mark.asyncio
async def test_measure_extraction_precision_and_recall() -> None:
    """Run the real provider over all thirty cases and report, asserting nothing.

    Deliberately not run against `local-rules`: that provider's own module says a
    benchmark against it measures the regexes, and a precision figure derived from
    the demo patterns filed under "extraction quality" would be a lie of exactly
    the kind this fixture exists to prevent.

    What *is* asserted is that the measurement happened — every case attempted, no
    silent skips. A report over three cases and a report over thirty look the same
    once they are a number in a table.
    """
    import datetime
    import uuid

    import httpx

    from contextplane.extraction.anthropic_provider import AnthropicExtractionProvider
    from contextplane.extraction.provider import ExtractionRequest
    from contextplane.service.memory.session_events import SessionEvent

    cases = _load()
    now = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=datetime.UTC)

    overall = Score(0, 0, 0)
    per_predicate: dict[str, Score] = {}
    attempted = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        provider = AnthropicExtractionProvider(api_key=_API_KEY, client=client)
        for case in cases:
            # The provider sees opaque uuids, as it does in production; the map
            # back to the fixture's e1/e2 labels is only needed for evidence and
            # the score does not read event ids.
            events = tuple(
                SessionEvent(
                    event_id=uuid.uuid4(),
                    session_id=case["case_id"],
                    seq=index,
                    kind=raw["kind"],
                    body=raw["body"],
                    tool_name=raw.get("tool_name"),
                    metadata={},
                    created_at=now,
                )
                for index, raw in enumerate(case["events"])
            )
            request = ExtractionRequest(
                events=events,
                strategy_id=OBSERVATION.strategy_id,
                system_prompt=OBSERVATION.system_prompt,
                output_schema=OBSERVATION.output_schema,
                model_id=provider.default_model_id,
                max_output_tokens=OBSERVATION.max_output_tokens,
                permitted_predicates=OBSERVATION.permitted_predicates,
                requested_at=now,
            )
            result = await provider.extract(request)
            attempted += 1

            expected = _expected_keys(case)
            extracted = _extracted_keys(result.claims)
            overall = overall + score_case(expected=expected, extracted=extracted)
            for predicate, part in score_by_predicate(expected=expected, extracted=extracted).items():
                per_predicate[predicate] = per_predicate.get(predicate, Score(0, 0, 0)) + part

    assert attempted == _CASE_COUNT, "every case must be measured, or the figure describes a subset"

    print(f"\nextraction ground truth — {attempted} cases, model={provider.default_model_id}")
    print(_report(sorted(per_predicate.items())))
    print(_report([("OVERALL", overall)]))
    print("\nRecord these figures in eval/EVAL.md. No threshold is asserted yet — see the module docstring.")
