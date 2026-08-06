"""The context-aware docs-env gate needs its own tests, like every other gate.

The design this pins down is deliberately *not* a bare all-caps token scan:
a placeholder like `TENANT_ID` must survive untouched while a genuinely dead
variable in an `export`/`.env`-block/`docker -e`/prose-env-var context is
caught. Each test plants one specific shape and asserts the gate notices --
or, for the shapes the docstring calls out as excluded on purpose, that it
correctly does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_doc_env_mentions as gate


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _scan(doc: Path, rel: str) -> list[gate.Violation]:
    """check_file against the real Settings-derived names and the real
    ALLOWLIST -- what every un-monkeypatched test wants."""
    names = gate.settings_env_names()
    allowed = gate._allowlisted_names()
    return gate.check_file(doc, rel=rel, settings_names=names, allowlisted=allowed)


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real docs."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a new phantom variable lands."""
    assert gate.main([]) == 0


def test_every_exemption_carries_a_reason() -> None:
    """An exemption with no reason is a bypass wearing the gate's clothes."""
    for exemption in gate.ALLOWLIST:
        assert exemption.reason.strip(), f"{sorted(exemption.names)} has no stated reason"
        assert exemption.names, "an exemption naming no names is a permission for nothing"


# ---------------------------------------------------------------------------
# Settings-name derivation
# ---------------------------------------------------------------------------


def test_settings_names_include_a_plain_field() -> None:
    assert "DATABASE_URL" in gate.settings_env_names()


def test_settings_names_use_the_alias_not_the_field_name() -> None:
    """`webhook_secret_github` reads `GITHUB_WEBHOOK_SECRET` via
    `validation_alias` -- the field-name-upper spelling must not also appear,
    or a doc could use either name and this gate would silently accept both."""
    names = gate.settings_env_names()
    assert "GITHUB_WEBHOOK_SECRET" in names
    assert "WEBHOOK_SECRET_GITHUB" not in names


# ---------------------------------------------------------------------------
# check_file: the four matched contexts, and what they correctly exclude
# ---------------------------------------------------------------------------


def test_a_dead_variable_in_export_context_is_flagged_with_file_and_line(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\n```bash\nexport AUTH_CLAIM_CACHE_TTL_SECONDS=60\n```\n",
    )
    violations = _scan(doc, "docs/a.md")
    assert len(violations) == 1
    v = violations[0]
    assert v.path == "docs/a.md"
    assert v.line == 4
    assert v.name == "AUTH_CLAIM_CACHE_TTL_SECONDS"
    assert v.context == "export"


def test_a_dead_variable_in_docker_e_context_is_flagged(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\n```bash\ndocker run --rm -e FAKE_SETTING=1 registry:dev\n```\n",
    )
    violations = _scan(doc, "docs/a.md")
    assert len(violations) == 1
    assert violations[0].name == "FAKE_SETTING"
    assert violations[0].context == "docker -e"


def test_a_dead_variable_in_a_pure_env_block_is_flagged(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\n```\nDATABASE_URL=postgresql://x\nAUTH_SEAL_ID_HEADER_ALIAS=x-seal-id\n```\n",
    )
    violations = _scan(doc, "docs/a.md")
    assert len(violations) == 1
    assert violations[0].name == "AUTH_SEAL_ID_HEADER_ALIAS"
    assert violations[0].context == ".env block"


def test_a_dead_variable_named_in_prose_is_flagged(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "docs/a.md",
        "# A\n\nSet the `AUTH_STALE_CEILING_SECONDS` environment variable to tune this.\n",
    )
    violations = _scan(doc, "docs/a.md")
    assert len(violations) == 1
    assert violations[0].name == "AUTH_STALE_CEILING_SECONDS"
    assert violations[0].context == "prose"


def test_a_real_setting_is_not_flagged(tmp_path: Path) -> None:
    doc = _write(tmp_path, "docs/a.md", "# A\n\n```bash\nexport DATABASE_URL=postgresql://x\n```\n")
    assert _scan(doc, "docs/a.md") == []


def test_an_alias_only_setting_is_not_flagged(tmp_path: Path) -> None:
    """`GITHUB_WEBHOOK_SECRET` is a real Settings name only via `validation_alias`
    -- the gate must resolve the alias, not just the field's own spelling."""
    doc = _write(tmp_path, "docs/a.md", "# A\n\n```bash\nexport GITHUB_WEBHOOK_SECRET=shh\n```\n")
    assert _scan(doc, "docs/a.md") == []


def test_a_placeholder_in_prose_is_not_flagged(tmp_path: Path) -> None:
    """`TENANT_ID` is an ID path-parameter placeholder, not an environment
    variable -- the whole reason this gate is not a bare all-caps scan."""
    doc = _write(tmp_path, "docs/a.md", "# A\n\nPass the `TENANT_ID` value as the tenant path parameter.\n")
    assert _scan(doc, "docs/a.md") == []


def test_a_dynamic_credential_ref_example_from_the_allowlist_is_not_flagged(tmp_path: Path) -> None:
    doc = _write(tmp_path, "docs/a.md", "# A\n\n```bash\nexport GITHUB_API_TOKEN=ghp_xxx\n```\n")
    assert _scan(doc, "docs/a.md") == []


def test_export_of_a_command_substitution_is_not_flagged(tmp_path: Path) -> None:
    """`export TOKEN=$(make dev-jwt)` captures a command's output for reuse
    in the same script -- a shell idiom, not a configuration statement."""
    doc = _write(tmp_path, "docs/a.md", "# A\n\n```bash\nexport TOKEN=$(make dev-jwt)\n```\n")
    assert _scan(doc, "docs/a.md") == []


def test_a_mixed_script_block_bare_assignment_is_not_flagged(tmp_path: Path) -> None:
    """A pagination loop's `CURSOR=""` accumulator sits in a block that also
    has a `while`/`curl`/`done` -- not a pure `.env` snippet, so nothing in
    it is treated as a configuration surface."""
    doc = _write(
        tmp_path,
        "docs/a.md",
        (
            "# A\n\n```bash\n"
            'CURSOR=""\n'
            "while true; do\n"
            '  RESP=$(curl -s "https://api.example.com?cursor=$CURSOR")\n'
            '  [ -z "$RESP" ] && break\n'
            "done\n"
            "```\n"
        ),
    )
    assert _scan(doc, "docs/a.md") == []


def test_an_env_prefixed_command_line_is_not_matched() -> None:
    """A known, documented gap: `NAME=x NAME2=y command` (an env-prefixed
    invocation) is not a pure assignment line and is not matched by any
    bucket -- a false negative, not a false positive, and the module
    docstring says so."""
    text = "```bash\nEMBEDDING_DIM=1536 EMBEDDING_DIM_ALLOW_REBUILD=true alembic upgrade head\n```\n"
    assert gate.mentions_of(text) == []


# ---------------------------------------------------------------------------
# Stale allowlist entries
# ---------------------------------------------------------------------------


def test_a_stale_allowlist_entry_not_mentioned_anywhere_fails(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        (gate.Exemption(names=frozenset({"NOBODY_MENTIONS_THIS"}), reason="synthetic, for the test"),),
    )
    _write(repo_root, "docs/a.md", "# A\n\n```bash\nexport DATABASE_URL=postgresql://x\n```\n")
    assert gate.main(["--paths", "docs"]) == 1
    out = capsys.readouterr().out
    assert "stale-allowlist-entry" in out
    assert "NOBODY_MENTIONS_THIS" in out


def test_a_stale_allowlist_entry_now_a_real_setting_fails(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exemption that has since become a real Settings field is also a
    permission nobody needs -- it should say so, not merely 'unused'."""
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        (gate.Exemption(names=frozenset({"DATABASE_URL"}), reason="synthetic, for the test"),),
    )
    _write(repo_root, "docs/a.md", "# A\n\n```bash\nexport DATABASE_URL=postgresql://x\n```\n")
    assert gate.main(["--paths", "docs"]) == 1
    out = capsys.readouterr().out
    assert "stale-allowlist-entry" in out
    assert "DATABASE_URL" in out
    assert "real Settings name" in out


# ---------------------------------------------------------------------------
# main(): CLI-level cases
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo_root, "docs/a.md", "# A\n\n```bash\nexport AUTH_TENANT_ID_HEADER=x\n```\n")
    assert gate.main(["--paths", "docs"]) == 1
    out = capsys.readouterr().out
    assert "docs/a.md:4" in out
    assert "AUTH_TENANT_ID_HEADER" in out


def test_an_out_of_scope_path_fails_rather_than_passing_silently(repo_root: Path) -> None:
    assert gate.main(["--paths", "does/not/exist"]) == 1


def test_explain_describes_the_gate_and_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--explain"]) == 0
    out = capsys.readouterr().out
    assert "environment variable" in out.lower()
    assert "ALLOWLIST" in out
