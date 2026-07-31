"""Attested context resolution.

ARC gives an agent a deterministic, attested answer to "what am I obliged to
know before I act, and can I prove I was told?" It owns governed context
artifacts with structured applicability, attested task-manifest intake,
deterministic context-bundle assembly under a budget, immutable receipts, and
authorized just-in-time detail retrieval.

This is a logical subsystem inside the Registry monolith, not a separate
service: it uses the same FastAPI app, the same Postgres schema and Alembic
chain, the same MCP server, and the same scheduler. What it does not share is
CAP's authorization model — ARC artifacts and receipts have their own audience
rules, so `ArcAuthorizationService` is the chokepoint for those while CAP
capability visibility still delegates to `VisibilityService`.

Tables live under the `arc_` prefix and are created by migration
`0023_arc_phase1`.
"""

from __future__ import annotations
