"""The reliability curve, and the several ways it declines to say something.

Most of the value here is in the refusals. A reliability report is read as
evidence that a weighting works, so the cases that matter are the ones where it
must not look like evidence: no data, a bucket with four observations, one
measurable bucket and therefore no slope.
"""

from __future__ import annotations

import pytest

from contextplane.service.memory.salience_reliability import (
    ASSURANCE_NOT_EARNED,
    BUCKET_COUNT,
    MIN_BUCKET_OBSERVATIONS,
    MIN_TENANT_OBSERVATIONS,
    Observation,
    measure,
    measure_split,
    render,
    render_split,
)

_LABEL = "served in at least one resolution"


def _at(salience: float, *, retrieved: int, missed: int) -> list[Observation]:
    return [Observation(salience=salience, was_retrieved=True)] * retrieved + [
        Observation(salience=salience, was_retrieved=False)
    ] * missed


class TestTheEmptyCase:
    def test_no_observations_is_reported_as_no_curve(self) -> None:
        """A fresh deployment. Zeros would put a shape on a table nobody
        measured, and a flat curve reads as 'salience predicts nothing'."""
        report = measure([], label=_LABEL)
        assert report.total_observations == 0
        assert report.brier_score is None
        assert report.is_monotone is None
        assert all(bucket.retrieval_rate is None for bucket in report.buckets)

    def test_the_rendered_empty_report_says_why_it_is_empty(self) -> None:
        text = render(measure([], label=_LABEL))
        assert "no curve to draw" in text
        assert "0.000" not in text

    def test_a_brier_score_of_none_is_not_a_perfect_score(self) -> None:
        """0.0 is the score of a perfect predictor and exactly the wrong answer
        for having measured nothing."""
        assert measure([], label=_LABEL).brier_score is None


class TestTheObservationFloor:
    def test_a_thin_bucket_reports_its_count_and_no_rate(self) -> None:
        """A bucket showing 1.000 off one retrieval is the most misleading cell a
        reliability table can contain."""
        report = measure(_at(0.85, retrieved=1, missed=0), label=_LABEL)
        thin = next(b for b in report.buckets if b.observations)
        assert thin.observations == 1
        assert thin.retrieval_rate is None

    def test_a_bucket_at_the_floor_reports_a_rate(self) -> None:
        """The control. A floor that rejected its own boundary would be one
        higher than the number written down."""
        report = measure(_at(0.85, retrieved=MIN_BUCKET_OBSERVATIONS, missed=0), label=_LABEL)
        assert next(b for b in report.buckets if b.observations).retrieval_rate == 1.0

    def test_a_thin_bucket_is_absent_from_the_measurable_set(self) -> None:
        report = measure(_at(0.15, retrieved=2, missed=2), label=_LABEL)
        assert report.measurable_buckets == ()

    def test_the_rendered_report_distinguishes_thin_from_never_retrieved(self) -> None:
        """`n/a` and `0.000` mean opposite things and a reader will conflate them
        unless the table says so."""
        text = render(measure(_at(0.15, retrieved=0, missed=2) + _at(0.95, retrieved=30, missed=0), label=_LABEL))
        assert "n/a" in text
        assert "not a bucket where nothing was retrieved" in text


class TestTheCurve:
    def test_a_predictive_weighting_rises_with_salience(self) -> None:
        low = _at(0.05, retrieved=2, missed=38)
        high = _at(0.95, retrieved=38, missed=2)
        report = measure(low + high, label=_LABEL)
        assert report.is_monotone is True
        rates = [b.retrieval_rate for b in report.measurable_buckets]
        assert rates[0] < rates[-1]  # type: ignore[operator]

    def test_an_inverted_weighting_is_reported_as_not_rising(self) -> None:
        """The finding the report exists to be able to make: salience ordering
        claims by something retrieval does not care about."""
        report = measure(_at(0.05, retrieved=38, missed=2) + _at(0.95, retrieved=2, missed=38), label=_LABEL)
        assert report.is_monotone is False

    def test_one_measurable_bucket_has_no_slope_to_report(self) -> None:
        """`True` here would let a deployment with almost no data read as a
        validated weighting."""
        assert measure(_at(0.95, retrieved=30, missed=10), label=_LABEL).is_monotone is None

    def test_every_bucket_appears_whether_or_not_anything_landed_in_it(self) -> None:
        """A table that omitted empty buckets would hide the range the corpus
        never reaches, which is itself the finding on a corpus that scores
        everything the same."""
        report = measure(_at(0.55, retrieved=30, missed=0), label=_LABEL)
        assert len(report.buckets) == BUCKET_COUNT

    def test_the_buckets_partition_the_range(self) -> None:
        report = measure([], label=_LABEL)
        assert report.buckets[0].lower == 0.0
        assert report.buckets[-1].upper == 1.0
        for earlier, later in zip(report.buckets, report.buckets[1:], strict=False):
            assert earlier.upper == later.lower


class TestTheBrierScore:
    def test_a_perfect_predictor_scores_zero(self) -> None:
        perfect = [Observation(salience=1.0, was_retrieved=True), Observation(salience=0.0, was_retrieved=False)]
        assert measure(perfect, label=_LABEL).brier_score == pytest.approx(0.0)

    def test_a_confidently_wrong_predictor_scores_one(self) -> None:
        wrong = [Observation(salience=1.0, was_retrieved=False), Observation(salience=0.0, was_retrieved=True)]
        assert measure(wrong, label=_LABEL).brier_score == pytest.approx(1.0)

    def test_hedging_at_the_midpoint_scores_a_quarter(self) -> None:
        """The reference point that makes the number readable: anything worse
        than 0.25 is worse than saying 0.5 about everything."""
        hedged = [Observation(salience=0.5, was_retrieved=i % 2 == 0) for i in range(40)]
        assert measure(hedged, label=_LABEL).brier_score == pytest.approx(0.25)

    def test_the_score_is_not_a_substitute_for_the_curve(self) -> None:
        """Two populations with the same score and different shapes. Only the
        curve distinguishes a uniformly mediocre weighting from one that is
        excellent where it matters — and for retention, only the top matters.
        """
        uniform = [Observation(salience=0.5, was_retrieved=i % 2 == 0) for i in range(40)]
        split = _at(0.5, retrieved=20, missed=0) + _at(0.5, retrieved=0, missed=20)
        assert measure(uniform, label=_LABEL).brier_score == pytest.approx(measure(split, label=_LABEL).brier_score)


class TestTheLabel:
    def test_the_report_carries_the_label_it_was_measured_against(self) -> None:
        """Retrieval is necessary for citation and not sufficient, so a figure
        whose label is unstated will be read as the stronger claim."""
        assert measure([], label=_LABEL).label == _LABEL
        assert _LABEL in render(measure([], label=_LABEL))

    def test_an_unlabelled_report_is_refused(self) -> None:
        with pytest.raises(ValueError, match="states the label"):
            measure([], label="   ")


def test_a_salience_outside_the_range_is_clamped_rather_than_dropped() -> None:
    """The column constrains it, so a value outside means the observations came
    from somewhere else — and silently dropping it would shrink the population
    the report claims to describe."""
    report = measure([Observation(salience=1.5, was_retrieved=True)], label=_LABEL)
    assert report.total_observations == 1
    assert report.buckets[-1].observations == 1


# --- the per-tenant split ---------------------------------------------------------


def _for(tenant: str, *, own_weights: bool, salience: float, retrieved: int, missed: int) -> list[Observation]:
    return [
        Observation(salience=salience, was_retrieved=hit, tenant_id=tenant, uses_own_weights=own_weights)
        for hit in ([True] * retrieved + [False] * missed)
    ]


class TestTheSplit:
    def test_tenants_on_the_committed_defaults_are_pooled(self) -> None:
        """Pooling is what makes the shared curve mean anything: identical
        weights produce comparable numbers, so those observations are one
        population."""
        split = measure_split(
            _for("a", own_weights=False, salience=0.9, retrieved=30, missed=10)
            + _for("b", own_weights=False, salience=0.9, retrieved=30, missed=10),
            label=_LABEL,
        )
        assert split.shared.total_observations == 80
        assert split.pooled_tenants == ("a", "b")
        assert split.per_tenant == ()

    def test_a_tenant_with_its_own_weights_gets_its_own_curve(self) -> None:
        """The cost ADR 0004 recorded. One global curve describes a population no
        overriding tenant matches."""
        split = measure_split(
            _for("shared", own_weights=False, salience=0.9, retrieved=30, missed=10)
            + _for("own", own_weights=True, salience=0.9, retrieved=100, missed=20),
            label=_LABEL,
        )
        assert split.shared.total_observations == 40
        assert [entry.tenant_id for entry in split.per_tenant] == ["own"]
        assert split.per_tenant[0].report is not None
        assert split.per_tenant[0].report.total_observations == 120

    def test_an_overriding_tenants_observations_stay_out_of_the_shared_curve(self) -> None:
        """The half that is easy to get wrong: measuring a tenant separately and
        also leaving it in the pool would double-count it and contaminate the
        curve it was split out of."""
        split = measure_split(_for("own", own_weights=True, salience=0.9, retrieved=100, missed=20), label=_LABEL)
        assert split.shared.total_observations == 0
        assert "own" not in split.pooled_tenants

    def test_a_thin_overriding_tenant_is_told_assurance_is_not_earned(self) -> None:
        """Not folded into the shared curve. Borrowing it would attach a figure
        measured under one weighting to scores produced under another."""
        split = measure_split(_for("thin", own_weights=True, salience=0.9, retrieved=5, missed=5), label=_LABEL)
        entry = split.per_tenant[0]
        assert entry.report is None
        assert entry.withheld_because is not None
        assert ASSURANCE_NOT_EARNED in entry.withheld_because
        assert split.shared.total_observations == 0, "a withheld tenant must not leak into the pool"

    def test_the_withheld_reason_names_the_count_and_the_floor(self) -> None:
        """An operator reading 'not earned' needs to know how far off they are,
        or the message is a refusal with no next step."""
        split = measure_split(_for("thin", own_weights=True, salience=0.9, retrieved=5, missed=5), label=_LABEL)
        assert "10 observations" in split.per_tenant[0].withheld_because  # type: ignore[operator]
        assert str(MIN_TENANT_OBSERVATIONS) in split.per_tenant[0].withheld_because  # type: ignore[operator]

    def test_a_tenant_at_the_floor_is_measured(self) -> None:
        """The control. A floor that rejected its own boundary would be one
        higher than the number written down."""
        split = measure_split(
            _for("at", own_weights=True, salience=0.9, retrieved=MIN_TENANT_OBSERVATIONS, missed=0),
            label=_LABEL,
        )
        assert split.per_tenant[0].report is not None

    def test_a_tenant_that_adopted_an_override_midway_has_both_kinds_counted_apart(self) -> None:
        """Carried per observation rather than looked up per tenant. Pooling a
        mixed corpus would measure neither weighting."""
        split = measure_split(
            _for("mixed", own_weights=False, salience=0.3, retrieved=10, missed=30)
            + _for("mixed", own_weights=True, salience=0.9, retrieved=100, missed=20),
            label=_LABEL,
        )
        assert split.shared.total_observations == 40
        assert split.per_tenant[0].report is not None
        assert split.per_tenant[0].report.total_observations == 120

    def test_the_rendered_split_says_when_there_is_nothing_to_split(self) -> None:
        text = render_split(
            measure_split(_for("a", own_weights=False, salience=0.5, retrieved=1, missed=1), label=_LABEL)
        )
        assert "nothing to split out" in text

    def test_the_rendered_split_shows_a_withheld_tenant_rather_than_omitting_it(self) -> None:
        """A tenant absent from the report reads as a tenant with no data. A
        tenant whose line says why it has no curve reads as a decision."""
        text = render_split(
            measure_split(_for("thin", own_weights=True, salience=0.9, retrieved=5, missed=5), label=_LABEL)
        )
        assert "tenant thin" in text
        assert ASSURANCE_NOT_EARNED in text
