"""Parametrized SQL for the autonomy-envelope bindings table.

`autonomy_envelope.py` owns who may perform each act, what a suspension frees,
and why two envelopes cannot cover one window; this module owns the statements
that read and write the one table those decisions land in. Split when the
service reached the 800-line ceiling, along the seam `queries/source_admission.
py` already established for the same reason.

The comments here are load-bearing rather than descriptive. Three of these
statements encode a rule that is *not* in the schema — a flip requires the
interval to still be open, and only the CHECK constraints tie `state` to the
suspension columns — so a reader editing one of them without the comment would
remove a guard that looks like a redundant predicate.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text

INSERT = text(
    """
    INSERT INTO arc_autonomy_envelope_bindings (
        binding_id, tenant_id, revision_id, artifact_id, artifact_kind,
        principal_issuer, principal_subject, state,
        effective_from, effective_to, actor, reason, audit_reference, recorded_at
    )
    VALUES (
        :binding_id, :tenant_id, :revision_id, :artifact_id, 'policy',
        :issuer, :subject, 'active',
        :effective_from, :effective_to, :actor, :reason, :audit_reference, :recorded_at
    )
    """
)

#: The revision, its artifact, the artifact's kind and the revision's lifecycle
#: state in one read. `kind` and `lifecycle_state` are selected rather than
#: filtered on so a caller pointing at a `runbook` or a draft is told which it
#: is, instead of being told the revision does not exist.
LOAD_REVISION = text(
    """
    SELECT r.artifact_id, r.lifecycle_state, a.kind, a.tenant_id,
           v.risk_classification
    FROM arc_revisions AS r
    JOIN arc_artifacts AS a ON a.artifact_id = r.artifact_id
    LEFT JOIN arc_authoring_proposal_versions AS v ON v.revision_id = r.revision_id
    WHERE r.revision_id = :revision_id
    """
)

#: The hot read.
#:
#: **Active beats suspended, and only then does later beat earlier.** The
#: exclusion constraint forbids two `active` rows covering one instant, so there
#: is at most one to prefer -- but a suspended binding and the replacement
#: granted over the same window both cover `now`, which is exactly what the
#: widen path produces. Ordering by recency alone picks between them by
#: whichever happens to sort first, and a suspended envelope winning that tie
#: would leave a principal reading as suspended immediately after being
#: regranted. A suspended row is still returned when it is the only candidate,
#: because "suspended" and "never governed" are different answers.
RESOLVE = text(
    """
    SELECT b.binding_id, b.revision_id, b.artifact_id, b.principal_issuer, b.principal_subject,
           b.state, b.effective_from, b.effective_to, b.suspended_at, b.suspension_reason,
           r.lifecycle_state AS revision_lifecycle_state
    FROM arc_autonomy_envelope_bindings AS b
    JOIN arc_revisions AS r ON r.revision_id = b.revision_id
    WHERE b.tenant_id = :tenant_id
      AND b.principal_issuer = :issuer
      AND b.principal_subject = :subject
      AND b.effective_from <= :at
      AND (b.effective_to IS NULL OR b.effective_to > :at)
    ORDER BY (b.state = 'active') DESC, b.effective_from DESC, b.recorded_at DESC
    LIMIT 1
    """
)

#: The most bindings one page will return. An operator scanning who is governed
#: is scanning, not exporting; a caller wanting everything pages for it and the
#: cursor says so.
MAX_BINDING_PAGE: Final[int] = 200

#: Every binding this tenant holds, newest interval first.
#:
#: The cursor bounds are cast explicitly. asyncpg infers a parameter's type from
#: where it is used, and a bare `$2 IS NULL` gives it nothing to infer from --
#: the statement fails to prepare rather than returning a wrong answer, but only
#: on the path where a cursor is absent, which is every first page.
#:
#: Keyset on `(effective_from, binding_id)` rather than an offset: an operator
#: paging through during an incident is paging a list somebody else is writing
#: to, and an offset would silently skip a row when one is granted above them.
#:
#: Closed intervals are included. A revoked binding is not noise -- an operator
#: asking "was this agent ever governed" is asking about exactly those, and a
#: list that dropped them would answer "no" to a question whose real answer is
#: "yes, until Tuesday".
LIST = text(
    """
    SELECT b.binding_id, b.revision_id, b.artifact_id, b.principal_issuer, b.principal_subject,
           b.state, b.effective_from, b.effective_to, b.suspended_at, b.suspension_reason,
           r.lifecycle_state AS revision_lifecycle_state
    FROM arc_autonomy_envelope_bindings AS b
    JOIN arc_revisions AS r ON r.revision_id = b.revision_id
    WHERE b.tenant_id = :tenant_id
      AND (
        CAST(:cursor_from AS timestamptz) IS NULL
        OR (b.effective_from, b.binding_id)
             < (CAST(:cursor_from AS timestamptz), CAST(:cursor_id AS uuid))
      )
    ORDER BY b.effective_from DESC, b.binding_id DESC
    LIMIT :limit
    """
)

#: Both flips also require the interval to still be open. Without that, a
#: revoked binding -- interval closed, `state` still whatever it was -- could be
#: suspended and then reinstated, leaving a row reading `active` with
#: `effective_to` in the past and an audit event claiming authority was restored
#: when `resolve` returns nothing. The state and the interval are two halves of
#: one lifecycle, and only the CHECK constraints tie `state` to the suspension
#: columns; nothing in the schema ties it to the interval.
SUSPEND = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET state = 'suspended', suspended_at = :now, suspension_reason = :reason
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id AND state = 'active'
      AND (effective_to IS NULL OR effective_to > :now)
    """
)

REINSTATE = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET state = 'active', suspended_at = NULL, suspension_reason = NULL
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id AND state = 'suspended'
      AND (effective_to IS NULL OR effective_to > :now)
    """
)

#: `GREATEST` so a binding revoked before it ever took effect -- granted and
#: withdrawn in the same instant, or future-dated and cancelled first -- closes
#: to an empty interval rather than an inverted one. Empty is the true record:
#: it was in force for no time. Inverted is not a state the table should hold,
#: and the interval CHECK refuses it.
REVOKE = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET effective_to = GREATEST(:now, effective_from)
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id
      AND (effective_to IS NULL OR effective_to > :now)
    """
)
