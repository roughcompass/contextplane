"""Unit tests for the check_arc_approval_writers gate script.

Each planted-violation test writes a real, syntactically valid Python module
into a scratch directory and proves the gate finds it -- a gate that always
exits green when nothing writes the restricted evidence type is
indistinguishable from a gate that never ran. The false-positive tests prove
the AST anchor is call-sites, not prose: a docstring or comment that merely
discusses the same words must not trip it, or the gate would fail on its own
module docstring and on every file this restriction is discussed in.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_arc_approval_writers import ALLOWLIST, main  # noqa: E402

_WRITE_CALL = (
    "from sqlalchemy import text\n"
    "\n"
    "\n"
    "async def plant(session):\n"
    "    await session.execute(\n"
    "        text(\n"
    '            "INSERT INTO arc_approval_evidence ("\n'
    '            "  evidence_id, evidence_type"\n'
    "            \") VALUES (:eid, 'artifact_activation')\"\n"
    "        ),\n"
    '        {"eid": "x"},\n'
    "    )\n"
)

# The D2 protocol's own table -- no `evidence_type` literal anywhere in this
# statement, because that table carries no such column. Any write to it at
# all is a violation unless the writer is allowlisted.
_PROJECTION_EVIDENCE_WRITE_CALL = (
    "from sqlalchemy import text\n"
    "\n"
    "\n"
    "async def plant(session):\n"
    "    await session.execute(\n"
    "        text(\n"
    '            "INSERT INTO arc_projection_approval_evidence ("\n'
    '            "  evidence_id, approval_challenge_id"\n'
    '            ") VALUES (:eid, :cid)"\n'
    "        ),\n"
    '        {"eid": "x", "cid": "y"},\n'
    "    )\n"
)


def _write(directory: Path, name: str, content: str) -> Path:
    p = directory / name
    p.write_text(content, encoding="utf-8")
    return p


def test_a_second_writer_is_caught(tmp_path: Path) -> None:
    """The gate's whole reason to exist: a module that writes the restricted
    evidence type fails the gate, by name and line."""
    _write(tmp_path, "second_writer.py", _WRITE_CALL)
    assert main(["--paths", str(tmp_path)]) == 1


def test_a_clean_module_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fine.py",
        "from sqlalchemy import text\n\n\nasync def other(session):\n    await session.execute(text('SELECT 1'))\n",
    )
    assert main(["--paths", str(tmp_path)]) == 0


def test_removing_the_planted_writer_restores_a_clean_exit(tmp_path: Path) -> None:
    """Prove the gate's failure mode is not sticky: plant, fail, remove, pass.

    This is the exact plant/remove sequence the task's own verification
    performs against the real tree -- reproduced here against a scratch
    directory so it runs in every CI invocation, not only once by hand.
    """
    planted = _write(tmp_path, "second_writer.py", _WRITE_CALL)
    assert main(["--paths", str(tmp_path)]) == 1

    planted.unlink()
    assert main(["--paths", str(tmp_path)]) == 0
    assert list(tmp_path.glob("*.py")) == []


def test_a_docstring_mentioning_the_same_words_does_not_trip_the_gate(tmp_path: Path) -> None:
    """AST/call-site anchoring, not prose grep: a module whose *docstring*
    discusses `INSERT INTO arc_approval_evidence` and `artifact_activation`
    -- exactly what this gate's own module docstring does -- must not be
    flagged merely for saying so."""
    _write(
        tmp_path,
        "discusses_it.py",
        '"""This module never writes evidence_type = \'artifact_activation\' into\n'
        "arc_approval_evidence -- INSERT INTO arc_approval_evidence only ever\n"
        'appears in prose here, never as a call argument."""\n\n'
        "from sqlalchemy import text\n\n\n"
        "async def unrelated(session):\n"
        "    await session.execute(text('SELECT 1'))\n",
    )
    assert main(["--paths", str(tmp_path)]) == 0


def test_a_second_writer_into_the_projection_evidence_table_is_caught(tmp_path: Path) -> None:
    """The D2 table has no `evidence_type` literal to match on -- any write
    to it is governed, since every row it holds is `artifact_activation`-
    class evidence by construction."""
    _write(tmp_path, "second_writer.py", _PROJECTION_EVIDENCE_WRITE_CALL)
    assert main(["--paths", str(tmp_path)]) == 1


def test_removing_the_planted_projection_evidence_writer_restores_a_clean_exit(tmp_path: Path) -> None:
    """The same plant/fail/remove/pass sequence already proved for the
    legacy table above, reproduced for the newer projection-evidence
    table."""
    planted = _write(tmp_path, "second_writer.py", _PROJECTION_EVIDENCE_WRITE_CALL)
    assert main(["--paths", str(tmp_path)]) == 1

    planted.unlink()
    assert main(["--paths", str(tmp_path)]) == 0
    assert list(tmp_path.glob("*.py")) == []


def test_a_write_of_a_different_evidence_type_does_not_trip_the_gate(tmp_path: Path) -> None:
    """`exception_approval` has a real first-party writer; this gate
    constrains one literal value, not the whole table."""
    _write(
        tmp_path,
        "exception_writer.py",
        "from sqlalchemy import text\n\n\n"
        "async def write_exception_evidence(session):\n"
        "    await session.execute(\n"
        "        text(\n"
        '            "INSERT INTO arc_approval_evidence (evidence_id, evidence_type) "\n'
        "            \"VALUES (:eid, 'exception_approval')\"\n"
        "        ),\n"
        '        {"eid": "x"},\n'
        "    )\n",
    )
    assert main(["--paths", str(tmp_path)]) == 0


def test_an_allowlisted_file_is_exempt(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import check_arc_approval_writers as gate

    planted = _write(tmp_path, "allowed_writer.py", _WRITE_CALL)
    monkeypatch.setattr(gate, "ALLOWLIST", frozenset({"allowed_writer.py"}))
    assert gate.main(["--paths", str(tmp_path)]) == 0
    assert planted.exists()


def test_a_second_writer_into_the_new_table_bites_even_with_the_real_allowlist(tmp_path: Path) -> None:
    """Re-proves the gate still bites after the real allowlist addition
    below: a *different*, unlisted module writing the new table is still
    caught -- the allowlist names one file, not a blanket exemption for
    the table.
    """
    _write(tmp_path, "second_writer.py", _PROJECTION_EVIDENCE_WRITE_CALL)
    assert main(["--paths", str(tmp_path)]) == 1


def test_the_real_approval_challenge_service_is_the_allowlisted_writer() -> None:
    """`approval_challenge.py`'s own real `arc_projection_approval_evidence`
    INSERT is exactly what the allowlist entry exists to cover -- scanning
    it alone must pass."""
    module = Path(__file__).resolve().parents[2] / "registry" / "arc" / "service" / "approval_challenge.py"
    assert module.exists()
    assert main(["--paths", str(module)]) == 0


def test_exactly_one_writer_is_allowlisted() -> None:
    """`approval_challenge.py` is the deliberate, reviewed addition the
    module docstring describes: the D2 protocol's first-party writer for
    `arc_projection_approval_evidence`, and it is the only entry. A future
    allowlist entry is a deliberate, reviewed addition -- not a default
    this test assumes away.
    """
    assert ALLOWLIST == frozenset({"registry/arc/service/approval_challenge.py"})


def test_the_real_registry_tree_passes() -> None:
    """The gate's own subject: the real repository, today. No production
    module writes `artifact_activation` evidence."""
    package = Path(__file__).resolve().parents[2] / "registry"
    assert main(["--paths", str(package)]) == 0


def test_missing_scope_fails_rather_than_passing_silently(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert main(["--paths", str(missing)]) == 1


def test_explain_flag_runs_without_scanning(tmp_path: Path) -> None:
    assert main(["--explain"]) == 0
