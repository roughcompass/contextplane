"""Closed vocabularies for ARC columns the database constrains.

Two columns land here. Both were declared as closed sets in the data model and
shipped as bare ``TEXT NOT NULL`` with only a length bound, so anything up to 64
characters was storable and neither column could be relied on for filtering,
alerting, or retention policy.

They are enumerated in one module for the same reason
:mod:`registry.audit.actions` is: a conformance test can then assert that the
constants here and the ``CHECK`` constraints in the database describe the same
set. Two lists that must agree and live apart will eventually disagree.

**Why a hard CHECK rather than the tenant vocabulary table.** ``vocabulary_values``
is how the product lets a tenant extend a controlled list, and it is the wrong
mechanism for both of these. Its ``tenant_id`` is ``NOT NULL``, so it cannot
express a term that means the same thing for a globally-scoped artifact revision
and a tenant-scoped one. And a tenant able to add its own content-classification
tier could define one weaker than the tiers the handling rules are written
against -- the escape hatch this vocabulary exists to close.
"""

from typing import Final

__all__ = [
    "CONTENT_CLASSIFICATIONS",
    "CONTENT_CLASSIFICATION_PUBLIC",
    "CONTENT_CLASSIFICATION_INTERNAL",
    "CONTENT_CLASSIFICATION_CONFIDENTIAL",
    "CONTENT_CLASSIFICATION_REGULATED",
    "RECEIPT_EVENT_TYPES",
    "RECEIPT_EVENT_CREATED",
    "RECEIPT_EVENT_JIT_RETRIEVAL",
    "RECEIPT_EVENT_JIT_DENIED",
]


# ---------------------------------------------------------------------------
# arc_revisions.content_classification
# ---------------------------------------------------------------------------
#
# A sensitivity scale, ordered from least to most restricted. Four members and
# no "unclassified": a registrant who cannot say how content should be handled
# has not finished deciding, and a bucket for that would be the value everything
# defaults into. The service layer may pre-fill `internal` on a registration path,
# but the column carries no DEFAULT -- a silent default is the same escape hatch
# arriving through the database instead.
#
# Deliberately not a data-category taxonomy. `pii`, `secret`, `source_body` and
# friends would turn this column into a content inventory, which is a different
# product and one this system explicitly is not.

CONTENT_CLASSIFICATION_PUBLIC: Final[str] = "public"
"""Approved for unrestricted distribution. No confidentiality obligation."""

CONTENT_CLASSIFICATION_INTERNAL: Final[str] = "internal"
"""Ordinary governed operational content. Standard controls suffice."""

CONTENT_CLASSIFICATION_CONFIDENTIAL: Final[str] = "confidential"
"""Sensitive but not legally controlled. Disclosure is limited to authorized paths."""

CONTENT_CLASSIFICATION_REGULATED: Final[str] = "regulated"
"""Legally controlled. Must never be stored with plaintext content."""

CONTENT_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        CONTENT_CLASSIFICATION_PUBLIC,
        CONTENT_CLASSIFICATION_INTERNAL,
        CONTENT_CLASSIFICATION_CONFIDENTIAL,
        CONTENT_CLASSIFICATION_REGULATED,
    }
)


# ---------------------------------------------------------------------------
# arc_receipt_events.event_type
# ---------------------------------------------------------------------------
#
# Exactly the three transitions the code performs today, ratified rather than
# invented: every insert into that table goes through one helper, and these are
# the only three literals any call site passes it.
#
# There is deliberately no member for an integrity failure. A failed append is
# rolled back precisely so it leaves no row, and the outcome is recorded on the
# receipt's own integrity state and in the audit log. A member here would name a
# value no code path could legitimately write.

RECEIPT_EVENT_CREATED: Final[str] = "receipt_created"
"""System-written at sequence 0, anchoring the receipt's hash chain."""

RECEIPT_EVENT_JIT_RETRIEVAL: Final[str] = "jit_retrieval"
"""Host-originated: a just-in-time detail request was granted and served."""

RECEIPT_EVENT_JIT_DENIED: Final[str] = "jit_denied"
"""Host-originated: a just-in-time detail request was refused, for any reason."""

RECEIPT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        RECEIPT_EVENT_CREATED,
        RECEIPT_EVENT_JIT_RETRIEVAL,
        RECEIPT_EVENT_JIT_DENIED,
    }
)
