"""Which model does what: extraction, simulation, and the judges.

Split out of `config.py` when that file crossed its size ceiling, and along a
real seam rather than an arbitrary one. These fields are one subject — *which
model does what, under which credential, against which endpoint* — and they share
a validator, a selector vocabulary, and a rule that the roles must not collide.
The rest of `Settings` is database URLs, embedding paths and scheduler intervals,
which have nothing to do with any of that.

**Three roles, configured symmetrically, and the symmetry is the point.** ADR
0026 requires the simulated agent and its judge to be different provider
families, which a deployment cannot express with one set of settings. Extraction
was the first consumer of the provider layer and keeps its own credential for the
same reason: three roles sharing one key would be three roles nobody could point
at three vendors.

**Every role defaults to `noop`, which switches it off.** A deployment that
configures none of them is complete rather than broken — events are still
captured and served, prompt sets and runs and verdicts all work, and the three
deterministic evaluation criteria need no model at all. `extraction/provider.py`
states the rule and it holds for every role here: *a working deployment with one
feature switched off, not a broken one*.

**A `Settings` mixin rather than a nested model.** `pydantic-settings` reads flat
environment variables, so a nested model would mean nested env names — a
deployment-facing change to variables that already ship. Inheritance keeps every
name exactly as it was while giving the fields a file of their own.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings


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


class LlmRoleSettings(BaseSettings):
    """The provider-role half of `Settings`, inherited rather than nested.

    Every field here reads the same flat environment variable it always did;
    nesting would have renamed the deployment surface to tidy a file.
    """

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

    # --- Agent simulation and its judge (E24, ADR 0025 and ADR 0026) ---
    #
    # Two roles, configured symmetrically, because ADR 0026 requires them to be
    # different provider families and a deployment cannot satisfy that rule with
    # one set of settings. They are separate from the extraction settings above
    # for the same reason: three roles that happened to share a credential today
    # would be three roles nobody could point at three endpoints tomorrow.
    #
    # Both default to "noop", which switches the feature off. A deployment that
    # configures neither has a complete evaluation loop over retrieval — prompt
    # sets, runs, verdicts and the deterministic three criteria all work — and is
    # told which setting is missing when somebody asks for the agent half. That
    # is `extraction/provider.py`'s rule applied to a request path: "a working
    # deployment with one feature switched off, not a broken one".
    #
    # Which model answers as the simulated agent.
    simulation_provider: str = "noop"
    # Empty means the selected adapter's own default. A model id belongs to
    # whichever provider has to serve it, so naming one here would pick a single
    # vendor's model for every selector.
    simulation_model: str = ""
    simulation_base_url: str = ""
    # Longer than the extraction default: a simulation reads a whole envelope and
    # writes an argued answer, and a timeout tuned for a claim-extraction call
    # would fail the long ones systematically rather than at random.
    simulation_timeout_s: float = 120.0
    # The answer's ceiling. A budget, not a target — ARC's rule that a budget
    # changes presentation and never obligations is why this bounds the response
    # and nothing in the envelope.
    simulation_max_output_tokens: int = 2048
    simulation_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("SIMULATION_API_KEY"))

    # Which model grades groundedness and answer relevance. Never the same
    # provider family as `simulation_provider` — the service refuses the pair
    # rather than warning about it, per ADR 0026, because an advisory constraint
    # is the shape of guidance followed until the day it matters.
    judge_provider: str = "noop"
    judge_model: str = ""
    judge_base_url: str = ""
    judge_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("JUDGE_API_KEY"))

    # Two further judges, for the opt-in panel (E24-T8). Numbered rather than a
    # list, because pydantic-settings reads flat environment variables and a
    # parsed list of credentials would be one variable carrying three secrets.
    #
    # A panel is opt-in and costs 3x, which is right for a launch gate and wrong
    # for iteration — so these default to "noop" and a deployment that configures
    # neither simply has no panel. Family diversity is required *across the
    # panel*, not merely against the candidate: three judges from one family
    # cancel nothing.
    judge_2_provider: str = "noop"
    judge_2_model: str = ""
    judge_2_base_url: str = ""
    judge_2_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("JUDGE_2_API_KEY"))

    judge_3_provider: str = "noop"
    judge_3_model: str = ""
    judge_3_base_url: str = ""
    judge_3_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("JUDGE_3_API_KEY"))

    @field_validator(
        "extraction_provider",
        "simulation_provider",
        "judge_provider",
        "judge_2_provider",
        "judge_3_provider",
        mode="before",
    )
    @classmethod
    def _validate_extraction_provider(cls, value: object) -> object:
        """One resolver for all three provider selectors.

        The same names are legal in all three roles, including a provider a
        deployment supplied itself: a registry that accepted a third-party name
        for extraction and refused it for judging would make the discovery
        mechanism half-reachable, which is worse than not having it.

        An unknown value fails at startup rather than silently falling back. A
        typo'd judge selector that quietly became "noop" would look exactly like
        a deployment that had decided not to judge.
        """
        if value is None or isinstance(value, str):
            return _resolve_extraction_provider(value)
        return value
