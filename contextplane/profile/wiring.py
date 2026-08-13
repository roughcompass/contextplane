"""Profile-area service construction: publication and tenant bindings.

One registration entry point per area, called by the composition root. Both
services are built here rather than independently by their callers so that the
publication path and the binding path share one session factory and one clock —
a binding whose `recorded_at` came from a different clock than the revision it
names would make the two histories impossible to interleave when reading them
back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from contextplane.profile.bindings import BindingService
from contextplane.profile.service import ProfileService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock


@dataclass(frozen=True)
class ProfileArea:
    """What the profile area exposes to the composition root.

    Field names match `Services` exactly, because the root expands this container
    rather than enumerating it. `profile_bindings` is therefore not a redundant
    prefix: the container has to distinguish the immutable publication path from
    the state machine that binds tenants to it, and an area field named `bindings`
    would be a `TypeError` about an unexpected keyword at startup.
    """

    profiles: ProfileService
    profile_bindings: BindingService


def build_profile_services(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> ProfileArea:
    """Build the publication and binding services as one area."""
    return ProfileArea(
        profiles=ProfileService(session_factory, clock),
        profile_bindings=BindingService(session_factory, clock),
    )


__all__ = ["ProfileArea", "build_profile_services"]
