"""Configuration into a response provider, and the family rule ADR 0026 enforces.

E24-T3. Separate from `factory.py` because the two answer different questions:
that one builds the extraction provider a drain runs on, this one builds the pair
a simulation runs on, and the pair is the interesting part -- a candidate and a
judge from the same provider family is a configuration ADR 0026 refuses.

**The refusal is here rather than in a docstring.** Self-preference bias is
reported at 10-25 %, and the standing rule is that the judge must not be the
model under test. A note in a comment would be guidance followed until the day
somebody is in a hurry and has one key. So the pair is checked, and the check is
reached by every transport because the service calls it rather than a router.

**Absence is `None`, not a no-op.** `factory.build_provider` returns
`NoOpProvider` because extraction is a background drain and a deployment with no
credential should pause silently rather than raise every tick. A simulation is a
person clicking a button: an empty answer that looks like a model with nothing to
say is the wrong report, so this returns `None` and the service raises with the
name of the setting that is unset.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from contextplane.extraction.response_adapters import (
    ANTHROPIC_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    AnthropicResponseProvider,
    OpenAICompatibleResponseProvider,
)
from contextplane.extraction.response_provider import SimulationUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextplane.config import Settings
    from contextplane.extraction.response_provider import ResponseProvider

_log = logging.getLogger(__name__)

#: The selector that means "switched off". Shared with extraction rather than
#: minted again, because an operator who learned one spelling should not have to
#: learn a second.
DISABLED: Final = "noop"

#: Selector to the wire model that selector's provider sends when nothing pins
#: one. A lookup rather than a construction: a read path that needs the string
#: would otherwise mean a credential read and a possible raise while serving a
#: GET.
_DEFAULT_MODELS: Final[dict[str, str]] = {
    "anthropic": ANTHROPIC_DEFAULT_MODEL,
    "openai": OPENAI_DEFAULT_MODEL,
}

#: Which selectors this repo ships a *generation* adapter for. Deliberately
#: narrower than `provider_registry.BUILT_IN_PROVIDERS`: `local` is a rule engine
#: that extracts and cannot answer a prompt, and offering it here would give a
#: deployment a simulation that silently produced nothing.
GENERATION_PROVIDERS: Final[frozenset[str]] = frozenset(_DEFAULT_MODELS)


class JudgeFamilyRefused(SimulationUnavailable):
    """The candidate and the judge share a provider family.

    A subclass rather than a flag, because the two refusals have different
    remedies and the same HTTP shape: one says "configure a provider", this one
    says "configure a *different* one", and a caller that could not tell them
    apart would try the wrong fix first.
    """


def default_model_for(selector: str) -> str:
    """The wire model *selector*'s generation provider uses when nothing pins one.

    Answers the question without constructing anything. A selector with no
    shipped generation adapter answers with the empty string rather than
    inventing a model id.
    """
    return _DEFAULT_MODELS.get(selector, "")


def resolved_model(*, selector: str, pinned: str) -> str:
    """What will actually be sent: the operator's pin, or the adapter's default."""
    return pinned.strip() or default_model_for(selector)


def assert_families_differ(
    *, candidate_provider: str, judge_provider: str, candidate_model: str, judge_model: str
) -> None:
    """Refuse a simulation whose candidate and judge share a provider family.

    Named in both directions in the message: an operator reading it needs to know
    which two models were about to grade each other, not merely that something
    was wrong. ADR 0026's dissent is that this makes the judged criteria
    unavailable to a single-family deployment, and it is answered by telling that
    deployment exactly what it needs.

    Family is read from the selector rather than from the model id. That is the
    coarsest correct grain available: two models from one vendor share training
    lineage whether or not they share a name, and a finer rule would require
    knowing lineage the product cannot observe.
    """
    if judge_provider == DISABLED or candidate_provider == DISABLED:
        return
    if candidate_provider != judge_provider:
        return
    msg = (
        f"the simulated agent and its judge are both {candidate_provider!r} "
        f"({candidate_model or 'the adapter default'} judged by {judge_model or 'the adapter default'}). "
        "A judge from the candidate's own provider family scores it 10-25% higher than a third "
        "party does, so this is refused rather than corrected. Set JUDGE_PROVIDER to a different "
        "family; the deterministic criteria — required-fact recall, boundary violations and "
        "precision — need no judge and are unaffected."
    )
    raise JudgeFamilyRefused(msg)


def build_response_provider(settings: Settings, *, env: dict[str, str] | None = None) -> ResponseProvider | None:
    """The generation provider named by configuration, or `None` when switched off.

    Raises at startup for a selected-but-unusable provider, and never falls back
    silently: a deployment that asked for a model and got nothing would report
    healthy while refusing every simulation for a reason nobody could see.

    `env` is a test seam only. Production reads the credential from
    `settings.simulation_api_key`, which `Settings` already resolved.
    """
    return _build(
        selector=settings.simulation_provider,
        key=_secret(settings.simulation_api_key, env, "SIMULATION_API_KEY"),
        model=settings.simulation_model,
        base_url=settings.simulation_base_url,
        timeout_s=settings.simulation_timeout_s,
        role="simulation",
        setting="SIMULATION_PROVIDER",
        key_setting="SIMULATION_API_KEY",
    )


def build_judge_provider(settings: Settings, *, env: dict[str, str] | None = None) -> ResponseProvider | None:
    """The judging provider named by configuration, or `None` when switched off.

    The same construction as the candidate's, because a judge is a model answering
    a prompt about an answer. What is not the same is which credential it reads:
    two roles sharing one key would be two roles a deployment could not point at
    two vendors, which is the whole configuration ADR 0026 requires.
    """
    return _build(
        selector=settings.judge_provider,
        key=_secret(settings.judge_api_key, env, "JUDGE_API_KEY"),
        model=settings.judge_model,
        base_url=settings.judge_base_url,
        timeout_s=settings.simulation_timeout_s,
        role="judge",
        setting="JUDGE_PROVIDER",
        key_setting="JUDGE_API_KEY",
    )


def _secret(value: object, env: dict[str, str] | None, name: str) -> str:
    """The credential, unwrapped exactly here and nowhere else.

    Holding it as `SecretStr` everywhere else is what keeps it out of
    `repr(Settings())` and out of a startup crash log.
    """
    if env is not None:
        return env.get(name, "")
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else ""


def _build(
    *,
    selector: str,
    key: str,
    model: str,
    base_url: str,
    timeout_s: float,
    role: str,
    setting: str,
    key_setting: str,
) -> ResponseProvider | None:
    if selector == DISABLED:
        _log.info(
            "%s.provider_disabled: no %s provider configured. Prompt sets, runs, verdicts and the "
            "deterministic criteria are unaffected. Set %s to enable it.",
            role,
            role,
            setting,
        )
        return None

    if selector not in GENERATION_PROVIDERS:
        msg = (
            f"{setting}={selector!r} names no generation adapter. The selectors that can answer a "
            f"prompt are {sorted(GENERATION_PROVIDERS)}; {DISABLED!r} switches the feature off. "
            "`local` extracts claims with pattern rules and cannot generate, so it is deliberately "
            "not offered here."
        )
        raise SimulationUnavailable(msg)

    if not key.strip():
        msg = (
            f"{setting}={selector!r} was selected but {key_setting} is not set. Set the credential, "
            f"or set {setting}={DISABLED!r} to switch the feature off deliberately."
        )
        raise SimulationUnavailable(msg)

    _log.info("%s.provider_selected: %s (model %s)", role, selector, resolved_model(selector=selector, pinned=model))
    if selector == "anthropic":
        return AnthropicResponseProvider(key, timeout_s=timeout_s, base_url=base_url)
    return OpenAICompatibleResponseProvider(key, timeout_s=timeout_s, base_url=base_url)


__all__ = [
    "DISABLED",
    "GENERATION_PROVIDERS",
    "JudgeFamilyRefused",
    "assert_families_differ",
    "build_judge_provider",
    "build_response_provider",
    "default_model_for",
    "resolved_model",
]
