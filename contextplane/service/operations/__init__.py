"""The operations subdomain: operator-facing truth about how this deployment is doing.

One module today. ``health`` collects the conditions an operator would
otherwise have to read raw metrics or stand up a dashboard tool to see --
queue depths, curation backlog, proposal age -- and returns them as readings
the operator console renders.

A single-module package is a deliberate choice, not an accident of the move
that created it. The alternative was to leave ``health`` in a package whose
stated subject was "what actors do with a catalog that already exists",
which described half the codebase and so described nothing; a package with
one module and one real subject is smaller but honest, and it is the shape
that lets the next operator-facing concern land somewhere obvious instead of
in whichever grab-bag is nearest.

It sits in the service layer rather than below it because it reads the
memory subdomain to answer curation-backlog and proposal-age questions.

Nothing here is re-exported. Import the module you need directly, e.g.
``from contextplane.service.operations.health import collect_operational_health``.
"""

from __future__ import annotations
