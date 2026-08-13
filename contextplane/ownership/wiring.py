"""Ownership-area service construction.

One registration entry point beside the code it builds, like every other area.
The area exposes a single service; it is still built here rather than inline at
the composition root so that adding a second ownership service later is an edit
to this file and not to the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from contextplane.ownership.service import OwnershipService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock


@dataclass(frozen=True)
class OwnershipArea:
    """What the ownership area exposes to the composition root.

    Field names match `Services` exactly, because the root expands this container
    rather than enumerating it.
    """

    ownership: OwnershipService


def build_ownership_services(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> OwnershipArea:
    """Build the ownership area."""
    return OwnershipArea(ownership=OwnershipService(session_factory=session_factory, clock=clock))


__all__ = ["OwnershipArea", "build_ownership_services"]
