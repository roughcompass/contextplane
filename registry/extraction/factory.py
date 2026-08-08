"""One place that turns configuration into a provider.

Separate from `main.py` so the decision is testable without building an app, and
separate from each adapter so no adapter has to know about the others.

The built-in branches are spelled out here because each one plumbs a credential
differently, and a table of four entries that all take different arguments is a
table in name only. Anything not built in comes from `provider_registry`, which
owns discovery and holds a supplied provider to the same construction checks.

The failure mode this guards against: a deployment selects a provider it has not
supplied credentials for, and extraction quietly does nothing. That looks
identical to a working deployment whose sessions happen to contain no extractable
claims, so it is refused loudly at startup instead — with the two ways out named
in the message, because "set the key" is not the only correct answer and the
key-free provider is usually the one a developer wanted.
"""

from __future__ import annotations

import logging

from registry.config import Settings
from registry.extraction.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from registry.extraction.anthropic_provider import build_from_env
from registry.extraction.local_rules import MODEL_ID as LOCAL_MODEL_ID
from registry.extraction.local_rules import LocalRulesProvider
from registry.extraction.openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from registry.extraction.openai_provider import build_from_env as build_openai_from_env
from registry.extraction.provider import ExtractionProvider, NoOpProvider
from registry.extraction.provider_registry import (
    ENTRY_POINT_GROUP,
    build_third_party,
    is_third_party,
    provider_names,
)

_log = logging.getLogger(__name__)

# Selector name to the wire model that selector's provider sends when a strategy
# pins none. A lookup rather than a construction: the one caller is a read-only
# admin view that needs a single string, and building a provider to ask it would
# mean a credential read and a possible raise while serving a GET.
#
# Total over the built-in selectors, so a name added to one and not the other is
# a test failure rather than a missing row at runtime.
_BUILT_IN_DEFAULT_MODELS: dict[str, str] = {
    "noop": NoOpProvider.default_model_id,
    "local": LOCAL_MODEL_ID,
    "anthropic": ANTHROPIC_DEFAULT_MODEL,
    "openai": OPENAI_DEFAULT_MODEL,
}


def default_model_for(selector: str) -> str:
    """The wire model *selector*'s provider uses when a strategy pins none.

    Answers the question without constructing anything. A provider supplied by
    another package is not covered -- naming its default would mean importing
    and building it, which is a credential read and a possible raise on a read
    path -- so it answers with the empty string, and a caller reports the
    strategy's own pin or nothing rather than inventing a model id.
    """
    return _BUILT_IN_DEFAULT_MODELS.get(selector, "")


def build_provider(settings: Settings, *, env: dict[str, str] | None = None) -> ExtractionProvider:
    """The provider named by configuration.

    Raises at startup for a selected-but-unusable provider. Never falls back
    silently: a deployment that asked for a model and got the no-op would report
    healthy while producing nothing.

    `env` is a test seam only -- it lets a unit test hand in a synthetic
    mapping without touching the process environment. Production never passes
    it: the key comes from ``settings.extraction_api_key``, which `Settings`
    already resolved from `EXTRACTION_API_KEY`, falling back to the legacy
    `CLAUDE_API_KEY`/`ANTHROPIC_API_KEY` spellings (see `registry/config.py`),
    so this module has no reason to read `os.environ` itself.
    """
    selected = settings.extraction_provider

    if selected == "noop":
        # Logged at info, once, because it is a normal state that explains an
        # otherwise puzzling absence of claims. Not a warning: nothing is wrong.
        _log.info(
            "extraction.provider_disabled: no extraction provider configured, so no "
            "session-derived claims will be produced. Session capture, replay, and "
            "connector-fed claims are unaffected. Set EXTRACTION_PROVIDER=local for a "
            "provider that needs no credentials."
        )
        return NoOpProvider()

    if selected == "local":
        _log.info(
            "extraction.provider_local: using deterministic pattern rules. Output quality "
            "reflects the rule set, not a model -- do not benchmark extraction against this."
        )
        return LocalRulesProvider()

    if selected == "anthropic":
        if env is not None:
            environ = env
        elif settings.extraction_api_key:
            # Unwrapped only here, at the point the credential is handed to the
            # transport. Holding it as SecretStr everywhere else is what keeps it
            # out of repr(Settings()) and out of a startup crash log.
            environ = {"CLAUDE_API_KEY": settings.extraction_api_key.get_secret_value()}
        else:
            environ = {}
        # The transport settings are passed through as configured, empty
        # included: each one empty means "whatever this adapter defaults to",
        # which is what keeps a deployment that configured none of them working
        # exactly as it did before any of them existed.
        provider = build_from_env(
            environ,
            timeout_s=settings.extraction_timeout_s,
            base_url=settings.extraction_base_url,
            auth_header=settings.extraction_auth_header,
            auth_template=settings.extraction_auth_template,
            extra_headers=settings.extraction_extra_header_pairs(),
        )
        _log.info("extraction.provider_anthropic: model=%s", settings.extraction_model)
        return provider

    if selected == "openai":
        if env is not None:
            environ = env
        elif settings.extraction_api_key:
            # Unwrapped only here, where it is handed to the transport. This
            # adapter's own env fallback names OPENAI_API_KEY; the canonical
            # spelling is what Settings already resolved, so that is what it
            # gets handed.
            environ = {"EXTRACTION_API_KEY": settings.extraction_api_key.get_secret_value()}
        else:
            environ = {}
        openai_provider = build_openai_from_env(
            environ,
            timeout_s=settings.extraction_timeout_s,
            base_url=settings.extraction_base_url,
            auth_header=settings.extraction_auth_header,
            auth_template=settings.extraction_auth_template,
            extra_headers=settings.extraction_extra_header_pairs(),
        )
        _log.info("extraction.provider_openai: model=%s", settings.extraction_model)
        return openai_provider

    if is_third_party(selected):
        # The only place third-party code is imported, and only for the
        # provider that was actually selected. The registry construct-checks
        # what it gets back; nothing here re-decides that.
        return build_third_party(selected, settings)

    # Not a built-in and not installed. Reached when a caller constructs
    # `Settings` directly and bypasses selector validation, and after this the
    # name is genuinely unknown -- returning the no-op would be the silent
    # fallback this module exists to prevent.
    msg = (
        f"unknown extraction provider {selected!r}; expected one of {sorted(provider_names())}. "
        "A provider supplied by another package must declare an entry point in the "
        f"{ENTRY_POINT_GROUP!r} group and be installed in this environment."
    )
    raise ValueError(msg)


__all__ = ["build_provider", "default_model_for"]
