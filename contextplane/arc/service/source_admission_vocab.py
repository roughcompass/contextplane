"""Wire vocabulary <-> persisted-profile vocabulary for source admission.

The wire enums (`AdmissionMethod`, `VerificationMethod`) are the closed
REST/MCP contract; `configured_connector` / `source_signed` etc. are the
exact literals `arc_source_approval_evidence_v1` fixes and
`authoring_profiles.py` validates. Translating at one boundary means neither
side needs to know the other's spelling.

This lives apart from `source_admission.py` because two services now admit
evidence under it: that module (fetched and uploaded bytes) and
`source_admission_graph.py` (a claim the canonical graph already promoted).
A second copy of these maps is how the two would eventually disagree about
what a stored `admission_method` means, and a stored evidence row outlives
whichever service wrote it.
"""

from __future__ import annotations

from typing import Final

#: Admitted through the graph rather than through bytes this service fetched.
#: Distinct from both existing methods on purpose: nothing was fetched and
#: nothing was uploaded, and recording either would put a false provenance
#: in an evidence chain whose only job is to be true.
GRAPH_PROMOTION: Final = "graph_promotion"

#: Vouched for by a promotion into the canonical graph, not by a signature.
#: The authority is the promotion journal row -- a second actor who is not
#: the claim's author moved it onto the graph -- so no signature or provider
#: assertion exists to record, and both stay NULL.
GRAPH_PROMOTED: Final = "graph_promoted"

ADMISSION_METHOD_TO_CANONICAL: Final[dict[str, str]] = {
    "connector_fetch": "configured_connector",
    "authorized_upload": "authorized_upload",
    GRAPH_PROMOTION: GRAPH_PROMOTION,
}
ADMISSION_METHOD_FROM_CANONICAL: Final[dict[str, str]] = {v: k for k, v in ADMISSION_METHOD_TO_CANONICAL.items()}

VERIFICATION_METHOD_TO_CANONICAL: Final[dict[str, str]] = {
    "detached_signature": "source_signed",
    "verifier_attestation": "verifier_attested",
    "graph_promotion": GRAPH_PROMOTED,
}
VERIFICATION_METHOD_FROM_CANONICAL: Final[dict[str, str]] = {v: k for k, v in VERIFICATION_METHOD_TO_CANONICAL.items()}

__all__ = [
    "ADMISSION_METHOD_FROM_CANONICAL",
    "ADMISSION_METHOD_TO_CANONICAL",
    "GRAPH_PROMOTED",
    "GRAPH_PROMOTION",
    "VERIFICATION_METHOD_FROM_CANONICAL",
    "VERIFICATION_METHOD_TO_CANONICAL",
]
