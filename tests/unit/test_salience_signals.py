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
