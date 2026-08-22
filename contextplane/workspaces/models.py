"""ORM rows for task memory.

Mirrors `0030_task_memory` exactly. The constraints live in the database rather
than here on purpose — a self-grant or a rewritten checkpoint must be refused for
every writer, including a psql session and a future service nobody has written
yet, and a check that only exists in Python is a check the next writer skips
without noticing.

What these classes add is names and types for the columns, so a reader is not
reconstructing the shape from raw SQL. Where a rule is enforced below, it is
enforced *again*, not instead.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base

_PARTICIPANT_ROLES = ("reader", "contributor", "owner", "auditor")


class IntentParticipantGrant(Base):
    """One actor's participation in one task, with the evidence for it."""

    __tablename__ = "intent_participant_grants"
    __table_args__ = (
        CheckConstraint(
            "role IN ('reader', 'contributor', 'owner', 'auditor')",
            name="ck_grant_role",
        ),
        # Repeated from the migration deliberately: this is the rule that makes
        # a grant a grant rather than a claim about oneself.
        CheckConstraint("actor_id <> granted_by", name="ck_grant_not_self"),
        # Load-bearing for the temporal exclusion, not just tidiness: a
        # zero-width window would be an *empty* tstzrange, and an empty range
        # overlaps nothing, so a degenerate row would slip past the overlap
        # check entirely.
        CheckConstraint("expires_at IS NULL OR expires_at > granted_at", name="ck_grant_window"),
        # There is deliberately no unique constraint on (tenant, intent, actor).
        # Participation may legitimately be granted, revoked and granted again;
        # what may not exist is two grants in force at once, which is when "what
        # role does this actor have?" acquires two answers. That is enforced by
        # `ex_intent_participant_grants_no_overlap`, a gist exclusion over
        # `tstzrange(granted_at, expires_at)` installed by migration 0070 -- and
        # it is what keeps `fetch_actor_role`'s `scalar_one_or_none()` correct
        # now that several rows may exist for one actor.
        #
        # It lives only in the migration because SQLAlchemy has no
        # exclusion-constraint construct, which is the same place
        # `relationship_metadata` keeps its equivalent.
    )

    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Absent means the grant lasts as long as the task does -- a decision
    # somebody made, not a gap in the record.
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolver_version: Mapped[str] = mapped_column(Text, nullable=False)


class IntentCheckpoint(Base):
    """One recorded step on a task. Append-only; the database enforces it."""

    __tablename__ = "intent_checkpoints"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_checkpoint_sequence_positive"),
        CheckConstraint(
            "(sequence = 1 AND predecessor_id IS NULL) OR (sequence > 1 AND predecessor_id IS NOT NULL)",
            name="ck_checkpoint_predecessor",
        ),
        UniqueConstraint("tenant_id", "intent_id", "sequence", name="uq_task_checkpoint_sequence"),
        Index("ix_task_checkpoint_task", "tenant_id", "intent_id", text("sequence DESC")),
    )

    checkpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intent_checkpoints.checkpoint_id"), nullable=True
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # Kept as separate lists rather than one prose blob because resume treats
    # them differently: an open question is work remaining, a completed check is
    # work that need not repeat.
    decisions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    assumptions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    completed_checks: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    open_questions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Absent when the task is finished, which differs from an empty string: one
    # says nothing is left to do, the other says nobody said.
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Bound at write time so a later policy change cannot retroactively decide
    # how long this row was kept.
    retention_policy: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)


class IntentHead(Base):
    """The current position on a task. A projection, meant to be overwritten.

    Carries no history of its own: the checkpoint chain is the history, and a
    second copy here would be a second answer to what happened.
    """

    __tablename__ = "intent_heads"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "intent_id"),
        CheckConstraint("head_sequence >= 1", name="ck_head_sequence_positive"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    head_checkpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intent_checkpoints.checkpoint_id"), nullable=False
    )
    head_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["IntentCheckpoint", "IntentHead", "IntentParticipantGrant", "_PARTICIPANT_ROLES"]
