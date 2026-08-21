"""Each salience signal, against windows built to isolate exactly one of them.

Two properties matter more than any individual case. Every signal must answer
0.0 for an empty window rather than raise, because an episode with no events is
not salient and that is an answer. And each must *discriminate* — a signal
returning the same value for a window that has the thing and one that does not
carries no information, however plausible its implementation reads.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import pytest

from contextplane import ranking
from contextplane.extraction import salience
from contextplane.service.memory.session_events import SessionEvent

_NOW = datetime.datetime(2026, 8, 19, tzinfo=datetime.UTC)


def _event(kind: str, body: str = "", *, tool: str | None = None, seq: int = 1) -> SessionEvent:
    return SessionEvent(
        event_id=uuid.uuid4(),
        session_id="s",
        seq=seq,
        kind=kind,
        body=body,
        tool_name=tool,
        metadata={},
        created_at=_NOW,
    )


ALL_SIGNALS = [
    salience.state_change,
    salience.outcome_decisive,
    salience.human_engagement,
    salience.entity_density,
    salience.tool_diversity,
]


@pytest.mark.parametrize("signal", ALL_SIGNALS, ids=lambda f: f.__name__)
def test_an_empty_window_scores_zero_rather_than_raising(signal: Callable[[list[SessionEvent]], float]) -> None:
    assert signal([]) == 0.0


@pytest.mark.parametrize("signal", ALL_SIGNALS, ids=lambda f: f.__name__)
def test_every_signal_stays_inside_the_unit_interval(signal: Callable[[list[SessionEvent]], float]) -> None:
    """A signal outside [0, 1] silently reweights the whole sum."""
    busy = [
        _event("user_message", "no, actually the Order.Service is wrong", seq=1),
        _event("tool_invocation", "deployed payment-gateway", tool="deploy_service", seq=2),
        _event("tool_invocation", "wrote config", tool="write_file", seq=3),
        _event("tool_invocation", "read logs", tool="read_logs", seq=4),
        _event("tool_invocation", "ran suite", tool="run_tests", seq=5),
        _event("agent_action", "root cause confirmed in `billing.worker`", seq=6),
    ]
    assert 0.0 <= signal(busy) <= 1.0


class TestStateChange:
    def test_a_mutating_tool_scores_full(self) -> None:
        assert salience.state_change([_event("tool_invocation", "", tool="deploy_service")]) == 1.0

    def test_a_read_only_tool_scores_zero(self) -> None:
        assert salience.state_change([_event("tool_invocation", "", tool="read_logs")]) == 0.0

    def test_prose_describing_a_deployment_is_not_a_deployment(self) -> None:
        """The discriminator that keeps a plan from scoring like an act."""
        talked = [_event("agent_action", "I will deploy and then write the config file")]
        assert salience.state_change(talked) == 0.0


class TestOutcomeDecisive:
    def test_a_verdict_at_the_end_scores_full(self) -> None:
        window = [_event("agent_action", "looking into it", seq=1), _event("agent_action", "fixed", seq=2)]
        assert salience.outcome_decisive(window) == 1.0

    def test_the_same_word_early_does_not_count(self) -> None:
        """ "fixed" up front is describing the problem; at the end it is the result.

        The window states the problem once and then trails off, so the marker
        is present but outside the closing events the signal reads.
        """
        window = [
            _event("user_message", "this needs to be fixed", seq=1),
            _event("agent_action", "looking at the logs", seq=2),
            _event("agent_action", "still reading", seq=3),
            _event("agent_action", "no clear lead yet", seq=4),
        ]
        assert salience.outcome_decisive(window) == 0.0

    def test_an_episode_that_trails_off_scores_zero(self) -> None:
        assert salience.outcome_decisive([_event("agent_action", "still investigating")]) == 0.0


class TestHumanEngagement:
    def test_a_correction_outscores_plain_participation(self) -> None:
        corrective = salience.human_engagement([_event("user_message", "no, not that one")])
        passive = salience.human_engagement([_event("user_message", "sounds good")])
        assert corrective > passive > 0.0

    def test_an_agent_only_episode_scores_zero(self) -> None:
        assert salience.human_engagement([_event("agent_action", "no, wrong")]) == 0.0


class TestEntityDensity:
    def test_named_things_score_above_prose(self) -> None:
        named = [_event("agent_action", "checked `billing.worker` and order_service and PaymentGateway")]
        prose = [_event("agent_action", "checked the thing and the other thing")]
        assert salience.entity_density(named) > salience.entity_density(prose)

    def test_repeating_one_name_is_one_thing(self) -> None:
        """Counting repeats would score a transcript about one service as forty."""
        repeated = [_event("agent_action", "order_service " * 40, seq=i) for i in range(1, 5)]
        varied = [
            _event("agent_action", "order_service billing_worker payment_gateway audit_log", seq=i) for i in range(1, 5)
        ]
        assert salience.entity_density(repeated) < salience.entity_density(varied)


class TestToolDiversity:
    def test_more_distinct_tools_scores_higher(self) -> None:
        one = [_event("tool_invocation", "", tool="read_logs", seq=1)]
        three = [
            _event("tool_invocation", "", tool="read_logs", seq=1),
            _event("tool_invocation", "", tool="run_tests", seq=2),
            _event("tool_invocation", "", tool="write_file", seq=3),
        ]
        assert salience.tool_diversity(three) > salience.tool_diversity(one)

    def test_the_same_tool_repeatedly_is_one_tool(self) -> None:
        repeated = [_event("tool_invocation", "", tool="read_logs", seq=i) for i in range(1, 9)]
        assert salience.tool_diversity(repeated) == salience.tool_diversity(
            [_event("tool_invocation", "", tool="read_logs")]
        )


class TestSignalVector:
    def test_it_carries_every_declared_signal_even_when_empty(self) -> None:
        """A caller combining with weights must never have to guess a missing key."""
        assert set(salience.signal_vector([])) == set(salience.SIGNAL_NAMES)

    def test_novelty_is_absent_because_it_cannot_be_computed_at_write(self) -> None:
        """Embedding is queued at write, so a synchronous novelty term would lie.

        Pinned as a test rather than left to the docstring: a later author
        adding `novelty` here would be introducing a signal that either blocks
        the write on a model call or silently reports zero.
        """
        assert "novelty" not in salience.signal_vector([])

    def test_a_substantive_episode_outscores_an_idle_one_on_every_axis(self) -> None:
        substantive = salience.signal_vector(
            [
                _event("user_message", "no, actually check `billing.worker`", seq=1),
                _event("tool_invocation", "", tool="write_config", seq=2),
                _event("tool_invocation", "", tool="run_tests", seq=3),
                _event("agent_action", "root cause confirmed in order_service", seq=4),
            ]
        )
        idle = salience.signal_vector([_event("agent_action", "thinking about it", seq=1)])
        assert all(substantive[name] >= idle[name] for name in salience.SIGNAL_NAMES)
        assert sum(substantive.values()) > sum(idle.values())


# --- the weighted sum ------------------------------------------------------------


#: The core defaults, stated at every call site below rather than defaulted
#: inside `combine`. These tests are about the arithmetic, and the arithmetic is
#: the same whoever's weights it runs on -- what changed is that the function no
#: longer decides whose they are.
_CORE = ranking.weights(salience.WEIGHTS_MODEL_ID)


def test_combine_will_not_choose_whose_weights_to_use() -> None:
    """The parameter is required, and that is the guard rather than a style.

    This function used to read `ranking.weights(...)` itself, so it scored every
    tenant on the deployment's core values and silently ignored an override the
    tenant had published, validated and activated. A default here would restore
    that path the first time somebody found the argument inconvenient.
    """
    with pytest.raises(TypeError, match="weights"):
        salience.combine(dict.fromkeys(salience.SIGNAL_NAMES, 1.0))  # type: ignore[call-arg]


def test_a_tenants_weights_change_the_score() -> None:
    """What the whole accessor chain is for, at the point it lands.

    Core weights lead with state change; a tenant that cares about human
    engagement instead should get a different number for the same episode. If
    this passed with identical scores the override would be reaching the
    arithmetic and doing nothing.
    """
    signals = dict.fromkeys(salience.SIGNAL_NAMES, 0.0) | {"human_engagement": 1.0}
    tenant_weights = dict.fromkeys(_CORE, 0.0) | {"human_engagement": 1.0}

    assert salience.combine(signals, weights=_CORE) == pytest.approx(_CORE["human_engagement"])
    assert salience.combine(signals, weights=tenant_weights) == pytest.approx(1.0)


def test_the_weights_come_from_the_registry_and_sum_to_one() -> None:
    """A weighted sum whose weights do not sum to one produces a number that is
    not comparable between episodes, which is the only thing it is for."""
    weights = ranking.weights(salience.WEIGHTS_MODEL_ID)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert set(weights) == set(salience.SIGNAL_NAMES) | {salience.NOVELTY}


def test_an_empty_window_is_not_salient() -> None:
    assert salience.combine(salience.signal_vector([]), weights=_CORE) == 0.0


def test_a_missing_novelty_costs_exactly_its_weight_and_no_more() -> None:
    """Novelty arrives later than the rest, so its absence has to be a bounded,
    stated cost rather than a redistribution nobody can see."""
    everything = dict.fromkeys(salience.SIGNAL_NAMES, 1.0)
    novelty_weight = ranking.weights(salience.WEIGHTS_MODEL_ID)[salience.NOVELTY]
    assert salience.combine(everything, weights=_CORE) == pytest.approx(1.0 - novelty_weight)
    assert salience.combine(everything, weights=_CORE, novelty=1.0) == pytest.approx(1.0)


def test_novelty_landing_can_only_raise_a_score() -> None:
    """The property that makes the two-phase fill legible. Redistributing the
    novelty weight across the other five would let a score fall when a signal
    arrives, which reads as a bug however it is documented."""
    signals = {"state_change": 1.0, "outcome_decisive": 0.5, "human_engagement": 0.0}
    signals |= {"entity_density": 0.25, "tool_diversity": 0.0}
    before = salience.combine(signals, weights=_CORE)
    for novelty in (0.0, 0.5, 1.0):
        assert salience.combine(signals, weights=_CORE, novelty=novelty) >= before


def test_a_signal_the_weights_do_not_name_is_refused() -> None:
    """A signal nobody weights contributes nothing, silently, and the score still
    looks like a score."""
    signals = dict.fromkeys(salience.SIGNAL_NAMES, 0.5) | {"invented": 1.0}
    with pytest.raises(ranking.UngovernedMagnitude, match="invented"):
        salience.combine(signals, weights=_CORE)


def test_a_weighted_signal_nobody_supplied_is_refused() -> None:
    """The other direction: a dropped signal lowers every score by its weight,
    which looks like the corpus got less interesting."""
    with pytest.raises(ranking.UngovernedMagnitude, match="state_change"):
        salience.combine({"outcome_decisive": 1.0}, weights=_CORE)


def test_novelty_alone_may_be_absent() -> None:
    """The one exception, and it is the whole reason the check is not symmetric."""
    assert salience.combine(dict.fromkeys(salience.SIGNAL_NAMES, 0.0), weights=_CORE) == 0.0


def test_the_result_stays_inside_the_range_the_column_accepts() -> None:
    """Brute force, because the database CHECK would otherwise report this as a
    constraint name rather than as a disagreement between two artifacts."""
    for value in (0.0, 0.25, 0.5, 0.75, 1.0):
        for novelty in (None, 0.0, 1.0):
            result = salience.combine(dict.fromkeys(salience.SIGNAL_NAMES, value), weights=_CORE, novelty=novelty)
            assert 0.0 <= result <= 1.0
