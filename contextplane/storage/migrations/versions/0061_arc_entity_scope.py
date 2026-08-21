"""ARC authority scope: `capability` becomes `entity`, in place.

One table backs the whole catalog -- `Entity`, `__tablename__ = "entities"`, with
`entity_type` discriminating capability, concept and operation. There is no
`capabilities` table. The authority vocabulary said `capability` for the narrowest
scope above `intent` and for the selector arrays beneath it, which named one
entity type where it meant any entity.

Nothing enforced the narrower name, which is the evidence it was wrong:
submission parsed the selector as bare UUIDs and validated nothing about their
type, and selection matched them as opaque set membership. A rule scoped to a
concept or an operation was already expressible; the field name was the only
thing saying otherwise. The vocabulary was narrower than the mechanism, which is
the direction that misleads -- a reader concludes the matrix cannot scope to an
operation, and it can.

**Renamed in place, never recreated**, following `0049_arc_intent_nomenclature`,
which moved this same vocabulary from `task` to `intent` and is the template for
every step below. `ALTER TABLE ... RENAME COLUMN` keeps the rows, the GIN index,
the grants and the foreign keys exactly as they were, and a renamed column
carries its CHECK expressions with it.

**A closed-value check has to be dropped before the values it closes can move.**
Each is dropped, then the rows are updated, then the check is added back naming
the new value -- in that order, per constraint. Updating first would violate the
old check; adding first would violate on the old rows. That includes the three
risk-classification checks, because the reducer builds its result as
f"{scope}_{mandatory}", so renaming the scope starts producing `entity_mandatory`
for columns whose checks only admit `capability_mandatory`.

**The obligation snapshot is derived state, so it is rewritten and its digest
recomputed.** `applicability_snapshot` is a JSONB dedup key built by the service
from rule rows, and `applicability_digest` is a SHA-256 over its compact
sorted-key JSON. Leaving a key spelled `capability_ids` would split one
obligation into two the first time the service rebuilt it from renamed rules, and
the tombstone left by the first could never be cleared. The digest algorithm is
copied inline rather than imported from
`contextplane.arc.service.artifact_integrity`: a migration is a statement about
one moment in the schema's history and must keep producing the same bytes after
the service it was written beside has moved on.

**No alias, no sunset window.** The published-surface procedure exists for names
consumers already hold. This service has never been released, so there are no
signed manifests, no generated clients and no stored artifacts spelled the old
way -- which is also why the manifest side renames here rather than being frozen
against hosts that do not exist.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0061_arc_entity_scope"
down_revision: str | None = "0060_binding_extension_members"
branch_labels: str | None = None
depends_on: str | None = None

_OLD_KIND = "capability"
_NEW_KIND = "entity"

#: The three columns whose values the reducer builds as f"{scope}_{mandatory}".
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
        f"'{narrowest}_mandatory', '{narrowest}_non_mandatory', "
        "'intent_mandatory', 'intent_non_mandatory'"
    )


def _applicability_digest(snapshot: dict[str, Any]) -> str:
    """SHA-256 over the compact sorted-key JSON, copied inline on purpose."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _assert_absent(connection: sa.Connection, what: str, query: str) -> None:
    """Refuse before mutating anything.

    A row already holding the destination spelling means someone has moved part
    of this vocabulary by hand, and continuing would merge two populations that
    a reader would afterwards be unable to tell apart.
    """
    count = connection.execute(sa.text(query)).scalar_one()
    if count:
        msg = f"{what} already holds {count} row(s) in the destination vocabulary; refusing to merge two populations"
        raise RuntimeError(msg)


def _rewrite_obligation_snapshots(connection: sa.Connection, *, old_scope: str, new_scope: str) -> None:
    rows = connection.execute(
        sa.text("SELECT obligation_id, applicability_snapshot FROM arc_mandatory_obligations")
    ).all()
    for obligation_id, snapshot in rows:
        payload: dict[str, Any] = dict(snapshot if isinstance(snapshot, dict) else json.loads(snapshot))
        if "capability_ids" in payload:
            payload["entity_ids"] = payload.pop("capability_ids")
        elif "entity_ids" in payload:
            payload["capability_ids"] = payload.pop("entity_ids")
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


def _move(connection: sa.Connection, *, old_kind: str, new_kind: str) -> None:
    """One direction of the cutover. Both directions are the same steps with the
    two vocabularies exchanged, so they cannot drift apart."""

    # --- Refuse before mutating anything, in every table this touches. ---
    for table, column in (
        ("arc_applicability_rules", "scope"),
        ("arc_approved_exceptions", "lower_scope_kind"),
        ("arc_approval_evidence", "scope_kind"),
    ):
        _assert_absent(
            connection,
            f"{table}.{column}",
            f"SELECT count(*) FROM {table} WHERE {column} = '{new_kind}'",  # noqa: S608 - both interpolated values are module constants
        )
    for table, _constraint, column in _RISK_CLASSIFICATION_CHECKS:
        _assert_absent(
            connection,
            f"{table}.{column}",
            f"SELECT count(*) FROM {table} WHERE {column} LIKE '{new_kind}\\_%'",  # noqa: S608 - both interpolated values are module constants
        )

    # --- arc_applicability_rules: two selector columns, its GIN index, scope. ---
    old_ids, new_ids = (f"{old_kind}_ids", f"{new_kind}_ids")
    old_labels, new_labels = (f"{old_kind}_labels", f"{new_kind}_labels")
    op.execute(f"ALTER TABLE arc_applicability_rules RENAME COLUMN {old_ids} TO {new_ids}")
    op.execute(f"ALTER TABLE arc_applicability_rules RENAME COLUMN {old_labels} TO {new_labels}")
    op.execute(f"ALTER INDEX ix_arc_rules_{old_ids} RENAME TO ix_arc_rules_{new_ids}")
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_scope")
    op.execute(f"ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_{old_kind}_scope_target")
    op.execute(f"UPDATE arc_applicability_rules SET scope = '{new_kind}' WHERE scope = '{old_kind}'")  # noqa: S608 - both interpolated values are module constants
    op.execute(
        "ALTER TABLE arc_applicability_rules ADD CONSTRAINT ck_arc_rules_scope "
        f"CHECK (scope IN ('global', 'tenant', 'domain', '{new_kind}', 'intent'))"
    )
    op.execute(
        f"ALTER TABLE arc_applicability_rules ADD CONSTRAINT ck_arc_rules_{new_kind}_scope_target CHECK ("
        f"scope <> '{new_kind}'"
        f" OR ({new_ids} IS NOT NULL AND array_length({new_ids}, 1) >= 1)"
        f" OR ({new_labels} IS NOT NULL AND array_length({new_labels}, 1) >= 1)"
        ")"
    )

    # --- arc_approved_exceptions: its selector column and both scope checks. ---
    op.execute(
        f"ALTER TABLE arc_approved_exceptions RENAME COLUMN lower_scope_{old_kind}_id " f"TO lower_scope_{new_kind}_id"
    )
    op.execute("ALTER TABLE arc_approved_exceptions DROP CONSTRAINT ck_arc_exceptions_lower_scope_kind")
    op.execute("ALTER TABLE arc_approved_exceptions DROP CONSTRAINT ck_arc_exceptions_scope_selectors")
    op.execute(
        f"UPDATE arc_approved_exceptions SET lower_scope_kind = '{new_kind}' WHERE lower_scope_kind = '{old_kind}'"  # noqa: S608 - both interpolated values are module constants
    )
    op.execute(
        "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT ck_arc_exceptions_lower_scope_kind "
        f"CHECK (lower_scope_kind IN ('tenant', 'domain', '{new_kind}', 'intent'))"
    )
    op.execute(
        "ALTER TABLE arc_approved_exceptions ADD CONSTRAINT ck_arc_exceptions_scope_selectors CHECK ("
        "(lower_scope_kind = 'tenant'"
        f" AND lower_scope_domain_id IS NULL AND lower_scope_{new_kind}_id IS NULL"
        " AND lower_scope_intent_kind IS NULL AND lower_scope_action_class IS NULL)"
        " OR (lower_scope_kind = 'domain'"
        f" AND lower_scope_domain_id IS NOT NULL AND lower_scope_{new_kind}_id IS NULL"
        " AND lower_scope_intent_kind IS NULL AND lower_scope_action_class IS NULL)"
        f" OR (lower_scope_kind = '{new_kind}'"
        f" AND lower_scope_{new_kind}_id IS NOT NULL AND lower_scope_domain_id IS NULL"
        " AND lower_scope_intent_kind IS NULL AND lower_scope_action_class IS NULL)"
        " OR (lower_scope_kind = 'intent'"
        " AND lower_scope_intent_kind IS NOT NULL AND lower_scope_action_class IS NOT NULL)"
        ")"
    )

    # --- arc_approval_evidence: the same narrowest-but-one scope value. ---
    op.execute("ALTER TABLE arc_approval_evidence DROP CONSTRAINT ck_arc_evidence_scope_kind")
    op.execute(f"UPDATE arc_approval_evidence SET scope_kind = '{new_kind}' WHERE scope_kind = '{old_kind}'")  # noqa: S608 - both interpolated values are module constants
    op.execute(
        "ALTER TABLE arc_approval_evidence ADD CONSTRAINT ck_arc_evidence_scope_kind "
        f"CHECK (scope_kind IN ('global', 'tenant', 'domain', '{new_kind}', 'intent'))"
    )

    # --- The three risk-classification vocabularies the reducer now produces. ---
    for table, constraint, column in _RISK_CLASSIFICATION_CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        for suffix in ("mandatory", "non_mandatory"):
            op.execute(f"UPDATE {table} SET {column} = '{new_kind}_{suffix}' WHERE {column} = '{old_kind}_{suffix}'")  # noqa: S608 - both interpolated values are module constants
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({column} IN ({_risk_values(new_kind)}))")

    # --- Derived obligation snapshots, and only then their digests. ---
    _rewrite_obligation_snapshots(connection, old_scope=old_kind, new_scope=new_kind)


def upgrade() -> None:
    """Move the authority vocabulary from `capability` to `entity`."""
    _move(op.get_bind(), old_kind=_OLD_KIND, new_kind=_NEW_KIND)


def downgrade() -> None:
    """The same steps with the two vocabularies exchanged."""
    _move(op.get_bind(), old_kind=_NEW_KIND, new_kind=_OLD_KIND)
