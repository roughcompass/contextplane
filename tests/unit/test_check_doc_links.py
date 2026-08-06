"""The relative-link gate needs its own tests, like every other gate.

Each test plants one specific link shape -- a missing file, an escape past
the repository root, a missing anchor, a valid link, an external URL, the
bypass marker -- and asserts the gate notices (or correctly does not). The
mutation is the point: a resolver that matched nothing would pass every
"not flagged" test for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_doc_links as gate


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real docs."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a new broken link lands."""
    assert gate.main([]) == 0


# ---------------------------------------------------------------------------
# check_file: direct, function-level cases
# ---------------------------------------------------------------------------


def test_a_link_to_a_missing_file_is_flagged_with_file_and_line(tmp_path: Path) -> None:
    doc = _write(tmp_path, "docs/a.md", "# A\n\nSee [elsewhere](does-not-exist.md).\n")
    violations = gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path)
    assert len(violations) == 1
    v = violations[0]
    assert v.path == "docs/a.md"
    assert v.line == 3
    assert "does-not-exist.md" in v.target
    assert "no file or directory" in v.reason


def test_a_valid_relative_link_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "docs/b.md", "# B\n")
    doc = _write(tmp_path, "docs/a.md", "# A\n\nSee [b](b.md).\n")
    assert gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path) == []


def test_a_link_escaping_the_repository_root_is_flagged(tmp_path: Path) -> None:
    """A link that resolves outside the repo root fails even when the target
    genuinely exists on disk (a sibling checkout, in the real bug this gate
    fixes) -- existing outside the root is exactly the problem."""
    outside_dir = tmp_path.parent / f"{tmp_path.name}-sibling"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "CLAUDE.md").write_text("irrelevant\n", encoding="utf-8")
    doc = _write(tmp_path, "README.md", f"# Root\n\nSee [rules](../{outside_dir.name}/CLAUDE.md).\n")
    violations = gate.check_file(doc, rel="README.md", repo_root=tmp_path)
    assert len(violations) == 1
    assert "escapes" in violations[0].reason or "outside the repository root" in violations[0].reason


def test_a_missing_anchor_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "docs/b.md", "# B\n\n## Real heading\n")
    doc = _write(tmp_path, "docs/a.md", "# A\n\nSee [b](b.md#not-a-real-heading).\n")
    violations = gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path)
    assert len(violations) == 1
    assert "not-a-real-heading" in violations[0].reason


def test_a_valid_anchor_matches_the_slugified_heading(tmp_path: Path) -> None:
    _write(tmp_path, "docs/b.md", "# B\n\n## Real heading\n")
    doc = _write(tmp_path, "docs/a.md", "# A\n\nSee [b](b.md#real-heading).\n")
    assert gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path) == []


def test_a_bare_in_page_anchor_checks_this_files_own_headings(tmp_path: Path) -> None:
    body = "# A\n\n## Section one\n\nSee [above](#section-one) and [gone](#section-two).\n"
    doc = _write(tmp_path, "docs/a.md", body)
    violations = gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path)
    assert len(violations) == 1
    assert "section-two" in violations[0].reason


def test_external_urls_are_never_checked(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\n[external](https://example.com/does-not-exist)\n[mail](mailto:a@example.com)\n",
    )
    assert gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path) == []


def test_the_bypass_marker_exempts_a_single_line(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\n[broken](nowhere.md) <!-- doc-link: intentional -->\n",
    )
    assert gate.check_file(doc, rel="docs/a.md", repo_root=tmp_path) == []


def test_a_directory_link_is_resolved_like_a_file(tmp_path: Path) -> None:
    (tmp_path / "deploy").mkdir()
    doc = _write(tmp_path, "README.md", "# Root\n\nSee [deploy](deploy/).\n")
    assert gate.check_file(doc, rel="README.md", repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# GitHub-slug computation
# ---------------------------------------------------------------------------


def test_slugify_matches_githubs_double_hyphen_quirk() -> None:
    """A removed character between two spaces leaves both spaces behind,
    which GitHub turns into two hyphens -- an observable, real GitHub anchor
    shape, not a bug in this resolver."""
    assert gate.slugify("Cache + stale-on-failure") == "cache--stale-on-failure"


def test_slugify_keeps_underscores_inside_a_code_span() -> None:
    """`LOG_FORMAT=text` guidance -> the '=' is dropped (not replaced with a
    space), and the underscore in LOG_FORMAT is kept -- this is the exact
    heading/anchor pair used in docs/06-operations/01-ops.md."""
    assert gate.slugify("`LOG_FORMAT=text` guidance") == "log_formattext-guidance"


def test_duplicate_headings_get_githubs_incrementing_suffix() -> None:
    text = "# Doc\n\n## Setup\n\ntext\n\n## Setup\n\nmore text\n\n## Setup\n\neven more\n"
    slugs = gate.anchor_slugs(text)
    assert slugs == {"doc", "setup", "setup-1", "setup-2"}


# ---------------------------------------------------------------------------
# main(): CLI-level cases
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo_root, "docs/a.md", "# A\n\n[broken](nowhere.md)\n")
    assert gate.main(["--paths", "docs"]) == 1
    out = capsys.readouterr().out
    assert "docs/a.md:3" in out
    assert "nowhere.md" in out


def test_main_is_clean_when_every_link_resolves(repo_root: Path) -> None:
    _write(repo_root, "docs/b.md", "# B\n")
    _write(repo_root, "docs/a.md", "# A\n\n[b](b.md)\n")
    assert gate.main(["--paths", "docs"]) == 0


def test_an_out_of_scope_path_fails_rather_than_passing_silently(repo_root: Path) -> None:
    assert gate.main(["--paths", "does/not/exist"]) == 1


def test_explain_describes_the_gate_and_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--explain"]) == 0
    out = capsys.readouterr().out
    assert "repository root" in out
    assert "doc-link: intentional" in out
