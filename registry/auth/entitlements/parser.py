"""Entitlement string parser — new format and legacy compatibility shim.

The current entitlement string format is `<tenant_slug>_<DISCRIMINATOR>_<ROLE>`,
where the discriminator and the external→internal role mapping are read from
`Settings` at parse time so different deployments can share the same upstream
entitlement service with their own service-name token and role suffixes.

Parsing rules (split on `f"_{settings.entitlement_service_discriminator}_"`,
maxsplit=1):

- Fewer than 2 parts → silently dropped (entitlement belongs to a different
  service sharing the same upstream).
- `parts[0]` empty → logged at WARNING and dropped (malformed; e.g.
  ``_REGISTRY_ADMIN``).
- `parts[1]` not in `settings.entitlement_role_mapping` → logged at WARNING
  and dropped (unknown role suffix; e.g. ``111205_REGISTRY_GHOST``).
- Otherwise → ``ParsedEntitlement(tenant_slug=parts[0], role=mapping[parts[1]])``.

Two Prometheus counters instrument non-match outcomes — see
``registry_entitlement_parse_ignored_total`` and
``registry_entitlement_parse_dropped_total``.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import Counter

if TYPE_CHECKING:
    from registry.config import Settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API — new entitlement parser
#
# ParsedEntitlement carries the resolved internal role (already mapped from
# the external suffix), not the raw external suffix. Callers do not need to
# know the deployment's role-mapping table.


@dataclass(frozen=True)
class ParsedEntitlement:
    """One successfully parsed entitlement string.

    `tenant_slug` is the leading token of the original entitlement (the
    tenant identifier in the upstream system). `role` is the registry's
    internal role name (one of ``admin``, ``producer``, ``consumer``,
    ``auditor``) — already mapped from the external suffix via
    ``settings.entitlement_role_mapping``.
    """

    tenant_slug: str
    role: str


# Two Prometheus counters distinguish "not for this service" (expected,
# silent) from "for this service but not usable" (logged, alertable).
# Label cardinality is bounded by the small set of reasons enumerated below.

_PARSE_IGNORED = Counter(
    "registry_entitlement_parse_ignored_total",
    "Entitlement string was silently ignored (not addressed to this service).",
    ["reason"],
)

_PARSE_DROPPED = Counter(
    "registry_entitlement_parse_dropped_total",
    "Entitlement string addressed this service but could not be used.",
    ["reason"],
)


def parse_entitlements(
    raw_list: list[str],
    settings: Settings,
) -> list[ParsedEntitlement]:
    """Parse a list of raw entitlement strings into ``ParsedEntitlement``.

    Filtering and reason classification happen here; callers receive only
    successfully parsed, role-mapped entitlements. Dropped entries are
    counted in Prometheus and logged at WARNING when they signal a problem
    rather than just a different-service entitlement.

    The function never raises — every malformed or unrecognized input
    increments a counter and is dropped. This is by design: a single bad
    entitlement string in a multi-row response must not fail the whole
    request.
    """
    discriminator = settings.entitlement_service_discriminator
    mapping = settings.entitlement_role_mapping
    delimiter = f"_{discriminator}_"

    parsed: list[ParsedEntitlement] = []
    for raw in raw_list:
        parts = raw.split(delimiter, maxsplit=1)

        if len(parts) < 2:
            # Entitlement does not contain this deployment's discriminator
            # — addressed to a different service sharing the upstream.
            # Silent drop; expected and high-volume in shared deployments.
            _PARSE_IGNORED.labels(reason="other_namespace").inc()
            continue

        tenant_slug, role_suffix = parts

        if not tenant_slug:
            # Delimiter at position 0 (e.g. "_REGISTRY_ADMIN"). The
            # entitlement is addressed to this service but has no tenant
            # — malformed at the source.
            _log.warning(
                "entitlement_parse_dropped reason=malformed raw=%r " "(delimiter at position 0; missing tenant slug)",
                raw,
            )
            _PARSE_DROPPED.labels(reason="malformed").inc()
            continue

        if role_suffix not in mapping:
            # Suffix is not in this deployment's role mapping. Could be a
            # role this service doesn't expose, or a typo upstream.
            _log.warning(
                "entitlement_parse_dropped reason=unknown_role raw=%r "
                "tenant_slug=%s role_suffix=%s "
                "(suffix not in entitlement_role_mapping)",
                raw,
                tenant_slug,
                role_suffix,
            )
            _PARSE_DROPPED.labels(reason="unknown_role").inc()
            continue

        parsed.append(ParsedEntitlement(tenant_slug=tenant_slug, role=mapping[role_suffix]))

    return parsed
