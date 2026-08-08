"""The governance subdomain: the cross-cutting rules every other subdomain calls into.

Nothing here owns a primary data model of its own the way catalog owns entities or
memory owns claims. Each module is a policy or enforcement primitive that other
service code depends on rather than something a consumer asks for directly.

``visibility`` is the cross-tenant chokepoint: every query path that returns entity
data funnels through ``filter_entities()`` or ``assert_visible()`` here, and it is
the one place outside ``catalog/core.py`` and ``retrieval/`` permitted to issue
SELECT statements against ``entities`` or ``attributes``. ``temporal`` holds the
bi-temporal predicate builders — pure filter *fragments*, never a query of its own
— that both raw-SQL and SQLAlchemy call sites share so "valid as of this instant"
means the same thing everywhere it is asked. ``authority`` is the source-authority
ladder: an ordered, closed vocabulary ranking where a claim's value came from,
used by the claim pipeline to decide which of two disagreeing claims wins.
``erasure`` is the right-to-be-forgotten fan-out: subsystems that hold personal
data register themselves as participants, and one request walks the registered
list in order rather than a hardcoded path that silently misses whichever
subsystem was added after it was written.

Nothing here is re-exported. Import the module you need directly, e.g.
``from contextplane.service.governance.visibility import VisibilityService`` or
``from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK``.
"""

from __future__ import annotations
