"""The test Clock: a configurable instant that only moves when told to.

Lives in tests/helpers rather than the production package deliberately — a
test double shipping inside the wheel invites production code to grow a
dependency on it, and the type checker would never object. Production code
depends on the `Clock` protocol in `registry.types`; this is one
implementation of it that tests own.
"""

from __future__ import annotations

import datetime


class FakeClock:
    """Test Clock. Returns a configurable instant; tick() advances it."""

    def __init__(self, start: datetime.datetime) -> None:
        self._t = start.astimezone(datetime.UTC)

    def now(self) -> datetime.datetime:
        return self._t

    def set(self, t: datetime.datetime) -> None:
        self._t = t.astimezone(datetime.UTC)

    def tick(self, delta: datetime.timedelta) -> None:
        self._t = self._t + delta


__all__ = ["FakeClock"]
