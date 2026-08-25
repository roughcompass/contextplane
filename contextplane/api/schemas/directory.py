"""Wire shapes for the three listings E23-T1 adds.

Projections, not translations. Which rows each listing may contain is settled in
the services — participation for intents, credential memberships for tenants,
what the detail read would serve for receipts — because the MCP surface asks the
same questions and an authorization argument made in one adapter is one the other
will eventually make differently.

**Nothing here carries a request body or a query digest.** A receipt summary says
when a resolution happened, what state it reached and how much it served; what it
*asked* is on the detail read, behind the servability check. A list that carried
the query would answer most of what a withheld receipt is withholding, from the
one surface that was allowed to say the receipt exists.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from contextplane.context.models_receipt import ContextReceipt
from contextplane.service.governance.tenants import ReachableTenant
from contextplane.workspaces.directory import IntentSummary


class IntentSummaryResponse(BaseModel):
    """One task this caller participates in."""

    intent_id: uuid.UUID
    goal: str | None = Field(
        default=None,
        description=(
            "The latest checkpoint's goal, or absent when the task has none yet — a grant is "
            "written before the first checkpoint, so that is a real state rather than a gap."
        ),
    )
    role: str = Field(description="This caller's role on the task. What they may do, not what exists.")
    checkpoint_count: int
    latest_checkpoint_at: datetime.datetime | None = None
    granted_at: datetime.datetime
    expires_at: datetime.datetime | None = None

    @classmethod
    def of(cls, entry: IntentSummary) -> IntentSummaryResponse:
        """Project one task summary onto the wire."""
        return cls(
            checkpoint_count=entry.checkpoint_count,
            expires_at=entry.expires_at,
            goal=entry.goal,
            granted_at=entry.granted_at,
            intent_id=entry.intent_id,
            latest_checkpoint_at=entry.latest_checkpoint_at,
            role=entry.role,
        )


class IntentListResponse(BaseModel):
    """Tasks this caller participates in."""

    items: list[IntentSummaryResponse]


class TenantSummaryResponse(BaseModel):
    """One tenant this credential reaches."""

    tenant_id: uuid.UUID
    tenant_slug: str
    display_name: str | None = Field(
        default=None,
        description=(
            "Absent when this deployment has not materialised the tenant row yet. A credential may "
            "name a tenant that has never been seen here, and dropping it would report no access "
            "to a tenant the caller does have."
        ),
    )
    roles: list[str]
    is_provisioned: bool
    is_current: bool

    @classmethod
    def of(cls, entry: ReachableTenant) -> TenantSummaryResponse:
        """Project one reachable tenant onto the wire."""
        return cls(
            display_name=entry.display_name,
            is_current=entry.is_current,
            is_provisioned=entry.is_provisioned,
            roles=list(entry.roles),
            tenant_id=entry.tenant_id,
            tenant_slug=entry.tenant_slug,
        )


class TenantListResponse(BaseModel):
    """The tenants this credential reaches, the current one first."""

    items: list[TenantSummaryResponse]


class ReceiptSummaryResponse(BaseModel):
    """One resolution, enough to choose it by and no more."""

    receipt_id: uuid.UUID
    intent_id: uuid.UUID | None = None
    state: str = Field(description="One of complete, degraded, blocked.")
    item_count: int
    exclusion_count: int = Field(
        description=(
            "How much this resolution withheld. Zero means nothing was withheld, which is a claim "
            "this listing may make because a receipt whose exclusions are not yet recorded is not "
            "in it at all."
        )
    )
    resolved_at: datetime.datetime
    requested_by: str

    @classmethod
    def of(cls, receipt: ContextReceipt) -> ReceiptSummaryResponse:
        """Project one receipt header onto the wire."""
        return cls(
            exclusion_count=receipt.exclusion_count,
            intent_id=receipt.intent_id,
            item_count=receipt.item_count,
            receipt_id=receipt.receipt_id,
            requested_by=receipt.requested_by,
            resolved_at=receipt.resolved_at,
            state=receipt.state,
        )


class ReceiptDirectoryResponse(BaseModel):
    """Recent resolutions this caller may open.

    **Not `ReceiptListResponse`, and the rename is a correction rather than a
    preference.** `api/schemas/receipts.py` already publishes that name for a
    different shape — `{receipts: [...]}` from `GET /v1/receipts/by-reference`,
    against this one's `{items, next_before}`. Two classes under one name make
    FastAPI qualify *both* by module path, so both appeared in the contract as
    `contextplane__api__schemas__…__ReceiptListResponse` and neither had the
    plain name a client would reference. The newer of the two takes the longer
    name, because a collision renames whichever was published first.

    `scripts/check_contract_schema_names.py` is the gate that found this; it was
    written for a collision E24 introduced and caught this one on its first run.
    """

    items: list[ReceiptSummaryResponse]
    next_before: datetime.datetime | None = Field(
        default=None,
        description=(
            "Send as `before` for the next page. Keyset rather than an offset, so a receipt written "
            "between two pages cannot shift the window and hide one. Absent when the page was short, "
            "which is how a caller knows it reached the end."
        ),
    )


__all__ = [
    "IntentListResponse",
    "IntentSummaryResponse",
    "ReceiptDirectoryResponse",
    "ReceiptSummaryResponse",
    "TenantListResponse",
    "TenantSummaryResponse",
]
