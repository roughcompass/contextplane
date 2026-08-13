"""ARC selector vocabulary, renamed in place: two columns, six closed-value checks, one derived snapshot.

The code above this migration already calls the thing an artifact applies to an
*intent*. This revision moves the relational layer to the same vocabulary in one
transaction, so there is no interval in which a query written against
`intent_kinds` reads a database that still holds `task_kinds`.

**Renamed in place, never recreated.** `ALTER TABLE ... RENAME COLUMN` keeps the
rows, the GIN index, the grants and the foreign keys exactly as they were, and a
renamed column carries its CHECK expressions with it. Copying into new tables
would give every rule and exception a new physical identity for what is a
spelling correction, and `arc_mandatory_obligations` dedups on a digest whose
inputs those rows are.

**A closed-value check has to be dropped before the values it closes can move.**
Six of them name `'task'` or `'task_mandatory'`. Each is dropped, then the rows
are updated, then the check is added back naming the new value -- in that order,
per constraint. Updating first would violate the old check; adding first would
violate on the old rows. The three risk-classification checks are the ones the
plan's own list does not name: the reducer builds its result as
f"{scope}_{mandatory}", so renaming the scope silently started producing
`intent_mandatory` for a column whose check only admits `task_mandatory`. That
is a write the database rejects, not a cosmetic mismatch, and it is the reason
this revision touches three tables the ARC selector rename otherwise would not.

**Why the file is numbered 0049 when it follows 0053.** The number is a label
reserved for this change; `down_revision` is the chain. `0049` was set aside
while `0048` was the head, and `0050`-`0053` have landed on `0048` since. Binding
this revision to `0048` as originally planned would put two revisions on one
parent and produce two heads -- the exact failure `0050`'s own docstring records
happening between itself and `0048`, invisible on either branch alone. The parent
below was resolved by walking `down_revision` from the root, which is the only
thing that orders these.

**The obligation snapshot is derived state, so it is rewritten and its digest
recomputed.** `applicability_snapshot` is a JSONB dedup key built by the service
from rule rows, and `applicability_digest` is a SHA-256 over its compact
sorted-key JSON. It is not signed evidence and nothing external verifies it, so
leaving it spelled `task_kinds` would split one obligation into two the first
time the service rebuilt it from renamed rules -- and the tombstone left by the
first could never be cleared. The digest algorithm is copied inline below rather
than imported from `contextplane.arc.service.artifact_integrity`: a migration is
a statement about one moment in the schema's history and must keep producing the
same bytes after the service it was written beside has moved on.

**What this revision deliberately does not touch.**
`arc_authoring_proposal_versions.semantics`, signed envelopes, canonical
payloads, receipt events and every recorded digest over them are left exactly as
written. Those carry a profile literal that says which field spelling they used,
and they are verified under it. `task_summary_template` is a value inside such a
payload -- an artifact-semantics profile kind, not a relational
`arc_artifacts.kind` -- so no row here spells it and none is rewritten.

**The downgrade is the exact inverse, and it refuses rather than guesses.** Both
directions assert first that no row already carries the spelling they are about
to introduce. If one does, the mapping is not one-to-one and reversal would be
ambiguous, so the migration stops before any mutation instead of merging two
distinct values into one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0049_arc_intent_nomenclature"
# The chain head, not the numeric predecessor. `0049` was reserved while `0048`
# was the head; `0050`-`0053` landed on `0048` in the meantime, so binding to
# `0048` here would fork the graph into two heads.
down_revision: str | None = "0053_ownership_and_grants"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# The seven intent kinds, unchanged in value by this rename -- only the columns
# and checks that carry them are respelled.
_KINDS = (
    "'read_only', 'code_change', 'dependency_change', 'configuration_change', "
    "'security_sensitive_change', 'data_access', 'deployment'"
)

# The column is not spelled the same in all three: the sticky record calls it
# `classification`, the two observation tables call it `risk_classification`.
# Carried per row rather than assumed, because assuming it is what a rename
# gets wrong quietly.
_RISK_CLASSIFICATION_CHECKS = (
    ("arc_risk_classifications", "ck_arc_risk_classifications_classification", "classification"),
    ("arc_observation_cohorts", "ck_arc_observation_cohorts_classification", "risk_classification"),
    ("arc_observation_qualifications", "ck_arc_observation_qualifications_classification", "risk_classification"),
)


def _risk_values(narrowest: str) -> str:
    return (
        "'global_mandatory', 'global_non_mandatory', "
        "'tenant_mandatory', 'tenant_non_mandatory', "
        "'domain_mandatory', 'domain_non_mandatory', "
        "'capability_mandatory', 'capability_non_mandatory', "
        f"'{narrowest}_mandatory', '{narrowest}_non_mandatory'"
    )


def _applicability_digest(snapshot: dict[str, Any]) -> str:
    """The obligation dedup key: compact sorted-key JSON, then SHA-256.

    A deliberate copy of the service's algorithm rather than an import. The
    service is free to change how it builds a snapshot; the bytes this
    revision wrote must stay reproducible from this file alone.
    """
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_absent(connection: sa.Connection, description: str, statement: str) -> None:
    """Refuse the whole run if the destination spelling already exists.

    This is what makes the rewrite one-to-one and therefore exactly
    reversible: every source value maps to a destination value nothing else
    already occupies, so the inverse is a function rather than a guess.
    """
    count = connection.execute(sa.text(statement)).scalar_one()
    if count:
        msg = (
            f"{description}: {count} row(s) already carry the destination spelling, so this rename is not "
            "one-to-one and could not be reversed unambiguously. Nothing has been modified."
        )
        raise RuntimeError(msg)


def _rewrite_obligation_snapshots(
    connection: sa.Connection, *, old_key: str, new_key: str, old_scope: str, new_scope: str
) -> None:
    """Respell every obligation snapshot and recompute its digest.

    Read-modify-write in Python rather than a JSONB expression in SQL,
    because the digest is defined over Python's compact separators and
    `jsonb::text` renders `{"a": 1}` with a space that would change every
    digest while looking identical in a diff.
    """
    rows = connection.execute(
        sa.text("SELECT obligation_id, applicability_snapshot FROM arc_mandatory_obligations")
    ).all()
    for obligation_id, snapshot in rows:
        payload = dict(snapshot)
        if old_key in payload:
            payload[new_key] = payload.pop(old_key)
        if payload.get("scope") == old_scope:
            payload["scope"] = new_scope
        connection.execute(
            sa.text(
                "UPDATE arc_mandatory_obligations "
                "SET applicability_snapshot = CAST(:snapshot AS JSONB), applicability_digest = :digest "
                "WHERE obligation_id = :obligation_id"
            ),
            {
                "snapshot": json.dumps(payload),
                "digest": _applicability_digest(payload),
                "obligation_id": obligation_id,
            },
        )


def _move(
    connection: sa.Connection,
    *,
    old_kind: str,
    new_kind: str,
    old_selector: str,
    new_selector: str,
    old_risk: str,
    new_risk: str,
) -> None:
    """One direction of the cutover. Both directions are the same steps with
    the two vocabularies exchanged, so they cannot drift apart."""

    # --- Refuse before mutating anything, in every table this touches. ---
    _assert_absent(
        connection,
        "arc_applicability_rules.scope",
        f"SELECT count(*) FROM arc_applicability_rules WHERE scope = '{new_kind}'",  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    )
    _assert_absent(
        connection,
        "arc_approved_exceptions.lower_scope_kind",
        f"SELECT count(*) FROM arc_approved_exceptions WHERE lower_scope_kind = '{new_kind}'",  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    )
    _assert_absent(
        connection,
        "arc_approval_evidence.scope_kind",
        f"SELECT count(*) FROM arc_approval_evidence WHERE scope_kind = '{new_kind}'",  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    )
    for table, _constraint, column in _RISK_CLASSIFICATION_CHECKS:
        _assert_absent(
            connection,
            f"{table}.{column}",
            f"SELECT count(*) FROM {table} WHERE {column} LIKE '{new_risk}\\_%'",  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
        )
    _assert_absent(
        connection,
        "arc_mandatory_obligations.applicability_snapshot",
        "SELECT count(*) FROM arc_mandatory_obligations " f"WHERE applicability_snapshot ? '{new_selector}'",  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    )

    # --- arc_applicability_rules: column, GIN index, closed kind set, scope. ---
    op.execute(f"ALTER TABLE arc_applicability_rules RENAME COLUMN {old_selector} TO {new_selector}")
    op.execute(f"ALTER INDEX ix_arc_rules_{old_selector} RENAME TO ix_arc_rules_{new_selector}")
    op.execute(
        f"ALTER TABLE arc_applicability_rules RENAME CONSTRAINT ck_arc_rules_{old_selector} "
        f"TO ck_arc_rules_{new_selector}"
    )
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_scope")
    op.execute(f"UPDATE arc_applicability_rules SET scope = '{new_kind}' WHERE scope = '{old_kind}'")  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    op.execute(
        "ALTER TABLE arc_applicability_rules ADD CONSTRAINT ck_arc_rules_scope "
        f"CHECK (scope IN ('global', 'tenant', 'domain', 'capability', '{new_kind}'))"
    )

    # --- arc_approved_exceptions: column, its closed kind set, both scope checks. ---
    op.execute(
        f"ALTER TABLE arc_approved_exceptions RENAME COLUMN lower_scope_{old_kind}_kind "
        f"TO lower_scope_{new_kind}_kind"
    )
    op.execute(
        f"ALTER TABLE arc_approved_exceptions RENAME CONSTRAINT ck_arc_exceptions_{old_kind}_kind "
        f"TO ck_arc_exceptions_{new_kind}_kind"
    )
    op.execute("ALTER TABLE arc_approved_exceptions DROP CONSTRAINT ck_arc_exceptions_lower_scope_kind")
    op.execute("ALTER TABLE arc_approved_exceptions DROP CONSTRAINT ck_arc_exceptions_scope_selectors")
    op.execute(
        f"UPDATE arc_approved_exceptions SET lower_scope_kind = '{new_kind}' WHERE lower_scope_kind = '{old_kind}'"  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    )
    op.execute(
        "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT ck_arc_exceptions_lower_scope_kind "
        f"CHECK (lower_scope_kind IN ('tenant', 'domain', 'capability', '{new_kind}'))"
    )
    op.execute(
        "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT ck_arc_exceptions_scope_selectors CHECK ("
        "(lower_scope_kind = 'tenant'"
        " AND lower_scope_domain_id IS NULL AND lower_scope_capability_id IS NULL"
        f" AND lower_scope_{new_kind}_kind IS NULL AND lower_scope_action_class IS NULL)"
        " OR (lower_scope_kind = 'domain'"
        " AND lower_scope_domain_id IS NOT NULL AND lower_scope_capability_id IS NULL"
        f" AND lower_scope_{new_kind}_kind IS NULL AND lower_scope_action_class IS NULL)"
        " OR (lower_scope_kind = 'capability'"
        " AND lower_scope_capability_id IS NOT NULL AND lower_scope_domain_id IS NULL"
        f" AND lower_scope_{new_kind}_kind IS NULL AND lower_scope_action_class IS NULL)"
        f" OR (lower_scope_kind = '{new_kind}'"
        f" AND lower_scope_{new_kind}_kind IS NOT NULL AND lower_scope_action_class IS NOT NULL)"
        ")"
    )

    # --- arc_approval_evidence: the same narrowest scope value. ---
    op.execute("ALTER TABLE arc_approval_evidence DROP CONSTRAINT ck_arc_evidence_scope_kind")
    op.execute(f"UPDATE arc_approval_evidence SET scope_kind = '{new_kind}' WHERE scope_kind = '{old_kind}'")  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
    op.execute(
        "ALTER TABLE arc_approval_evidence ADD CONSTRAINT ck_arc_evidence_scope_kind "
        f"CHECK (scope_kind IN ('global', 'tenant', 'domain', 'capability', '{new_kind}'))"
    )

    # --- The three risk-classification vocabularies the reducer now produces. ---
    for table, constraint, column in _RISK_CLASSIFICATION_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        for suffix in ("mandatory", "non_mandatory"):
            op.execute(f"UPDATE {table} SET {column} = '{new_risk}_{suffix}' WHERE {column} = '{old_risk}_{suffix}'")  # noqa: S608 - every interpolated value is a module constant or one of the two literal vocabularies upgrade/downgrade pass in; a migration has no request or caller to inject through
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} " f"CHECK ({column} IN ({_risk_values(new_risk)}))"
        )

    # --- Derived obligation snapshots, and only then their digests. ---
    _rewrite_obligation_snapshots(
        connection,
        old_key=old_selector,
        new_key=new_selector,
        old_scope=old_kind,
        new_scope=new_kind,
    )


def upgrade() -> None:
    _move(
        op.get_bind(),
        old_kind="task",
        new_kind="intent",
        old_selector="task_kinds",
        new_selector="intent_kinds",
        old_risk="task",
        new_risk="intent",
    )


def downgrade() -> None:
    _move(
        op.get_bind(),
        old_kind="intent",
        new_kind="task",
        old_selector="intent_kinds",
        new_selector="task_kinds",
        old_risk="intent",
        new_risk="task",
    )
