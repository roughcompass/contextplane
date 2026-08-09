"""Notifications-area service construction: capability-event fan-out and the inbox.

One registration entry point per area, called by the composition root — see
`contextplane.service.governance.wiring` for the shape and why the root
threads cross-area collaborators in as explicit parameters.

Built before the catalog area, because `AdoptionService` closes over
`SubscriptionService.adoption_hook()`: adopting a capability transparently
creates an inbox-only subscription, and it must be created through the one
subscription service every other write path uses.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.notifications.core import NotificationService
from contextplane.service.notifications.subscriptions import SubscriptionService
from contextplane.types import Clock


@dataclass(frozen=True)
class NotificationServices:
    """What this area contributes to the typed container, by container field name."""

    subscriptions: SubscriptionService
    notifications: NotificationService


def build_notification_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    visibility: VisibilityService,
) -> NotificationServices:
    """Construct the notifications area's services."""
    return NotificationServices(
        subscriptions=SubscriptionService(
            session_factory=session_factory,
            clock=clock,
            visibility=visibility,
        ),
        notifications=NotificationService(
            session_factory=session_factory,
            clock=clock,
        ),
    )
