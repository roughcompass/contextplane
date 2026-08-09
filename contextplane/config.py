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

The pure string-to-value parsers themselves live in `config_grammar`, which
this module imports and which imports nothing back. This module owns the
model: the field declarations, their defaults, and the validators that decide
when each parser runs and what happens to what it returns.

`get_settings()` reads from environment variables. Tests construct `Settings`
directly with keyword arguments. No module-level singleton — wired by FastAPI
DI like `Clock`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from contextplane.config_grammar import (
    _parse_csv_list,
    _parse_extraction_extra_headers,
    _parse_operator_allowlist,
    _parse_role_mapping,
    _resolve_embedding_provider,
    _resolve_extraction_auth_template,
    _resolve_extraction_base_url,
)

# Internal role names accepted by the registry's RBAC layer. Entitlement
# strings carry external suffix names that must map to one of these four.
_VALID_INTERNAL_ROLES: frozenset[str] = frozenset({"admin", "producer", "consumer", "auditor"})


def __getattr__(name: str) -> object:
    """Resolve `EXTRACTION_PROVIDERS` to the built-in names, on first access.

    The list itself lives in `extraction.provider_registry`, next to the
    shadowing check that compares a supplied name against it. Kept reachable
    from here because that is where it has always been imported from, and
    resolved lazily rather than imported at module level for the reason below:
    the registry needs `Settings` to type what a supplied provider is built
    from, so a module-level import back into it would close a cycle that only
    surfaces when the application actually starts.
    """
    if name == "EXTRACTION_PROVIDERS":
        from contextplane.extraction.provider_registry import BUILT_IN_PROVIDERS  # noqa: PLC0415

        return BUILT_IN_PROVIDERS
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def _resolve_extraction_provider(raw: str | None) -> str:
    """Validate the selector against every provider this deployment has.

    An unknown value fails at startup rather than silently falling back. A
    typo'd provider name that quietly became "noop" would look exactly like a
    working deployment producing no claims, and the operator would go looking
    for the bug in extraction.

    The legal names are no longer a literal here. A deployment that installs a
    provider of its own gets a name this repo has never seen, and validating
    against a frozen set would reject it during `Settings` construction --
    before the code that could build it is ever reached, which would make the
    whole discovery mechanism unreachable.

    **The registry is imported inside this function, not at module scope.** It
    needs `Settings` to type what a supplied provider is built from; importing
    it at the top of this module would close that loop into an ImportError at
    boot. No gate in this repo catches that -- the import-direction check
    polices scripts against tests -- so the reason is written here rather than
    left to be rediscovered.
    """
    if raw is None or not raw.strip():
        return "noop"
    value = raw.strip().lower()

    from contextplane.extraction.provider_registry import provider_names  # noqa: PLC0415

    # Names come from installed distribution metadata, which is read without
    # importing any of it. Constructing `Settings` must not execute a third
    # party's code: it happens in every test in this repo, and a provider that
    # ran on import would be running long before anything decided to select it.
    legal = provider_names()
    if value not in legal:
        msg = (
            f"unknown EXTRACTION_PROVIDER {raw!r}; expected one of {sorted(legal)}. "
            "Leave it unset for no extraction, or use 'local' for a provider that needs no key."
        )
        raise ValueError(msg)
    return value


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

    # `hide_input_in_errors` because a validation failure here is a startup
    # crash, and pydantic's default error envelope appends the whole input
    # mapping to the message -- which for a settings model is every
    # environment variable that fed it, credentials included. Careful wording
    # inside an individual validator cannot help: the leak is in the envelope,
    # not the message. The failing field is still named, which is the part an
    # operator needs.
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)

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
    #   "anthropic" — a real model. Requires EXTRACTION_API_KEY; never
    #                required by anything else.
    #   "openai"   — any endpoint speaking /v1/chat/completions with tool
    #                calling, which is most of them: OpenAI itself, Azure,
    #                Groq, Together, vLLM, Ollama, LiteLLM, and the majority
    #                of internal gateways. Point EXTRACTION_BASE_URL at one.
    extraction_provider: str = "noop"
    # Model the strategies request. Ignored by the noop and local providers,
    # which have no model to select.
    extraction_model: str = "claude-haiku-4-5-20251001"
    extraction_timeout_s: float = 60.0

    # Transport for whichever provider is selected. These four fields are
    # deliberately vendor-neutral: an operator pointing extraction at an
    # internal gateway needs an endpoint, a credential, the shape of the auth
    # header, and room for whatever else that gateway requires -- and that is
    # the whole surface, fixed, however many providers get added later. Adding
    # a vendor means adding an adapter, not another pair of settings.
    #
    # Left empty, each one means "whatever the selected adapter defaults to",
    # which is why an unconfigured deployment keeps working unchanged.
    extraction_base_url: str = ""
    # Header the credential is sent in. Empty means the adapter's own default,
    # which differs by vendor (`x-api-key` for one, `Authorization` for
    # OpenAI-shaped endpoints) and is the adapter's business, not this file's.
    extraction_auth_header: str = ""
    # How the credential is spelled inside that header, as a template with one
    # literal `{key}` -- e.g. `Bearer {key}`. Empty sends the credential bare.
    extraction_auth_template: str = ""
    # Anything else the endpoint requires, as `Name:value,Name:value`.
    #
    # A secret rather than a plain mapping because gateways routinely
    # authenticate with a second header, so this variable carries credentials
    # in practice whether or not it does in a given deployment -- and a plain
    # value would print them out of `repr(Settings())`. Held as the raw string
    # and parsed on demand by `extraction_extra_header_pairs`; a parsed copy
    # stored on the model would put them straight back into that repr.
    extraction_extra_headers: SecretStr = SecretStr("")
    # The credential itself, for whichever provider needs one.
    #
    # EXTRACTION_API_KEY is the canonical name. The two legacy spellings are
    # accepted because deployments and runbooks already carry them, and
    # declaration order is what makes the canonical name outrank them when
    # more than one is set. There is deliberately no deprecation warning for
    # the legacy spellings: AliasChoices discards which name actually
    # supplied the value, and recovering it would take an os.environ read of
    # exactly the kind this module exists to prevent. A warning that cannot
    # tell whether the thing it warns about happened is worse than none.
    extraction_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("EXTRACTION_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_API_KEY"),
    )

    # --- Embedding ---
    # Which implementation produces vectors. See contextplane/embedding/ for the
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
    # How often judged adjudications are refit into calibration mappings, one
    # extraction strategy at a time. Hours-scale rather than minutes-scale like
    # the sweeps above: a mapping needs a couple hundred judged outcomes before
    # `publish` will even store it, so ticking every few minutes would mostly
    # find nothing new -- widening this only delays how soon a fresh mapping
    # reflects the latest judged claims, never changes what it converges to.
    calibration_refit_interval_s: int = 3600 * 6
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
    http_methods_mode: str = Field(default="rest", validation_alias="CONTEXTPLANE_HTTP_METHODS_MODE")
    http_method_alias_separator: str = Field(
        default="colon", validation_alias="CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR"
    )

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

    # --- ARC drafter model gate ---
    # Whether the model-backed drafter may serve at all. Off by default,
    # including when this variable is absent from the environment entirely
    # -- not merely when it is set to a falsy string. The decision that can
    # ever justify turning this on does not live here: it is the committed
    # artifact at contextplane/arc/drafter/model_decision.json, evaluated
    # against a version-controlled fixture corpus. This flag can only ever
    # be as permissive as that artifact -- see
    # contextplane.wiring.services._assert_drafter_decision_permits_serving,
    # which refuses to start rather than let this flag override a
    # `human_only` verdict, a failed evaluation gate, or a swapped model
    # artifact.
    arc_drafter_model_enabled: bool = False
    # Filesystem path to the deployment-local model artifact this flag would
    # enable. Only ever read when arc_drafter_model_enabled is true; its
    # SHA-256 must equal model_decision.json's recorded
    # model_artifact_digest, checked at startup, not at first request.
    arc_drafter_model_artifact_path: str | None = None

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

    # --- Retention key material ---
    # Root key material the tenant pseudonymization salts are derived from, as
    # `key_id:hex,key_id:hex`. More than one because a rotation has to hold the
    # outgoing key while tombstones minted under it are still being read; the
    # active one is named separately so rotating is a change of pointer rather
    # than a rewrite of the material.
    #
    # A secret rather than a plain mapping for the same reason the extraction
    # headers are: the value is key material, and a plain field would print it out
    # of `repr(Settings())`. Held raw and parsed on demand by
    # `retention_key_material`; a parsed copy on the model would put it back.
    retention_keys: SecretStr = SecretStr("")
    # Which of those keys new tombstones are minted under.
    #
    # Unset by default, and that default is load-bearing rather than a
    # placeholder: with no active key the salt resolver refuses to derive a salt,
    # so an erasure that cannot mint a keyed tombstone fails loudly instead of
    # reporting a removal it did not record. An improvised or empty key would let
    # that erasure report success while writing a tombstone nobody can verify and
    # everybody can correlate across tenants, which is worse than a refusal a
    # deployment notices the first time it runs one.
    retention_active_key_id: str | None = None

    # --- Metrics exposition ---
    # Bearer credential the /metrics scraper must present. There is deliberately
    # no default: the endpoint publishes process-global counters, including
    # entitlement-failure counts and the full route table, and a default value
    # would be the same as no credential at all. Unset means /metrics refuses to
    # serve rather than serving to anyone.
    metrics_bearer_token: str | None = None

    # --- OTel ---
    otlp_endpoint: str | None = None
    service_name: str = "contextplane"
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
    def _parse_memory_jobstore_flag(cls, value: object) -> object:
        """Positive allowlist: only "1"/"true"/"yes" (case-insensitive, not
        whitespace-trimmed) mean true; every other spelling, including an
        unset variable, means false."""
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return value

    @field_validator("arc_drafter_model_enabled", mode="before")
    @classmethod
    def _parse_arc_drafter_model_enabled_flag(cls, value: object) -> object:
        """Same positive-allowlist grammar as `scheduler_use_memory_jobstore`:
        only "1"/"true"/"yes" mean true. An absent variable and every other
        spelling mean false -- this flag must never default to more
        permissive than an operator explicitly asked for."""
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return value

    @field_validator("rate_limit_enabled", mode="before")
    @classmethod
    def _parse_rate_limit_enabled_flag(cls, value: object) -> object:
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
    def _validate_extraction_provider(cls, value: object) -> object:
        if value is None or isinstance(value, str):
            return _resolve_extraction_provider(value)
        return value

    @field_validator("http_methods_mode", "http_method_alias_separator", mode="before")
    @classmethod
    def _normalize_http_method_settings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("build_revision", mode="before")
    @classmethod
    def _normalize_build_revision(cls, value: object) -> object:
        """Unset or set-but-empty (after stripping) both mean "unknown"."""
        if isinstance(value, str):
            return value.strip() or "unknown"
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _resolve_log_level(cls, value: object) -> object:
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
    def _parse_csv_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_csv_list(value)
        return value

    @field_validator("arc_global_operator_allowlist", mode="before")
    @classmethod
    def _parse_operator_allowlist_field(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_operator_allowlist(value)
        return value

    @field_validator("entitlement_role_mapping", mode="before")
    @classmethod
    def _parse_role_mapping_field(cls, value: object) -> object:
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
    def _resolve_extraction_transport(self) -> Settings:
        """Normalize and validate the extraction transport as one unit.

        A model validator rather than four field validators because the extra
        headers cannot be checked without the auth header: the one thing the
        operator must not be able to do is set the credential header twice,
        once through the template and once through a smuggled extra pair, and
        no single-field validator can see both.
        """
        self.extraction_base_url = _resolve_extraction_base_url(self.extraction_base_url)
        self.extraction_auth_header = self.extraction_auth_header.strip()
        self.extraction_auth_template = _resolve_extraction_auth_template(self.extraction_auth_template)
        # Parse and discard: this is where a malformed value fails, at startup,
        # rather than at the first extraction call hours later.
        self.extraction_extra_header_pairs()
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

    # ------------------------------------------------------------------
    # Accessors for values held as secrets.
    # ------------------------------------------------------------------

    def extraction_extra_header_pairs(self) -> tuple[tuple[str, str], ...]:
        """`EXTRACTION_EXTRA_HEADERS`, parsed into ordered header pairs.

        Parsed on each call rather than stored, because a parsed copy living
        on the model would put the header values back into `repr(Settings())`
        -- which is the entire reason the raw value is a `SecretStr`. The
        parse is a string split over a value with a handful of entries, and
        an adapter reads it once when it is constructed.
        """
        return _parse_extraction_extra_headers(
            self.extraction_extra_headers.get_secret_value(),
            auth_header=self.extraction_auth_header,
        )

    def retention_key_material(self) -> dict[str, bytes]:
        """`RETENTION_KEYS`, parsed into raw key material by key id.

        Parsed on each call rather than stored, for the reason the header
        accessor above gives: a parsed copy on the model would put the key
        material back into `repr(Settings())`.

        A malformed entry raises rather than being skipped. A skipped key is
        indistinguishable from one that was never configured, so the deployment
        that fat-fingered its active key would get the unkeyed-salt refusal and
        no indication that the material it supplied was the problem.
        """
        material: dict[str, bytes] = {}
        for entry in self.retention_keys.get_secret_value().split(","):
            entry = entry.strip()
            if not entry:
                continue
            key_id, separator, encoded = entry.partition(":")
            if not separator or not key_id.strip() or not encoded.strip():
                msg = "RETENTION_KEYS entries are 'key_id:hex' pairs; one entry has no key id or no material"
                raise ValueError(msg)
            try:
                material[key_id.strip()] = bytes.fromhex(encoded.strip())
            except ValueError as exc:
                msg = f"RETENTION_KEYS entry {key_id.strip()!r} is not valid hex"
                raise ValueError(msg) from exc
        return material


def get_settings() -> Settings:
    """Construct Settings from environment variables. Required vars must be set."""
    # database_url has no Python-level default because it is required at
    # runtime -- but mypy's static view of a pydantic model has no way to
    # know that BaseSettings fills required fields from the environment
    # when the caller doesn't; it just sees a constructor call missing a
    # required keyword. Absence still fails loudly, just inside Settings()
    # rather than at this call site.
    return Settings()  # type: ignore[call-arg]
