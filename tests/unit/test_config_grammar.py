"""Pins the env-var parsing grammar in `contextplane/config_grammar.py`.

This file exists to protect one thing: the mapping from environment-variable
strings to `Settings` field values. That grammar is easy to get subtly wrong
during any refactor of `config.py` (for example, swapping the dataclass +
`get_settings()` pair for a `pydantic-settings` model) because the two
call sites -- direct `Settings(...)` construction (used pervasively by
tests) and `get_settings()` (used by the running app) -- have historically
disagreed about how much parsing happens. A change that "obviously" just
moves code around can silently change what an operator's env file produces.

Every case here is derived by reading `contextplane/config.py` line by line, not
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
import subprocess  # noqa: S404
import sys

import pytest
from pydantic import ValidationError

from contextplane.config import (
    Settings,
    _resolve_extraction_provider,
    get_settings,
)
from contextplane.config_grammar import (
    _parse_csv_list,
    _parse_extraction_extra_headers,
    _parse_operator_allowlist,
    _parse_role_mapping,
    _resolve_embedding_provider,
    _resolve_extraction_auth_template,
    _resolve_extraction_base_url,
)

# ---------------------------------------------------------------------------
# Every env var name `contextplane/config.py` reads today, plus a few
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
    "EXTRACTION_BASE_URL",
    "EXTRACTION_AUTH_HEADER",
    "EXTRACTION_AUTH_TEMPLATE",
    "EXTRACTION_EXTRA_HEADERS",
    "EXTRACTION_API_KEY",
    "CLAUDE_API_KEY",
    "ANTHROPIC_API_KEY",
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
    "CONTEXTPLANE_HTTP_METHODS_MODE",
    "CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR",
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
# Pure-function grammar: the extraction transport. These four settings point
# extraction at an arbitrary endpoint, so between them they carry a
# credential, the header it travels in, and whatever else that endpoint
# needs -- which is why most of what is pinned here is a refusal.
# ---------------------------------------------------------------------------


class TestResolveExtractionBaseUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),
            ("   ", ""),
            ("https://gateway.internal/v1", "https://gateway.internal/v1"),
            ("  https://gateway.internal/v1  ", "https://gateway.internal/v1"),
            # An `@` past the authority is path or query, not userinfo.
            ("https://gateway.internal/v1/models@latest", "https://gateway.internal/v1/models@latest"),
        ],
    )
    def test_grammar(self, raw: str, expected: str) -> None:
        assert _resolve_extraction_base_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "https://user:secret@gateway.internal/v1",
            "https://token@gateway.internal/v1",
            "http://user:secret@gateway.internal",
        ],
    )
    def test_userinfo_is_refused(self, raw: str) -> None:
        with pytest.raises(ValueError, match="may not carry userinfo"):
            _resolve_extraction_base_url(raw)

    def test_the_refusal_does_not_echo_the_url(self) -> None:
        """The refused value contains a password. Repeating it in the message
        would move the credential from the variable into the crash log, which
        is the outcome the refusal exists to prevent."""
        with pytest.raises(ValueError) as caught:
            _resolve_extraction_base_url("https://user:hunter2@gateway.internal/v1")
        assert "hunter2" not in str(caught.value)


class TestResolveExtractionAuthTemplate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),
            ("   ", ""),
            ("Bearer {key}", "Bearer {key}"),
            ("  Bearer {key}  ", "Bearer {key}"),
            ("{key}", "{key}"),
            ("Token token={key}", "Token token={key}"),
        ],
    )
    def test_grammar(self, raw: str, expected: str) -> None:
        assert _resolve_extraction_auth_template(raw) == expected

    def test_a_pasted_credential_is_refused(self) -> None:
        """Zero placeholders means the operator put the credential itself into
        a setting that is not a secret -- one bound for a ConfigMap and for
        every log line reporting the effective configuration."""
        with pytest.raises(ValueError, match=r"exactly once"):
            _resolve_extraction_auth_template("Bearer sk-live-abc123")

    def test_a_repeated_placeholder_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"exactly once"):
            _resolve_extraction_auth_template("Bearer {key} {key}")

    def test_the_refusal_does_not_echo_the_template(self) -> None:
        with pytest.raises(ValueError) as caught:
            _resolve_extraction_auth_template("Bearer sk-live-abc123")
        assert "sk-live-abc123" not in str(caught.value)

    def test_substitution_is_replace_not_format(self) -> None:
        """A credential containing a brace must survive substitution intact.
        `str.format` would raise on it, or worse, re-enter formatting."""
        template = _resolve_extraction_auth_template("Bearer {key}")
        assert template.replace("{key}", "sk-{live}-abc") == "Bearer sk-{live}-abc"


class TestParseExtractionExtraHeaders:
    def test_empty_is_no_headers(self) -> None:
        assert _parse_extraction_extra_headers("", auth_header="") == ()
        assert _parse_extraction_extra_headers("   ", auth_header="") == ()

    def test_pairs_are_parsed_in_order_and_stripped(self) -> None:
        parsed = _parse_extraction_extra_headers(
            " X-Tenant : acme , anthropic-version:2023-06-01 ",
            auth_header="",
        )
        assert parsed == (("X-Tenant", "acme"), ("anthropic-version", "2023-06-01"))

    def test_an_empty_pair_is_skipped_rather_than_failing(self) -> None:
        """Matches every other list-shaped variable in this file: a trailing
        comma is a typo with an obvious intent, not a misconfiguration."""
        assert _parse_extraction_extra_headers("X-A:1,,X-B:2,", auth_header="") == (("X-A", "1"), ("X-B", "2"))

    def test_a_value_may_contain_a_colon(self) -> None:
        parsed = _parse_extraction_extra_headers("X-Trace:id:12345", auth_header="")
        assert parsed == (("X-Trace", "id:12345"),)

    @pytest.mark.parametrize(
        ("raw", "expected_index", "expected_reason"),
        [
            ("X-A:1,no-delimiter", 2, "missing the ':' delimiter"),
            ("X-A:1,:value", 2, "empty header name"),
            ("X-A:1,X-B:", 2, "empty header value"),
            ("X-A:1,X Bad:2", 2, "outside the permitted token characters"),
            ("X-A:1,X-A:2", 2, "repeats a header name"),
            ("Host:evil.example", 1, "the transport itself"),
            ("content-type:text/plain", 1, "the transport itself"),
            ("Content-Length:0", 1, "the transport itself"),
            ("Transfer-Encoding:chunked", 1, "the transport itself"),
            ("Connection:close", 1, "the transport itself"),
        ],
    )
    def test_rejections_name_the_pair_index_and_the_reason(
        self, raw: str, expected_index: int, expected_reason: str
    ) -> None:
        with pytest.raises(ValueError) as caught:
            _parse_extraction_extra_headers(raw, auth_header="")
        message = str(caught.value)
        assert f"pair {expected_index}" in message
        assert expected_reason in message

    def test_rejections_never_echo_the_offending_fragment(self) -> None:
        """The distinguishing property of this parser. These pairs carry
        credentials, and the message surfaces as an uncaught startup
        exception, so an echoed fragment lands a live token in the crash log."""
        for raw in ("X-Token sk-live-abc123", "X-Token:", "Host:sk-live-abc123"):
            with pytest.raises(ValueError) as caught:
                _parse_extraction_extra_headers(raw, auth_header="")
            assert "sk-live-abc123" not in str(caught.value)
            assert "X-Token" not in str(caught.value)

    def test_the_configured_auth_header_cannot_be_set_again(self) -> None:
        """A second credential smuggled past the auth-template validation is
        the reason this parser needs the auth header at all."""
        with pytest.raises(ValueError, match="the transport itself"):
            _parse_extraction_extra_headers("X-Api-Key:sneaked", auth_header="x-api-key")

    def test_the_auth_header_match_is_case_insensitive(self) -> None:
        with pytest.raises(ValueError, match="the transport itself"):
            _parse_extraction_extra_headers("AUTHORIZATION:Bearer sneaked", auth_header="Authorization")

    def test_anthropic_version_is_overridable(self) -> None:
        """A vendor API-version selector is a legitimate reason to reach for
        this variable, so it is deliberately absent from the refused set."""
        assert _parse_extraction_extra_headers("anthropic-version:2023-06-01", auth_header="x-api-key") == (
            ("anthropic-version", "2023-06-01"),
        )

    @pytest.mark.parametrize("control", ["\r", "\n", "\x00", "\x7f"])
    def test_a_control_character_in_a_value_is_refused(self, control: str) -> None:
        with pytest.raises(ValueError, match="visible ASCII"):
            _parse_extraction_extra_headers(f"X-A:val{control}ue", auth_header="")


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
        with caplog.at_level(logging.WARNING, logger="contextplane.config"):
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
                "ENTITLEMENT_SERVICE_DISCRIMINATOR": "CONTEXTPLANE",
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


class TestExtractionCredentialWiring:
    """EXTRACTION_API_KEY is the canonical name; two legacy spellings still
    resolve to the same field because deployments and runbooks carry them."""

    _BASE = {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"}

    def test_absent_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _settings_from_env(monkeypatch, dict(self._BASE)).extraction_api_key is None

    @pytest.mark.parametrize("name", ["EXTRACTION_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_API_KEY"])
    def test_every_accepted_name_resolves_to_one_field(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        settings = _settings_from_env(monkeypatch, {**self._BASE, name: "sk-from-" + name.lower()})
        assert settings.extraction_api_key is not None
        assert settings.extraction_api_key.get_secret_value() == "sk-from-" + name.lower()

    def test_the_canonical_name_outranks_both_legacy_spellings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declaration order inside AliasChoices is the whole mechanism. A
        deployment mid-migration sets more than one, and the canonical name
        has to be the one that wins or the rename accomplishes nothing."""
        settings = _settings_from_env(
            monkeypatch,
            {
                **self._BASE,
                "EXTRACTION_API_KEY": "sk-canonical",
                "CLAUDE_API_KEY": "sk-legacy",
                "ANTHROPIC_API_KEY": "sk-oldest",
            },
        )
        assert settings.extraction_api_key is not None
        assert settings.extraction_api_key.get_secret_value() == "sk-canonical"

    def test_claude_api_key_outranks_anthropic_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {**self._BASE, "CLAUDE_API_KEY": "sk-legacy", "ANTHROPIC_API_KEY": "sk-oldest"},
        )
        assert settings.extraction_api_key is not None
        assert settings.extraction_api_key.get_secret_value() == "sk-legacy"

    def test_the_key_and_the_extra_headers_stay_out_of_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both hold credentials, and `repr(Settings())` reaches logs, error
        reports, and debugger frames. Held as secrets from the first commit
        rather than retrofitted, because a plain-str interval is an interval
        during which they leak."""
        settings = _settings_from_env(
            monkeypatch,
            {
                **self._BASE,
                "EXTRACTION_API_KEY": "sk-must-not-appear",
                "EXTRACTION_EXTRA_HEADERS": "X-Gateway-Token:tok-must-not-appear",
            },
        )
        rendered = repr(settings)
        assert "sk-must-not-appear" not in rendered
        assert "tok-must-not-appear" not in rendered

    def test_a_validation_failure_does_not_dump_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pydantic's default error envelope appends the whole input mapping,
        which for a settings model is every variable that fed it. Careful
        wording inside a validator cannot help; the leak is in the envelope."""
        with pytest.raises(ValidationError) as caught:
            _settings_from_env(
                monkeypatch,
                {
                    **self._BASE,
                    "EXTRACTION_API_KEY": "sk-must-not-appear",
                    "EXTRACTION_AUTH_TEMPLATE": "no-placeholder-here",
                },
            )
        assert "sk-must-not-appear" not in str(caught.value)


class TestExtractionTransportWiring:
    _BASE = {"DATABASE_URL": "postgresql+asyncpg://u:p@h/d"}

    def test_unset_transport_leaves_every_field_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The unconfigured deployment: each empty value means "whatever the
        selected adapter defaults to", so adding these fields changes nothing
        for anyone who does not set them."""
        settings = _settings_from_env(monkeypatch, dict(self._BASE))
        assert settings.extraction_base_url == ""
        assert settings.extraction_auth_header == ""
        assert settings.extraction_auth_template == ""
        assert settings.extraction_extra_header_pairs() == ()

    def test_a_configured_transport_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                **self._BASE,
                "EXTRACTION_BASE_URL": "  https://gateway.internal/v1  ",
                "EXTRACTION_AUTH_HEADER": "  Authorization  ",
                "EXTRACTION_AUTH_TEMPLATE": "  Bearer {key}  ",
                "EXTRACTION_EXTRA_HEADERS": "X-Tenant:acme,anthropic-version:2023-06-01",
            },
        )
        assert settings.extraction_base_url == "https://gateway.internal/v1"
        assert settings.extraction_auth_header == "Authorization"
        assert settings.extraction_auth_template == "Bearer {key}"
        assert settings.extraction_extra_header_pairs() == (
            ("X-Tenant", "acme"),
            ("anthropic-version", "2023-06-01"),
        )

    def test_extra_headers_are_validated_against_the_configured_auth_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross-field check, wired: no single-field validator can see
        both, which is why this is a model validator."""
        with pytest.raises(ValidationError, match="the transport itself"):
            _settings_from_env(
                monkeypatch,
                {
                    **self._BASE,
                    "EXTRACTION_AUTH_HEADER": "Authorization",
                    "EXTRACTION_EXTRA_HEADERS": "authorization:Bearer sneaked",
                },
            )

    def test_a_malformed_transport_fails_at_construction_not_at_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Startup is where an operator can act on it. Hours later, mid-drain,
        it is an outage with no obvious cause."""
        with pytest.raises(ValidationError, match="may not carry userinfo"):
            _settings_from_env(
                monkeypatch,
                {**self._BASE, "EXTRACTION_BASE_URL": "https://user:secret@gateway.internal/v1"},
            )

    def test_extra_headers_are_parsed_fresh_rather_than_cached_on_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Equal values, distinct objects: proof there is no parsed copy
        living on the model, which is what would put the header values back
        into the repr the SecretStr exists to keep them out of."""
        settings = _settings_from_env(
            monkeypatch,
            {**self._BASE, "EXTRACTION_EXTRA_HEADERS": "X-Tenant:acme"},
        )
        assert settings.extraction_extra_header_pairs() == settings.extraction_extra_header_pairs()
        assert "X-Tenant" not in repr(settings)


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
                "CONTEXTPLANE_HTTP_METHODS_MODE": "post_only",
                "HTTP_METHODS_MODE": "both",  # decoy: must be ignored
            },
        )
        assert settings.http_methods_mode == "post_only"

    def test_http_method_alias_separator_reads_registry_prefixed_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_from_env(
            monkeypatch,
            {
                "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
                "CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR": "slash",
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
                "CONTEXTPLANE_HTTP_METHODS_MODE": " POST_ONLY ",
                "CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR": " SLASH ",
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
    "CONTEXTPLANE_HTTP_METHODS_MODE": "POST_ONLY",
    "CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR": "SLASH",
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
        assert settings.service_name == "contextplane"
        assert settings.otlp_exporter_timeout_s == 2
        assert settings.connector_run_timeout_s == 300
        assert settings.webhook_secret_github is None
        assert settings.webhook_secret_gitlab is None
        assert settings.log_format == "json"
        assert settings.log_level == logging.INFO


# ---------------------------------------------------------------------------
# The one-way dependency between config and the provider registry
# ---------------------------------------------------------------------------
#
# The registry needs `Settings` to type what a supplied provider is built from,
# and this module needs the registry's names to validate a selector. Written as
# two module-level imports that is a cycle, and it surfaces only when the
# application is actually started -- no gate in this repo catches it, which is
# why it is pinned here instead.


class TestConfigAndTheProviderRegistryDoNotFormACycle:
    @pytest.mark.parametrize(
        "order",
        [
            "import contextplane.config, contextplane.extraction.provider_registry",
            "import contextplane.extraction.provider_registry, contextplane.config",
            "import contextplane.main",
        ],
    )
    def test_either_import_order_works_in_a_fresh_interpreter(self, order: str) -> None:
        """A fresh interpreter, because whichever module the test process
        imported first would otherwise hide the failure."""
        result = subprocess.run(
            [sys.executable, "-c", f"{order}; print('ok')"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

    def test_the_built_in_names_are_reachable_from_config(self) -> None:
        """They are imported from here by name elsewhere in the tree, and they
        resolve on access rather than at module import for the reason above."""
        from contextplane.config import EXTRACTION_PROVIDERS
        from contextplane.extraction.provider_registry import BUILT_IN_PROVIDERS

        assert EXTRACTION_PROVIDERS is BUILT_IN_PROVIDERS

    def test_an_attribute_this_module_does_not_have_still_raises(self) -> None:
        """A module-level `__getattr__` that returned something for every name
        would make a typo'd import succeed and fail somewhere else later."""
        import contextplane.config

        with pytest.raises(AttributeError, match="no attribute"):
            _ = contextplane.config.NOT_A_SETTING
