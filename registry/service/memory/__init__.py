"""The memory subdomain: claims from observation to curated truth, plus the session event log.

Two things live here, related but distinct.

The claim substrate is the staging ground between something a session or a
connector asserted and something the canonical graph treats as true. Claims
are created, ontology-checked, scored, contested, consolidated, confirmed, and
eventually promoted — or they are rejected, corrected, or erased along the
way. Every module in this package that is not the session log is a stage in
that pipeline, or a service that reads the pipeline's output (serving claims
as claims, listing what needs a curator, reporting on how promotion targets
and calibration are behaving).

`session_events` is the other thing: an agent's own append-only conversation
log, scoped by actor rather than by claim lifecycle. It has no ontology, no
confidence score, and no promotion path — a session event is not raw material
for a claim until extraction reads it and decides otherwise. It sits in this
package because it is the substrate the claim pipeline is fed from, not
because it shares claims' invariants.

Nothing here is re-exported. Import the module you need directly, e.g.
``from registry.service.memory.claim_writer import ClaimService`` or
``from registry.service.memory.session_events import MemoryService``.
"""

from __future__ import annotations
