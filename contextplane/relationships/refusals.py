"""What a governed relationship write may be refused for, and the exception that says so.

Its own module because two layers need it and neither should import the other.
`service.py` decides whether a write may proceed; `rows.py` writes the rows and,
in the one case where a write has to read before it writes, refuses. Leaving the
vocabulary in `service.py` would make `rows` import it and `service` import
`rows`, which is a cycle for the sake of nine string constants.

Codes rather than message text, so an API layer can map a refusal without
matching on prose. Prose changes; these do not.
"""

from __future__ import annotations

from typing import Final

#: The audit action a governed assertion records.
RELATIONSHIP_ASSERTED: Final = "relationship.asserted"

#: The audit action a supersession records. Distinct from an assertion because a
#: reader reconstructing what happened to an edge needs to tell the row that
#: started a history from the row that ended one.
RELATIONSHIP_SUPERSEDED: Final = "relationship.superseded"

#: What a caller may not do, spelled as codes so an API layer can map them without
#: matching on message text.
UNKNOWN_TYPE: Final = "unknown_relationship_type"
NO_ACTIVE_BINDING: Final = "no_active_binding"
ENDPOINT_MISSING: Final = "endpoint_missing"
ENDPOINT_TYPE_MISMATCH: Final = "endpoint_type_mismatch"
CROSS_ORG_DENIED: Final = "cross_org_denied"
DUPLICATE_REFUSED: Final = "duplicate_refused"
MAXIMUM_EXCEEDED: Final = "maximum_cardinality_exceeded"
UNDECLARED_PROPERTY: Final = "undeclared_property"
INVERSE_NOT_WRITABLE: Final = "inverse_not_writable"
SYMMETRY_ENDPOINTS_DIFFER: Final = "symmetry_requires_identical_endpoints"
SUPERSEDED_NOT_IN_FORCE: Final = "superseded_relationship_not_in_force"
SUPERSEDED_IDENTITY_DIFFERS: Final = "superseded_relationship_identity_differs"


class RelationshipWriteRefused(Exception):
    """A governed assertion was refused, with the code saying which rule refused it."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


__all__ = [
    "CROSS_ORG_DENIED",
    "DUPLICATE_REFUSED",
    "ENDPOINT_MISSING",
    "ENDPOINT_TYPE_MISMATCH",
    "INVERSE_NOT_WRITABLE",
    "MAXIMUM_EXCEEDED",
    "NO_ACTIVE_BINDING",
    "RELATIONSHIP_ASSERTED",
    "RELATIONSHIP_SUPERSEDED",
    "SUPERSEDED_IDENTITY_DIFFERS",
    "SUPERSEDED_NOT_IN_FORCE",
    "SYMMETRY_ENDPOINTS_DIFFER",
    "UNDECLARED_PROPERTY",
    "UNKNOWN_TYPE",
    "RelationshipWriteRefused",
]
