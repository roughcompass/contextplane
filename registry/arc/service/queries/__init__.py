"""Raw-SQL query helpers for the authoring surface's service modules.

A sibling package of `registry/arc/service/`, not a separate write surface:
`tests/conformance/test_arc_no_orm_bypass.py` permits a mutating statement
anywhere under `registry/arc/service/`, and a module nested one directory
deeper is still under it. The split exists for readability, not for a new
permission boundary — a service module that grew past a comfortable size
moves its parametrized-SQL row loads and writes here and keeps its own
validation, orchestration, and transaction boundaries in place.
"""

from __future__ import annotations
