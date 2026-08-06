"""ARC documentation coverage: a gate, not a grep.

`grep -q 'ARC_' .env.example` passes the moment one variable exists, which
would let most of a documentation contract go unwritten while looking
enforced. This instead enumerates the ARC fields on `Settings` and asserts
each is documented, and asserts the runbook covers each required subject by
name.

The failure this prevents is specific: an operator hitting a refusal at
three in the morning, searching the runbook for the thing that refused
them, and finding nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from registry.config import Settings

_REPO = Path(__file__).parent.parent.parent
_ENV_EXAMPLE = _REPO / ".env.example"
_CONFIG_REFERENCE = _REPO / "docs" / "05-reference" / "03-configuration.md"
_RUNBOOK = _REPO / "docs" / "06-operations" / "03-arc-runbook.md"

# The env var name for each ARC setting. Derived from the field name rather
# than hardcoded, so a renamed field fails here instead of silently losing
# its documentation.
_ARC_FIELD_TO_ENV = {
    "arc_global_operator_allowlist": "ARC_GLOBAL_OPERATOR_ALLOWLIST",
    "arc_drafter_model_enabled": "ARC_DRAFTER_MODEL_ENABLED",
    "arc_drafter_model_artifact_path": "ARC_DRAFTER_MODEL_ARTIFACT_PATH",
}

# Subjects the runbook must cover. Each is something an operator reaches for
# during an incident; a missing one is a page that does not answer the
# question that brought them there.
_REQUIRED_RUNBOOK_SUBJECTS = {
    "operator allowlist": ("Operator allowlist",),
    "key custody and rotation": ("Key custody and rotation",),
    "the reserved deployment tenant": ("reserved `_deployment` tenant",),
    "why an auditor sees the deployment tenant": ("Why an auditor sees it",),
    "review expiry and renewal": ("Review expiry and renewal",),
    "the audit outbox stuck-row gauge": ("stuck-row gauge",),
    "the downgrade guard": ("Downgrade guard",),
    "the archive-first procedure": ("Archive-first procedure",),
    "activation gates": ("Activation gates",),
}


def _arc_settings_fields() -> list[str]:
    return [name for name in Settings.model_fields if name.startswith("arc_")]


def test_every_arc_setting_has_a_known_env_var_name() -> None:
    """A new ARC setting must be added to the mapping above, which is what
    drags it into every other assertion in this file."""
    for field in _arc_settings_fields():
        assert field in _ARC_FIELD_TO_ENV, (
            f"Settings.{field} is an ARC setting with no documented env var name; "
            "add it to _ARC_FIELD_TO_ENV so the coverage assertions cover it"
        )


@pytest.mark.parametrize("field", _arc_settings_fields())
def test_every_arc_setting_appears_in_the_env_example(field: str) -> None:
    """`.env.example` is the canonical inventory operators copy from."""
    env_var = _ARC_FIELD_TO_ENV.get(field, field.upper())
    assert env_var in _ENV_EXAMPLE.read_text(), f"{env_var} is not documented in .env.example"


@pytest.mark.parametrize("field", _arc_settings_fields())
def test_every_arc_setting_appears_in_the_configuration_reference(field: str) -> None:
    env_var = _ARC_FIELD_TO_ENV.get(field, field.upper())
    assert (
        env_var in _CONFIG_REFERENCE.read_text()
    ), f"{env_var} is not documented in docs/05-reference/03-configuration.md"


def test_the_env_example_explains_the_empty_default() -> None:
    """The most dangerous misreading of this variable is that leaving it
    blank is permissive. It is not, and the file has to say so."""
    text = _ENV_EXAMPLE.read_text()
    assert (
        "grants nobody" in text or "fall open" in text
    ), "the env example must state that an empty operator allowlist grants nobody"


def test_the_runbook_exists() -> None:
    assert _RUNBOOK.is_file(), "the ARC operator runbook is missing"


@pytest.mark.parametrize(("subject", "markers"), sorted(_REQUIRED_RUNBOOK_SUBJECTS.items()))
def test_the_runbook_covers_each_required_subject(subject: str, markers: tuple[str, ...]) -> None:
    text = _RUNBOOK.read_text()
    assert any(marker in text for marker in markers), f"the ARC runbook has no section covering {subject!r}"


def test_the_runbook_warns_against_deleting_undrained_outbox_rows() -> None:
    """The mistake an operator is most likely to make under pressure: the
    gauge is high, deleting rows makes it drop, and the audit record for
    committed state changes is gone."""
    text = _RUNBOOK.read_text()
    assert "Never delete undrained rows" in text


def test_the_runbook_warns_against_removing_retired_signing_keys() -> None:
    """Removing one both breaks verification of old receipts and hides the
    compromise it was retired for."""
    text = _RUNBOOK.read_text()
    assert "Do not remove the old public key" in text


def test_the_runbook_states_that_re_keying_must_be_joint() -> None:
    """Directive prose derives its protection from the parent revision, so
    re-keying a revision alone silently produces undecryptable content."""
    text = _RUNBOOK.read_text()
    assert "together" in text and "directives" in text


def test_the_runbook_distinguishes_a_blocked_resolution_from_a_refusal() -> None:
    """The single most confusing ARC response. Without this an operator
    treats a working policy refusal as an authentication incident."""
    text = _RUNBOOK.read_text()
    assert "blocked_manifest_unverified" in text
    assert "resolution_status" in text


def test_the_runbook_names_what_each_activation_gate_blocks() -> None:
    """A gate list that says something is "not enabled" without saying what
    that stops is not actionable during triage."""
    text = _RUNBOOK.read_text()
    assert "Blocks" in text
    assert "501" in text, "the runbook should name the operation that returns 501 and why"


def test_the_runbook_carries_no_planning_document_citations() -> None:
    """Shipped docs must stand on their own. A future operator cannot
    resolve a reference to a planning artifact they have never seen.

    `make doc-refs` enforces this repo-wide; asserting it here means the
    runbook's own failure is reported by the test that owns the runbook.
    """
    import re

    text = _RUNBOOK.read_text()
    forbidden = (
        r"\bADR-\d+\b",
        r"\bF\d+\.\d+\b",
        r"\bPhase \d+\b",
        r"PRD \u00a7",  # doc-ref: intentional
        r"TDD \u00a7",  # doc-ref: intentional
    )
    for pattern in forbidden:
        assert not re.search(pattern, text), f"the runbook cites a planning document: {pattern}"
