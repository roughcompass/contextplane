"""The shared policy kernel: the cross-cutting rules the rest of the service layer calls into.

This is not a peer of the subject-owning subdomains it sits beside. Nothing here
owns a primary data model the way ``catalog`` owns entities or ``memory`` owns
claims; each module is a policy or enforcement primitive other code depends on
rather than something a consumer asks for directly. ``catalog``, ``memory``,
``retrieval``, ``workspace`` and ``notifications`` all import this package, and so
do the routers, the MCP tool surface, and the wiring that assembles them. The
traffic runs one way: the policy kernel imports none of them.

**What this package may import: storage and below.** In first-party terms that is
``contextplane.types``, ``contextplane.exceptions`` and
``contextplane.storage.models`` — ``visibility`` reads the ``Entity`` and
``Attribute`` models to answer a visibility question, so a rule saying "leaf
packages only" would be one this kernel breaks on its own first line. The floor is
machine-checked rather than merely stated: the module-boundary contract in
``pyproject.toml`` names ``contextplane.api``, ``contextplane.wiring`` and each of
the six sibling subdomains as forbidden imports from here, so a policy decision
that reaches sideways for the catalog or memory service it is supposed to be
independent of fails ``make lint`` instead of surfacing in a review weeks later.

``visibility`` is the cross-tenant chokepoint: ``filter_entities()`` and
``assert_visible()`` are how a caller narrows entity rows to the subset this tenant
may see. ``make test-hygiene`` holds the rest of the tree to it — a module that
reaches the ``entities`` table, whether through SQL text or the ``Entity`` model,
must import ``visibility``, carry a reasoned exemption in
``scripts/check_visibility_chokepoint.py`` naming the line that keeps its read
inside one tenant, or mark the individual line as a deliberate bypass.

``temporal`` holds the bi-temporal predicate builders — filter *fragments*, never a
query of their own, published in both a SQLAlchemy form and a raw-SQL form — so
"valid as of this instant" means the same thing at every call site that asks.

``authority`` is the source-authority ladder: an ordered, closed vocabulary of
seven values ranking where a claim's value came from, used by the claim pipeline to
decide which of two disagreeing claims wins. Exactly one ordering over those values
exists, and this is where it lives.

``erasure`` is the right-to-be-forgotten fan-out. Subsystems holding personal data
register themselves as participants, and one request walks them in registration
order — so a subsystem added later is reached by registering rather than by
somebody remembering to edit a hardcoded path. A participant that raises stops the
walk instead of being collected past: partial erasure reported as success is the
outcome worth failing loudly to avoid.

``wiring`` is this area's construction entry point, called by the composition root
the same way every other service area's is.

Nothing here is re-exported. Import the module you need directly, e.g.
``from contextplane.service.governance.visibility import VisibilityService`` or
``from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK``.
"""

from __future__ import annotations
