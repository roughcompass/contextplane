"""Pins the env-var parsing grammar in `registry/config.py`.

This file exists to protect one thing: the mapping from environment-variable
strings to `Settings` field values. That grammar is easy to get subtly wrong
during any refactor of `config.py` (for example, swapping the dataclass +
`get_settings()` pair for a `pydantic-settings` model) because the two
call sites -- direct `Settings(...)` construction (used pervasively by
tests) and `get_settings()` (used by the running app) -- have historically
disagreed about how much parsing happens. A change that "obviously" just
moves code around can silently change what an operator's env file produces.

Every case here is derived by reading `registry/config.py` line by line, not
by guessing at intended behavior. Where the current code's behavior looks
like a bug (e.g. the two boolean grammars disagree on how whitespace-padded
values resolve), the test pins the *current* behavior anyway -- fixing it is
a separate, deliberate change, not a side effect of a refactor.

Structure:
  - Pure-function tests hit the parsing helpers (`_parse_csv_list`,
    `_parse_operator_allowlist`, `_resolve_extraction_provider`,
    `_resolve_embedding_provider`, `_parse_role_mapping`) directly. These
    functions carry the grammar today and must keep carrying it, whatever
    object reattaches them.
  - Everything that is only expressed inline inside `get_settings()` today
    (the two boolean grammars, LOG_LEVEL, BUILD_REVISION, the env-name
    mismatches, the cross-field URL defaults) is pinned through
    `get_settings()` itself, via `monkeypatch` + a scrubbed environment.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from registry.config import (
    Settings,
    _parse_csv_list,
    _parse_operator_allowlist,
    _parse_role_mapping,
    _resolve_embedding_provider,
    _resolve_extraction_provider,
    get_settings,
)

# ---------------------------------------------------------------------------
# Every env var name `registry/config.py` reads today, plus a few
# "looks-plausible-but-wrong" decoy names (the field name in SCREAMING_SNAKE,
# for the four fields whose env var is deliberately not that). Clearing all
# of these before each scenario means a test only ever sees the variables it
# explicitly sets — no ambient shell/CI export can leak into an assertion.
# ---------------------------------------------------------------------------
_ALL_CONFIG_ENV_VARS: tuple[str, ...] = (
    "DATABASE_URL",
    "PGBOUNCER_URL",
    "SCHEDULER_JOBSTORE_URL",
    "SCHEDULER_USE_MEMORY_JOBSTORE",
    "EMBEDDING_MODEL",
    "EMBEDDING_PROVIDER",
    "EXTRACTION_PROVIDER",
    "EXTRACTION_MODEL",
    "EXTRACTION_TIMEOUT_S",
    "EMBEDDING_MODEL_PATH",
    "EMBEDDING_DIM",
    "EMBEDDING_CHUNK_TOKENS",
    "EMBEDDING_CACHE_MAXSIZE",
    "EMBEDDING_HTTP_ENDPOINT",
    "EMBEDDING_HTTP_CONNECT_TIMEOUT_MS",
    "EMBEDDING_HTTP_READ_TIMEOUT_MS",
    "EMBEDDING_HTTP_MAX_RETRIES",
    "OUTBOX_POLL_INTERVAL_S",
    "CONSOLIDATION_SWEEP_INTERVAL_S",
    "OUTBOX_BATCH_SIZE",
    "OUTBOX_MAX_ATTEMPTS",
    "BACKFILL_BATCH_SIZE",
    "OIDC_DISCOVERY_URL",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_WRITE_PER_MINUTE",
    "RATE_LIMIT_READ_PER_MINUTE",
    "USAGE_RETENTION_DAYS",
    "METRICS_BEARER_TOKEN",
    "OTLP_ENDPOINT",
    "SERVICE_NAME",
    "OTLP_EXPORTER_TIMEOUT_S",
    "CONNECTOR_RUN_TIMEOUT_S",
    "GITHUB_WEBHOOK_SECRET",
    "GITLAB_WEBHOOK_SECRET",
    "WEBHOOK_DRAIN_INTERVAL_S",
    "WEBHOOK_REQUEST_TIMEOUT_S",
    "WEBHOOK_BATCH_SIZE",
    "REGISTRY_HTTP_METHODS_MODE",
    "REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR",
    "OIDC_ISSUER_ALLOWLIST",
    "OIDC_CLIENT_ID_ALLOWLIST",
    "ARC_GLOBAL_OPERATOR_ALLOWLIST",
    "BUILD_REVISION",
    "OIDC_MAX_TOKEN_TTL_SECONDS",
    "RESOURCE_URI_ALLOWLIST",
    "ENTITLEMENT_SERVICE_URL",
    "ENTITLEMENT_SERVICE_ENV",
    "ENTITLEMENT_SERVICE_DISCRIMINATOR",
    "ENTITLEMENT_ROLE_MAPPING",
    "ENTITLEMENT_CONNECT_TIMEOUT_MS",
    "ENTITLEMENT_READ_TIMEOUT_MS",
    "ENTITLEMENT_MAX_RETRIES",
    "ENTITLEMENT_CACHE_MAX_ENTRIES",
    "PROGRESSION_DEFINITION_CACHE_TTL_SECONDS",
    "LOG_FORMAT",
    "LOG_LEVEL",
    # Decoys: the field-name-shaped env var for the four fields whose real
    # env var is deliberately different. Must never be read.
    "WEBHOOK_SECRET_GITHUB",
    "WEBHOOK_SECRET_GITLAB",
    "HTTP_METHODS_MODE",
    "HTTP_METHOD_ALIAS_SEPARATOR",
)


def _settings_from_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Settings:
    """Scrub every config-relevant env var, set exactly `env`, call `get_settings()`.

    This is the only construction path that exercises the grammar the way a
    deployment does -- direct `Settings(**kwargs)` construction (used by the
    rest of the test suite) bypasses `get_settings()` entirely and, today,
    most of this parsing along with it.
    """
    for name in _ALL_CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return get_settings()


# ---------------------------------------------------------------------------
# Pure-function grammar: _parse_csv_list
# ---------------------------------------------------------------------------


class TestParseCsvList:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, []),
            ("", []),
            (" ", []),  # whitespace-only is truthy but strips to nothing per entry
            ("a,b,c", ["a", "b", "c"]),
            (" a , b ,c ", ["a", "b", "c"]),
            ("a,,b,", ["a", "b"]),  # doubled/trailing commas drop empty entries
            (",,,", []),
        ],
    )
    def test_grammar(self, raw: str | None, expected: list[str]) -> None:
        assert _parse_csv_list(raw) == expected


# ---------------------------------------------------------------------------
# Pure-function grammar: _parse_operator_allowlist
# ---------------------------------------------------------------------------


class TestParseOperatorAllowlist:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ()),
            ("", ()),
            ("https://issuer.example|subject-a", (("https://issuer.example", "subject-a"),)),
            (
                "https://issuer-a.example|subject-a,https://issuer-b.example|subject-b",
                (("https://issuer-a.example", "subject-a"), ("https://issuer-b.example", "subject-b")),
            ),
            (
                " https://issuer.example | subject-a , ",
                (("https://issuer.example", "subject-a"),),
            ),
            # '|' is the delimiter, not ':' -- issuers are URLs and contain colons.
            ("https://issuer.example:8443|subject-a", (("https://issuer.example:8443", "subject-a"),)),
        ],
    )
    def test_grammar(self, raw: str | None, expected: tuple[tuple[str, str], ...]) -> None:
        assert _parse_operator_allowlist(raw) == expected

    def test_missing_delimiter_raises(self) -> None:
        with pytest.raises(ValueError, match="missing the '\\|' delimiter"):
            _parse_operator_allowlist("https://issuer.example-no-pipe")

    def test_empty_issuer_raises(self) -> None:
        with pytest.raises(ValueError, match="empty issuer or subject"):
            _parse_operator_allowlist("|subject-a")

    def test_empty_subject_raises(self) -> None:
        with pytest.raises(ValueError, match="empty issuer or subject"):
            _parse_operator_allowlist("https://issuer.example|")


# ---------------------------------------------------------------------------
# Pure-function grammar: _parse_role_mapping
# ---------------------------------------------------------------------------


class TestParseRoleMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, {}),
            ("", {}),
            ("ADMIN:admin", {"ADMIN": "admin"}),
            (
                "ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor",
                {"ADMIN": "admin", "PRODUCER": "producer", "CONSUMER": "consumer", "AUDITOR": "auditor"},
            ),
            (" ADMIN : admin , PRODUCER : producer ", {"ADMIN": "admin", "PRODUCER": "producer"}),
            ("ADMIN:admin,,PRODUCER:producer,", {"ADMIN": "admin", "PRODUCER": "producer"}),
            # Duplicate external keys: last-wins (LDAP rename rollouts).
            ("ADMIN:admin,ADMIN:producer", {"ADMIN": "producer"}),
        ],
    )
    def test_grammar(self, raw: str | None, expected: dict[str, str]) -> None:
        assert _parse_role_mapping(raw) == expected

    def test_missing_colon_raises(self) -> None:
        with pytest.raises(ValueError, match="missing the ':' delimiter"):
            _parse_role_mapping("ADMIN:admin,NOTAVALIDPAIR")


# ---------------------------------------------------------------------------
# Pure-function grammar: _resolve_extraction_provider
# ---------------------------------------------------------------------------


class TestResolveExtractionProvider:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "noop"),
            ("", "noop"),
            ("   ", "noop"),
            ("noop", "noop"),
            ("NOOP", "noop"),
            ("local", "local"),
            ("Local", "local"),
            ("anthropic", "anthropic"),
            (" ANTHROPIC ", "anthropic"),
        ],
    )
    def test_grammar(self, raw: str | None, expected: str) -> None:
        assert _resolve_extraction_provider(raw) == expected

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown EXTRACTION_PROVIDER"):
            _resolve_extraction_provider("anthropik")


# ---------------------------------------------------------------------------
# Pure-function grammar: _resolve_embedding_provider
# ---------------------------------------------------------------------------


class TestResolveEmbeddingProvider:
    @pytest.mark.parametrize(
        ("raw_provider", "model", "expected"),
        [
            (None, "all-MiniLM-L6-v2", "onnx"),
            ("", "all-MiniLM-L6-v2", "onnx"),
            (None, "stub", "stub"),  # deprecated EMBEDDING_MODEL=stub selector
            ("", "stub", "stub"),
            ("onnx", "stub", "onnx"),  # explicit provider wins over the deprecated selector
            ("ONNX", "anything", "onnx"),  # case-insensitive, stripped
            (" http ", "anything", "http"),
            ("Stub", "anything", "stub"),
        ],
    )
    def test_grammar(self, raw_provider: str | None, model: str, expected: str) -> None:
        assert _resolve_embedding_provider(raw_provider, model) == expected

    def test_stub_model_deprecation_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="registry.config"):
            result = _resolve_embedding_provider(None, "stub")
        assert result == "stub"
        assert any("deprecated" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# The two boolean grammars — pinned via get_settings(), since neither is a
# pure function today. They are NOT the same grammar: one is a positive
# allowlist (only listed spellings mean True; everything else, including
# unset, means False), the other is a negative denylist (only listed
# spellings mean False; everything else, including unset, means True).
# Whitespace-padded values expose the asymmetry: a padded "true" is False
# under the allowlist grammar, and a padded "false" is True under the
# denylist grammar. Both behaviors are pinned as-is; unifying them is a
# deliberate, separate change.
# ---------------------------------------------------------------------------


class TestSchedulerUseMemoryJobstoreGrammar:
    """Positive allowlist: SCHEDULER_USE_MEMORY_JOBSTORE."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("TRUE", True),
            ("Yes", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("garbage", False),
            ("on", False),  # not in the allowlist
            ("y", False),  # not in the allowlist
            (" true", False),  # no strip() in this grammar -- padding defeats the match
            ("true ", False),
        ],
    )
    def test_grammar(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "SCHEDULER_USE_MEMORY_JOBSTORE": raw},
        )
        assert settings.scheduler_use_memory_jobstore is expected

    def test_unset_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.scheduler_use_memory_jobstore is False


class TestRateLimitEnabledGrammar:
    """Negative denylist: RATE_LIMIT_ENABLED — the inverse convention."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", False),
            ("false", False),
            ("no", False),
            ("FALSE", False),
            ("No", False),
            ("true", True),
            ("TRUE", True),
            ("yes", True),
            ("1", True),
            ("garbage", True),
            ("off", True),  # not in the denylist
            (" false", True),  # no strip() -- padding defeats the match, opposite failure mode
            ("false ", True),
        ],
    )
    def test_grammar(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "RATE_LIMIT_ENABLED": raw},
        )
        assert settings.rate_limit_enabled is expected

    def test_unset_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.rate_limit_enabled is True


# ---------------------------------------------------------------------------
# LOG_LEVEL: name -> `logging` module attribute, including the silent
# garbage fallback.
# ---------------------------------------------------------------------------


class TestLogLevelGrammar:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
            ("info", logging.INFO),  # case-insensitive via .upper()
            ("debug", logging.DEBUG),
            ("notalevel", logging.INFO),  # silent fallback -- not a raise
            ("", logging.INFO),  # "".upper() == "" -> getattr fails -> fallback
        ],
    )
    def test_grammar(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "LOG_LEVEL": raw},
        )
        assert settings.log_level == expected

    def test_unset_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.log_level == logging.INFO


# ---------------------------------------------------------------------------
# BUILD_REVISION: unset vs set-but-empty (both fall back to "unknown").
# ---------------------------------------------------------------------------


class TestBuildRevisionGrammar:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", "unknown"),
            ("   ", "unknown"),
            ("v1.2.3", "v1.2.3"),
            ("  v1.2.3  ", "v1.2.3"),
        ],
    )
    def test_grammar(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "BUILD_REVISION": raw},
        )
        assert settings.build_revision == expected

    def test_unset_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.build_revision == "unknown"


# ---------------------------------------------------------------------------
# Cross-field defaults: PGBOUNCER_URL and SCHEDULER_JOBSTORE_URL both
# default to DATABASE_URL when unset, and are left alone when set.
# ---------------------------------------------------------------------------


class TestCrossFieldUrlDefaults:
    def test_both_default_to_database_url_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.pgbouncer_url == "postgresql+asyncpg://u:p@h/d"
        assert settings.scheduler_jobstore_url == "postgresql+asyncpg://u:p@h/d"

    def test_explicit_values_are_not_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "PGBOUNCER_URL": "postgresql+asyncpg://pgb:pgb@pgb-host/pgb",
                "SCHEDULER_JOBSTORE_URL": "postgresql+asyncpg://sched:sched@sched-host/sched",
            },
        )
        assert settings.pgbouncer_url == "postgresql+asyncpg://pgb:pgb@pgb-host/pgb"
        assert settings.scheduler_jobstore_url == "postgresql+asyncpg://sched:sched@sched-host/sched"


# ---------------------------------------------------------------------------
# CSV/list-valued field wiring — the splitting grammar itself is pinned
# above (TestParseCsvList); this confirms each field reads its own env var.
# ---------------------------------------------------------------------------


class TestCsvFieldWiring:
    def test_oidc_issuer_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "OIDC_ISSUER_ALLOWLIST": "https://a.example, https://b.example ,,",
            },
        )
        assert settings.oidc_issuer_allowlist == ["https://a.example", "https://b.example"]

    def test_oidc_client_id_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "OIDC_CLIENT_ID_ALLOWLIST": "client-a,client-b"},
        )
        assert settings.oidc_client_id_allowlist == ["client-a", "client-b"]

    def test_resource_uri_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d", "RESOURCE_URI_ALLOWLIST": "uri-a,uri-b"},
        )
        assert settings.resource_uri_allowlist == ["uri-a", "uri-b"]

    def test_all_three_default_to_empty_list_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})
        assert settings.oidc_issuer_allowlist == []
        assert settings.oidc_client_id_allowlist == []
        assert settings.resource_uri_allowlist == []


class TestCustomFormatFieldWiring:
    def test_arc_global_operator_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "ARC_GLOBAL_OPERATOR_ALLOWLIST": "https://issuer-x.example|subject-x,https://issuer-y.example|subject-y",
            },
        )
        assert settings.arc_global_operator_allowlist == (
            ("https://issuer-x.example", "subject-x"),
            ("https://issuer-y.example", "subject-y"),
        )

    def test_arc_global_operator_allowlist_malformed_raises_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="missing the '\\|' delimiter"):
            _settings_from_env(
                monkeypatch,
                {
                    "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                    "ARC_GLOBAL_OPERATOR_ALLOWLIST": "not-a-valid-entry",
                },
            )

    def test_entitlement_role_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "ENTITLEMENT_SERVICE_URL": "https://entitlement.example",
                "ENTITLEMENT_SERVICE_ENV": "DEV",
                "ENTITLEMENT_SERVICE_DISCRIMINATOR": "REGISTRY",
                "ENTITLEMENT_ROLE_MAPPING": "ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor",
            },
        )
        assert settings.entitlement_role_mapping == {
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        }


# ---------------------------------------------------------------------------
# Env-name / field-name mismatches. Each of these four fields reads an env
# var whose name does not match the field name's SCREAMING_SNAKE_CASE form.
# The field-name-shaped variable must have zero effect.
# ---------------------------------------------------------------------------


class TestEnvNameMismatches:
    def test_webhook_secret_github_reads_reversed_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "GITHUB_WEBHOOK_SECRET": "the-real-secret",
                "WEBHOOK_SECRET_GITHUB": "decoy-must-be-ignored",
            },
        )
        assert settings.webhook_secret_github == "the-real-secret"

    def test_webhook_secret_gitlab_reads_reversed_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "GITLAB_WEBHOOK_SECRET": "the-real-secret",
                "WEBHOOK_SECRET_GITLAB": "decoy-must-be-ignored",
            },
        )
        assert settings.webhook_secret_gitlab == "the-real-secret"

    def test_http_methods_mode_reads_registry_prefixed_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "REGISTRY_HTTP_METHODS_MODE": "post_only",
                "HTTP_METHODS_MODE": "both",  # decoy: must be ignored
            },
        )
        assert settings.http_methods_mode == "post_only"

    def test_http_method_alias_separator_reads_registry_prefixed_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR": "slash",
                "HTTP_METHOD_ALIAS_SEPARATOR": "colon",  # decoy: must be ignored
            },
        )
        assert settings.http_method_alias_separator == "slash"

    def test_http_methods_mode_and_separator_are_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_settings() applies .strip().lower() -- pinned alongside the alias."""
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "REGISTRY_HTTP_METHODS_MODE": " POST_ONLY ",
                "REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR": " SLASH ",
            },
        )
        assert settings.http_methods_mode == "post_only"
        assert settings.http_method_alias_separator == "slash"


# ---------------------------------------------------------------------------
# DATABASE_URL is required; its absence must fail loudly at construction.
# ---------------------------------------------------------------------------


class TestDatabaseUrlRequired:
    def test_missing_database_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deliberately updated in the same commit that swapped Settings onto
        # pydantic-settings: the dataclass-era implementation raised KeyError
        # (`os.environ["DATABASE_URL"]`); the pydantic-settings model raises
        # ValidationError for the same missing-required-field condition.
        # ValidationError is a ValueError subclass, so this is still an
        # equally loud, equally unambiguous failure -- the type changed
        # because the construction mechanism changed, not because the
        # grammar did.
        for name in _ALL_CONFIG_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ValidationError):
            get_settings()


# ---------------------------------------------------------------------------
# Full-env and minimal-env construction.
# ---------------------------------------------------------------------------

_FULL_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://full-db-user:full-db-pass@full-db-host:5432/full_db",
    "PGBOUNCER_URL": "postgresql+asyncpg://full-pgb-user:full-pgb-pass@full-pgb-host:6432/full_pgb",
    "SCHEDULER_JOBSTORE_URL": "postgresql+asyncpg://full-sched-user:full-sched-pass@full-sched-host:5432/full_sched",
    "SCHEDULER_USE_MEMORY_JOBSTORE": "true",
    "EMBEDDING_MODEL": "full-embedding-model",
    "EMBEDDING_PROVIDER": "FULL-PROVIDER",
    "EXTRACTION_PROVIDER": "local",
    "EXTRACTION_MODEL": "full-extraction-model",
    "EXTRACTION_TIMEOUT_S": "101",
    "EMBEDDING_MODEL_PATH": "/full/embedding/path",
    "EMBEDDING_DIM": "102",
    "EMBEDDING_CHUNK_TOKENS": "103",
    "EMBEDDING_CACHE_MAXSIZE": "104",
    "EMBEDDING_HTTP_ENDPOINT": "https://full-embedding-endpoint.example",
    "EMBEDDING_HTTP_CONNECT_TIMEOUT_MS": "105",
    "EMBEDDING_HTTP_READ_TIMEOUT_MS": "106",
    "EMBEDDING_HTTP_MAX_RETRIES": "107",
    "OUTBOX_POLL_INTERVAL_S": "108",
    "CONSOLIDATION_SWEEP_INTERVAL_S": "109",
    "OUTBOX_BATCH_SIZE": "110",
    "OUTBOX_MAX_ATTEMPTS": "111",
    "BACKFILL_BATCH_SIZE": "112",
    "OIDC_DISCOVERY_URL": "https://full-oidc.example",
    "RATE_LIMIT_ENABLED": "false",
    "RATE_LIMIT_WRITE_PER_MINUTE": "113",
    "RATE_LIMIT_READ_PER_MINUTE": "114",
    "USAGE_RETENTION_DAYS": "115",
    "METRICS_BEARER_TOKEN": "full-metrics-token",
    "OTLP_ENDPOINT": "https://full-otlp.example",
    "SERVICE_NAME": "full-service-name",
    "OTLP_EXPORTER_TIMEOUT_S": "116",
    "CONNECTOR_RUN_TIMEOUT_S": "117",
    "GITHUB_WEBHOOK_SECRET": "full-github-secret",
    "GITLAB_WEBHOOK_SECRET": "full-gitlab-secret",
    "WEBHOOK_DRAIN_INTERVAL_S": "118",
    "WEBHOOK_REQUEST_TIMEOUT_S": "119",
    "WEBHOOK_BATCH_SIZE": "120",
    "REGISTRY_HTTP_METHODS_MODE": "POST_ONLY",
    "REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR": "SLASH",
    "OIDC_ISSUER_ALLOWLIST": "https://issuer-a.example, https://issuer-b.example ,,",
    "OIDC_CLIENT_ID_ALLOWLIST": "client-a, client-b",
    "ARC_GLOBAL_OPERATOR_ALLOWLIST": "https://issuer-x.example|subject-x,https://issuer-y.example|subject-y",
    "BUILD_REVISION": "  full-build-rev  ",
    "OIDC_MAX_TOKEN_TTL_SECONDS": "121",
    "RESOURCE_URI_ALLOWLIST": "uri-a,uri-b",
    "ENTITLEMENT_SERVICE_URL": "https://full-entitlement.example",
    "ENTITLEMENT_SERVICE_ENV": "FULLENV",
    "ENTITLEMENT_SERVICE_DISCRIMINATOR": "FULLDISC",
    "ENTITLEMENT_ROLE_MAPPING": "ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor",
    "ENTITLEMENT_CONNECT_TIMEOUT_MS": "122",
    "ENTITLEMENT_READ_TIMEOUT_MS": "123",
    "ENTITLEMENT_MAX_RETRIES": "124",
    "ENTITLEMENT_CACHE_MAX_ENTRIES": "125",
    "PROGRESSION_DEFINITION_CACHE_TTL_SECONDS": "126",
    "LOG_FORMAT": "text",
    "LOG_LEVEL": "WARNING",
}


class TestFullEnvConstruction:
    def test_every_field_takes_its_distinctive_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, _FULL_ENV)

        assert settings.database_url == "postgresql+asyncpg://full-db-user:full-db-pass@full-db-host:5432/full_db"
        assert settings.pgbouncer_url == "postgresql+asyncpg://full-pgb-user:full-pgb-pass@full-pgb-host:6432/full_pgb"
        assert (
            settings.scheduler_jobstore_url
            == "postgresql+asyncpg://full-sched-user:full-sched-pass@full-sched-host:5432/full_sched"
        )
        assert settings.scheduler_use_memory_jobstore is True
        assert settings.embedding_model == "full-embedding-model"
        assert settings.embedding_provider == "full-provider"  # .strip().lower() applied
        assert settings.extraction_provider == "local"
        assert settings.extraction_model == "full-extraction-model"
        assert settings.extraction_timeout_s == 101.0
        assert settings.embedding_model_path == "/full/embedding/path"
        assert settings.embedding_dim == 102
        assert settings.embedding_chunk_tokens == 103
        assert settings.embedding_cache_maxsize == 104
        assert settings.embedding_http_endpoint == "https://full-embedding-endpoint.example"
        assert settings.embedding_http_connect_timeout_ms == 105
        assert settings.embedding_http_read_timeout_ms == 106
        assert settings.embedding_http_max_retries == 107
        assert settings.outbox_poll_interval_s == 108
        assert settings.consolidation_sweep_interval_s == 109
        assert settings.outbox_batch_size == 110
        assert settings.outbox_max_attempts == 111
        assert settings.backfill_batch_size == 112
        assert settings.oidc_discovery_url == "https://full-oidc.example"
        assert settings.rate_limit_enabled is False
        assert settings.rate_limit_write_per_minute == 113
        assert settings.rate_limit_read_per_minute == 114
        assert settings.usage_retention_days == 115
        assert settings.metrics_bearer_token == "full-metrics-token"
        assert settings.otlp_endpoint == "https://full-otlp.example"
        assert settings.service_name == "full-service-name"
        assert settings.otlp_exporter_timeout_s == 116
        assert settings.connector_run_timeout_s == 117
        assert settings.webhook_secret_github == "full-github-secret"
        assert settings.webhook_secret_gitlab == "full-gitlab-secret"
        assert settings.webhook_drain_interval_s == 118
        assert settings.webhook_request_timeout_s == 119.0
        assert settings.webhook_batch_size == 120
        assert settings.http_methods_mode == "post_only"
        assert settings.http_method_alias_separator == "slash"
        assert settings.oidc_issuer_allowlist == ["https://issuer-a.example", "https://issuer-b.example"]
        assert settings.oidc_client_id_allowlist == ["client-a", "client-b"]
        assert settings.arc_global_operator_allowlist == (
            ("https://issuer-x.example", "subject-x"),
            ("https://issuer-y.example", "subject-y"),
        )
        assert settings.build_revision == "full-build-rev"
        assert settings.oidc_max_token_ttl_seconds == 121
        assert settings.resource_uri_allowlist == ["uri-a", "uri-b"]
        assert settings.entitlement_service_url == "https://full-entitlement.example"
        assert settings.entitlement_service_env == "FULLENV"
        assert settings.entitlement_service_discriminator == "FULLDISC"
        assert settings.entitlement_role_mapping == {
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        }
        assert settings.entitlement_connect_timeout_ms == 122
        assert settings.entitlement_read_timeout_ms == 123
        assert settings.entitlement_max_retries == 124
        assert settings.entitlement_cache_max_entries == 125
        assert settings.progression_definition_cache_ttl_seconds == 126
        assert settings.log_format == "text"
        assert settings.log_level == logging.WARNING


class TestMinimalEnvConstruction:
    def test_only_database_url_set_yields_every_documented_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"})

        assert settings.database_url == "postgresql+asyncpg://u:p@h/d"
        assert settings.pgbouncer_url == "postgresql+asyncpg://u:p@h/d"
        assert settings.scheduler_jobstore_url == "postgresql+asyncpg://u:p@h/d"
        assert settings.scheduler_use_memory_jobstore is False
        assert settings.extraction_provider == "noop"
        assert settings.extraction_model == "claude-haiku-4-5-20251001"
        assert settings.extraction_timeout_s == 60.0
        assert settings.embedding_provider == "onnx"
        assert settings.embedding_model == "all-MiniLM-L6-v2"
        assert settings.embedding_model_path == "/opt/models/all-MiniLM-L6-v2"
        assert settings.embedding_dim == 384
        assert settings.embedding_chunk_tokens == 400
        assert settings.embedding_cache_maxsize == 10_000
        assert settings.embedding_http_endpoint is None
        assert settings.embedding_http_connect_timeout_ms == 500
        assert settings.embedding_http_read_timeout_ms == 5_000
        assert settings.embedding_http_max_retries == 2
        assert settings.outbox_poll_interval_s == 5
        assert settings.consolidation_sweep_interval_s == 300
        assert settings.outbox_batch_size == 32
        assert settings.outbox_max_attempts == 5
        assert settings.webhook_drain_interval_s == 5
        assert settings.webhook_request_timeout_s == 10.0
        assert settings.webhook_batch_size == 50
        assert settings.http_methods_mode == "rest"
        assert settings.http_method_alias_separator == "colon"
        assert settings.backfill_batch_size == 64
        assert settings.oidc_discovery_url is None
        assert settings.oidc_issuer_allowlist == []
        assert settings.oidc_client_id_allowlist == []
        assert settings.arc_global_operator_allowlist == ()
        assert settings.build_revision == "unknown"
        assert settings.oidc_max_token_ttl_seconds == 900
        assert settings.resource_uri_allowlist == []
        assert settings.entitlement_service_url == ""
        assert settings.entitlement_service_env == ""
        assert settings.entitlement_service_discriminator == ""
        assert settings.entitlement_role_mapping == {}
        assert settings.entitlement_connect_timeout_ms == 250
        assert settings.entitlement_read_timeout_ms == 1500
        assert settings.entitlement_max_retries == 1
        assert settings.entitlement_cache_max_entries == 10000
        assert settings.progression_definition_cache_ttl_seconds == 60
        assert settings.rate_limit_enabled is True
        assert settings.rate_limit_write_per_minute == 60
        assert settings.rate_limit_read_per_minute == 600
        assert settings.usage_retention_days == 90
        assert settings.metrics_bearer_token is None
        assert settings.otlp_endpoint is None
        assert settings.service_name == "registry"
        assert settings.otlp_exporter_timeout_s == 2
        assert settings.connector_run_timeout_s == 300
        assert settings.webhook_secret_github is None
        assert settings.webhook_secret_gitlab is None
        assert settings.log_format == "json"
        assert settings.log_level == logging.INFO
