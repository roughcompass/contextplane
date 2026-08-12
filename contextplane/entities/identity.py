"""Type-aware identity: two names for one thing, and how to stop guessing.

The old rule was one name per tenant. That forbids a `checkout` service and a
`checkout` capability coexisting, which is a real thing to want and the reason
this migration exists. The new rule is one name per *type*, reached through a
qualified handle `<namespace>:<entity_type>/<name>`.

Replacing an identity scheme is where references die, so three properties are
enforced here rather than assumed:

**No opaque ID moves.** Handles are an additional way to name a row, never a
replacement for its ID. Every external mapping and dependent reference keeps
pointing at what it pointed at, because nothing about the row's identity column
is touched.

**An unqualified name that matches more than one type is refused, loudly.**
Not resolved by newest, not by type priority, not by "the one the caller
probably meant". A silent winner is a wrong answer that looks like a right one,
and the whole point of qualifying is that the ambiguity becomes sayable.

**The old and new schemes coexist until somebody proves nothing needs the old
one.** Dual read consults both and compares; divergence is a finding, not
something to paper over by preferring whichever answered. Contracting the old
constraint is deliberately not reachable from here.

The resolution functions take rows rather than a session. Identity is decided by
comparing candidates, and a decision procedure that can only be exercised
through a database is one nobody tests against the awkward candidate sets --
which is where every interesting ambiguity lives.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
from collections.abc import Iterable

# `<namespace>:<entity_type>/<name>`. Namespace and type are constrained the
# same way profile definitions constrain them; the name is looser because it is
# tenant data rather than vocabulary, and narrowing it here would reject rows the
# database already holds.
_SEGMENT = r"[a-z0-9][a-z0-9._-]*"
QUALIFIED_HANDLE_PATTERN = re.compile(
    rf"^(?P<namespace>{_SEGMENT}):(?P<entity_type>{_SEGMENT})/(?P<name>[^\s/][^\s]*)$"
)

# Handle kinds, as the migration spells them. `primary` is the one the
# type-aware uniqueness index covers; the rest are additional ways to arrive at
# a row that has already been named.
HandleKind = enum.StrEnum(
    "HandleKind", {"PRIMARY": "primary", "ALIAS": "alias", "LEGACY": "legacy", "EXTERNAL_MAPPING": "external_mapping"}
)

HANDLE_KINDS: frozenset[str] = frozenset(kind.value for kind in HandleKind)


class IdentityError(ValueError):
    """A lookup or a transition was refused."""


class UnknownIdentity(IdentityError):
    """Nothing active matched. Distinct from ambiguity: no answer, not too many."""


class AmbiguousIdentity(IdentityError):
    """An unqualified name matched more than one type.

    Carries the candidates so the caller can requalify without a second query,
    and so the refusal names what to disambiguate between rather than only that
    something was ambiguous.
    """

    def __init__(self, name: str, entity_types: Iterable[str]) -> None:
        self.name = name
        self.entity_types = tuple(sorted(entity_types))
        super().__init__(
            f"{name!r} names {len(self.entity_types)} types: {list(self.entity_types)}. Qualify it as "
            f"<namespace>:<entity_type>/{name} -- an unqualified name is answered by exactly one type or by "
            "this error, never by whichever candidate happened to be found first"
        )


@dataclasses.dataclass(frozen=True)
class QualifiedHandle:
    """The parsed form. Round-trips through `str` exactly."""

    namespace: str
    entity_type: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}:{self.entity_type}/{self.name}"

    @property
    def lookup_key(self) -> str:
        """The normalized key the active-handle uniqueness index is built on.

        Lower-cased with `str.lower`, deliberately not `str.casefold`, because
        the database enforces uniqueness with SQL `lower()` and Python has to
        agree with the index rather than be independently more correct. The two
        differ on real input: `"ß".casefold()` is `"ss"` while `lower('ß')` is
        `"ß"`, so a casefolded key would judge two rows identical that Postgres
        happily stores side by side -- and resolution would then report a broken
        index for a database behaving exactly as specified.
        """
        return str(self).lower()

    @classmethod
    def parse(cls, value: str) -> QualifiedHandle:
        """Parse the qualified form, or refuse a bare name for lacking a type."""
        match = QUALIFIED_HANDLE_PATTERN.match(value.strip())
        if match is None:
            raise IdentityError(
                f"{value!r} is not a qualified handle. The form is <namespace>:<entity_type>/<name>; an "
                "unqualified name cannot be parsed into one because the type is the part that was missing"
            )
        return cls(**match.groupdict())


def lookup_key_for(namespace: str, entity_type: str, name: str) -> str:
    """The key to store, so writers and readers derive it one way."""
    return QualifiedHandle(namespace=namespace, entity_type=entity_type, name=name).lookup_key


@dataclasses.dataclass(frozen=True)
class HandleRow:
    """One `entity_handles` row, as resolution needs to see it.

    A narrow view on purpose. Resolution depends on the type, the name, the kind
    and whether the row is still active; giving it the whole row would let a
    later change start deciding identity from a column nobody reasoned about.
    """

    entity_id: str
    entity_type: str
    namespace: str
    handle_name: str
    kind: str
    valid_to: dt.datetime | None = None

    @property
    def active(self) -> bool:
        """Still current. A retired handle stays readable and resolves nothing."""
        return self.valid_to is None

    @property
    def lookup_key(self) -> str:
        """This row's key, derived the same way a writer derives it."""
        return lookup_key_for(self.namespace, self.entity_type, self.handle_name)


def _active(rows: Iterable[HandleRow]) -> list[HandleRow]:
    return [row for row in rows if row.active]


def resolve_qualified(rows: Iterable[HandleRow], handle: str | QualifiedHandle) -> str:
    """Resolve a fully qualified handle to one entity ID.

    Unambiguous by construction: the active qualified key is unique per tenant,
    so more than one match means the uniqueness index is not doing its job and
    that is worth saying rather than picking one.
    """
    parsed = handle if isinstance(handle, QualifiedHandle) else QualifiedHandle.parse(handle)
    key = parsed.lookup_key
    matches = [row for row in _active(rows) if row.lookup_key == key]
    if not matches:
        raise UnknownIdentity(f"no active handle matches {parsed}")
    identities = {row.entity_id for row in matches}
    if len(identities) > 1:
        raise IdentityError(
            f"{parsed} resolves to {len(identities)} entities {sorted(identities)}; the active qualified handle "
            "is unique per tenant, so this is a broken index rather than an ambiguity to resolve"
        )
    return matches[0].entity_id


def resolve_unqualified(rows: Iterable[HandleRow], name: str) -> str:
    """Resolve a bare name, or refuse because it names more than one type.

    Matching is over active rows of every kind, not only primaries: an alias
    that collides with another type's name is exactly as ambiguous to a caller
    as two primaries would be, and answering it because the collision happened
    to involve an alias would make the refusal depend on how the row was
    created.
    """
    # `lower`, matching the index rather than Python's stricter folding; see
    # `QualifiedHandle.lookup_key` for why agreeing with SQL beats being right
    # independently of it.
    folded = name.strip().lower()
    if not folded:
        raise IdentityError("an empty name resolves to nothing; a blank lookup is a caller bug, not a miss")

    matches = [row for row in _active(rows) if row.handle_name.lower() == folded]
    if not matches:
        raise UnknownIdentity(f"no active handle is named {name!r}")

    by_type = {row.entity_type for row in matches}
    if len(by_type) > 1:
        raise AmbiguousIdentity(name, by_type)

    # A second, non-redundant guard. The type check above cannot catch two
    # *aliases* that share a name and a type while pointing at different
    # entities: the database's primary-name uniqueness index is partial on
    # `kind = 'primary'`, so nothing stops that row pair existing. Both checks
    # raise the same error because a caller cannot act differently on the two,
    # but neither is dead code and removing either opens a real case.
    identities = {row.entity_id for row in matches}
    if len(identities) > 1:
        raise AmbiguousIdentity(name, by_type)
    return matches[0].entity_id


# --- migration phases ---------------------------------------------------------


class Phase(enum.StrEnum):
    """Where a tenant is in the identity migration.

    Ordered, and only reachable one step at a time. Skipping is refused because
    each phase's whole purpose is to make the next one safe: backfilling without
    expanding writes into columns that do not exist, and cutting over without
    dual reading means nobody ever compared the two answers.
    """

    LEGACY = "legacy"
    EXPANDED = "expanded"
    BACKFILLED = "backfilled"
    DUAL_READ = "dual_read"
    DUAL_WRITE = "dual_write"
    CUT_OVER = "cut_over"
    ROLLED_BACK = "rolled_back"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.LEGACY,
    Phase.EXPANDED,
    Phase.BACKFILLED,
    Phase.DUAL_READ,
    Phase.DUAL_WRITE,
    Phase.CUT_OVER,
)

# Rollback is reachable from anywhere the new scheme is partly live, and from
# nowhere else: there is nothing to roll back before expansion, and a tenant
# already rolled back does not roll back again.
_ROLLBACK_FROM: frozenset[Phase] = frozenset(
    {Phase.EXPANDED, Phase.BACKFILLED, Phase.DUAL_READ, Phase.DUAL_WRITE, Phase.CUT_OVER}
)


def assert_phase_transition(current: Phase, target: Phase) -> None:
    """Refuse a phase move that skips a step or reverses one silently."""
    if target is Phase.ROLLED_BACK:
        if current not in _ROLLBACK_FROM:
            raise IdentityError(
                f"cannot roll back from {current.value!r}; there is nothing expanded to undo, and recording a "
                "rollback that reversed nothing would leave a tenant looking migrated-then-reverted"
            )
        return
    if current is Phase.ROLLED_BACK:
        raise IdentityError(
            "a rolled-back tenant restarts from the beginning rather than resuming; resuming would carry "
            "forward the state that was rolled back for a reason nobody has recorded as fixed"
        )
    if current not in PHASE_ORDER or target not in PHASE_ORDER:
        raise IdentityError(f"unknown phase transition {current.value!r} -> {target.value!r}")
    index, target_index = PHASE_ORDER.index(current), PHASE_ORDER.index(target)
    if target_index != index + 1:
        raise IdentityError(
            f"identity migration moves one phase at a time: {current.value!r} -> {target.value!r} skips "
            f"{[phase.value for phase in PHASE_ORDER[index + 1 : target_index]]}. Each phase makes the next one "
            "safe, so a skipped one is a safety check nobody ran"
        )


# --- the rollback window ------------------------------------------------------

ROLLBACK_WINDOW_DAYS = 30
ROLLBACK_WINDOW_MAX_DAYS = 60


@dataclasses.dataclass(frozen=True)
class RollbackWindow:
    """Thirty days from activation, extendable once, never twice.

    The extension is a dataclass field rather than a mutable counter so that
    "has this already been extended" is answerable from the record itself. A
    window that could be extended repeatedly is not a window.
    """

    activated_at: dt.datetime
    extended_reason: str | None = None

    def __post_init__(self) -> None:
        if self.activated_at.tzinfo is None:
            raise IdentityError("activation time is timezone-aware; a naive one silently means the server's zone")
        if self.extended_reason is not None and not self.extended_reason.strip():
            raise IdentityError(
                "an extension records why; a blank reason passes an 'is it set' check and tells the next "
                "operator nothing about whether it may be extended again"
            )

    @property
    def days(self) -> int:
        """Thirty, or sixty once an extension has been recorded with its reason."""
        return ROLLBACK_WINDOW_MAX_DAYS if self.extended_reason else ROLLBACK_WINDOW_DAYS

    @property
    def closes_at(self) -> dt.datetime:
        """When rollback stops being available."""
        return self.activated_at + dt.timedelta(days=self.days)

    def extend(self, reason: str) -> RollbackWindow:
        """Extend once to sixty days. A second attempt is refused, not ignored."""
        if self.extended_reason:
            raise IdentityError(
                f"the rollback window was already extended ({self.extended_reason!r}) and does not extend "
                "twice; a second extension is a decision to keep both schemes live indefinitely and needs to "
                "be made as that rather than as another 30 days"
            )
        return dataclasses.replace(self, extended_reason=reason)

    def is_open_at(self, moment: dt.datetime) -> bool:
        """Whether rollback is still available at a given instant."""
        if moment.tzinfo is None:
            raise IdentityError("comparison time is timezone-aware")
        return moment < self.closes_at


def may_contract_old_constraint(
    *,
    window: RollbackWindow,
    moment: dt.datetime,
    legacy_consumers: int,
) -> tuple[bool, tuple[str, ...]]:
    """Both conditions or neither. Returns the verdict and every reason against.

    Deliberately returns reasons rather than raising on the first: an operator
    asking "can we contract yet" needs the whole list, and one that stopped at
    the closed window would hide a consumer inventory nobody has drained.

    This module never performs the contraction. It answers whether the
    preconditions hold, which is a different act from doing it and belongs to a
    later change that has its own approvals.
    """
    reasons: list[str] = []
    if window.is_open_at(moment):
        remaining = window.closes_at - moment
        reasons.append(
            f"the rollback window is open for another {remaining.days} day(s), until " f"{window.closes_at.isoformat()}"
        )
    if legacy_consumers > 0:
        reasons.append(
            f"{legacy_consumers} legacy consumer(s) still read the old identity; removing it while one remains "
            "breaks a reader that has no replacement to move to"
        )
    return (not reasons), tuple(reasons)


# --- dual read ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DualReadOutcome:
    """What the two schemes said, and whether they agreed.

    Both answers are kept even when they match. A comparison that discarded the
    agreeing case could not later show which names had ever been checked, and
    "we compared and it was fine" is not evidence anybody compared.
    """

    name: str
    legacy_entity_id: str | None
    handle_entity_id: str | None
    error: str | None = None

    @property
    def agrees(self) -> bool:
        """Both schemes named the same entity, and neither refused."""
        return self.error is None and self.legacy_entity_id == self.handle_entity_id


def compare_dual_read(
    rows: Iterable[HandleRow],
    *,
    name: str,
    legacy_entity_id: str | None,
) -> DualReadOutcome:
    """Resolve through the handle scheme and compare with the legacy answer.

    An ambiguity here is not a divergence: the legacy scheme could only ever
    return one row because it forbade the second, so a name that now matches two
    types is the migration working. It is recorded as an error so the operator
    sees it, and it is not counted as the two schemes disagreeing about one
    entity.
    """
    try:
        resolved: str | None = resolve_unqualified(rows, name)
        error: str | None = None
    except AmbiguousIdentity as ambiguous:
        resolved, error = None, str(ambiguous)
    except UnknownIdentity as missing:
        resolved, error = None, str(missing)
    return DualReadOutcome(
        name=name,
        legacy_entity_id=legacy_entity_id,
        handle_entity_id=resolved,
        error=error,
    )


__all__ = [
    "HANDLE_KINDS",
    "PHASE_ORDER",
    "QUALIFIED_HANDLE_PATTERN",
    "ROLLBACK_WINDOW_DAYS",
    "ROLLBACK_WINDOW_MAX_DAYS",
    "AmbiguousIdentity",
    "DualReadOutcome",
    "HandleKind",
    "HandleRow",
    "IdentityError",
    "Phase",
    "QualifiedHandle",
    "RollbackWindow",
    "UnknownIdentity",
    "assert_phase_transition",
    "compare_dual_read",
    "lookup_key_for",
    "may_contract_old_constraint",
    "resolve_qualified",
    "resolve_unqualified",
]
