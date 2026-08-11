"""Task memory's derivative, and task memory's participation in an erasure.

Two halves of one obligation. A checkpoint holds what an agent decided, in the
agent's own words; the head summary is a second copy of some of those words,
projected into a row the checkpoint's own table knows nothing about. Erasing the
checkpoint and stopping there leaves the summary readable, which is the failure
the whole derivative registry exists to prevent.

**The checkpoint is minimized, never deleted.** The chain is append-only and every
successor names its predecessor, so a deleted checkpoint is a hole in the history
resume walks. The approved disposition is minimize-and-tombstone: clear the body,
keep identity, position, provenance and the digest, and record a tombstone that
proves the erasure without holding any part of what was erased. Migration
`0045_checkpoint_erasure_exception` is what makes that write possible at all --
before it, the immutability trigger refused every UPDATE, erasure included.

**A minimized checkpoint stops reading back as content, and that is the point.**
The stored digest is preserved deliberately, so it no longer matches the blanked
body: rehydrating one through `TaskCheckpointV1` therefore refuses it rather than
serving a checkpoint whose content silently changed. The structural facts a
verifier is entitled to -- that the record existed, its sequence, its predecessor,
its recorded instant, its digest -- are all still on the row and all still
queryable.

**The summary is redacted in place, never deleted, whichever operation asks.**
`delete` would mean the `task_heads` row, and that row carries the chain's head
pointer and sequence -- structure the erasure has no mandate to destroy and the
verifier's post-erasure integrity check depends on. So both `delete` and `redact`
converge on replacing the prose with a content-free marker, which satisfies the
policy (no erased words survive) inside the projection's own invariant (the head
keeps pointing at the chain). `rebuild` is refused loudly instead: a head summary
is prose an agent wrote or a caller set, not a computation over the chain, so
there is nothing to recompute it from and a handler that "rebuilt" it by blanking
it would be a deletion reported as a refresh.

**One registration per task head, not one per summary version.** The head is a
single mutable row whose locator never changes, so re-registering it on every
append upserts the same row and re-links the current head checkpoint as its
source. The alternative -- a registration per checkpoint the summary was ever
derived from -- would accumulate one dead registration per append pointing at a
locator that no longer holds that checkpoint's words.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import RegistryError
from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import Clock, SystemClock, TenantContext

_log = logging.getLogger(__name__)

#: The goal a minimized checkpoint carries. The immutability trigger admits this
#: exact literal and no other, so the two spellings are one fact stored twice on
#: purpose: the database enforces the shape and this module produces it, and an
#: integration test drives the real UPDATE so a drift is a red test rather than a
#: silently refused erasure.
ERASED_CHECKPOINT_GOAL = f"{tombstones.ERASED_KEY_PREFIX}checkpoint"

#: What a redacted head summary says. Recognisable through
#: `tombstones.is_erased_key`, which is what makes a second redaction a no-op
#: instead of a second write of the same value.
ERASED_SUMMARY = f"{tombstones.ERASED_KEY_PREFIX}summary"

#: How a summary derivative is addressed. The head is one row per task, so the
#: task identifies it; the scheme prefix is carried so a handler handed some other
#: subsystem's locator refuses it rather than parsing a UUID out of it by luck.
SUMMARY_LOCATOR_SCHEME = "task_heads://"

#: The audience a head summary was built for: the task's own participants. A
#: rebuild for a different audience would be a different derivative, and task
#: membership is exactly the boundary the checkpoint reads enforce.
SUMMARY_AUDIENCE_PREFIX = "task:"

#: Head summaries carry task content, which is tenant-internal by default. Stated
#: as a constant because the registration writes it into a closed column set and a
#: value invented per call site is how two registrations of one artefact end up
#: classified differently.
SUMMARY_CLASSIFICATION = "internal"

#: Bumped when what this handler does to a summary changes, so a derivative built
#: by an older one can be identified rather than assumed rebuildable.
SUMMARY_HANDLER_VERSION = "task-head-summary/1"

#: The instant an event-bounded derivative is registered with.
#:
#: A checkpoint's retention is bounded by tenant or workspace deletion rather than
#: by a duration, so it contributes no expiry to the minimum and a summary built
#: from one has no clock of its own either. `derivative_registrations.expires_at`
#: is NOT NULL by design -- an unbounded derivative is the case that outlives its
#: sources silently -- so the event bound has to be written as *some* instant.
#:
#: This is that instant, and it is deliberately unreachable: the bounding event is
#: tenant deletion, whose grace period purges every content class long before any
#: clock-driven sweep would select this row. Naming it says "no clock applies"
#: where a plausible-looking date would say "expires then" and be wrong. The
#: registry's own rule keeps it honest in the one direction that matters: a
#: re-registration takes the *earlier* of stored and incoming, so the moment this
#: derivative reads a source that does have a duration, that duration wins.
EVENT_BOUNDED_HORIZON = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)


class UnknownDerivativeLocator(RegistryError):
    """Raised when a handler is handed a locator it cannot address.

    Loud rather than treated as "nothing to do": a locator this handler does not
    recognise names an artefact somebody registered and nobody can now reach, and
    reporting zero artefacts touched would mark the propagation done while the
    content stays where it is.
    """


class SummaryCannotBeRebuilt(RegistryError):
    """Raised when propagation asks for a head summary to be rebuilt.

    A summary is prose -- the appending agent's next action, or a caller's own
    words through the head-summary write path. Neither is recomputable from the
    chain, so there is no rebuild to perform, and the two ways to fake one are
    both wrong: blanking it is a deletion reported as a refresh, and leaving it is
    a refresh reported as done. Nothing enqueues a rebuild for this kind today;
    this refusal is what makes that stay true visibly.
    """


def summary_locator(task_id: uuid.UUID) -> str:
    """Where this task's head summary lives, in the addressing the registry stores."""
    return f"{SUMMARY_LOCATOR_SCHEME}{task_id}/summary"


def summary_audience(task_id: uuid.UUID) -> str:
    """The audience partition a head summary is built for: the task's participants."""
    return f"{SUMMARY_AUDIENCE_PREFIX}{task_id}"


def _task_from_locator(locator: str) -> uuid.UUID:
    """The task a summary locator addresses, or a refusal naming what arrived."""
    if locator.startswith(SUMMARY_LOCATOR_SCHEME) and locator.endswith("/summary"):
        body = locator[len(SUMMARY_LOCATOR_SCHEME) : -len("/summary")]
        try:
            return uuid.UUID(body)
        except ValueError:
            pass
    msg = f"{locator!r} does not address a task head summary; this handler can reach nothing else"
    raise UnknownDerivativeLocator(msg)


class SummaryDerivativeHandler:
    """Removes the erased person's words from a task head summary.

    Registered for the `summary` kind. What it touches is one column of one row:
    the projection stays, its prose goes.
    """

    kind = derivatives.KIND_SUMMARY
    version = SUMMARY_HANDLER_VERSION

    async def apply(
        self,
        session: AsyncSession,
        registration: derivatives.Registration,
        operation: str,
    ) -> int:
        """Redact this task's head summary and report whether it still held prose.

        Zero is a successful answer: the summary was already redacted, which is
        what a retry of a partly-applied propagation looks like.
        """
        if operation == derivatives.OPERATION_REBUILD:
            msg = (
                f"a head summary cannot be rebuilt (derivative {registration.derivative_id}): "
                "it is prose an agent or a caller wrote, not a projection of the chain"
            )
            raise SummaryCannotBeRebuilt(msg)

        task_id = _task_from_locator(registration.storage_locator)
        result = await session.execute(
            text(
                "UPDATE task_heads SET summary = :erased "
                "WHERE tenant_id = :tenant AND task_id = :task AND summary <> :erased"
            ),
            {"erased": ERASED_SUMMARY, "tenant": registration.tenant_id, "task": task_id},
        )
        # The head's `updated_at` is left where it was on purpose. It records when
        # the task last moved, and a redaction is not progress on the task; moving
        # it would make an erasure read as activity to everything that sorts on it.
        return int(cast("CursorResult[Any]", result).rowcount)


#: Every authored checkpoint this actor still has content in. `goal` is the
#: cheapest of the body columns to test and the one the trigger pins to a single
#: literal, so "already minimized" is one comparison rather than seven.
_AUTHORED_CHECKPOINTS = """
SELECT checkpoint_id, digest
  FROM task_checkpoints
 WHERE tenant_id = :tenant AND author = :actor AND goal <> :erased
 ORDER BY task_id, sequence
"""

#: The one UPDATE the immutability trigger admits. Every column it does not name
#: is a column the trigger requires to be unchanged.
_MINIMIZE_CHECKPOINT = """
UPDATE task_checkpoints
   SET goal = :erased,
       decisions = '[]'::jsonb,
       assumptions = '[]'::jsonb,
       evidence = '[]'::jsonb,
       completed_checks = '[]'::jsonb,
       open_questions = '[]'::jsonb,
       next_action = NULL
 WHERE tenant_id = :tenant AND checkpoint_id = :cid
"""

#: One tombstone per erased checkpoint, holding no part of the body. `DO NOTHING`
#: rather than an upsert: erasing twice is not two erasures, and the first
#: tombstone's instant is the one the proof was minted against.
_TOMBSTONE_CHECKPOINT = """
INSERT INTO source_tombstones
    (tombstone_id, tenant_id, record_class, subject_id, policy_version,
     request_authority, reason, effective_at, proof_hmac, propagation_state)
VALUES (:id, :tenant, :cls, :subject, :policy, :authority, :reason, :now, :proof, 'pending')
ON CONFLICT (tenant_id, record_class, subject_id) DO NOTHING
"""


class CheckpointErasure:
    """The checkpoint chain's participation in erasing one actor.

    Minimizes the bodies this actor authored and tombstones each one. It does not
    enqueue derivative propagation: the context subsystem's participant already
    walks `task_checkpoint` sources for that, and a second enqueue under a second
    tombstone would schedule every summary redaction twice -- the outbox is unique
    per cause, and two tombstones are two causes.
    """

    subsystem = "task_checkpoints"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        salts: tombstones.TenantSaltResolver,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._salts = salts
        # `Clock` states the rule: service code takes one and never calls
        # `datetime.now()`. This is an erasure participant, and the moment it
        # stamps is the moment a drain and an overdue read both compare against
        # -- so a caller working at a fixed instant could write work that no
        # query at that instant could see. Defaulted so the composition root
        # keeps constructing this unchanged.
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Minimize every checkpoint this actor authored, and tombstone each one."""
        now = self._clock.now()
        # Before anything is written. With no key material there is no proof to
        # mint, and a minimization committed without its tombstone would be an
        # erasure nobody can account for -- indistinguishable from data loss.
        salt = self._salts.salt_for(ctx.tenant_id)

        minimized = 0
        tombstoned = 0
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(_AUTHORED_CHECKPOINTS),
                        {"tenant": ctx.tenant_id, "actor": str(target_actor_id), "erased": ERASED_CHECKPOINT_GOAL},
                    )
                )
                .mappings()
                .all()
            )

            for row in rows:
                checkpoint_id = uuid.UUID(str(row["checkpoint_id"]))
                await session.execute(
                    text(_MINIMIZE_CHECKPOINT),
                    {"erased": ERASED_CHECKPOINT_GOAL, "tenant": ctx.tenant_id, "cid": checkpoint_id},
                )
                minimized += 1

                written = await session.execute(
                    text(_TOMBSTONE_CHECKPOINT),
                    {
                        "id": uuid.uuid4(),
                        "tenant": ctx.tenant_id,
                        "cls": policies.RECORD_TASK_CHECKPOINT,
                        "subject": checkpoint_id,
                        "policy": policies.POLICY_VERSION,
                        "authority": str(ctx.actor_id),
                        "reason": derivatives.TRIGGER_ERASURE,
                        "now": now,
                        # The digest the row already held: the proof commits to
                        # what was erased without the tombstone carrying it.
                        "proof": tombstones.mint_proof(
                            salt,
                            record_class=policies.RECORD_TASK_CHECKPOINT,
                            subject_id=checkpoint_id,
                            content_digest=str(row["digest"]),
                            effective_at=now,
                        ),
                    },
                )
                tombstoned += int(cast("CursorResult[Any]", written).rowcount)

            # One commit: a minimized body and the tombstone accounting for it
            # land together or not at all.
            await session.commit()

        _log.info(
            "task_checkpoints.erasure_applied: actor=%s minimized=%d tombstoned=%d",
            target_actor_id,
            minimized,
            tombstoned,
        )
        return {"checkpoints": minimized, "tombstones": tombstoned}


__all__ = [
    "ERASED_CHECKPOINT_GOAL",
    "ERASED_SUMMARY",
    "EVENT_BOUNDED_HORIZON",
    "SUMMARY_AUDIENCE_PREFIX",
    "SUMMARY_CLASSIFICATION",
    "SUMMARY_HANDLER_VERSION",
    "SUMMARY_LOCATOR_SCHEME",
    "CheckpointErasure",
    "SummaryCannotBeRebuilt",
    "SummaryDerivativeHandler",
    "UnknownDerivativeLocator",
    "summary_audience",
    "summary_locator",
]
