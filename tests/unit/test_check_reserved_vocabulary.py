"""The reserved-vocabulary gate catches a second meaning, and only that.

The gate exists because two collisions were found by hand, one at a time. A gate
that cannot demonstrate a failure would be the same situation with more files, so
every refusal this script can produce is exercised here.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts import check_reserved_vocabulary as gate


@pytest.fixture
def tree(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A miniature repo, so a test never depends on what the real tree happens to hold."""
    schemas = tmp_path / "contextplane" / "api" / "schemas"
    migrations = tmp_path / "contextplane" / "storage" / "migrations" / "versions"
    owner = tmp_path / "contextplane" / "security"
    for directory in (schemas, migrations, owner):
        directory.mkdir(parents=True)
    (owner / "pii_scanner.py").write_text("_POLICY_SEVERITY = {'advisory': 0}\n", encoding="utf-8")

    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "SCHEMA_ROOT", schemas)
    monkeypatch.setattr(gate, "MIGRATION_ROOT", migrations)
    monkeypatch.setattr(
        gate,
        "RESERVED",
        {
            "severity": gate.ReservedWord(
                meaning="the PII scanner's advisory < warn < block ordering",
                defined_by="_POLICY_SEVERITY",
                owner_paths=("contextplane/security/pii_scanner.py",),
                reason="two orderings sharing one field name is a defect",
            )
        },
    )
    monkeypatch.setattr(gate, "ALLOWLIST", ())
    return tmp_path


def test_a_wire_field_that_reuses_a_governed_noun_is_refused(tree: pathlib.Path) -> None:
    """The exact case that was refused by hand: a `severity` field on a new object."""
    (tree / "contextplane/api/schemas/obligations.py").write_text(
        "class ReportingObligation(BaseModel):\n    severity: str\n", encoding="utf-8"
    )

    findings = gate.collect_findings()

    assert [(f.identifier, f.word) for f in findings] == [("severity", "severity")]


def test_a_qualified_field_is_refused_too(tree: pathlib.Path) -> None:
    """`obligation_severity` is the same second meaning wearing a prefix."""
    (tree / "contextplane/api/schemas/obligations.py").write_text(
        "class ReportingObligation(BaseModel):\n    obligation_severity: int\n", encoding="utf-8"
    )

    assert [f.identifier for f in gate.collect_findings()] == ["obligation_severity"]


def test_a_migration_column_is_refused(tree: pathlib.Path) -> None:
    (tree / "contextplane/storage/migrations/versions/0099_x.py").write_text(
        '    op.add_column("t", sa.Column("severity", sa.Text()))\n', encoding="utf-8"
    )

    assert [f.identifier for f in gate.collect_findings()] == ["severity"]


def test_a_word_that_merely_contains_the_noun_is_not_refused(tree: pathlib.Path) -> None:
    """Component-wise, not substring. `adversity` is not a second meaning."""
    (tree / "contextplane/api/schemas/weather.py").write_text(
        "class Report(BaseModel):\n    adversity: str\n", encoding="utf-8"
    )

    assert gate.collect_findings() == []


def test_the_owner_may_use_its_own_word(tree: pathlib.Path) -> None:
    """The reservation protects the meaning; it does not exile the word from the
    module that defines it."""
    schemas = tree / "contextplane/api/schemas"
    (schemas / "unrelated.py").write_text("class A(BaseModel):\n    name: str\n", encoding="utf-8")

    assert gate.collect_findings() == []


def test_a_reservation_whose_owner_stopped_defining_it_is_stale(tree: pathlib.Path) -> None:
    """A registry nobody re-checks stops describing the tree and starts
    describing its own past."""
    (tree / "contextplane/security/pii_scanner.py").write_text("# moved elsewhere\n", encoding="utf-8")

    stale = gate.stale_reservations()

    assert len(stale) == 1
    assert "_POLICY_SEVERITY" in stale[0]


def test_an_allowlist_entry_that_no_longer_names_a_reuse_is_stale(
    tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlist that only ever grows stops meaning "tracked" and starts
    meaning "nobody removes these"."""
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        (
            gate.AllowlistEntry(
                path="contextplane/api/schemas/gone.py",
                identifier="severity",
                word="severity",
                reason="kept for a migration window that has since closed",
            ),
        ),
    )

    assert len(gate.stale_allowlist()) == 1


def test_an_allowlisted_reuse_passes(tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tree / "contextplane/api/schemas/legacy.py").write_text(
        "class Legacy(BaseModel):\n    severity: str\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        (
            gate.AllowlistEntry(
                path="contextplane/api/schemas/legacy.py",
                identifier="severity",
                word="severity",
                reason="a deliberate decision, with the reason the gate requires",
            ),
        ),
    )

    assert gate.collect_findings() == []
    assert gate.stale_allowlist() == []


def test_an_allowlist_entry_cannot_be_written_without_a_reason() -> None:
    """A bare path is indistinguishable from turning the gate off for that file."""
    with pytest.raises(TypeError):
        gate.AllowlistEntry(  # type: ignore[call-arg]
            path="contextplane/api/schemas/x.py", identifier="severity", word="severity"
        )


def test_the_real_tree_passes_today() -> None:
    """Shipped with an empty allowlist, which is what makes every future entry a
    decision somebody made rather than debt somebody inherited."""
    assert gate.collect_findings() == []
    assert gate.stale_reservations() == []
    assert gate.stale_allowlist() == []
    assert gate.ALLOWLIST == ()
