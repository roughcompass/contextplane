"""The notifications subdomain: subscription lifecycle and the in-catalog inbox it fills.

Two modules, one subject. ``subscriptions`` owns subscription lifecycle and
the fan-out that turns one capability mutation into a ``notifications`` row
per active subscriber, plus the ``auto_subscribe`` hook the catalog's
``adoption`` module calls from inside its own transaction so adopting and
subscribing commit together. ``core`` is the read side of that same fan-out:
the inbox a consumer polls instead of only receiving webhooks.

``core`` is named for its subject rather than repeated as ``notifications``,
so the module does not stutter against the package it lives in -- the same
reason ``catalog.core`` is not ``catalog.catalog``.

These two were filed under a "platform" package whose docstring described
"what actors do with a catalog that already exists", which fits half the
codebase and therefore named nothing. Fan-out and its inbox do share a
subject, which is why they are a domain rather than two loose modules: a
subscription exists to produce notifications, and a notification is only
ever produced by a subscription.

Nothing here is re-exported. Import the module you need directly, e.g.
``from contextplane.service.notifications.core import NotificationService``.
"""

from __future__ import annotations
