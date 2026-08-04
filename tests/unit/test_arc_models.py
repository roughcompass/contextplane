"""Structural checks on the ARC ORM that need no database.

The ORM-versus-live-schema round trip is in
`tests/integration/test_arc_models_schema.py` — it needs a real Postgres and
cannot live in the unit bucket. What is checkable here is the shape of the
declarations themselves, and two of these assertions guard mistakes that would
be tenant-isolation bugs rather than style problems.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, inspect

from registry.arc.models import (
    ARC_MODELS,
    DEPLOYMENT_TENANT_ID,
    ArcApprovedException,
    ArcArtifact,
    ArcAuditOutbox,
    ArcDirective,
    ArcReceipt,
    ArcReceiptEvent,
    ArcReceiptSelectedDirective,
    ArcRevision,
)
from registry.storage.models import TenantMixin

# Global-capable: `tenant_id IS NULL` is the only global marker, so these must
# NOT carry TenantMixin's non-null insert assertion.
_GLOBAL_CAPABLE = {
    "arc_artifacts",
    "arc_revisions",
    "arc_directives",
    "arc_applicability_rules",
}

# Request-side: always a concrete requesting tenant, even when the receipt
# selected global artifacts.
_TENANT_SCOPED = {
    "arc_context_challenges",
    "arc_receipts",
    "arc_receipt_events",
    "arc_receipt_selected_revisions",
    "arc_receipt_selected_directives",
    "arc_audit_outbox",
}


def test_all_twenty_arc_tables_are_mapped() -> None:
    """The baseline migration creates 20 arc_ tables; every one has a mapped
    class. (ARC shipped with 21 originally; `arc_content_deletion_verifications`
    was excluded from the baseline and its model deleted — nothing in the
    codebase ever wrote a row to it.)"""
    assert len(ARC_MODELS) == 20
    names = {m.__tablename__ for m in ARC_MODELS}  # type: ignore[attr-defined]
    assert len(names) == 20, "duplicate __tablename__ among ARC models"
    assert all(n.startswith("arc_") for n in names)


def test_global_capable_tables_have_nullable_tenant_and_no_tenant_mixin() -> None:
    """A global artifact carries NULL tenant_id; TenantMixin would forbid it.

    If one of these ever gained TenantMixin, every global artifact insert would
    raise and global scope would silently become unusable.
    """
    for model in ARC_MODELS:
        if model.__tablename__ not in _GLOBAL_CAPABLE:  # type: ignore[attr-defined]
            continue
        assert not issubclass(model, TenantMixin), f"{model.__name__} is global-capable and must not use TenantMixin"
        column = inspect(model).columns["tenant_id"]
        assert column.nullable, f"{model.__name__}.tenant_id must be nullable for global scope"


def test_request_side_tables_are_tenant_scoped_and_not_nullable() -> None:
    """A receipt with a NULL tenant would escape every tenant-scoped read."""
    for model in ARC_MODELS:
        if model.__tablename__ not in _TENANT_SCOPED:  # type: ignore[attr-defined]
            continue
        assert issubclass(model, TenantMixin), f"{model.__name__} must use TenantMixin"
        column = inspect(model).columns["tenant_id"]
        assert not column.nullable, f"{model.__name__}.tenant_id must be NOT NULL"


def test_directive_projection_is_keyed_by_revision_and_identity() -> None:
    """One stable identity has at most one projection per revision."""
    pk = {c.name for c in inspect(ArcDirective).primary_key}
    assert pk == {"revision_id", "directive_id"}


def test_selected_directive_composite_fk_targets_the_projection() -> None:
    """JIT authorizes against an exact (revision, directive) projection.

    A single-column FK to the identity would let a receipt reference a directive
    from a different revision than the one it actually selected.
    """
    fks = inspect(ArcReceiptSelectedDirective).local_table.foreign_key_constraints
    composite = [fk for fk in fks if len(fk.columns) == 2]
    assert composite, "expected a composite FK on (revision_id, directive_id)"
    cols = {c.name for fk in composite for c in fk.columns}
    assert cols == {"revision_id", "directive_id"}


def test_exception_references_an_exact_higher_scope_projection() -> None:
    """Same reasoning as above: an exception excepts one projection, not a family."""
    fks = inspect(ArcApprovedException).local_table.foreign_key_constraints
    composite = [fk for fk in fks if len(fk.columns) == 2]
    assert composite, "expected a composite FK to arc_directives"


def test_receipt_consumes_exactly_one_challenge() -> None:
    """NOT NULL plus UNIQUE is half the challenge single-use invariant."""
    column = inspect(ArcReceipt).columns["challenge_id"]
    assert not column.nullable
    assert column.unique is True


def test_cyclic_references_carry_no_orm_foreign_key() -> None:
    """The revision/evidence cycle is deferrable in SQL only.

    Declaring these as ORM ForeignKeys would make SQLAlchemy try to order the
    inserts, which is exactly what DEFERRABLE INITIALLY DEFERRED exists to avoid.
    """
    assert not inspect(ArcRevision).columns["approval_evidence_id"].foreign_keys
    assert not inspect(ArcRevision).columns["superseded_by_revision_id"].foreign_keys


def test_receipt_event_sequence_is_a_plain_integer_with_no_positive_default() -> None:
    """Sequences are 0-indexed; nothing here may imply a 1-based start.

    The migration first enforced `sequence >= 1`, which rejected the creation
    event of every receipt. This asserts the ORM does not reintroduce that
    assumption through a default.
    """
    column = inspect(ArcReceiptEvent).columns["sequence"]
    assert isinstance(column.type, Integer)
    assert column.default is None
    assert column.server_default is None


def test_audit_outbox_carries_on_row_retry_state() -> None:
    """There is no dead-letter sidecar; failure state lives on the row."""
    columns = inspect(ArcAuditOutbox).columns
    for name in ("attempts", "last_error_code", "last_attempt_at"):
        assert name in columns, f"arc_audit_outbox is missing {name}"
    assert columns["attempts"].default is not None


def test_reserved_deployment_tenant_is_not_the_seed_default_tenant() -> None:
    """The all-zero UUID is the seed `default` tenant, not ARC's sentinel.

    Reusing it made the migration's insert a silent no-op and its downgrade
    delete a real tenant.
    """
    assert DEPLOYMENT_TENANT_ID != uuid.UUID("00000000-0000-0000-0000-000000000000")
    assert DEPLOYMENT_TENANT_ID == uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


def test_artifact_slug_is_not_globally_unique_on_its_own() -> None:
    """Uniqueness is per scope, via a COALESCE expression index in the migration.

    A plain unique constraint here would forbid two tenants using the same slug.
    """
    assert inspect(ArcArtifact).columns["slug"].unique is not True
