"""Who may participate in a task, and what a checkpoint on it says.

Task memory is the first thing an agent reads when it resumes work somebody
else started, which makes both halves of this module authorization surfaces
rather than storage shapes.

**Participation is granted, never inferred.** There is no implicit membership
rule -- not "anyone in the tenant", not "whoever touched it last". Every
participant is named by a grant that records who granted it, when, under which
resolver, and until when. The alternative is a task whose readership nobody can
enumerate, which is the same as a task with no audience control at all.

**A checkpoint is what the server observed, not what a client claimed.** The
author, the time, the identity and the digest are all server-derived, and a
payload that supplies any of them is refused rather than overridden. Overriding
would be the more forgiving behaviour and the more dangerous one: the field
would still be wrong, just wrong invisibly, and a later reader has no way to
tell an attributed checkpoint from a forged one.

**The digest is checked, not trusted.** It travels with the checkpoint so a
reader can verify the content it names, and it is recomputed on construction --
a digest that does not match its own content is refused, because a checkpoint
that lies about its identity breaks the predecessor chain that resume depends
on.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from contextplane.context.schemas.trust import (
    ExternalReferenceV1,
    InvalidContextItem,
    _digest,
)

# What a participant may do. Ordered by breadth in prose only -- deliberately
# not comparable, for the same reason trust levels are not: a ">= contributor"
# check is how `auditor` quietly acquires write access the day somebody
# reorders the list.
ParticipantRole = Literal["reader", "contributor", "owner", "auditor"]

ROLE_READER: ParticipantRole = "reader"
ROLE_CONTRIBUTOR: ParticipantRole = "contributor"
ROLE_OWNER: ParticipantRole = "owner"
ROLE_AUDITOR: ParticipantRole = "auditor"

PARTICIPANT_ROLES: frozenset[str] = frozenset({ROLE_READER, ROLE_CONTRIBUTOR, ROLE_OWNER, ROLE_AUDITOR})

# Fields a client may never supply on a checkpoint. Each is derived from the
# authenticated request or from the content itself, and accepting a
# client-supplied value would let the caller choose its own attribution.
SERVER_DERIVED_FIELDS: frozenset[str] = frozenset(
    {"checkpoint_id", "sequence", "predecessor_id", "author", "recorded_at", "digest", "retention_policy"}
)

# Everything a client may supply. Closed: an unknown key is refused rather than
# ignored, because a silently-dropped field is indistinguishable from one the
# server understood and acted on.
CLIENT_FIELDS: frozenset[str] = frozenset(
    {"goal", "decisions", "assumptions", "evidence", "completed_checks", "open_questions", "next_action"}
)


@dataclasses.dataclass(frozen=True)
class IntentParticipantGrantV1:
    """One actor's participation in one task, and the evidence for it.

    `granted_by` is separate from `actor_id` and must differ. A self-grant is
    the spoof this shape exists to make impossible: an actor who can name
    themselves a participant has not been granted anything, they have asserted
    it, and the record would look identical to a real grant afterwards.
    """

    intent_id: uuid.UUID
    actor_id: str
    role: ParticipantRole
    # Who conferred it. An authority, not a relay -- the same distinction the
    # trust contract draws between `source` and `authority`.
    granted_by: str
    # Temporal evidence: when the grant was made, and when it stops applying.
    # `expires_at` absent means the grant lasts as long as the task does, which
    # is a decision somebody made rather than a gap in the record.
    granted_at: datetime.datetime
    expires_at: datetime.datetime | None
    # Which resolver decided this. Recorded because a grant made under one
    # resolution rule is not evidence under a later one, and without the
    # version a re-resolution cannot tell which grants it may keep.
    resolver_version: str

    def __post_init__(self) -> None:
        if self.role not in PARTICIPANT_ROLES:
            raise InvalidContextItem(
                f"unknown participant role {self.role!r}; legal values are {sorted(PARTICIPANT_ROLES)}"
            )
        for name, value in (
            ("actor_id", self.actor_id),
            ("granted_by", self.granted_by),
            ("resolver_version", self.resolver_version),
        ):
            if not value.strip():
                raise InvalidContextItem(f"participant grant needs a {name}")
        if self.actor_id.strip() == self.granted_by.strip():
            raise InvalidContextItem(
                "an actor cannot grant themselves participation; a self-grant is an assertion, not a grant, "
                "and it is indistinguishable from a real one once stored"
            )
        if self.granted_at.tzinfo is None:
            raise InvalidContextItem("granted_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise InvalidContextItem("expires_at must be timezone-aware")
            if self.expires_at <= self.granted_at:
                raise InvalidContextItem(
                    "a grant that expires at or before it was granted never applied; "
                    "storing it would show an audience the task never had"
                )

    def is_active_at(self, moment: datetime.datetime) -> bool:
        """Whether this grant confers anything at *moment*."""
        if moment.tzinfo is None:
            raise InvalidContextItem("cannot evaluate a grant against a naive timestamp")
        if moment < self.granted_at:
            return False
        return self.expires_at is None or moment < self.expires_at


def checkpoint_digest(
    *,
    checkpoint_id: uuid.UUID,
    intent_id: uuid.UUID,
    sequence: int,
    predecessor_id: uuid.UUID | None,
    goal: str,
    decisions: tuple[str, ...],
    assumptions: tuple[str, ...],
    evidence: tuple[ExternalReferenceV1, ...],
    completed_checks: tuple[str, ...],
    open_questions: tuple[str, ...],
    next_action: str | None,
    author: str,
    retention_policy: str,
) -> str:
    """The canonical digest of a checkpoint's content.

    A module-level function rather than a method alone because the builder needs
    it before an instance exists, and the instance verifies against it on
    construction -- computing it two ways would be two answers to whether two
    checkpoints are the same.

    Uses the one algorithm the context contract froze. Covers content and
    identity but not `recorded_at`: the same content recorded twice is the same
    checkpoint, and folding the clock in would make every retry look like new
    work.
    """
    return _digest(
        str(checkpoint_id),
        str(intent_id),
        str(sequence),
        "" if predecessor_id is None else str(predecessor_id),
        goal,
        *decisions,
        *assumptions,
        *(reference.collision_key() for reference in evidence),
        *completed_checks,
        *open_questions,
        next_action or "",
        author,
        retention_policy,
    )


@dataclasses.dataclass(frozen=True)
class IntentCheckpointV1:
    """One recorded step on a task, in the shape resume reads back.

    The structured fields are separate rather than one prose blob because
    resume treats them differently: an open question is work remaining, a
    completed check is work that need not repeat, and an assumption is
    something a later agent may need to invalidate. Flattened into prose, all
    three read as narrative and a resuming agent re-derives the distinction by
    guessing.
    """

    checkpoint_id: uuid.UUID
    intent_id: uuid.UUID
    # Position in this task's own sequence, not a global one. Starts at 1.
    sequence: int
    # The checkpoint this one continues. Absent only at sequence 1 -- a gap
    # anywhere else means the chain resume walks has a hole in it.
    predecessor_id: uuid.UUID | None
    goal: str
    decisions: tuple[str, ...]
    assumptions: tuple[str, ...]
    # Normalized: at most one reference per collision key. Two references to
    # one external thing are a duplicate, not corroboration.
    evidence: tuple[ExternalReferenceV1, ...]
    completed_checks: tuple[str, ...]
    open_questions: tuple[str, ...]
    # Absent when the task is finished, which is different from an empty
    # string -- one says "nothing left to do", the other says "nobody said".
    next_action: str | None
    # Server-derived from the authenticated request.
    author: str
    recorded_at: datetime.datetime
    # Which retention rule governs this row. Bound at write time so a later
    # policy change cannot retroactively decide how long this was kept.
    retention_policy: str
    # Carried so a reader can verify the content, recomputed so it cannot lie.
    digest: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise InvalidContextItem(f"checkpoint sequence starts at 1, got {self.sequence}")
        if self.sequence == 1 and self.predecessor_id is not None:
            raise InvalidContextItem("the first checkpoint on a task has no predecessor")
        if self.sequence > 1 and self.predecessor_id is None:
            raise InvalidContextItem(
                f"checkpoint at sequence {self.sequence} names no predecessor; resume walks the chain backwards "
                "and a missing link is indistinguishable from the start of the task"
            )
        for name, value in (("goal", self.goal), ("author", self.author), ("retention_policy", self.retention_policy)):
            if not value.strip():
                raise InvalidContextItem(f"checkpoint needs a {name}")
        if self.next_action is not None and not self.next_action.strip():
            raise InvalidContextItem(
                "next_action is absent or says something; an empty string reads as 'nothing left to do' "
                "to a renderer and as 'unset' to a reader"
            )
        if self.recorded_at.tzinfo is None:
            raise InvalidContextItem("recorded_at must be timezone-aware")

        seen: set[str] = set()
        for reference in self.evidence:
            key = reference.collision_key()
            if key in seen:
                raise InvalidContextItem(
                    "evidence references are normalized; the same external thing appears twice, which "
                    "reads as two independent sources supporting one claim"
                )
            seen.add(key)

        expected = self.compute_digest()
        if self.digest != expected:
            raise InvalidContextItem(
                "checkpoint digest does not match its content; a checkpoint that misnames itself breaks "
                "the predecessor chain resume depends on"
            )

    def compute_digest(self) -> str:
        """The canonical digest of this checkpoint's content."""
        return checkpoint_digest(
            checkpoint_id=self.checkpoint_id,
            intent_id=self.intent_id,
            sequence=self.sequence,
            predecessor_id=self.predecessor_id,
            goal=self.goal,
            decisions=self.decisions,
            assumptions=self.assumptions,
            evidence=self.evidence,
            completed_checks=self.completed_checks,
            open_questions=self.open_questions,
            next_action=self.next_action,
            author=self.author,
            retention_policy=self.retention_policy,
        )


def checkpoint_from_client_payload(
    payload: Mapping[str, Any],
    *,
    checkpoint_id: uuid.UUID,
    intent_id: uuid.UUID,
    sequence: int,
    predecessor_id: uuid.UUID | None,
    author: str,
    recorded_at: datetime.datetime,
    retention_policy: str,
    evidence: tuple[ExternalReferenceV1, ...] = (),
) -> IntentCheckpointV1:
    """Build a checkpoint from what a client sent plus what the server knows.

    The split is the point. Everything a client may supply is content; every
    field that decides attribution, ordering or identity comes from the server's
    own view of the request. A payload carrying one of those is refused, not
    overridden -- an overridden field is still wrong, and silently so.
    """
    unknown = set(payload) - CLIENT_FIELDS
    spoofed = unknown & SERVER_DERIVED_FIELDS
    if spoofed:
        raise InvalidContextItem(
            f"payload supplies server-derived field(s) {sorted(spoofed)}; author, ordering and identity come "
            "from the authenticated request, and accepting them would let a caller choose its own attribution"
        )
    if unknown:
        raise InvalidContextItem(
            f"unknown checkpoint field(s) {sorted(unknown)}; the shape is closed, and a dropped field is "
            "indistinguishable from one the server understood"
        )

    def _strings(name: str) -> tuple[str, ...]:
        raw = payload.get(name, ())
        if isinstance(raw, str) or not isinstance(raw, list | tuple):
            raise InvalidContextItem(
                f"{name} is a list of strings; a bare string would be read one character at a time"
            )
        for entry in raw:
            if not isinstance(entry, str):
                raise InvalidContextItem(f"{name} entries are strings, got {type(entry).__name__}")
        return tuple(raw)

    goal = payload.get("goal", "")
    if not isinstance(goal, str):
        raise InvalidContextItem(f"goal is a string, got {type(goal).__name__}")
    next_action = payload.get("next_action")
    if next_action is not None and not isinstance(next_action, str):
        raise InvalidContextItem(f"next_action is a string or absent, got {type(next_action).__name__}")

    fields: dict[str, Any] = {
        "checkpoint_id": checkpoint_id,
        "intent_id": intent_id,
        "sequence": sequence,
        "predecessor_id": predecessor_id,
        "goal": goal,
        "decisions": _strings("decisions"),
        "assumptions": _strings("assumptions"),
        "evidence": evidence,
        "completed_checks": _strings("completed_checks"),
        "open_questions": _strings("open_questions"),
        "next_action": next_action,
        "author": author,
        "recorded_at": recorded_at,
        "retention_policy": retention_policy,
    }
    # `recorded_at` is deliberately outside the digest -- see checkpoint_digest.
    digest = checkpoint_digest(**{k: v for k, v in fields.items() if k != "recorded_at"})
    return IntentCheckpointV1(**fields, digest=digest)


__all__ = [
    "CLIENT_FIELDS",
    "checkpoint_digest",
    "PARTICIPANT_ROLES",
    "ROLE_AUDITOR",
    "ROLE_CONTRIBUTOR",
    "ROLE_OWNER",
    "ROLE_READER",
    "SERVER_DERIVED_FIELDS",
    "ParticipantRole",
    "IntentCheckpointV1",
    "IntentParticipantGrantV1",
    "checkpoint_from_client_payload",
]
