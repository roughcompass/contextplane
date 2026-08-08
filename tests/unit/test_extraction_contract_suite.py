"""The two first-party in-process providers, run against the shipped contract.

The suite exists so a third party can prove its own adapter. Pointing it at our
own providers here is what stops it rotting: a contract only outsiders run has
nothing failing when it drifts, and drift in this one is how "open to any
provider" quietly stops being true.

Both providers here are in-process, so both take the in-process tier only. The
networked tier asks for things neither can promise -- structured output forced
out of a model, an error taxonomy from an endpoint -- and running it against them
would fail on facts that are correct for what they are. The Anthropic adapter
takes the networked tier where it is refitted onto the kit.

No test method is declared in this file on purpose. Every one is inherited, which
is the whole point of a suite: a subclass that redefines a test is a subclass
exempting itself from the contract it claims to satisfy.
"""

from __future__ import annotations

from registry.extraction.contract_suite import ExtractionProviderContract
from registry.extraction.local_rules import LocalRulesProvider
from registry.extraction.provider import NoOpProvider


class TestNoOpProviderContract(ExtractionProviderContract):
    """The default a deployment with nothing configured runs.

    It is in the suite precisely because it looks like it has nothing to prove.
    `model_id="noop"` and `duration_ms=0` are correct answers here, and usage
    reported as unknown rather than zero is the distinction that keeps an
    unconfigured deployment from looking identical to a free one -- which is the
    contract's single most load-bearing rule, held by the provider least likely
    to be thought about.
    """

    @staticmethod
    def make_provider() -> NoOpProvider:
        return NoOpProvider()


class TestLocalRulesProviderContract(ExtractionProviderContract):
    """The deterministic rules provider local development and CI run on.

    It reports estimated usage rather than reported or unknown, which is a third
    source the contract has to accommodate without treating an estimate as a
    measurement.
    """

    @staticmethod
    def make_provider() -> LocalRulesProvider:
        return LocalRulesProvider()
