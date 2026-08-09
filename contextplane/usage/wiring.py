"""Usage-area service construction: the one buffered usage writer per process.

One registration entry point per area, called by the composition root. The
root owns the writer's *lifecycle* — `contextplane.main`'s lifespan starts its
drain task once there is a running event loop and stops it with a final flush
so a rolling deploy does not discard events it already accepted — but the
construction itself belongs beside the subsystem it constructs.

One writer, not one per caller. Two would each hold their own buffer and each
report their own queue depth, so the gauge would describe neither.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.usage.writer import UsageWriter


@dataclass(frozen=True)
class UsageServices:
    """What this area contributes to the typed container, by container field name."""

    usage_writer: UsageWriter


def build_usage_services(session_factory: async_sessionmaker[AsyncSession]) -> UsageServices:
    """Construct the usage area's services."""
    return UsageServices(usage_writer=UsageWriter(session_factory))
