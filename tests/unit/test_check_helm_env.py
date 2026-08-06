"""The helm-env gate is the enforcement for the Settings/.env.example/Helm
deployment contract, so it needs its own tests. Each test plants one
synthetic mutation -- a documented-but-unrendered secret, a rendered dead
key, an unwired (undocumented) Secret key, or a stale exclusion -- and
asserts the checker notices. The mutation is the point: SEC-01 (the chart
documented three secret keys it never rendered, and rendered one dead
`API_TOKEN` no `Settings` field read) was exactly this class of drift, found
by nobody until someone went looking by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_helm_env as gate


@pytest.fixture
def real_values_text() -> str:
    return gate._VALUES_YAML.read_text(encoding="utf-8")


@pytest.fixture
def real_secret_text() -> str:
    return gate._SECRET_TEMPLATE.read_text(encoding="utf-8")


def test_the_real_chart_passes() -> None:
    """The gate's own subject. Fails the moment values.yaml, secret.yaml, or
    .env.example drift from each other again."""
    assert gate.main([]) == 0


def test_every_exclusion_carries_a_reason() -> None:
    for exclusion in gate.EXCLUSIONS:
        assert exclusion.reason.strip(), f"{exclusion.name} has no stated reason"
        assert exclusion.check in {"dead_key", "secret_uncharted"}, f"{exclusion.name} has an unknown check"


# ---------------------------------------------------------------------------
# Settings name derivation, including AliasChoices
# ---------------------------------------------------------------------------


def test_settings_names_includes_every_alias_choice() -> None:
    """registry/config.py's extraction_anthropic_api_key resolves from either
    CLAUDE_API_KEY or ANTHROPIC_API_KEY via AliasChoices -- both must be
    counted as canonical, not just the first."""
    names = gate.settings_env_names()
    assert "CLAUDE_API_KEY" in names
    assert "ANTHROPIC_API_KEY" in names


# ---------------------------------------------------------------------------
# 1. Documented-but-unrendered secret
# ---------------------------------------------------------------------------


def test_documented_but_unrendered_secret_is_reported(real_values_text: str, real_secret_text: str) -> None:
    mutated_secret = real_secret_text.replace(
        "  {{- if (.Values.secrets).claudeApiKey }}\n"
        "  CLAUDE_API_KEY: {{ (.Values.secrets).claudeApiKey | quote }}\n"
        "  {{- end }}\n",
        "",
    )
    assert mutated_secret != real_secret_text, "fixture did not match the real template; update this test"

    violations = gate.check_secret_doc_vs_render(real_values_text, mutated_secret)
    kinds = {(v.kind, v.name) for v in violations}
    assert ("secret_not_rendered", "claudeApiKey") in kinds


# ---------------------------------------------------------------------------
# 2. Rendered dead key
# ---------------------------------------------------------------------------


def test_rendered_dead_configmap_key_is_reported(real_values_text: str, real_secret_text: str) -> None:
    mutated_values = real_values_text.replace('LOG_LEVEL: "info"', 'LOG_LEVEL: "info"\n  BOGUS_DEAD_KEY: "1"')
    assert mutated_values != real_values_text

    violations = gate.check_dead_keys(mutated_values, real_secret_text, exclusions=())
    kinds = {(v.kind, v.name) for v in violations}
    assert ("dead_key", "BOGUS_DEAD_KEY") in kinds


def test_rendered_dead_secret_key_is_reported(real_values_text: str, real_secret_text: str) -> None:
    mutated_secret = real_secret_text.replace(
        "  {{- if (.Values.secrets).claudeApiKey }}",
        "  {{- if (.Values.secrets).mysteryKey }}\n  BOGUS_SECRET_NAME: {{ (.Values.secrets).mysteryKey | quote }}\n"
        "  {{- end }}\n  {{- if (.Values.secrets).claudeApiKey }}",
    )
    assert mutated_secret != real_secret_text

    violations = gate.check_dead_keys(real_values_text, mutated_secret, exclusions=())
    kinds = {(v.kind, v.name) for v in violations}
    assert ("dead_key", "BOGUS_SECRET_NAME") in kinds


def test_real_configmap_and_secret_keys_are_all_alive(real_values_text: str, real_secret_text: str) -> None:
    """Every key the shipped chart renders today is a real Settings field or
    .env.example entry -- pins the state check_dead_keys's own docstring
    claims (EMBEDDING_DIM etc.) so a future edit that reintroduces a dead key
    like the old API_TOKEN or PGBOUNCER_HOST/PORT is caught immediately."""
    assert gate.check_dead_keys(real_values_text, real_secret_text, exclusions=gate.EXCLUSIONS) == []


# ---------------------------------------------------------------------------
# 3. Unwired (undocumented) Secret key
# ---------------------------------------------------------------------------


def test_unwired_secret_key_is_reported(real_values_text: str, real_secret_text: str) -> None:
    """secret.yaml renders a key values.yaml's own comments never mention --
    the chart accepts the value but nothing tells an operator it exists."""
    mutated_secret = real_secret_text.replace(
        "  {{- if (.Values.secrets).claudeApiKey }}",
        "  {{- if (.Values.secrets).mysteryKey }}\n"
        "  MYSTERY_SECRET: {{ (.Values.secrets).mysteryKey | quote }}\n"
        "  {{- end }}\n"
        "  {{- if (.Values.secrets).claudeApiKey }}",
    )
    assert mutated_secret != real_secret_text

    violations = gate.check_secret_doc_vs_render(real_values_text, mutated_secret)
    kinds = {(v.kind, v.name) for v in violations}
    assert ("secret_not_documented", "mysteryKey") in kinds


# ---------------------------------------------------------------------------
# 4. Stale exclusion
# ---------------------------------------------------------------------------


def test_stale_dead_key_exclusion_is_reported(real_values_text: str, real_secret_text: str) -> None:
    planted = (gate.Exclusion(name="NOPE_NOT_A_REAL_KEY", check="dead_key", reason="test"),)
    stale = gate._stale_exclusions(real_values_text, real_secret_text, planted)
    assert len(stale) == 1
    assert "NOPE_NOT_A_REAL_KEY" in stale[0]


def test_stale_secret_uncharted_exclusion_is_reported(real_values_text: str, real_secret_text: str) -> None:
    """An exclusion for a name the chart now renders (e.g. after a fix like
    this task's own CLAUDE_API_KEY addition) is stale the moment it renders."""
    planted = (gate.Exclusion(name="CLAUDE_API_KEY", check="secret_uncharted", reason="test"),)
    stale = gate._stale_exclusions(real_values_text, real_secret_text, planted)
    assert len(stale) == 1
    assert "CLAUDE_API_KEY" in stale[0]


def test_a_still_valid_exclusion_is_not_reported_stale(real_values_text: str, real_secret_text: str) -> None:
    mutated_secret = real_secret_text.replace(
        "  {{- if (.Values.secrets).claudeApiKey }}\n"
        "  CLAUDE_API_KEY: {{ (.Values.secrets).claudeApiKey | quote }}\n"
        "  {{- end }}\n",
        "",
    )
    planted = (gate.Exclusion(name="CLAUDE_API_KEY", check="secret_uncharted", reason="test"),)
    stale = gate._stale_exclusions(real_values_text, mutated_secret, planted)
    assert stale == []


def test_main_fails_on_a_planted_stale_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate,
        "EXCLUSIONS",
        (gate.Exclusion(name="NOPE_NOT_A_REAL_KEY", check="dead_key", reason="test"),),
    )
    assert gate.main([]) == 1


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------


def test_multiline_comment_continuation_does_not_truncate_the_doc_block() -> None:
    """The real pgbouncerUrl comment spans several continuation lines before
    the next key -- a parser that stops at the first non-`key: ""` line would
    silently drop every key documented after it. Regression pin for exactly
    that bug."""
    text = (
        "secrets: {}\n"
        '  # databaseUrl: ""     # required\n'
        '  # pgbouncerUrl: ""    # optional. A long explanation that spans\n'
        "  #                       multiple continuation lines before the\n"
        "  #                       next key.\n"
        '  # oidcDiscoveryUrl: ""   # optional\n'
        "\n"
        "pgbouncer:\n"
        "  enabled: true\n"
    )
    keys = [e.key for e in gate.parse_values_secrets_doc(text)]
    assert keys == ["databaseUrl", "pgbouncerUrl", "oidcDiscoveryUrl"]


def test_metrics_token_env_name_resolves_from_values(real_values_text: str) -> None:
    assert gate.metrics_token_env_name(real_values_text) == "METRICS_BEARER_TOKEN"


# ---------------------------------------------------------------------------
# End-to-end through main(), against a scratch tree -- same convention as
# check_state_access.py / check_visibility_chokepoint.py: the mutation lands
# in tmp_path, the module's own file-path constants are redirected there, and
# the real entry point is exercised rather than only the internal check_*
# helpers above.
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_values_text: str, real_secret_text: str
) -> Path:
    """A scratch copy of the real chart's three inputs, redirected via the
    module's own path constants so `main()` reads the scratch copy."""
    values_path = tmp_path / "values.yaml"
    secret_path = tmp_path / "secret.yaml"
    env_example_path = tmp_path / ".env.example"
    values_path.write_text(real_values_text, encoding="utf-8")
    secret_path.write_text(real_secret_text, encoding="utf-8")
    env_example_path.write_text(gate._ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(gate, "_VALUES_YAML", values_path)
    monkeypatch.setattr(gate, "_SECRET_TEMPLATE", secret_path)
    monkeypatch.setattr(gate, "_ENV_EXAMPLE", env_example_path)
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    return tmp_path


def test_scratch_copy_of_the_real_chart_passes(scratch_chart: Path) -> None:
    """The redirection itself is correct before any mutation is planted."""
    assert gate.main([]) == 0


def test_main_fails_end_to_end_on_a_planted_unrendered_secret(scratch_chart: Path) -> None:
    secret_path = scratch_chart / "secret.yaml"
    secret_path.write_text(
        secret_path.read_text(encoding="utf-8").replace(
            "  {{- if (.Values.secrets).claudeApiKey }}\n"
            "  CLAUDE_API_KEY: {{ (.Values.secrets).claudeApiKey | quote }}\n"
            "  {{- end }}\n",
            "",
        ),
        encoding="utf-8",
    )
    assert gate.main([]) == 1


def test_main_fails_end_to_end_on_a_planted_dead_key(scratch_chart: Path) -> None:
    values_path = scratch_chart / "values.yaml"
    values_path.write_text(
        values_path.read_text(encoding="utf-8").replace(
            'LOG_LEVEL: "info"', 'LOG_LEVEL: "info"\n  BOGUS_DEAD_KEY: "1"'
        ),
        encoding="utf-8",
    )
    assert gate.main([]) == 1


def test_main_fails_end_to_end_on_a_planted_unwired_secret_key(scratch_chart: Path) -> None:
    secret_path = scratch_chart / "secret.yaml"
    secret_path.write_text(
        secret_path.read_text(encoding="utf-8").replace(
            "  {{- if (.Values.secrets).claudeApiKey }}",
            "  {{- if (.Values.secrets).mysteryKey }}\n"
            "  MYSTERY_SECRET: {{ (.Values.secrets).mysteryKey | quote }}\n"
            "  {{- end }}\n"
            "  {{- if (.Values.secrets).claudeApiKey }}",
        ),
        encoding="utf-8",
    )
    assert gate.main([]) == 1
