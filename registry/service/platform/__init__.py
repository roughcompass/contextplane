"""The platform subdomain: what a producer, consumer, or operator does with a catalog that already exists.

Everything here is built on top of the entity/edge graph rather than defining it —
each module answers a question about capabilities that already exist, not about
what a capability is.

``adoption`` records a consumer tenant's declared dependency on a provider's
capability and is the only writer of the ``provides_to`` edge that relationship
creates. ``subscriptions`` owns subscription lifecycle and the fan-out that turns
one capability mutation into a ``notifications`` row per active subscriber, plus
the ``auto_subscribe`` hook ``adoption`` calls from inside its own transaction so
adopting and subscribing commit together. ``notifications`` is the read side of
that fan-out: the in-catalog inbox a consumer polls instead of only receiving
webhooks. ``projections`` answers "what does my tenant ship" and "what does my
tenant consume" as RBAC-scoped, cross-tenant-visibility-filtered views over the
same graph. ``integration_lookup`` is a narrower, public read-only surface over
one denormalized index: which integrations connect two specific capabilities.
``operational_health`` is the operator console's source of truth for conditions
worth checking without reading raw metrics or standing up a dashboard tool.
``progression`` validates a capability's lifecycle-stage transitions against a
tenant-defined state machine, gates, and override consumption.

Nothing here is re-exported. Import the module you need directly, e.g.
``from registry.service.platform.adoption import AdoptionService`` or
``from registry.service.platform.progression import ProgressionService``.
"""

from __future__ import annotations
