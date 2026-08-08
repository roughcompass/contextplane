"""Which extraction providers exist, including ones this codebase never saw.

Every other part of the extraction seam tidies or adds a vendor. This is the
part that lets a deployment bring one nobody here has heard of: an organisation
points `EXTRACTION_PROVIDER` at a name their own package declares, and the
platform builds it, meters it, and holds it to the same containment contract as
the adapters that ship in this repo.

**Names are the key; `provider_id` is not.** A selector is what an operator
types and what the `provider` metric label carries. `provider_id` is a frozen
identifier the provider declares about itself, and it is part of the persisted
`(provider_id, model_id, strategy_id)` calibration key. The two already
disagree for a built-in -- `local` selects a provider whose `provider_id` is
`local-rules` -- and deriving one from the other would silently repartition
mappings that have already been published. So the registry keys on the
selector and never invents a `provider_id` from it.

**Discovery reads metadata; it does not import.** Enumerating entry points
reads names out of installed distributions without executing any of their
code. Only the *selected* provider is ever loaded, and only when it is being
built, so a deployment running `noop` executes no third-party code at all.

**Every failure here is loud, at startup.** A name that collides, a
distribution whose metadata cannot be read, an entry point that fails to
import, an object that is not a builder: each raises. The tempting
alternative -- skip the broken one and carry on -- produces a deployment that
reports healthy while extracting nothing, which is the exact failure this
whole seam is built to make impossible. Install order deciding which code
receives the credential and every tenant's session text is not a resolvable
ambiguity; it is a refusal.
"""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import EntryPoint, PackageNotFoundError, entry_points
from typing import TYPE_CHECKING, Protocol

from contextplane.exceptions import RegistryError

if TYPE_CHECKING:
    # Imported for typing only. `config` will come to depend on this module to
    # validate the selector, and a module-level import back into `config` would
    # close that loop into an ImportError at boot -- one that no gate catches,
    # because it only appears when the application is actually started.
    from contextplane.config import Settings
    from contextplane.extraction.provider import ExtractionProvider

_log = logging.getLogger(__name__)

# Minted in this platform's own namespace rather than borrowed from the
# package this repo currently installs as. Third parties hard-depend on this
# string in their own packaging metadata, so it is a public identifier with no
# back-compat burden yet -- which makes now the only cheap time to name it
# after the product rather than after a directory.
ENTRY_POINT_GROUP = "contextplane.extraction_providers"

# The selectors this repo ships. Held here rather than in the config module so
# there is one list, and so the shadowing check below has something to compare
# a third-party name against.
BUILT_IN_PROVIDERS: frozenset[str] = frozenset({"noop", "local", "anthropic", "openai"})

# The selector is lowercased before it ever reaches validation, so a name
# carrying uppercase could never be selected -- it would be a provider that
# installs cleanly and cannot be reached, which is worse than a refusal.
_NAME_CHARS: frozenset[str] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.")


class ProviderDiscoveryError(RegistryError):
    """A provider could not be discovered, loaded, or trusted.

    Always raised, never logged-and-skipped. A registry that quietly drops a
    broken entry hands the deployment a working-looking `noop`, and the
    operator goes looking for the fault in extraction.
    """


class ProviderBuilder(Protocol):
    """What an entry point must resolve to.

    One argument, the settings the application already resolved, so a provider
    reads its configuration the same way every built-in does rather than
    reaching into the environment on its own.
    """

    def __call__(self, settings: Settings) -> ExtractionProvider: ...


def _assert_usable_name(name: str, *, distribution: str) -> None:
    if not name:
        msg = f"distribution {distribution!r} declares an extraction provider with an empty name"
        raise ProviderDiscoveryError(msg)
    if not set(name) <= _NAME_CHARS:
        msg = (
            f"distribution {distribution!r} declares extraction provider {name!r}, which is not a "
            "lowercase name; the selector is lowercased before it is matched, so this entry could "
            "never be selected"
        )
        raise ProviderDiscoveryError(msg)


def _distribution_label(point: EntryPoint) -> str:
    """`name version`, or a stand-in when the metadata will not say.

    Only ever used in messages. A missing distribution is not itself fatal --
    an entry point registered from a path a test put on `sys.path` has none --
    so this reports what it can rather than raising on a cosmetic gap.
    """
    try:
        dist = point.dist
    except (PackageNotFoundError, AttributeError):  # pragma: no cover - defensive
        return "an unidentified distribution"
    if dist is None:
        return "an unidentified distribution"
    return f"{dist.name} {dist.version}"


def _discover() -> dict[str, EntryPoint]:
    """Every third-party provider name, read from metadata without importing."""
    try:
        points = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        # A corrupted `.dist-info` in any installed package makes enumeration
        # itself fail. Returning nothing here would empty the registry and turn
        # a packaging fault into "your provider is not installed", which sends
        # the operator to the wrong place entirely.
        msg = (
            "extraction provider discovery failed while reading installed distribution metadata; "
            f"an installed package's metadata is unreadable ({type(exc).__name__}). Extraction "
            "cannot start until it is repaired or removed."
        )
        raise ProviderDiscoveryError(msg) from exc

    found: dict[str, EntryPoint] = {}
    for point in points:
        label = _distribution_label(point)
        _assert_usable_name(point.name, distribution=label)
        if point.name in BUILT_IN_PROVIDERS:
            msg = (
                f"{label} declares extraction provider {point.name!r}, which is a name this "
                "platform already ships. Install order would decide which code receives the "
                "credential and every tenant's session text, so this is refused rather than "
                "resolved. Rename the third-party provider."
            )
            raise ProviderDiscoveryError(msg)
        if point.name in found:
            msg = (
                f"extraction provider {point.name!r} is declared by two installed distributions, "
                f"{_distribution_label(found[point.name])} and {label}. Install order would decide "
                "which one receives the credential and every tenant's session text, so this is "
                "refused rather than resolved. Uninstall one."
            )
            raise ProviderDiscoveryError(msg)
        found[point.name] = point
    return found


# Discovery is stable for the life of the process -- nothing installs a
# distribution into a running interpreter -- and `Settings()` is constructed
# per instance in tests, so re-enumerating every installed package on each
# construction would be pure cost.
_cache: dict[str, EntryPoint] | None = None


def discovered_providers() -> dict[str, EntryPoint]:
    """Third-party provider names, discovered once per process."""
    global _cache
    if _cache is None:
        _cache = _discover()
    return dict(_cache)


def reset_discovery_cache() -> None:
    """Forget what discovery found. For tests that manipulate `sys.path`."""
    global _cache
    _cache = None


def provider_names() -> frozenset[str]:
    """Every selector this deployment will accept."""
    return BUILT_IN_PROVIDERS | frozenset(discovered_providers())


def is_third_party(name: str) -> bool:
    return name not in BUILT_IN_PROVIDERS and name in discovered_providers()


def build_third_party(name: str, settings: Settings) -> ExtractionProvider:
    """Load and construct one discovered provider.

    This is the only place third-party code is imported, and it happens at
    startup while building the provider that was actually selected. Nothing is
    sandboxed: an entry point executes at import like any other dependency in
    the process, and pretending otherwise would suggest a boundary that is not
    there.
    """
    point = discovered_providers().get(name)
    if point is None:  # pragma: no cover - callers check first
        msg = f"extraction provider {name!r} is not an installed third-party provider"
        raise ProviderDiscoveryError(msg)

    label = _distribution_label(point)
    try:
        builder = point.load()
    except Exception as exc:
        msg = (
            f"extraction provider {name!r} from {label} failed to import "
            f"({type(exc).__name__}: {exc}). It is the selected provider, so this is fatal rather "
            "than a reason to fall back to no extraction at all."
        )
        raise ProviderDiscoveryError(msg) from exc

    _assert_builder(builder, name=name, label=label)

    provider: ExtractionProvider = builder(settings)

    if not callable(getattr(provider, "extract", None)):
        msg = (
            f"extraction provider {name!r} from {label} built an object with no callable "
            f"extract(); {type(provider).__name__} does not satisfy the provider contract, and "
            "the first thing to notice would otherwise be a drained queue."
        )
        raise ProviderDiscoveryError(msg)

    declared = getattr(provider, "provider_id", None)
    if declared != name:
        # Enforced for third parties and deliberately not for built-ins, whose
        # one disagreement (`local` -> `local-rules`) is older than this check
        # and is a published calibration key. For a new provider the two must
        # agree, or its metrics are filed under the selector while its
        # calibration rows are filed under something else, and nobody joins
        # them again.
        msg = (
            f"extraction provider {name!r} from {label} declares provider_id {declared!r}. The "
            "selector names the metric label and provider_id is a persisted calibration key; when "
            "they disagree the two cannot be joined afterwards, so they must match."
        )
        raise ProviderDiscoveryError(msg)

    _log.info("extraction.provider_third_party: name=%s supplied_by=%s", name, label)
    return provider


def _assert_builder(builder: object, *, name: str, label: str) -> None:
    """Refuse anything that is not a one-argument callable, before calling it.

    Checked rather than discovered by `TypeError` at the call, because the
    message a failed call produces names an argument count and not the
    packaging mistake that caused it.
    """
    if not callable(builder):
        msg = (
            f"extraction provider {name!r} from {label} resolves to "
            f"{type(builder).__name__}, not a callable. An entry point must name a function "
            "taking the application settings and returning a provider."
        )
        raise ProviderDiscoveryError(msg)
    try:
        signature = inspect.signature(builder)
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return
    accepts = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
    ]
    if not accepts:
        msg = (
            f"extraction provider {name!r} from {label} takes no positional argument. An entry "
            "point must accept the application settings, so that a provider reads configuration "
            "the way every built-in does rather than reaching into the environment itself."
        )
        raise ProviderDiscoveryError(msg)


__all__ = [
    "BUILT_IN_PROVIDERS",
    "ENTRY_POINT_GROUP",
    "ProviderBuilder",
    "ProviderDiscoveryError",
    "build_third_party",
    "discovered_providers",
    "is_third_party",
    "provider_names",
    "reset_discovery_cache",
]
