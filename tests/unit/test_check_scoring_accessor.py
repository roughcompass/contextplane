"""Unit tests for the scoring-accessor gate.

The rule is narrow and its edges are the interesting part: a *weights* read
outside `profile/scoring.py` is a bypass of the tenant override, and a
`threshold` or `ladder` read anywhere is not, because neither is overridable.
Tested against planted call expressions rather than files, plus one end-to-end
run of the real script against the real tree — the per-rule tests would keep
passing if the script stopped walking `contextplane/`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_scoring_accessor import _reads_weights, main  # noqa: E402


def _expr(source: str) -> ast.AST:
    return ast.parse(source, mode="eval").body


def test_a_weights_read_is_recognised() -> None:
    assert _reads_weights(_expr('ranking.weights("m@1")'))


def test_a_threshold_or_ladder_read_is_not() -> None:
    """The control, and the one that keeps this from reading as "never touch the
    registry".

    Neither form is tenant-overridable — `validate_overrides` takes a weight map,
    demands the key set match the core and demands it sum to one — so reading
    either directly is the correct call. `confidence_decay.py` and `salience.py`
    do, and a gate that flagged them would be teaching the wrong lesson.
    """
    assert not _reads_weights(_expr('ranking.threshold("m@1")'))
    assert not _reads_weights(_expr('ranking.ladder("m@1")'))


def test_an_unrelated_weights_call_is_not_a_registry_read() -> None:
    """`weights` is an ordinary word. Only the registry's is guarded."""
    assert not _reads_weights(_expr('policy.weights("m@1")'))
    assert not _reads_weights(_expr("weights(signals)"))


def test_something_that_is_not_a_call_is_not_a_read() -> None:
    """A reference to the function without invoking it. Passing
    `ranking.weights` somewhere would be a bypass worth catching, and this
    records that it is not caught rather than implying it is."""
    assert not _reads_weights(_expr("ranking.weights"))


def test_the_tree_passes(capsys: Any) -> None:
    """End to end against the real source, which the tests above cannot cover.

    Also the anti-vacuity check: the script fails rather than passes if it finds
    no read inside the accessor, so a clean result here means it looked.
    """
    assert main() == 0
    out = capsys.readouterr().out
    assert "0 outside it" in out
    assert "read(s) in contextplane/profile/scoring.py" in out
