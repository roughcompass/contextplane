"""The env-documentation gate needs its own tests, like every other gate.

A gate that silently matches nothing reads as enforcement in review while the
drift it exists to catch continues. This one found four genuinely undocumented
variables on its first run, which is the evidence that the drift was real — so the
tests pin both that it catches a gap and that it does not fire on the things that
merely look like variables.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_env_documented as gate


def test_the_real_files_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's own subject. Fails the moment the two documents drift.

    Both files are located from this test rather than from the gate's own idea of
    where the repository sits. The gate resolves them under a parent directory
    holding a checkout named `registry`, which a git worktree is not — so this
    test failed on a missing file there rather than comparing anything, in
    exactly the checkouts used to isolate concurrent work.
    """
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", repo / ".env.example")
    monkeypatch.setattr(gate, "_REFERENCE", repo / "docs" / "05-reference" / "03-configuration.md")

    assert gate.main([]) == 0


def test_a_variable_only_in_the_example_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An operator copying the example would set it without knowing what it does."""
    example = tmp_path / "env"
    example.write_text("KNOWN=1\nMYSTERY_KNOB=2\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("| `KNOWN` | 1 | documented |\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    undocumented, unoffered = gate.compare()
    assert undocumented == {"MYSTERY_KNOB"}
    assert unoffered == set()


def test_a_variable_only_in_the_reference_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The more embarrassing half: the reference promises a knob nobody copying
    the example would know exists."""
    example = tmp_path / "env"
    example.write_text("KNOWN=1\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("| `KNOWN` | | |\n| `PHANTOM_SETTING` | | |\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    undocumented, unoffered = gate.compare()
    assert undocumented == set()
    assert unoffered == {"PHANTOM_SETTING"}


def test_a_commented_out_variable_still_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`# CLAUDE_API_KEY=` is how the example offers a secret without setting one.
    It is still part of the surface and still needs documenting."""
    example = tmp_path / "env"
    example.write_text("# SECRET_KEY=\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("nothing here\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    undocumented, _ = gate.compare()
    assert undocumented == {"SECRET_KEY"}


def test_documented_in_prose_rather_than_a_table_row_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Matching backticked tokens rather than parsing tables is deliberate: a
    variable explained in a paragraph is documented."""
    example = tmp_path / "env"
    example.write_text("SOME_FLAG=1\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("Set `SOME_FLAG` when you want the thing.\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    assert gate.compare() == (set(), set())


def test_sql_and_protocol_tokens_are_not_treated_as_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The reference is full of backticked `JSONB`, `PATCH`, `OIDC`. A gate that
    demanded those in the example would be noise, and noise gets a gate
    switched off."""
    example = tmp_path / "env"
    example.write_text("REAL_VAR=1\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text(
        "| `REAL_VAR` | | Stored as `JSONB`, set via `PATCH`, validated by `OIDC`. |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    assert gate.compare() == (set(), set())


def test_log_level_values_are_not_variables() -> None:
    """`DEBUG` and `INFO` are values of LOG_LEVEL, and appear backticked next to
    it. Documenting a value is not promising a variable."""
    for value in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        assert value in gate._NOT_VARIABLES


def test_entitlement_grammar_values_are_not_variables() -> None:
    """`PRD`, `NPD`, `DEV` are environment names passed to the entitlement
    service, and `REGISTRY` / `GRAPHREGISTRY` are discriminators."""
    for value in ("PRD", "NPD", "DEV", "REGISTRY", "GRAPHREGISTRY"):
        assert value in gate._NOT_VARIABLES


def test_a_missing_file_fails_rather_than_passing_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A renamed or moved document must break the gate, not empty it. An empty
    comparison trivially agrees."""
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", tmp_path / "does-not-exist")
    assert gate.main([]) == 1


def test_explain_describes_the_gate_and_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--explain"]) == 0
    assert "drift" in capsys.readouterr().out


def test_a_violation_exits_non_zero_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    example = tmp_path / "env"
    example.write_text("UNDOCUMENTED_THING=1\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("nothing\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", example)
    monkeypatch.setattr(gate, "_REFERENCE", reference)

    assert gate.main([]) == 1
    assert "UNDOCUMENTED_THING" in capsys.readouterr().err
