"""What a profile migration must account for, and what blocks its activation.

A migration that inventories 99% of a graph is not 99% correct; it is a migration
with an unknown number of rows nobody looked at. So the inventory here is
*closed*: every category is enumerated, and a category with no count is a gap
rather than a zero. `Inventory.assert_complete` refuses a report that skipped one,
because an inventory silently missing a category and one that genuinely found
nothing there produce identical output.

**Every finding needs a disposition, and a disposition is four things.** Owner,
reason, expiry, action — all four, or the finding is unresolved. A disposition
with an owner and no expiry is a decision that never has to be revisited; one with
an action and no reason is a change nobody can review later. The four are required
by construction rather than checked afterwards.

**Expiry is enforced, not recorded.** A grandfathered finding whose expiry has
passed blocks activation exactly as an unresolved one does. That is the whole
value of the field: a `grandfather` that never expires is a `remove` nobody
admitted to, and the entire point of the disposition vocabulary is that those are
different decisions.

**Activation is blocked by unresolved findings, and blocking is the default.** A
migration plan reports whether it may proceed rather than being asked; there is no
parameter through which a caller can say "go anyway", because that parameter is
the one used at 2am.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Mapping, Sequence
from typing import Final

#: Every category a migration must count. Closed on purpose: a migration is
#: complete only when it has looked at all of them, and a category nobody counted
#: is invisible in a report that lists only what was found.
INVENTORY_CATEGORIES: Final[tuple[str, ...]] = (
    "entities",
    "assertions",
    "edges",
    "external_references",
    "caches",
    "search_indexes",
    "closures",
    "compatibility_consumers",
)

#: What a migration can find wrong. Each is a reason a row may not survive the
#: move unchanged, and each needs a decision rather than a default.
COLLISION: Final = "collision"
UNTYPED_ENDPOINT: Final = "untyped_endpoint"
PROVENANCE_GAP: Final = "provenance_gap"
ISOLATION_RISK: Final = "isolation_risk"

FINDING_KINDS: Final[frozenset[str]] = frozenset({COLLISION, UNTYPED_ENDPOINT, PROVENANCE_GAP, ISOLATION_RISK})

#: The four decisions available for a finding. `grandfather` is temporary by
#: definition — it carries an expiry, and an expired one blocks activation, which
#: is what keeps it from becoming a permanent exemption nobody revisits.
MIGRATE: Final = "migrate"
GRANDFATHER: Final = "grandfather"
QUARANTINE: Final = "quarantine"
REMOVE: Final = "remove"

DISPOSITION_ACTIONS: Final[frozenset[str]] = frozenset({MIGRATE, GRANDFATHER, QUARANTINE, REMOVE})


class MigrationRefused(RuntimeError):
    """A migration may not proceed, and this says why."""


class IncompleteInventory(MigrationRefused):
    """The inventory did not account for every category."""


@dataclasses.dataclass(frozen=True)
class Disposition:
    """A decision about one finding: who, why, until when, and what.

    All four are required. An owner with no expiry is a decision nobody revisits;
    an action with no reason is a change nobody can review. Required by
    construction rather than validated afterwards, so a half-filled disposition
    cannot exist long enough to be read.
    """

    action: str
    owner: str
    reason: str
    expires_at: datetime.datetime

    def __post_init__(self) -> None:
        if self.action not in DISPOSITION_ACTIONS:
            msg = f"unknown disposition {self.action!r}; legal: {', '.join(sorted(DISPOSITION_ACTIONS))}"
            raise MigrationRefused(msg)
        for field, value in (("owner", self.owner), ("reason", self.reason)):
            if not value.strip():
                msg = f"a disposition names its {field}; without one the decision cannot be reviewed later"
                raise MigrationRefused(msg)

    def is_expired(self, at: datetime.datetime) -> bool:
        """Whether this decision has run out.

        An expired disposition blocks activation exactly as an absent one does. A
        `grandfather` that never expires is a `remove` nobody admitted to.
        """
        return self.expires_at <= at


@dataclasses.dataclass(frozen=True)
class Finding:
    """One thing a migration found that needs a decision."""

    kind: str
    subject: str
    detail: str
    disposition: Disposition | None = None

    def __post_init__(self) -> None:
        if self.kind not in FINDING_KINDS:
            msg = f"unknown finding kind {self.kind!r}; legal: {', '.join(sorted(FINDING_KINDS))}"
            raise MigrationRefused(msg)

    def is_resolved(self, at: datetime.datetime) -> bool:
        """Whether this finding is settled at `at`."""
        return self.disposition is not None and not self.disposition.is_expired(at)


@dataclasses.dataclass(frozen=True)
class Inventory:
    """What the migration counted, by category.

    Counts rather than rows: the rows belong to the database, and an inventory
    that carried them would be a second copy going stale from the moment it was
    taken. What this needs to answer is whether every category was *looked at*.
    """

    counts: Mapping[str, int]

    def assert_complete(self) -> None:
        """Refuse an inventory that skipped a category.

        A missing category and a category that genuinely found nothing produce
        identical output in any report that lists only what it found, which is why
        absence is an error here rather than a zero.
        """
        missing = sorted(set(INVENTORY_CATEGORIES) - set(self.counts))
        if missing:
            msg = (
                f"the inventory did not account for {', '.join(missing)}; a category nobody counted is "
                "indistinguishable from one that was empty, and a migration is complete only when every "
                "category has been looked at"
            )
            raise IncompleteInventory(msg)
        unexpected = sorted(set(self.counts) - set(INVENTORY_CATEGORIES))
        if unexpected:
            msg = f"the inventory reports categories the migration does not define: {', '.join(unexpected)}"
            raise IncompleteInventory(msg)

    @property
    def total(self) -> int:
        """Everything counted, across every category."""
        return sum(self.counts.values())


@dataclasses.dataclass(frozen=True)
class MigrationPlan:
    """An inventory, its findings, and whether activation may proceed."""

    inventory: Inventory
    findings: Sequence[Finding]

    def unresolved(self, at: datetime.datetime) -> tuple[Finding, ...]:
        """Findings with no disposition, or whose disposition has expired.

        The two are reported together deliberately: to an activation decision they
        are the same thing, and separating them would invite treating an expired
        grandfather as softer than an undecided finding.
        """
        return tuple(finding for finding in self.findings if not finding.is_resolved(at))

    def assert_may_activate(self, at: datetime.datetime) -> None:
        """Refuse activation while anything is unresolved.

        Takes no override. A caller able to say "go anyway" is a caller who will,
        at the moment the migration is latest and the reasoning weakest.
        """
        self.inventory.assert_complete()
        blocking = self.unresolved(at)
        if blocking:
            described = ", ".join(f"{finding.kind}:{finding.subject}" for finding in blocking)
            msg = f"{len(blocking)} finding(s) are unresolved or expired and block activation: {described}"
            raise MigrationRefused(msg)

    def may_activate(self, at: datetime.datetime) -> bool:
        """Whether activation may proceed, for a caller that wants to branch."""
        try:
            self.assert_may_activate(at)
        except MigrationRefused:
            return False
        return True

    def warnings(self, at: datetime.datetime) -> tuple[str, ...]:
        """Dispositions that hold now but expire soon enough to act on.

        A warning window rather than a silent countdown: a grandfather expiring
        next week and one expiring next year need different attention, and a plan
        that reported neither would surprise somebody on the day it lapsed.
        """
        soon = at + datetime.timedelta(days=30)
        return tuple(
            f"{finding.kind}:{finding.subject} is grandfathered until {finding.disposition.expires_at:%Y-%m-%d}"
            for finding in self.findings
            if finding.disposition is not None
            and not finding.disposition.is_expired(at)
            and finding.disposition.expires_at <= soon
        )


def empty_inventory() -> Inventory:
    """Every category present, at zero.

    Zero is a real answer and a missing key is not, so a deployment with nothing
    to migrate still produces a *complete* inventory rather than an empty report
    that reads the same as a failed one.

    Lives here rather than in either script because both need it, and a helper
    imported from one script into another makes the same file reachable under two
    module names -- which mypy refuses outright.
    """
    return Inventory(counts=dict.fromkeys(INVENTORY_CATEGORIES, 0))


def compare_identities(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, ...]:
    """What a dry run changed that it should not have.

    Identities and references must survive a migration unchanged; a subject whose
    identity moved is one every existing reference now misses. Reported as a list
    rather than raised, because a dry run's job is to say what *would* happen.
    """
    drifted = [
        f"{subject}: {before[subject]!r} -> {after[subject]!r}"
        for subject in sorted(set(before) & set(after))
        if before[subject] != after[subject]
    ]
    lost = [f"{subject}: present before, absent after" for subject in sorted(set(before) - set(after))]
    return tuple(drifted + lost)


__all__ = [
    "COLLISION",
    "DISPOSITION_ACTIONS",
    "FINDING_KINDS",
    "GRANDFATHER",
    "INVENTORY_CATEGORIES",
    "ISOLATION_RISK",
    "MIGRATE",
    "PROVENANCE_GAP",
    "QUARANTINE",
    "REMOVE",
    "UNTYPED_ENDPOINT",
    "Disposition",
    "Finding",
    "IncompleteInventory",
    "Inventory",
    "MigrationPlan",
    "MigrationRefused",
    "compare_identities",
    "empty_inventory",
]
