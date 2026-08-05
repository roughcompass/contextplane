"""Configuration surface for the registry service.

`Settings` is a `pydantic-settings` model: every default is stated exactly
once, on the field itself (or, for a value borrowed from another field, in a
cross-field `model_validator`). There used to be two statements of every
default -- the dataclass field default, and a matching `os.environ.get(NAME,
literal)` in `get_settings()` -- and nothing checked that the two agreed.
`get_settings()` is now a thin constructor call; the environment-to-field
mapping lives on `Settings` itself, so it applies the same way whether a
caller reads the environment or constructs `Settings(...)` directly (as
every test in this repo does).

Field and model validators carry the historical env-var grammar forward --
CSV splitting, the two different boolean spellings, the `LOG_LEVEL` name
lookup, the entitlement role-mapping format, the operator-allowlist format.
None of that parsing is new; it is the same code that used to live inline in
`get_settings()`, reattached to the field it parses. Treat a change to one
of these validators as a deployment-facing contract change, because it is
one.

`get_settings()` reads from environment variables. Tests construct `Settings`
directly with keyword arguments. No module-level singleton — wired by FastAPI
DI like `Clock`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Internal role names accepted by the registry's RBAC layer. Entitlement
# strings carry external suffix names that must map to one of these four.
_VALID_INTERNAL_ROLES: frozenset[str] = frozenset({"admin", "producer", "consumer", "auditor"})


def _parse_csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated env value into a stripped, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_operator_allowlist(value: str | None) -> tuple[tuple[str, str], ...]:
    """Parse `ISSUER|SUBJECT,ISSUER|SUBJECT,...` into exact identity pairs.

    ARC authorizes deployment-wide governance writes on an exact
    `(issuer, subject)` pair rather than on a role, because every role in
    this system is tenant-scoped -- an admin of any tenant would otherwise
    be able to edit policy that binds every tenant.

    A malformed entry raises rather than being skipped. A silently dropped
    entry means an operator who believes they have access and does not, or
    worse, an allowlist that looks configured and is empty; startup failing
    loudly is the only outcome an operator can act on.

    `|` rather than `:` as the delimiter because issuers are URLs and
    contain colons.
    """
    if not value:
        return ()
    pairs: list[tuple[str, str]] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "|" not in entry:
            msg = (
                f"ARC_GLOBAL_OPERATOR_ALLOWLIST entry {entry!r} is missing the '|' delimiter; "
                "expected 'https://issuer.example|subject'."
            )
            raise ValueError(msg)
        issuer, _, subject = entry.partition("|")
        issuer, subject = issuer.strip(), subject.strip()
        if not issuer or not subject:
            msg = (
                f"ARC_GLOBAL_OPERATOR_ALLOWLIST entry {entry!r} has an empty issuer or subject; "
                "both halves identify the operator and neither may be blank."
            )
            raise ValueError(msg)
        pairs.append((issuer, subject))
    return tuple(pairs)


EXTRACTION_PROVIDERS = frozenset({"noop", "local", "anthropic"})


def _resolve_extraction_provider(raw: str | None) -> str:
    """Validate the selector, defaulting to no extraction at all.

    An unknown value fails at startup rather than silently falling back. A
    typo'd provider name that quietly became "noop" would look exactly like a
    working deployment producing no claims, and the operator would go looking
    for the bug in extraction.
    """
    if raw is None or not raw.strip():
        return "noop"
    value = raw.strip().lower()
    if value not in EXTRACTION_PROVIDERS:
        msg = (
            f"unknown EXTRACTION_PROVIDER {raw!r}; expected one of {sorted(EXTRACTION_PROVIDERS)}. "
            "Leave it unset for no extraction, or use 'local' for a provider that needs no key."
        )
        raise ValueError(msg)
    return value


def _resolve_embedding_provider(raw_provider: str | None, model: str) -> str:
    """Pick the embedding provider, honouring the superseded spelling.

    `EMBEDDING_MODEL=stub` used to be how an operator asked for zero vectors,
    before the provider became a setting of its own. Deployments and runbooks
    still carry it, so it keeps working — but it is ambiguous (a model id doing
    double duty as an implementation switch) and is reported as deprecated.
    """
    provider = (raw_provider or "").strip().lower()
    if provider:
        return provider
    if model == "stub":
        logging.getLogger(__name__).warning(
            "EMBEDDING_MODEL=stub is deprecated and will stop selecting the stub embedder; "
            "set EMBEDDING_PROVIDER=stub instead"
        )
        return "stub"
    return "onnx"


def _parse_role_mapping(value: str | None) -> dict[str, str]:
    """Parse `EXTERNAL:internal,EXTERNAL:internal,...` into a dict.

    Pairs missing a colon raise ValueError immediately. Whitespace surrounding
    keys and values is stripped. Duplicate external keys take last-wins —
    legitimate during LDAP rename rollouts where old and new strings ship
    concurrently. Semantic validation (non-empty, internal role membership)
    happens in a model validator so direct-dict construction in tests is
    also covered.
    """
    if not value:
        return {}
    result: dict[str, str] = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"ENTITLEMENT_ROLE_MAPPING pair {pair!r} is missing the ':' delimiter; " "expected 'EXTERNAL:internal'."
            )
        external, internal = pair.split(":", maxsplit=1)
        result[external.strip()] = internal.strip()
    return result


class Settings(BaseSettings):
    """Application configuration, sourced from environment variables.

    Unrecognized keyword arguments are ignored rather than rejected
    (`extra="ignore"`) so that callers passing a superset of fields --
    scripts sharing one kwargs dict across a few call sites -- do not
    break when a field is removed. Every field also accepts its own name
    as a keyword regardless of whether it declares an env-var alias
    (`populate_by_name=True`), which is what lets tests construct
    `Settings(webhook_secret_github=...)` directly using the field name
    even though the field reads `GITHUB_WEBHOOK_SECRET` from the
    environment.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    # --- Database ---
    # asyncpg requires prepared_statement_cache_size=0 for PgBouncer transaction mode — wired in storage/pg.py
    database_url: str

    # Optional separate URL for the runtime app -> PgBouncer path, and for
    # APScheduler's SQLAlchemyJobStore. Both default to database_url when
    # left as "" -- see _apply_url_defaults below, the one place this
    # default is stated.
    pgbouncer_url: str = ""
    scheduler_jobstore_url: str = ""

    # --- APScheduler ---
    # Set True to force MemoryJobStore (unit tests, envs without psycopg2).
    scheduler_use_memory_jobstore: bool = False

    # --- Session-observation extraction ---
    # Which provider turns session events into candidate claims.
    #   "noop"     — the default. Extraction pauses; events are still captured
    #                and served, and connector-fed claims still land. A
    #                deployment that configures nothing is complete, not broken.
    #   "local"    — deterministic pattern rules. No key, no network, no model.
    #                What the local dev stack runs, so a developer never needs a
    #                credential to work on anything downstream of extraction.
    #   "anthropic" — a real model. Requires CLAUDE_API_KEY or
    #                ANTHROPIC_API_KEY; never required by anything else.
    extraction_provider: str = "noop"
    # Model the strategies request. Ignored by the noop and local providers,
    # which have no model to select.
    extraction_model: str = "claude-haiku-4-5-20251001"
    extraction_timeout_s: float = 60.0

    # --- Embedding ---
    # Which implementation produces vectors. See registry/embedding/ for the
    # accepted values; "onnx" runs a locally-staged artifact and needs no network.
    # Left as "" until resolved (see _resolve_embedding_provider_field below),
    # because the resolved default depends on embedding_model.
    embedding_provider: str = ""
    # Identifies the embedding space. Stamped into embeddings.model_id and used
    # by the semantic arm to avoid comparing vectors from different models, so
    # changing it partitions new rows away from existing ones.
    embedding_model: str = "all-MiniLM-L6-v2"
    # Directory holding the staged model artifact. Container images bake it in;
    # see scripts/fetch_embedding_model.py to stage it anywhere else.
    embedding_model_path: str = "/opt/models/all-MiniLM-L6-v2"
    # Width of the stored vectors. Must match both the model and the DDL — the
    # app refuses to start when it disagrees with the live embeddings column.
    embedding_dim: int = 384
    embedding_chunk_tokens: int = 400
    embedding_cache_maxsize: int = 10_000
    # Remote provider only.
    embedding_http_endpoint: str | None = None
    embedding_http_connect_timeout_ms: int = 500
    embedding_http_read_timeout_ms: int = 5_000
    embedding_http_max_retries: int = 2

    # --- Outbox ---
    outbox_poll_interval_s: int = 5

    # Closure-cache refresh drain. Same outbox pattern as embeddings: edge
    # mutations enqueue, this drains. Until this setting existed the worker
    # was never scheduled at all — the cache was written once and every
    # traversal after the first edge change fell back to the recursive CTE.
    closure_refresh_interval_s: int = 5
    # How often staged claims are reconciled against each other. Far wider than the
    # embedding poll because a consolidation decision can cost a provider call, and
    # because the work is idempotent: running more often only reduces how stale an
    # answer can be, it never changes what the answer converges to.
    consolidation_sweep_interval_s: int = 300
    # How often consolidated claims are proposed for promotion, and auto-accepted
    # where a tenant's own guardrails permit it. Wider than the embedding poll for
    # the same reason the consolidation sweep is: the work is idempotent, so a
    # longer interval only makes the review queue and the canonical graph staler,
    # never wrong.
    promotion_sweep_interval_s: int = 300
    outbox_batch_size: int = 32
    outbox_max_attempts: int = 5

    # --- Webhook delivery ---
    webhook_drain_interval_s: int = 5
    webhook_request_timeout_s: float = 10.0
    webhook_batch_size: int = 50

    # --- HTTP method routing ---
    # 'rest'     — only the standard verb (PATCH/DELETE) is registered.
    # 'post_only'— only the POST-tunneled alias (POST .../{id}:action) is registered.
    # 'both'     — both routes are registered for enterprise-gateway compatibility.
    # Default is 'rest': the POST-tunneled aliases are opt-in for deployments
    # behind proxies that strip non-GET/POST verbs.
    #
    # Both fields read a REGISTRY_-prefixed env var, not the SCREAMING_SNAKE
    # form of their own field name — pinned via validation_alias.
    http_methods_mode: str = Field(default="rest", validation_alias="REGISTRY_HTTP_METHODS_MODE")
    http_method_alias_separator: str = Field(default="colon", validation_alias="REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR")

    # --- Backfill / reindex scripts ---
    backfill_batch_size: int = 64

    # --- Auth ---
    oidc_discovery_url: str | None = None
    # The expected `aud` claim in JWTs issued for this service. When set,
    # validate_oidc_token rejects tokens whose audience does not match — this
    # blocks confused-deputy attacks in shared-IdP deployments (Auth0, Okta,
    # …) where a token issued for a different application would otherwise be
    # accepted. Leave unset for backward compatibility, but a startup warning
    # fires whenever OIDC is enabled and this is absent.

    # --- Auth: OIDC validation contract ---
    # Acceptable `iss` values. Tokens whose issuer is not in this list are
    # rejected even if their signature validates against a trusted JWKS — this
    # blocks confused-deputy attacks across applications sharing an IDP.
    # Empty list = legacy behavior (no issuer allowlisting). Production
    # deployments should populate this.
    oidc_issuer_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Acceptable `azp` (authorized party) or `client_id` values. Applies to
    # all token grant types. Empty list = check skipped (NOT recommended in
    # production; an empty allowlist allows any token issued by a trusted
    # JWKS to pass the service-token check).
    oidc_client_id_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Exact `(issuer, subject)` pairs permitted to write deployment-wide ARC
    # governance. Empty means no one can -- deliberately, because a
    # deployment that configured nothing must not fall open on the one
    # surface that binds every tenant.
    arc_global_operator_allowlist: Annotated[tuple[tuple[str, str], ...], NoDecode] = ()

    # Which build produced a receipt, recorded in its provenance so a replay
    # years later can tell whether a different outcome is tampering or just
    # a newer engine. Defaults to `unknown` rather than to a plausible-looking
    # value: a receipt asserting a build revision that was never deployed is
    # worse than one admitting the deployment did not say.
    build_revision: str = "unknown"

    # Registry-enforced upper bound on token lifetime: middleware rejects
    # tokens where `exp - iat` exceeds this bound, or where `iat` is absent.
    # Defense-in-depth against IDP misconfiguration that could issue
    # long-lived tokens. Clock skew tolerance ±60s (applied at validation,
    # not in this setting).
    oidc_max_token_ttl_seconds: int = 900

    # Set of acceptable `aud` (audience) values. ADFS carries the resource
    # URI here. Empty list disables audience validation, with a one-time
    # startup warning — there is no single-audience fallback knob.
    resource_uri_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Auth: Entitlement service ---
    # Base URL of the enterprise entitlement service. When set, this enables
    # the entitlement-resolution code path; the entitlement-related fields
    # below all become required (validated in _validate_entitlement_config).
    # Empty/unset = entitlement path is disabled (legacy behavior continues
    # to apply via the legacy claim-source URL field).
    entitlement_service_url: str = ""

    # Environment indicator passed as the `env` query param to the
    # entitlement service (e.g. `PRD`, `NPD`, `DEV`). Required if
    # entitlement_service_url is set.
    entitlement_service_env: str = ""

    # Middle token of the entitlement grammar for this deployment
    # (`<tenant_slug>_<DISCRIMINATOR>_<ROLE_SUFFIX>`). Per-deployment
    # config — multiple registry-shaped services may share one entitlement
    # endpoint with different discriminators. Required if
    # entitlement_service_url is set; non-empty; no internal whitespace.
    entitlement_service_discriminator: str = ""

    # External-suffix → internal-role mapping
    # (e.g. {"ADMIN": "admin", "ADMINISTRATOR": "admin"}). Internal values
    # must be in {admin, producer, consumer, auditor}. Multiple external
    # suffixes may map to the same internal role — covers LDAP rename
    # rollouts with concurrent old/new strings. Required if
    # entitlement_service_url is set; non-empty.
    entitlement_role_mapping: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    # HTTP timeouts and retry budget for entitlement-service calls. The
    # entitlement call sits in the auth hot path on every request, so
    # bounded failure behavior is required to prevent request thread
    # pile-up on a slow upstream.
    entitlement_connect_timeout_ms: int = 250
    entitlement_read_timeout_ms: int = 1500
    entitlement_max_retries: int = 1

    # In-process LRU cache size bound for entitlement responses (per process).
    # TTL is bounded by the JWT's own `exp`, not this setting.
    entitlement_cache_max_entries: int = 10000

    # --- Progression ---
    # TTL (seconds) for the cached progression-definition lookup. The definition
    # describes which capability transitions are allowed. A short TTL keeps the
    # cache fresh after operator edits; 0 disables caching entirely.
    progression_definition_cache_ttl_seconds: int = 60

    # --- Rate limiting ---
    # In-process token-bucket limits (per tenant, per minute).  Separate
    # budgets for reads (GET/HEAD) and writes (POST/PUT/PATCH/DELETE).
    # Set rate_limit_enabled=False to disable enforcement without redeploying.
    rate_limit_enabled: bool = True
    rate_limit_write_per_minute: int = 60
    rate_limit_read_per_minute: int = 600

    # --- Usage recording ---
    # How long raw usage events are kept. Permitted band 30-180; the worker
    # refuses a value outside it rather than clamping, because a deployment that
    # asked for a year and silently got 180 days would only find out when a query
    # returned less than it should — by which time the rows are gone.
    #
    # Aggregate answers survive expiry: the rollups are actor-free and retained
    # indefinitely, so deleting raw rows costs nothing analytically and removes
    # the personal-data liability. That trade is why this table may hold identity.
    usage_retention_days: int = 90

    # --- Metrics exposition ---
    # Bearer credential the /metrics scraper must present. There is deliberately
    # no default: the endpoint publishes process-global counters, including
    # entitlement-failure counts and the full route table, and a default value
    # would be the same as no credential at all. Unset means /metrics refuses to
    # serve rather than serving to anyone.
    metrics_bearer_token: str | None = None

    # --- OTel ---
    otlp_endpoint: str | None = None
    service_name: str = "registry"
    # Timeout (seconds) for a single OTLP export attempt.  The exporter uses
    # blocking HTTP under the hood, so this caps how long the BatchSpanProcessor
    # worker thread can be tied up on a slow or unreachable collector.  Keeping
    # it short (default 2 s) means a stalling Jaeger/OTEL collector cannot block
    # the worker long enough to fill the span queue and cause span drops on busy
    # services.  Raise it only if your collector is reliably slow but functional.
    otlp_exporter_timeout_s: int = 2

    # --- Sync ---
    connector_run_timeout_s: int = 300
    # Both webhook secrets read a "PROVIDER_WEBHOOK_SECRET" env var — the
    # reverse word order of the field name — pinned via validation_alias.
    webhook_secret_github: str | None = Field(default=None, validation_alias="GITHUB_WEBHOOK_SECRET")
    webhook_secret_gitlab: str | None = Field(default=None, validation_alias="GITLAB_WEBHOOK_SECRET")

    # --- Logging ---
    # "json" emits structured JSON to stdout (production default); "text" emits
    # human-readable plain text (local development). configure_logging() branches
    # on this value — unrecognised strings fall through to the text renderer.
    log_format: str = "json"

    # Root logger level. logging.DEBUG surfaces SQLAlchemy queries and
    # OpenTelemetry SDK internals — high volume; reserve for diagnosis.
    log_level: int = logging.INFO

    # ------------------------------------------------------------------
    # Field validators — each carries one historical env-var grammar.
    # `mode="before"` runs on the raw input (string from the environment,
    # or whatever a caller passed as a keyword) before pydantic's own type
    # coercion; a non-string input (e.g. a bool or list passed directly by
    # a test) is returned unchanged rather than reparsed.
    # ------------------------------------------------------------------

    @field_validator("scheduler_use_memory_jobstore", mode="before")
    @classmethod
    def _parse_memory_jobstore_flag(cls, value: Any) -> Any:
        """Positive allowlist: only "1"/"true"/"yes" (case-insensitive, not
        whitespace-trimmed) mean true; every other spelling, including an
        unset variable, means false."""
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return value

    @field_validator("rate_limit_enabled", mode="before")
    @classmethod
    def _parse_rate_limit_enabled_flag(cls, value: Any) -> Any:
        """Negative denylist: only "0"/"false"/"no" (case-insensitive, not
        whitespace-trimmed) mean false; every other spelling, including an
        unset variable, means true. The inverse convention of
        scheduler_use_memory_jobstore -- preserved as-is; unifying the two
        grammars is a deliberate, separate change, not a side effect of
        this one."""
        if isinstance(value, str):
            return value.lower() not in ("0", "false", "no")
        return value

    @field_validator("extraction_provider", mode="before")
    @classmethod
    def _validate_extraction_provider(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return _resolve_extraction_provider(value)
        return value

    @field_validator("http_methods_mode", "http_method_alias_separator", mode="before")
    @classmethod
    def _normalize_http_method_settings(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("build_revision", mode="before")
    @classmethod
    def _normalize_build_revision(cls, value: Any) -> Any:
        """Unset or set-but-empty (after stripping) both mean "unknown"."""
        if isinstance(value, str):
            return value.strip() or "unknown"
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _resolve_log_level(cls, value: Any) -> Any:
        """LOG_LEVEL names a `logging` module attribute, matched
        case-insensitively via `.upper()`. An unrecognized name falls back
        to INFO silently -- that is the historical behavior, not a
        validation gap left open here."""
        if isinstance(value, str):
            return getattr(logging, value.upper(), logging.INFO)
        return value

    @field_validator(
        "oidc_issuer_allowlist",
        "oidc_client_id_allowlist",
        "resource_uri_allowlist",
        mode="before",
    )
    @classmethod
    def _parse_csv_fields(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _parse_csv_list(value)
        return value

    @field_validator("arc_global_operator_allowlist", mode="before")
    @classmethod
    def _parse_operator_allowlist_field(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _parse_operator_allowlist(value)
        return value

    @field_validator("entitlement_role_mapping", mode="before")
    @classmethod
    def _parse_role_mapping_field(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _parse_role_mapping(value)
        return value

    # ------------------------------------------------------------------
    # Model validators — cross-field defaults and required-together checks.
    # These run after every field has been individually validated, in the
    # order defined below.
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _apply_url_defaults(self) -> Settings:
        """pgbouncer_url and scheduler_jobstore_url both default to
        database_url -- most deployments point every DB-touching component
        at the same connection string, so naming it three times would just
        be three chances to typo it differently."""
        if not self.pgbouncer_url:
            self.pgbouncer_url = self.database_url
        if not self.scheduler_jobstore_url:
            self.scheduler_jobstore_url = self.database_url
        return self

    @model_validator(mode="after")
    def _resolve_embedding_provider_field(self) -> Settings:
        self.embedding_provider = _resolve_embedding_provider(self.embedding_provider, self.embedding_model)
        return self

    @model_validator(mode="after")
    def _validate_entitlement_config(self) -> Settings:
        # Entitlement service config: required-together. When the entitlement
        # path is wired (entitlement_service_url is non-empty), every related
        # field must also be provided and well-formed. Defaults of empty
        # string / empty dict / empty list are permitted only when the
        # entitlement path is disabled — this keeps existing tests that
        # construct minimal Settings(...) without the new fields working,
        # while still failing loudly at startup the moment the new path is
        # enabled with incomplete config.
        if self.entitlement_service_url:
            if not self.entitlement_service_env:
                raise ValueError("ENTITLEMENT_SERVICE_ENV must be set when ENTITLEMENT_SERVICE_URL is set.")
            if not self.entitlement_service_discriminator:
                raise ValueError("ENTITLEMENT_SERVICE_DISCRIMINATOR must be set when ENTITLEMENT_SERVICE_URL is set.")
            if any(c.isspace() for c in self.entitlement_service_discriminator):
                raise ValueError(
                    "ENTITLEMENT_SERVICE_DISCRIMINATOR may not contain whitespace; "
                    f"got {self.entitlement_service_discriminator!r}."
                )
            if not self.entitlement_role_mapping:
                raise ValueError(
                    "ENTITLEMENT_ROLE_MAPPING must be a non-empty mapping when " "ENTITLEMENT_SERVICE_URL is set."
                )
            for external, internal in self.entitlement_role_mapping.items():
                if not external:
                    raise ValueError(
                        "ENTITLEMENT_ROLE_MAPPING contains an entry with an empty external "
                        f"key (mapped to {internal!r})."
                    )
                if not internal:
                    raise ValueError(
                        f"ENTITLEMENT_ROLE_MAPPING entry {external!r} has an empty internal " "role value."
                    )
                if internal not in _VALID_INTERNAL_ROLES:
                    raise ValueError(
                        f"ENTITLEMENT_ROLE_MAPPING entry {external!r}:{internal!r} maps to an "
                        f"unknown internal role; valid roles are "
                        f"{sorted(_VALID_INTERNAL_ROLES)}."
                    )
            mapped_roles = set(self.entitlement_role_mapping.values())
            uncovered = _VALID_INTERNAL_ROLES - mapped_roles
            if uncovered:
                # Soft warning, not a hard failure: a deployment may legitimately
                # not expose every internal role (e.g. `auditor` may be omitted).
                logging.getLogger(__name__).warning(
                    "ENTITLEMENT_ROLE_MAPPING does not map any external suffix to "
                    "the following internal role(s): %s. Endpoints requiring those "
                    "roles will be inaccessible.",
                    sorted(uncovered),
                )
        return self


def get_settings() -> Settings:
    """Construct Settings from environment variables. Required vars must be set."""
    # database_url has no Python-level default because it is required at
    # runtime -- but mypy's static view of a pydantic model has no way to
    # know that BaseSettings fills required fields from the environment
    # when the caller doesn't; it just sees a constructor call missing a
    # required keyword. Absence still fails loudly, just inside Settings()
    # rather than at this call site.
    return Settings()  # type: ignore[call-arg]
