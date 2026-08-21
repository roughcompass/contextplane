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


# --- validation evidence -------------------------------------------------------
#
# The refusals are asserted by loading a synthetic registry rather than by
# reading the committed one: a test that only inspects today's three entries
# passes forever once they are correct, and would not notice the loader
# dropping the check entirely.


def _load_with(tmp_path: Path, entry: dict[str, object]) -> dict[str, ranking.GovernedMagnitude]:
    """Run the loader over a one-entry registry, so a refusal is observable."""
    registry = tmp_path / "ranking_registry.json"
    registry.write_text(json.dumps({"artifact_version": 1, "magnitudes": [entry]}), encoding="utf-8")
    original = ranking.REGISTRY_PATH
    try:
        ranking.REGISTRY_PATH = registry  # type: ignore[misc]
        return ranking._load()
    finally:
        ranking.REGISTRY_PATH = original  # type: ignore[misc]


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "model_id": "probe@1",
        "form": "weights",
        "consumer": "contextplane/ranking.py",
        "coupling": "consumed",
        "parameters": {"a": 1.0},
        "reason": " ".join(["word"] * 25),
        "validation": {"status": "grandfathered", "reason": "not checked, and says so"},
        "requires_validated": False,
    }
    base.update(overrides)
    return base


def test_the_synthetic_entry_loads_so_the_refusals_below_are_not_vacuous(tmp_path: Path) -> None:
    """Control. Without it every refusal test could pass for the wrong reason."""
    loaded = _load_with(tmp_path, _entry())
    assert loaded["probe@1"].validation_status == "grandfathered"


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        (None, "validation.status"),
        ({"status": "unchecked"}, "validation.status"),
        ({"status": "grandfathered", "reason": "  "}, "without a stated reason"),
        ({"status": "validated"}, "a status without its evidence"),
        ({"status": "validated", "validated_by": "someone"}, "a status without its evidence"),
    ],
)
def test_an_entry_that_cannot_say_whether_it_was_checked_is_refused(
    tmp_path: Path, validation: dict[str, object] | None, expected: str
) -> None:
    """Absent, unknown, unexplained and unevidenced all refuse.

    Absence is refused rather than defaulted in either direction: defaulting to
    grandfathered lets an entry omit the field and be quietly exempt, and
    defaulting to validated asserts a check nobody ran.
    """
    with pytest.raises(ranking.UngovernedMagnitude, match=expected):
        _load_with(tmp_path, _entry(validation=validation))


def test_a_fully_evidenced_validated_entry_is_accepted(tmp_path: Path) -> None:
    """The positive case, so the four required fields are a bar and not a wall."""
    loaded = _load_with(
        tmp_path,
        _entry(
            validation={
                "status": "validated",
                "reason": "",
                "validated_by": "second-line reviewer",
                "validated_on": "2026-08-19",
                "method": "held-out replay over the frozen question set",
                "result": "precision@10 0.91 against 0.87 incumbent",
            }
        ),
    )
    assert loaded["probe@1"].validation_status == "validated"


# --- validation gating: the refusal is the activation gate ----------------------
#
# There is no feature-flag mechanism to hang this on -- the repository has one
# genuine feature switch and ADR-0005 rules out env-var flags that widen
# authority -- so reading the number *is* the activation, and a magnitude that
# cannot be read cannot serve.


def test_a_validation_gated_magnitude_cannot_load_unvalidated(tmp_path: Path) -> None:
    """The rule the `requires_validated` field exists for, at runtime.

    It lived only in `scripts/check_governed_magnitudes.py` until now, which left
    the running service more permissive than the pipeline that reviewed it -- a
    gate is a thing somebody can skip, and this module's stated posture is that
    an unknown id raises and an empty registry raises at import.
    """
    with pytest.raises(ranking.UngovernedMagnitude, match="requires_validated is true"):
        _load_with(tmp_path, _entry(requires_validated=True))


def test_a_validation_gated_magnitude_loads_once_it_is_validated(tmp_path: Path) -> None:
    """The positive case, so the refusal above is a bar rather than a wall."""
    loaded = _load_with(
        tmp_path,
        _entry(
            requires_validated=True,
            validation={
                "status": "validated",
                "validated_by": "second-line reviewer",
                "validated_on": "2026-08-21",
                "method": "held-out replay over the frozen question set",
                "result": "precision@10 0.91 against 0.87 incumbent",
            },
        ),
    )
    assert loaded["probe@1"].requires_validated is True


def test_one_bad_entry_refuses_the_whole_registry(tmp_path: Path) -> None:
    """Not a lazy refusal at whichever accessor happens to ask.

    Refusing per-read would protect a magnitude only as far as some code path
    reads it, and "was this read on this deployment" is not something a
    governance guarantee should rest on. Refusing at load means the process does
    not start: `_REGISTRY` is bound at import and every consumer imports this
    module. The good entry here is the evidence -- it would load on its own, and
    does not.
    """
    registry = tmp_path / "ranking_registry.json"
    good = _entry(model_id="fine@1")
    bad = _entry(model_id="gated@1", requires_validated=True)
    registry.write_text(json.dumps({"artifact_version": 1, "magnitudes": [good, bad]}), encoding="utf-8")

    original = ranking.REGISTRY_PATH
    try:
        ranking.REGISTRY_PATH = registry  # type: ignore[misc]
        with pytest.raises(ranking.UngovernedMagnitude, match="gated@1"):
            ranking._load()
    finally:
        ranking.REGISTRY_PATH = original  # type: ignore[misc]


def test_the_registry_is_read_at_import_so_a_refusal_stops_the_process() -> None:
    """What makes the refusal a boot failure rather than a lazy error.

    Two facts carry it, and both are asserted rather than assumed: this module
    binds its registry at import, and at least one consumer obtains a governed
    magnitude at *its* import. Together they mean an unloadable registry fails
    the import graph, which is the same guarantee
    `assert_drafter_decision_permits_serving` gives the drafter -- reached
    without a flag, because reading the number is the activation.

    If a future refactor made every consumer read lazily, this fails and the
    boot-failure claim in the module docstring would need rewriting rather than
    quietly becoming false.
    """
    source = (REPO_ROOT / "contextplane" / "ranking.py").read_text(encoding="utf-8")
    assert "_REGISTRY: Final[dict[str, GovernedMagnitude]] = _load()" in source, (
        "the registry is no longer bound at import, so an unloadable registry "
        "would surface at first use rather than at start"
    )

    from contextplane.service.memory import claim_serving

    assert isinstance(claim_serving._ARM_WEIGHTS, dict), (
        "no consumer reads a governed magnitude at import, so a refusal would "
        "not stop a process that never touches one"
    )


def test_every_shipped_magnitude_declares_a_validation_status() -> None:
    """The committed artifact, which the synthetic tests above cannot cover."""
    for model_id in ranking.model_ids():
        assert ranking.validation_status(model_id) in {"validated", "grandfathered"}


def test_nothing_shipped_claims_validation_it_has_not_had() -> None:
    """Every magnitude here is in-production behaviour brought under governance.

    Marking one `validated` would assert a check nobody ran. If this test ever
    fails because a magnitude was genuinely validated, the fix is to delete the
    assertion for that id and record the evidence -- not to relax the rule.
    """
    for model_id in ranking.model_ids():
        assert ranking.validation_status(model_id) == "grandfathered", (
            f"{model_id} claims validation; if a check really happened, record its "
            "evidence and remove it from this assertion"
        )
