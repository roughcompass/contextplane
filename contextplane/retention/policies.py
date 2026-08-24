"""The approved disposition of every record class, as data the code can read.

Twelve classes, one row each: what legal basis holds the record, how long it is
kept, what erasure does to it, and what a verifier may say afterwards. The table
below is the single in-code statement of that policy — the same values the
`retention_policies` rows carry — so a handler asks the policy rather than
carrying its own copy of a number somebody wrote down twice.

**Versioned, not edited.** Correcting a period is a new `POLICY_VERSION` plus
re-propagation, never an in-place change: every tombstone and every registered
derivative names the version it was decided under, and a value that moved
underneath them would make those references unreadable. That is why the version
is a module constant compared by equality rather than a "latest" lookup.

**Two clocks, not one.** Three classes reduce content before the record itself
goes: a signal's payload minimizes long before its envelope is deleted, feedback
free text goes before its structured discriminants, and a claim excerpt is bound
to the payload clock of the source it quotes rather than to the claim's own life.
`payload_retention_days` is that earlier clock. Collapsing the two into one field
would force a choice between keeping personal content for the longer period or
destroying structure needed for the shorter one, and both are wrong.

**"Life of tenant" is not "forever".** A NULL period is event-bounded: the event
is tenant deletion, which starts `TENANT_GRACE_DAYS` and then purges every
content class. Storing a very large number instead would make "bounded by an
event" and "bounded by a long duration" indistinguishable to every reader.

**Two classes are erasure-exempt and both are logs.** They carry no values —
their subject references are already one-way derived ids — and their clocks run
past tenant deletion because accountability for what was done to a tenant's data
outlives the tenant. Exemption is a property of the record class, asserted here,
not a decision a handler makes about a row.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable

from contextplane.exceptions import RegistryError

#: The approved policy version these values were decided under. Every tombstone,
#: derivative registration and work item records it, so a later reader can tell
#: which numbers were in force rather than assuming today's.
POLICY_VERSION = "CP-POLICY-2026-08-A"

#: Days between tenant deletion and the purge of every content class. The grace
#: exists so an accidental deletion is recoverable; after it, nothing content-
#: bearing survives and the tenant's pseudonymization salt is destroyed, which is
#: what turns the two exempt logs' derived ids from pseudonymous to unreversible.
TENANT_GRACE_DAYS = 30

# --- record classes -------------------------------------------------------
#
# Spelled as constants because they are written into `source_tombstones` and
# `derivative_source_links` as text and read back by handlers. A typo in a
# literal produces a tombstone for a class nothing propagates.

RECORD_TASK_CHECKPOINT = "intent_checkpoint"
RECORD_CONTEXT_RECEIPT = "context_receipt"
RECORD_RECEIPT_ITEM = "receipt_item"
RECORD_RECEIPT_EXCLUSION = "receipt_exclusion"
RECORD_EXTERNAL_SIGNAL = "external_signal"
RECORD_CONTEXT_FEEDBACK = "context_feedback"
RECORD_MEMORY_CLAIM = "memory_claim"
RECORD_DERIVATIVE = "derivative"
RECORD_AUDIT_LOG = "audit_log"
RECORD_PII_DETECTION_LOG = "pii_detection_log"
RECORD_EXPORT = "export"
RECORD_WORKSPACE_ENTRY = "workspace_entry"
#: The highest-volume class in the system, and the last one outside this
#: framework. See its disposition for how a per-tenant period lives inside a
#: class-level policy.
RECORD_SESSION_EVENT = "session_event"

# --- who is a person, for the purpose of an erasure -----------------------

#: The origin types that make a record's author an actor of this system.
#:
#: Two content tables name their author by the author's own id *as text*, beside
#: a column saying what kind of thing that author is: a signal has
#: `producer_id`/`producer_type`, feedback has `reporter_id`/`reporter_type`. In
#: both, an `external` origin is another system rather than a person — so an
#: erasure that matched on the id alone could delete a vendor's entire feed
#: because one identifier happened to collide with an actor's.
#:
#: It lives here, below every module that reads it, because it is a statement
#: about *what an erasure request covers*, which is the same kind of statement as
#: the dispositions below. Two copies of it in two subsystems would erase two
#: different sets of rows the first time one of them was extended.
ACTOR_ORIGIN_TYPES: tuple[str, ...] = ("human", "agent")

# --- erasure modes --------------------------------------------------------
#
# The same closed set the schema admits. `exempt` is the accountability-log
# case and is not a way to skip work: it asserts the class holds no values to
# erase in the first place.

MODE_DELETE = "delete"
MODE_MINIMIZE = "minimize"
MODE_MINIMIZE_AND_TOMBSTONE = "minimize_and_tombstone"
MODE_EXEMPT = "exempt"

ERASURE_MODES = frozenset({MODE_DELETE, MODE_MINIMIZE, MODE_MINIMIZE_AND_TOMBSTONE, MODE_EXEMPT})


class UnknownRecordClass(RegistryError):
    """Raised when a caller names a record class the policy does not cover.

    Loud rather than defaulted: a class with no disposition has no retention, no
    erasure mode and no verifier rule, and guessing one of those on its behalf is
    how a record ends up kept forever because nobody declared how long to keep it.
    """


class NoComputableExpiry(RegistryError):
    """Raised when a derivative's expiry cannot be derived from its sources.

    A derivative registration may not be written without an expiry, so a caller
    that supplies no sources and no fallback is asking for the unbounded case the
    schema exists to refuse.
    """


@dataclasses.dataclass(frozen=True)
class Disposition:
    """One record class's approved handling, in the vocabulary the tables use.

    `minimization_action`, `tombstone_behaviour` and `verifier_disclosure` are
    sentences rather than codes on purpose. They are read by a human deciding
    whether an implementation matches the policy, and a code would move that
    decision into a lookup table nobody consults during review.
    """

    record_class: str
    legal_basis: str
    #: Days from the class's own anchor instant, or None when the period is
    #: bounded by tenant/workspace deletion instead of by a duration.
    retention_days: int | None
    #: The earlier clock, where content reduces before the record goes. None when
    #: the class has only one clock.
    payload_retention_days: int | None
    erasure_mode: str
    minimization_action: str | None
    tombstone_behaviour: str | None
    verifier_disclosure: str

    @property
    def writes_tombstone(self) -> bool:
        """Whether erasing this class leaves a tombstone behind.

        Derived from `tombstone_behaviour` rather than from the mode, because a
        class may be deleted outright and still owe a tombstone: a workspace
        entry is removed entirely, and the record that it was removed is what
        stops the removal looking like data loss.
        """
        return self.tombstone_behaviour is not None

    @property
    def is_exempt(self) -> bool:
        """Whether erasure passes this class over because it holds no values."""
        return self.erasure_mode == MODE_EXEMPT


_VERIFIER_STRUCTURAL = (
    "structural integrity and tombstone metadata only: that the record existed, "
    "its chain position and internally-held digest are intact, and it was erased "
    "on the recorded date under the recorded policy version. Never the erased "
    "content, its size or shape, or any subject identity beyond the derived id."
)

_VERIFIER_NONE = "nothing beyond the record's own existence; this class carries no erasure disclosure of its own."

_VERIFIER_EXEMPT = (
    "the record itself, unmodified: it carries no values and its subject "
    "references are one-way derived ids, so there is nothing to withhold."
)

_DISPOSITIONS: dict[str, Disposition] = {
    d.record_class: d
    for d in (
        Disposition(
            record_class=RECORD_TASK_CHECKPOINT,
            legal_basis="contract performance",
            retention_days=None,
            payload_retention_days=None,
            erasure_mode=MODE_MINIMIZE_AND_TOMBSTONE,
            minimization_action=(
                "clear the body fields (goal, decisions, assumptions, evidence, "
                "completed checks, open questions, next action); keep id, tenant, "
                "sequence, predecessor linkage, digest and recorded_at"
            ),
            tombstone_behaviour="one tombstone per erased checkpoint, holding no part of the body",
            verifier_disclosure=_VERIFIER_STRUCTURAL,
        ),
        Disposition(
            record_class=RECORD_CONTEXT_RECEIPT,
            legal_basis="legitimate interest (verification)",
            retention_days=730,
            payload_retention_days=None,
            erasure_mode=MODE_MINIMIZE_AND_TOMBSTONE,
            minimization_action=(
                "minimize the receipt's items and exclusions; " "keep the envelope and its resolution facts"
            ),
            tombstone_behaviour="one tombstone per minimized receipt",
            verifier_disclosure=_VERIFIER_STRUCTURAL,
        ),
        Disposition(
            record_class=RECORD_RECEIPT_ITEM,
            legal_basis="legitimate interest (verification)",
            retention_days=None,
            payload_retention_days=None,
            erasure_mode=MODE_MINIMIZE,
            minimization_action=(
                "replace item_key with a tenant-keyed erased marker; " "keep block, source and the item's contract id"
            ),
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_RECEIPT_EXCLUSION,
            legal_basis="legitimate interest (verification)",
            retention_days=None,
            payload_retention_days=None,
            erasure_mode=MODE_MINIMIZE,
            minimization_action=(
                "replace item_key with a tenant-keyed erased marker; " "keep block and the withholding reason"
            ),
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_SESSION_EVENT,
            legal_basis="contract performance",
            # The class *ceiling*, and the same 180 the `tenants` CHECK already
            # enforces on `memory_retention_days`. That reconciles the two
            # without a new table: the policy says how long a session event may
            # ever be kept, and the tenant's integer is its choice *within* the
            # class. Every row already carries the resulting `expires_at`, so
            # the sweep honours the tenant's number by reading the row rather
            # than by the framework learning about tenants.
            #
            # The CHECK therefore stops being an unexplained bound and becomes
            # the class ceiling enforced at write, which is the one question
            # this task had to answer about it.
            retention_days=180,
            payload_retention_days=None,
            erasure_mode=MODE_DELETE,
            minimization_action=(
                "delete the event outright; there is no envelope worth keeping "
                "once the body is gone, unlike a receipt or a signal"
            ),
            # No tombstone, and the reason is not volume alone. A session event's
            # durable trace already outlives it: claims extracted from a session
            # survive its erasure and carry an `independence_key` digest of the
            # session they came from, so the disposal is already evidenced by the
            # records that depended on it. A tombstone per event would add the
            # system's largest table again to record what the derivatives show.
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_EXTERNAL_SIGNAL,
            legal_basis="legitimate interest",
            retention_days=730,
            payload_retention_days=180,
            erasure_mode=MODE_DELETE,
            minimization_action="clear the payload and evidence handle at the payload clock; the envelope outlives it",
            tombstone_behaviour="one tombstone per erased signal, so dependents can be invalidated by cause",
            verifier_disclosure=_VERIFIER_STRUCTURAL,
        ),
        Disposition(
            record_class=RECORD_CONTEXT_FEEDBACK,
            legal_basis="contract performance",
            retention_days=730,
            payload_retention_days=365,
            erasure_mode=MODE_MINIMIZE,
            minimization_action="clear the free-text note; the discriminant, rating and receipt linkage survive",
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_MEMORY_CLAIM,
            legal_basis="legitimate interest",
            retention_days=None,
            # Excerpts quote a source payload and minimize on that source's
            # clock, not on the claim's own life.
            payload_retention_days=180,
            erasure_mode=MODE_MINIMIZE,
            minimization_action=(
                "minimize excerpts, invalidate the claim, retain the shell for audit and serve " "it nowhere"
            ),
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_DERIVATIVE,
            legal_basis="inherited from every source",
            retention_days=None,
            payload_retention_days=None,
            erasure_mode=MODE_DELETE,
            minimization_action="redact where the derivative's kind supports it, delete where it does not",
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_AUDIT_LOG,
            legal_basis="legitimate interest (accountability)",
            retention_days=1095,
            payload_retention_days=None,
            erasure_mode=MODE_EXEMPT,
            minimization_action=None,
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_EXEMPT,
        ),
        Disposition(
            record_class=RECORD_PII_DETECTION_LOG,
            legal_basis="legitimate interest",
            retention_days=730,
            payload_retention_days=None,
            erasure_mode=MODE_EXEMPT,
            minimization_action=None,
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_EXEMPT,
        ),
        Disposition(
            record_class=RECORD_EXPORT,
            legal_basis="contract performance",
            retention_days=30,
            payload_retention_days=None,
            erasure_mode=MODE_DELETE,
            minimization_action=None,
            tombstone_behaviour=None,
            verifier_disclosure=_VERIFIER_NONE,
        ),
        Disposition(
            record_class=RECORD_WORKSPACE_ENTRY,
            legal_basis="contract performance",
            retention_days=None,
            payload_retention_days=None,
            erasure_mode=MODE_DELETE,
            minimization_action=None,
            tombstone_behaviour="one tombstone per deleted entry, so the deletion is accountable",
            verifier_disclosure=_VERIFIER_STRUCTURAL,
        ),
    )
}

#: Every class the policy covers, in a stable order for reporting and tests.
RECORD_CLASSES: tuple[str, ...] = tuple(_DISPOSITIONS)


def disposition(record_class: str) -> Disposition:
    """The approved handling of one record class, or a refusal.

    Raises rather than returning a default: a class with no declared disposition
    has no retention period and no erasure mode, and inventing either on its
    behalf is how content is kept because nobody said how long to keep it.
    """
    try:
        return _DISPOSITIONS[record_class]
    except KeyError:
        msg = f"no retention disposition is declared for record class {record_class!r}"
        raise UnknownRecordClass(msg) from None


def is_erasure_exempt(record_class: str) -> bool:
    """Whether erasure passes this class over entirely."""
    return disposition(record_class).is_exempt


def expiry_deadline(record_class: str, anchor: datetime.datetime) -> datetime.datetime | None:
    """When this class's record, anchored at `anchor`, stops being retainable.

    None means the period is event-bounded — the record lives until the tenant or
    workspace holding it is deleted — and a caller that needs a concrete instant
    for such a class must supply the bounding event's own date.
    """
    days = disposition(record_class).retention_days
    if days is None:
        return None
    return anchor + datetime.timedelta(days=days)


def payload_deadline(record_class: str, anchor: datetime.datetime) -> datetime.datetime | None:
    """When this class's *content* must be reduced, ahead of the record's own expiry.

    None means the class has one clock, so its content goes when the record does.
    """
    days = disposition(record_class).payload_retention_days
    if days is None:
        return None
    return anchor + datetime.timedelta(days=days)


def minimum_expiry(
    source_expiries: Iterable[datetime.datetime | None],
    *,
    fallback: datetime.datetime | None = None,
) -> datetime.datetime:
    """The expiry a derivative built from these sources inherits: the earliest of them.

    A derivative never outlives any source it was built from, so the minimum is
    the only safe answer — a maximum or an average would leave content readable
    through a derived artefact after the record it came from was gone.

    A source with no expiry of its own is event-bounded, not unbounded, and
    contributes nothing to the minimum. When *every* source is event-bounded the
    caller must supply `fallback` (the bounding event's own instant); without one
    there is no expiry to write and the registration is refused rather than
    written with a value somebody guessed.
    """
    bounded = [expiry for expiry in source_expiries if expiry is not None]
    if bounded:
        earliest = min(bounded)
        # A fallback that lands earlier than any source still wins: it is the
        # tenant's own horizon, and nothing may outlive that either.
        return min(earliest, fallback) if fallback is not None else earliest
    if fallback is not None:
        return fallback
    msg = "a derivative needs at least one bounded source or an explicit fallback expiry"
    raise NoComputableExpiry(msg)


__all__ = [
    "ACTOR_ORIGIN_TYPES",
    "ERASURE_MODES",
    "MODE_DELETE",
    "MODE_EXEMPT",
    "MODE_MINIMIZE",
    "MODE_MINIMIZE_AND_TOMBSTONE",
    "POLICY_VERSION",
    "RECORD_AUDIT_LOG",
    "RECORD_CLASSES",
    "RECORD_CONTEXT_FEEDBACK",
    "RECORD_CONTEXT_RECEIPT",
    "RECORD_DERIVATIVE",
    "RECORD_EXPORT",
    "RECORD_EXTERNAL_SIGNAL",
    "RECORD_MEMORY_CLAIM",
    "RECORD_PII_DETECTION_LOG",
    "RECORD_RECEIPT_EXCLUSION",
    "RECORD_RECEIPT_ITEM",
    "RECORD_TASK_CHECKPOINT",
    "RECORD_WORKSPACE_ENTRY",
    "TENANT_GRACE_DAYS",
    "Disposition",
    "NoComputableExpiry",
    "UnknownRecordClass",
    "disposition",
    "expiry_deadline",
    "is_erasure_exempt",
    "minimum_expiry",
    "payload_deadline",
]
