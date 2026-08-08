"""ARC service layer.

Every ARC operation flows through one of these services. There are no direct ORM
writes outside them, and `tests/conformance/test_arc_no_orm_bypass.py` is what
keeps that true.
"""

from __future__ import annotations
