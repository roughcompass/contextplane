"""Unit tests for the Tenant ORM model's external-identity columns.

Covers:
- ORM model field construction with explicit external_tenant_id + provider.
- ORM model defaults: provider defaults to 'manual', external_tenant_id is None.
- ORM __table__ metadata exposes both columns.

These tests run without a live database.

The DDL-shape assertions that used to live here (loading migration
0015_add_tenant_external_id_and_provider.py and inspecting the SQL its
upgrade()/downgrade() emitted) were removed when the migration chain was
squashed into one baseline revision — `tenants.provider` and
`tenants.external_tenant_id` are now columns in that revision's single
CREATE TABLE, not a later ADD COLUMN step, so there is no per-migration DDL
shape left to assert. The schema properties those tests protected (the
provider CHECK values, the partial unique index, external_tenant_id's
nullability) are exercised by the tables this ORM model maps to and by the
integration tests that create tenants through JIT provisioning.
"""

from __future__ import annotations

import uuid


def test_tenant_explicit_external_id_and_provider() -> None:
    """Tenant accepts external_tenant_id and provider when supplied."""
    import datetime

    from contextplane.storage.models import Tenant

    t = Tenant(
        tenant_id=uuid.uuid4(),
        slug="seal-tenant",
        display_name="SEAL Tenant",
        created_at=datetime.datetime.now(tz=datetime.UTC),
        external_tenant_id="SEAL-112025",
        provider="jit",
    )
    assert t.external_tenant_id == "SEAL-112025"
    assert t.provider == "jit"


def test_tenant_defaults_provider_manual_and_external_id_none() -> None:
    """Tenant constructed without optional fields has None external_tenant_id.

    The provider default ('manual') is an INSERT-time default that fires during
    session flush, not at Python constructor time — so provider is None here.
    The ORM column's default value is verified via __table__ metadata instead.
    """
    import datetime

    from contextplane.storage.models import Tenant

    t = Tenant(
        tenant_id=uuid.uuid4(),
        slug="manual-tenant",
        display_name="Manual Tenant",
        created_at=datetime.datetime.now(tz=datetime.UTC),
    )
    assert t.external_tenant_id is None
    # Verify the INSERT-time default is registered on the column descriptor.
    provider_col = Tenant.__table__.c["provider"]  # type: ignore[attr-defined]
    assert provider_col.default is not None
    assert provider_col.default.arg == "manual"


def test_tenant_provider_column_exists_in_table_metadata() -> None:
    """The Tenant __table__ metadata exposes 'provider' and 'external_tenant_id' columns."""
    from contextplane.storage.models import Tenant

    col_names = {c.name for c in Tenant.__table__.c}  # type: ignore[attr-defined]
    assert "provider" in col_names
    assert "external_tenant_id" in col_names
