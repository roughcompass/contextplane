"""Unit tests for the governed-magnitudes gate.

Every rule gets a planted violation and a passing control. The controls are not
padding: a rule tested only by the thing it rejects is indistinguishable from a
rule that rejects everything, and a gate that fails on a correct registry gets
switched off within a week.

The end-to-end test runs the real script against the committed registry, because
the per-rule tests all operate on synthetic dicts and would keep passing if the
script stopped reading the file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_governed_magnitudes import _EVIDENCE_FIELDS, _violations, main  # noqa: E402


def _grandfathered(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model_id": "test-magnitude@1",
        "form": "weights",
        # Present because a real entry has it and one rule below reads it: a
        # helper missing a field the gate inspects tests a shape nothing ships.
        "coupling": "consumed",
        "requires_validated": False,
        "validation": {
            "status": "grandfathered",
            "reason": "In-production behaviour brought under governance, not a number anybody checked.",
            "validated_by": None,
            "validated_on": None,
            "method": None,
            "result": None,
        },
    }
    entry.update(overrides)
    return entry


def _derived(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model_id": "test-magnitude@1",
        "form": "threshold",
        "coupling": "consumed",
        "requires_validated": True,
        "validation": {
            "status": "derived",
            "reason": "Follows by arithmetic from a stated defect rate and consumer's risk.",
            "derived_from": "AQL 0.05, consumer's risk 0.10, lot size 500",
            "derivation": "Binomial OC curve: smallest n with P(accept | p=0.05) <= 0.10.",
        },
    }
    entry.update(overrides)
    return entry


def _validated(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model_id": "test-magnitude@1",
        "form": "weights",
        "coupling": "consumed",
        "requires_validated": True,
        "validation": {
            "status": "validated",
            "reason": "Fitted and held out.",
            "validated_by": "platform-eng",
            "validated_on": "2026-08-19",
            "method": "held-out replay over 400 judged resolutions",
            "result": "top-1 relevance 0.71 against 0.62 for the prior weights",
        },
    }
    entry.update(overrides)
    return entry


# --- the rule the gate exists for -------------------------------------------------


def test_a_flag_demanding_validation_cannot_ride_a_grandfathered_number() -> None:
    """The safety ordering E3 and E5 both depend on. Without this check the
    field that expresses it is a comment."""
    entry = _grandfathered(requires_validated=True)
    found = _violations(entry)
    assert any("requires_validated" in line and "grandfathered" in line for line in found)


def test_a_flag_demanding_validation_passes_on_a_validated_number() -> None:
    """The control. A rule that rejected both cases would be a wall."""
    assert _violations(_validated()) == []


def test_a_grandfathered_number_no_flag_demands_is_fine() -> None:
    """Where every shipped magnitude sits today. Grandfathered is an honest
    state, not a defect."""
    assert _violations(_grandfathered()) == []


def test_a_flag_demanding_validation_passes_on_a_derived_number() -> None:
    """A reproducible derivation is a stronger warrant than a validation
    somebody ran once, so it satisfies the same gate."""
    assert _violations(_derived()) == []


def test_a_derived_entry_missing_either_field_is_refused() -> None:
    """The mirror of the validated-evidence rule. A derivation nobody can
    reproduce is a number with a nicer word on it."""
    for field in ("derived_from", "derivation"):
        entry = _derived()
        entry["validation"][field] = ""
        found = _violations(entry)
        assert any("derived" in line and field in line for line in found), field


def test_derived_does_not_reopen_the_door_grandfathered_is_kept_out_of() -> None:
    """The rule the third status could have quietly broken. Adding a status the
    gate accepts is only safe while `grandfathered` still fails it."""
    found = _violations(_grandfathered(requires_validated=True))
    assert any("requires_validated" in line and "grandfathered" in line for line in found)


def test_a_gated_magnitude_nothing_reads_is_refused() -> None:
    """The rule that lives only here, because the loader cannot see it.

    A `pinned` entry is one a test asserts agreement with rather than one the
    code reads — `source-authority-ladder@1`, whose order the governance kernel
    keeps its own copy of. Nothing on a serving path calls its accessor, so the
    loader's refusal never reaches production for it and the flag protects
    nothing while looking like it does. The entry is perfectly well-formed,
    which is exactly why only a gate reading the artifact can catch it.
    """
    found = _violations(_validated(coupling="pinned"))

    assert any("coupling" in line and "pinned" in line for line in found), found


def test_a_gated_magnitude_with_no_coupling_at_all_is_refused() -> None:
    """Absent is not read as 'consumed'. An omitted field would be the quiet
    way to acquire the exemption the test above refuses to grant."""
    entry = _validated()
    del entry["coupling"]

    assert any("coupling" in line for line in _violations(entry))


def test_coupling_is_only_checked_for_a_gated_magnitude() -> None:
    """`pinned` is a legitimate state — the shipped authority ladder is one.

    The rule is about gating something nothing reads, not about pinning.
    Without this control the new check would read as 'pinned is deprecated',
    and the next author would 'fix' the ladder by inventing a consumer for it.
    """
    assert _violations(_grandfathered(coupling="pinned")) == []


# --- the shape rules that keep it meaningful --------------------------------------


def test_an_entry_with_no_validation_block_is_refused() -> None:
    entry = _grandfathered()
    del entry["validation"]
    found = _violations(entry)
    assert len(found) == 1
    assert "no `validation` block" in found[0]


def test_an_unknown_status_is_refused() -> None:
    assert any("validation.status" in line for line in _violations(_grandfathered(validation={"status": "pending"})))


def test_a_grandfathered_entry_without_a_reason_is_refused() -> None:
    """An exemption nobody has to justify is one nobody will revisit."""
    entry = _grandfathered()
    entry["validation"]["reason"] = "   "
    assert any("without a reason" in line for line in _violations(entry))


def test_a_validated_entry_missing_any_evidence_field_is_refused() -> None:
    """Each of the four, separately: a loop that only checked the first would
    pass three quarters of the incomplete entries."""
    for field in _EVIDENCE_FIELDS:
        entry = _validated()
        entry["validation"][field] = None
        found = _violations(entry)
        assert any(field in line for line in found), f"{field} was not required"


def test_an_absent_requires_validated_is_refused_rather_than_read_as_false() -> None:
    """An absent field would read as 'no' for exactly the magnitude whose
    consumer says 'yes', which is the case this whole gate is about."""
    entry = _grandfathered()
    del entry["requires_validated"]
    assert any("requires_validated" in line for line in _violations(entry))


def test_a_non_boolean_requires_validated_is_refused() -> None:
    assert any("requires_validated" in line for line in _violations(_grandfathered(requires_validated="true")))


def test_one_entry_reports_every_one_of_its_problems() -> None:
    """A gate reporting the first failure makes fixing three problems three
    runs, and the third one is the one somebody gives up before reaching."""
    entry = _grandfathered(requires_validated=True)
    entry["validation"]["reason"] = ""
    entry["validation"]["status"] = "grandfathered"
    assert len(_violations(entry)) >= 2


# --- against the committed registry -----------------------------------------------


def test_the_committed_registry_passes(capsys: Any) -> None:
    """End to end, through the real file. The rule tests above all work on
    synthetic dicts and would keep passing if the script stopped reading it."""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "entr(ies) inspected" in out
    assert " 0 entr(ies) inspected" not in out, "a gate that inspected nothing must not report a pass"
