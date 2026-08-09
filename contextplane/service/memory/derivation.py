"""Turning evidence into an assertion, without inheriting more authority than the evidence had.

This module reads bound evidence and produces a small typed assertion plus the
record of the attempt that produced it. It writes no claim: staging one is
`claim_writer.py`'s job, and keeping the two apart is what preserves the
one-writer rule while letting extraction run wherever it likes.

**The authority ceiling is enforced here because it cannot be enforced in SQL.**
A derived claim may inherit at most what its weakest source was entitled to
assert — a CI system reporting that a run failed does not license a claim that a
change was wrong. The schema stores `source_authority` on the attempt and on
every evidence link precisely so the comparison is possible, but no CHECK can
express it: authority is a source-issued string, and the ordering that makes
"weakest" meaningful lives in `service/governance/authority.py` rather than in
the database. So the ceiling is computed from the evidence and an attempt that
claims more is refused, not clamped. Clamping would silently produce an
assertion nobody asked for; refusing says which evidence was too weak.

**Weakest is a `max()` over ranks, and the ranks run strongest-first.** Rank 0 is
`owner_human`; `unattributed` is last. A caller comparing the strings would get
alphabetical nonsense, which is why nothing here compares them directly.

**A diagnostic observation cannot be evidence.** It cites nothing, so nothing can
check what it refers to, and admitting one would let an unattributable complaint
become an assertion about a specific retrieved item. The feedback schema already
forbids a learning-eligible diagnostic; this module refuses one again on the way
in rather than trusting that the row it was handed came through that path.

**Excerpts are bounded and are never a body copy.** The extractor may keep the
smallest quotation that makes an assertion checkable. It may not copy a workspace
entry, a checkpoint payload, or a signal's full projection — a "bounded excerpt"
that happens to be the whole field is a copy with a shorter name, so the bound is
a length the code enforces rather than an intention the docstring states.

**Nothing here asserts causation.** "The run failed and this context was served"
is two observations; "this context caused the failure" is a third claim with its
own evidence requirements that no extractor can satisfy from what it reads. The
predicate vocabulary is closed to keep that distinction structural instead of
relying on whoever writes the next profile.

**Supersession is recorded, not resolved.** A re-run supersedes an earlier attempt
for learning without making either untrue, so an attempt whose evidence is
entirely superseded is still stored — and marked, so promotion can refuse it
later. Dropping it would lose the record that the derivation was made at all.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import (
    AUTHORITY_UNATTRIBUTED,
    SOURCE_AUTHORITY_RANK,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext

#: What an evidence link may point at. The same closed set the schema declares;
#: restated here because this module has to branch on it, and a kind it did not
#: recognise would otherwise be stored as evidence pointing nowhere.
EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"signal", "receipt", "receipt_item", "external_reference", "checkpoint"}
)

#: The assertions an extractor may make. Closed so that "caused" cannot be added
#: by a profile author who finds it convenient: a causal claim needs evidence no
#: extractor reading these inputs can supply.
ASSERTION_PREDICATES: Final[frozenset[str]] = frozenset(
    {
        "observed_outcome",
        "context_was_stale",
        "context_was_incomplete",
        "context_was_incorrect",
        "runbook_step_missing",
        "reference_unresolvable",
    }
)

#: The longest excerpt that still counts as an excerpt. Chosen to be obviously
#: too small to hold a checkpoint body or a workspace entry: the point is not the
#: exact number but that a copy cannot pass as a quotation.
MAX_EXCERPT_CHARS: Final[int] = 512

#: Statuses an attempt may be stored under, matching the schema's own set.
STATUS_PENDING: Final[str] = "pending"
STATUS_STAGED: Final[str] = "staged"
STATUS_REJECTED: Final[str] = "rejected"


class DerivationRefused(ValidationError):
    """An attempt that must not be stored as it stands.

    A `ValidationError` subclass so the surfaces above translate it the way they
    translate every other refusal, while callers that care about the distinction
    can still catch this one.
    """


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One input the extractor read, with the authority it carried when read."""

    kind: str
    source_authority: str
    classification: str
    signal_id: uuid.UUID | None = None
    receipt_id: uuid.UUID | None = None
    receipt_item_id: str | None = None
    reference_id: uuid.UUID | None = None
    checkpoint_id: uuid.UUID | None = None
    checkpoint_digest: str | None = None
    excerpt: str | None = None
    #: Whether a later attempt has overtaken this evidence for learning. Both
    #: remain true; only one is the thing to learn from.
    superseded_for_learning: bool = False

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            message = f"unknown evidence kind {self.kind!r}; expected one of {sorted(EVIDENCE_KINDS)}"
            raise DerivationRefused(message)
        if self.source_authority not in SOURCE_AUTHORITY_RANK:
            message = f"evidence carries an authority outside the ladder: {self.source_authority!r}"
            raise DerivationRefused(message)
        if self.kind == "checkpoint" and not (self.checkpoint_id and self.checkpoint_digest):
            # The id says which checkpoint; the digest says it had not changed
            # when it was read. A citation without the digest claims an
            # immutability it never verified.
            message = "checkpoint evidence needs both an id and the digest it was read at"
            raise DerivationRefused(message)
        if self.kind == "receipt_item" and not (self.receipt_id and self.receipt_item_id):
            message = "an exact item citation needs both the receipt and the item on it"
            raise DerivationRefused(message)
        if self.excerpt is not None and len(self.excerpt) > MAX_EXCERPT_CHARS:
            # Length, not intent: a "bounded excerpt" that happens to be the whole
            # field is a copy with a shorter name.
            message = f"excerpt is {len(self.excerpt)} chars; the bound is {MAX_EXCERPT_CHARS}"
            raise DerivationRefused(message)


@dataclasses.dataclass(frozen=True)
class Assertion:
    """The small typed thing an extractor concluded.

    `applicability` is where the assertion is claimed to hold, recorded rather
    than inferred from the evidence: narrowing it is a judgement the extractor
    made and a later reader has to be able to see it.
    """

    subject_reference: str
    predicate: str
    value: Mapping[str, Any]
    applicability: str

    def __post_init__(self) -> None:
        if self.predicate not in ASSERTION_PREDICATES:
            message = (
                f"predicate {self.predicate!r} is not one an extractor may assert; "
                f"expected one of {sorted(ASSERTION_PREDICATES)}"
            )
            raise DerivationRefused(message)
        for name, value in (("subject_reference", self.subject_reference), ("applicability", self.applicability)):
            if not value or not value.strip():
                message = f"{name} is required: an assertion that names neither subject nor scope cannot be reviewed"
                raise DerivationRefused(message)


@dataclasses.dataclass(frozen=True)
class DerivationProfile:
    """Which extractor, and which version of it.

    Both, because an assertion a later version would not have made must be
    identifiable without re-running anything.
    """

    name: str
    version: str


@dataclasses.dataclass(frozen=True)
class RecordedDerivation:
    """The stored attempt, and whether this call is what stored it."""

    derivation_id: uuid.UUID
    assertion_digest: str
    source_authority: str
    classification: str
    status: str
    evidence_count: int
    #: True when every piece of evidence has been superseded for learning.
    #: Promotion is barred on superseded-only evidence; the attempt is still
    #: stored, because it did happen.
    superseded_only: bool
    replayed: bool


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def assertion_digest(profile: DerivationProfile, assertion: Assertion) -> str:
    """The normalized digest two identical conclusions collide on.

    Covers the profile as well as the assertion: the same conclusion reached by a
    different extractor version is a different attempt, and folding the version
    out would make an upgrade look like a replay.
    """
    material = {
        "profile": profile.name,
        "profile_version": profile.version,
        "subject_reference": assertion.subject_reference,
        "predicate": assertion.predicate,
        "value": dict(assertion.value),
        "applicability": assertion.applicability,
    }
    return "sha256:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def weakest_authority(evidence: Sequence[Evidence]) -> str:
    """The ceiling a claim derived from this evidence may carry.

    `max()` over ranks because rank 0 is the strongest: the weakest link is the
    highest number. Comparing the strings would sort alphabetically and produce a
    ceiling with no relationship to authority at all.
    """
    if not evidence:
        # No evidence means nothing licenses anything. Returning the weakest tier
        # rather than raising lets a caller ask the question before assembling,
        # while `derive` still refuses to store an attempt with no inputs.
        return AUTHORITY_UNATTRIBUTED
    return max((item.source_authority for item in evidence), key=lambda value: SOURCE_AUTHORITY_RANK[value])


class DerivationService:
    """Derives assertions from bound evidence and records the attempt.

    Writes no claim. Staging one is the claim path's job; this service produces
    what that path would need and the record that it was produced, so an
    unreviewed assertion never reaches a serving surface by way of the extractor.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def derive(
        self,
        ctx: TenantContext,
        *,
        profile: DerivationProfile,
        assertion: Assertion,
        evidence: Sequence[Evidence],
        claimed_authority: str | None = None,
        classification: str = "internal",
    ) -> RecordedDerivation:
        """Record one derivation attempt, or return the one already recorded.

        `claimed_authority` is what the caller believes the assertion carries. It
        is checked against the evidence rather than trusted: an attempt claiming
        more than its weakest source is refused, naming the ceiling, so the
        caller learns which evidence was too weak instead of receiving a silently
        weakened assertion.
        """
        if not evidence:
            message = "a derivation with no evidence asserts something nothing licenses"
            raise DerivationRefused(message)

        ceiling = weakest_authority(evidence)
        if claimed_authority is not None:
            self._assert_within_ceiling(claimed_authority, ceiling)

        digest = assertion_digest(profile, assertion)
        superseded_only = all(item.superseded_for_learning for item in evidence)

        async with self._session_factory() as session:
            existing = await self._existing(session, ctx, profile, digest)
            if existing is not None:
                return dataclasses.replace(existing, replayed=True)
            return await self._store(
                session,
                ctx,
                profile=profile,
                assertion=assertion,
                evidence=evidence,
                digest=digest,
                authority=ceiling,
                classification=classification,
                superseded_only=superseded_only,
            )

    @staticmethod
    def _assert_within_ceiling(claimed: str, ceiling: str) -> None:
        """Refuse an attempt that claims more than its evidence licenses."""
        if claimed not in SOURCE_AUTHORITY_RANK:
            message = f"claimed authority {claimed!r} is not on the ladder"
            raise DerivationRefused(message)
        if SOURCE_AUTHORITY_RANK[claimed] < SOURCE_AUTHORITY_RANK[ceiling]:
            message = (
                f"a claim derived from this evidence may carry at most {ceiling!r}, "
                f"and {claimed!r} is stronger; the weakest source is what licenses the assertion"
            )
            raise DerivationRefused(message)

    async def _existing(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        profile: DerivationProfile,
        digest: str,
    ) -> RecordedDerivation | None:
        """The attempt already stored for this conclusion, if there is one."""
        row = (
            await session.execute(
                text(
                    "SELECT d.derivation_id, d.assertion_digest, d.source_authority, d.classification, d.status,"
                    " (SELECT count(*) FROM derivation_evidence_links l"
                    "    WHERE l.derivation_id = d.derivation_id) AS evidence_count"
                    " FROM claim_derivations d"
                    " WHERE d.tenant_id = :tid AND d.profile = :p AND d.profile_version = :v"
                    "   AND d.assertion_digest = :dig"
                ),
                {"tid": ctx.tenant_id, "p": profile.name, "v": profile.version, "dig": digest},
            )
        ).one_or_none()
        if row is None:
            return None
        return RecordedDerivation(
            derivation_id=row.derivation_id,
            assertion_digest=row.assertion_digest,
            source_authority=row.source_authority,
            classification=row.classification,
            status=row.status,
            evidence_count=row.evidence_count,
            superseded_only=False,
            replayed=True,
        )

    async def _store(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        *,
        profile: DerivationProfile,
        assertion: Assertion,
        evidence: Sequence[Evidence],
        digest: str,
        authority: str,
        classification: str,
        superseded_only: bool,
    ) -> RecordedDerivation:
        """Write the attempt and its evidence links in one transaction.

        Stored `pending` rather than `staged`: this service concluded something,
        and whether that conclusion becomes a claim is a decision made with the
        curation path's own evidence rules. An extractor that stored its own
        output as staged would be approving its own work.
        """
        derivation_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO claim_derivations (derivation_id, tenant_id, profile, profile_version, status,"
                " applicability, assertion_digest, source_authority, classification)"
                " VALUES (:d, :tid, :p, :v, :st, :app, :dig, :auth, :cls)"
            ),
            {
                "d": derivation_id,
                "tid": ctx.tenant_id,
                "p": profile.name,
                "v": profile.version,
                "st": STATUS_PENDING,
                "app": assertion.applicability,
                "dig": digest,
                "auth": authority,
                "cls": classification,
            },
        )
        for item in evidence:
            await session.execute(
                text(
                    "INSERT INTO derivation_evidence_links (link_id, derivation_id, evidence_kind, signal_id,"
                    " receipt_id, receipt_item_id, reference_id, checkpoint_id, checkpoint_digest,"
                    " source_authority, classification, excerpt)"
                    " VALUES (:l, :d, :k, :sig, :r, :i, :ref, :cid, :cdig, :auth, :cls, :ex)"
                ),
                {
                    "l": uuid.uuid4(),
                    "d": derivation_id,
                    "k": item.kind,
                    "sig": item.signal_id,
                    "r": item.receipt_id,
                    "i": item.receipt_item_id,
                    "ref": item.reference_id,
                    "cid": item.checkpoint_id,
                    "cdig": item.checkpoint_digest,
                    "auth": item.source_authority,
                    "cls": item.classification,
                    "ex": item.excerpt,
                },
            )
        await session.commit()
        return RecordedDerivation(
            derivation_id=derivation_id,
            assertion_digest=digest,
            source_authority=authority,
            classification=classification,
            status=STATUS_PENDING,
            evidence_count=len(evidence),
            superseded_only=superseded_only,
            replayed=False,
        )


def may_promote(recorded: RecordedDerivation) -> bool:
    """Whether this attempt's evidence can support promotion.

    False when every input has been superseded for learning. Both the superseded
    run and its successor happened, so the attempt is kept — but promoting on
    evidence that has been overtaken would canonicalize a conclusion the later
    evidence may already contradict.
    """
    return not recorded.superseded_only


__all__ = [
    "ASSERTION_PREDICATES",
    "EVIDENCE_KINDS",
    "MAX_EXCERPT_CHARS",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_STAGED",
    "Assertion",
    "DerivationProfile",
    "DerivationRefused",
    "DerivationService",
    "Evidence",
    "RecordedDerivation",
    "assertion_digest",
    "may_promote",
    "weakest_authority",
]
