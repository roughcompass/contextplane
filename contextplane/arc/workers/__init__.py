"""ARC background workers.

Everything in `contextplane.arc.service` reacts to a caller; everything here
reacts to a clock. A revision does not know its own review date has passed
until something reads `now()` and compares, an unconsumed challenge does not
know it will never be presented again, and an outbox row does not move
itself into `audit_log` -- each needs a process that runs on a schedule
rather than in response to a request.

That makes this package ARC's second write surface, alongside the service
layer. Every worker here still funnels through the same discipline the
service layer enforces on itself: a `session_factory` and a `Clock` passed
in rather than a global connection or `datetime.now()`, so a test can run a
worker against a fake clock and assert on exactly which rows it touched.
"""

from __future__ import annotations
