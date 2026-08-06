"""One place that turns configuration into a provider.

Separate from `main.py` so the decision is testable without building an app, and
separate from each adapter so no adapter has to know about the others.

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
from registry.extraction.anthropic_provider import build_from_env
from registry.extraction.local_rules import LocalRulesProvider
from registry.extraction.provider import ExtractionProvider, NoOpProvider

_log = logging.getLogger(__name__)


def build_provider(settings: Settings, *, env: dict[str, str] | None = None) -> ExtractionProvider:
    """The provider named by configuration.

    Raises at startup for a selected-but-unusable provider. Never falls back
    silently: a deployment that asked for a model and got the no-op would report
    healthy while producing nothing.

    `env` is a test seam only -- it lets a unit test hand in a synthetic
    mapping without touching the process environment. Production never passes
    it: the key comes from ``settings.extraction_anthropic_api_key``, which
    `Settings` already resolved from `CLAUDE_API_KEY`/`ANTHROPIC_API_KEY` (see
    `registry/config.py`), so this module has no reason to read `os.environ`
    itself.
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
        elif settings.extraction_anthropic_api_key:
            environ = {"CLAUDE_API_KEY": settings.extraction_anthropic_api_key}
        else:
            environ = {}
        provider = build_from_env(environ, timeout_s=settings.extraction_timeout_s)
        _log.info("extraction.provider_anthropic: model=%s", settings.extraction_model)
        return provider

    # Unreachable via Settings, which validates the selector. Kept because a
    # caller constructing Settings directly bypasses that validation, and
    # returning the no-op here would be the silent fallback this module exists
    # to prevent.
    msg = f"unknown extraction provider {selected!r}"
    raise ValueError(msg)


__all__ = ["build_provider"]
