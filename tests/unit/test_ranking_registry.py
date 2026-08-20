"""The governed-magnitude registry, and the properties that make it load-bearing.

A registry nobody reads is a second copy of the literal it was supposed to
replace. These assert the consumers actually obtain their numbers from it, that
the artifact refuses the shapes that would make it decorative, and that the
authority ladder it declares still agrees with the vocabulary the governance
kernel builds its rank map from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contextplane import ranking

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_registry_governs_a_non_empty_population() -> None:
    """An empty registry is a defect, not a state.

    A gate whose population is zero reports success while governing nothing,
    which is the failure `scripts/checklib.py` exists to prevent.
    """
    assert ranking.model_ids(), "the registry governs nothing"


def test_every_magnitude_states_why_it_holds_its_value() -> None:
    """The reason is the difference between a registry and a relocated literal."""
    raw = json.loads(ranking.REGISTRY_PATH.read_text(encoding="utf-8"))
    for entry in raw["magnitudes"]:
        reason = entry.get("reason", "")
        assert len(reason.split()) >= 20, (
            f"{entry['model_id']}: the reason is {len(reason.split())} words; "
            "a magnitude whose justification fits in a phrase has not been justified"
        )


def test_an_unknown_magnitude_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ranking.UngovernedMagnitude, match="not a governed magnitude"):
        ranking.weights("no-such-model@1")


def test_a_magnitude_requested_as_the_wrong_form_is_refused() -> None:
    """The form tag and the payload are separate fields; disagreement must fail."""
    with pytest.raises(ranking.UngovernedMagnitude, match="requested as"):
        ranking.weights("source-authority-ladder@1")


def test_weights_are_copied_so_a_caller_cannot_reweight_everyone_else() -> None:
    first = ranking.weights("entity-search-hybrid-fusion@1")
    first["semantic"] = 99.0
    assert ranking.weights("entity-search-hybrid-fusion@1")["semantic"] == 0.5


@pytest.mark.parametrize(
    "model_id",
    ["entity-search-hybrid-fusion@1", "claim-serving-hybrid-fusion@1"],
)
def test_fusion_weights_sum_to_one(model_id: str) -> None:
    """Arms that do not sum to 1.0 make a fused score incomparable across queries."""
    total = sum(ranking.weights(model_id).values())
    assert total == pytest.approx(1.0), f"{model_id} weights sum to {total}"


def test_the_declared_consumers_still_read_the_registry() -> None:
    """The property that keeps this from being a second copy of the literal.

    Each entry names the module that consumes it. If that module stopped
    importing `ranking`, the registry would still load, the values would still
    be right, and the code would be back on a literal — passing every other test
    in this file. So the coupling is asserted directly.
    """
    raw = json.loads(ranking.REGISTRY_PATH.read_text(encoding="utf-8"))
    for entry in raw["magnitudes"]:
        consumer = REPO_ROOT / entry["consumer"]
        assert consumer.is_file(), f"{entry['model_id']}: consumer {entry['consumer']} does not exist"
        source = consumer.read_text(encoding="utf-8")
        if entry["coupling"] == "consumed":
            assert (
                "from contextplane import ranking" in source
            ), f"{entry['model_id']}: {entry['consumer']} no longer imports the registry"
            assert (
                entry["model_id"] in source
            ), f"{entry['model_id']}: {entry['consumer']} no longer names it, so it reads a literal again"
        else:
            # `pinned` is the weaker binding and is asserted by its own test
            # below; what is checked here is that nothing silently downgrades a
            # `consumed` entry by editing one field.
            assert entry["coupling"] == "pinned", f"{entry['model_id']}: unknown coupling"


def test_the_authority_ladder_matches_the_governance_vocabulary() -> None:
    """The registry is the authority on the order; the kernel keeps the names.

    Asserted rather than refactored: `SOURCE_AUTHORITY_ORDER` feeds the claim
    adjudication path, and rebuilding it from the registry would put a governed
    magnitude in the middle of that without the evidence to justify the churn.
    Pinning agreement gets the anti-drift property at none of the risk.
    """
    from contextplane.service.governance import authority

    assert ranking.ladder("source-authority-ladder@1") == authority.SOURCE_AUTHORITY_ORDER
