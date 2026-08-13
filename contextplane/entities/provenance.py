"""What every governed assertion must be able to say about where it came from.

An assertion with no provenance is a statement nobody is accountable for. One
with *partial* provenance is worse, because it looks accountable: a row saying
`authority = 'canonical_owner'` and nothing else reads as authoritative while
carrying no evidence that anyone can check.

So this module states the whole requirement in one place, as a record that
refuses to exist incomplete. The database is deliberately more permissive than
this — several of these columns are nullable there — because the table also holds
rows from paths that predate governed assertions, and tightening the schema would
mean rewriting history. The application requirement is stricter and lives here,
which is the layer that can distinguish "this assertion is governed" from "this
row exists".

**Four authorities, and they are not a ranking of confidence.** `canonical_owner`
is the party entitled to state the fact; `external_authority` is a system whose
say-so is accepted by agreement; `observed` is something the platform saw; and
`derived` is something inferred. Only a derived assertion carries a confidence,
and that is not a technicality: a confidence attached to what a canonical owner
stated invites a reader to discount a fact that was never inferred in the first
place.

**Freshness and revocation answer different questions.** Freshness asks whether
the assertion is still current; revocation asks whether it was withdrawn. A
revoked assertion has both a reference and a time, or it cannot be audited and
cannot be ordered against the assertions it invalidates.

**Provenance is immutable.** There is no correction path here on purpose. An
assertion whose evidence changed is a new assertion superseding the old one, and
`entities/assertions.py` is the only module permitted to write the table so that
this stays true — a caller that could rewrite provenance could make a claim
appear supported by evidence that never said it.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

#: Who is entitled to have said this, checked against the same set the database
#: constrains so the two cannot drift.
CANONICAL_OWNER: Final = "canonical_owner"
EXTERNAL_AUTHORITY: Final = "external_authority"
OBSERVED: Final = "observed"
DERIVED: Final = "derived"

AUTHORITIES: Final[frozenset[str]] = frozenset({CANONICAL_OWNER, EXTERNAL_AUTHORITY, OBSERVED, DERIVED})

FRESH: Final = "fresh"
STALE: Final = "stale"
EXPIRED: Final = "expired"
REVOKED: Final = "revoked"

FRESHNESS_STATES: Final[frozenset[str]] = frozenset({FRESH, STALE, EXPIRED, REVOKED})


class IncompleteProvenance(ValueError):
    """A governed assertion's provenance is missing something it must state.

    A `ValueError` rather than a domain exception with an HTTP mapping: reaching
    this is a programming error in a writer, not a caller supplying bad input.
    Every field here is something the writing path knows or can decide; none of it
    is typed in by a user.
    """


@dataclasses.dataclass(frozen=True)
class AssertionProvenance:
    """The complete provenance one governed assertion carries.

    Frozen, and validated on construction rather than at the insert. A writer
    holding one of these has already been told whether it is complete, so the
    check cannot be skipped by a path that builds its parameters inline.

    The three times are kept apart because they answer different questions: when
    it happened, when we saw it, when we stored it. Collapsing them makes
    staleness unmeasurable — you cannot tell a fact that is old from one that
    arrived late.
    """

    tenant_id: uuid.UUID
    source_system: str
    source_namespace: str
    ingested_at: datetime.datetime
    authority: str
    freshness_state: str
    produced_by: str
    validating_profile_revision_id: uuid.UUID

    external_record_id: str | None = None
    external_revision: str | None = None
    event_time: datetime.datetime | None = None
    observed_at: datetime.datetime | None = None
    derivation_method: str | None = None
    derivation_profile: str | None = None
    expires_at: datetime.datetime | None = None
    revocation_ref: str | None = None
    revoked_at: datetime.datetime | None = None
    confidence: float | None = None
    extension_set_digest: str | None = None
    approved_by: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("source_system", self.source_system),
            ("source_namespace", self.source_namespace),
            ("produced_by", self.produced_by),
        ):
            if not value or not value.strip():
                msg = f"{field} is required; an assertion nobody is named for cannot be held to account"
                raise IncompleteProvenance(msg)

        if self.authority not in AUTHORITIES:
            msg = f"unknown authority {self.authority!r}; legal: {', '.join(sorted(AUTHORITIES))}"
            raise IncompleteProvenance(msg)
        if self.freshness_state not in FRESHNESS_STATES:
            msg = f"unknown freshness state {self.freshness_state!r}; legal: {', '.join(sorted(FRESHNESS_STATES))}"
            raise IncompleteProvenance(msg)

        # The database permits a NULL validating revision because the table also
        # holds rows written before profiles existed. A *governed* assertion must
        # name the revision that accepted it, or nothing can later say which rules
        # it was judged against.
        if self.validating_profile_revision_id is None:
            msg = "a governed assertion must name the profile revision that validated it"
            raise IncompleteProvenance(msg)

        self._check_derivation()
        self._check_revocation()
        self._check_external_identity()

    def _check_derivation(self) -> None:
        """A derived assertion says how it was derived and how much it is trusted.

        Both directions are refused. A derived assertion with no method cannot be
        reproduced or challenged; a non-derived one carrying a confidence invites
        a reader to discount something that was stated rather than inferred, which
        the database refuses too.
        """
        if self.authority == DERIVED:
            if not self.derivation_method:
                msg = "a derived assertion states its derivation method, or it cannot be reproduced or challenged"
                raise IncompleteProvenance(msg)
            if self.confidence is None:
                msg = "a derived assertion states its confidence; an inference with no confidence reads as a fact"
                raise IncompleteProvenance(msg)
            if not 0.0 <= self.confidence <= 1.0:
                msg = f"confidence {self.confidence} is outside 0..1"
                raise IncompleteProvenance(msg)
        elif self.confidence is not None:
            msg = (
                f"only a derived assertion carries a confidence; {self.authority!r} stated this rather than "
                "inferring it, and a confidence would invite a reader to discount it"
            )
            raise IncompleteProvenance(msg)

    def _check_revocation(self) -> None:
        """A revocation has both a reference and a time, or neither.

        A reference with no time cannot be ordered against the assertions it
        invalidates; a time with no reference cannot be audited. The database says
        the same, and this says it earlier and by name.
        """
        if (self.revoked_at is None) != (self.revocation_ref is None):
            msg = (
                "a revocation carries both a reference and a time: one without the other is either unauditable "
                "or unorderable against what it invalidates"
            )
            raise IncompleteProvenance(msg)
        if self.freshness_state == REVOKED and self.revoked_at is None:
            msg = "an assertion marked revoked records when it was revoked and by what reference"
            raise IncompleteProvenance(msg)

    def _check_external_identity(self) -> None:
        """An externally-authored assertion identifies the record it came from.

        `external_authority` means a system's say-so is being accepted. Which
        record of theirs, at which revision, is the whole of what makes that
        checkable later — without it the acceptance cannot be re-verified against
        the source, only believed.
        """
        if self.authority != EXTERNAL_AUTHORITY:
            return
        if not self.external_record_id:
            msg = (
                "an external-authority assertion names the upstream record it came from, or the acceptance "
                "cannot be re-verified against its source"
            )
            raise IncompleteProvenance(msg)
        if not self.external_revision:
            msg = (
                "an external-authority assertion names the upstream revision it came from; without it, two "
                "different upstream states are indistinguishable here"
            )
            raise IncompleteProvenance(msg)


__all__ = [
    "AUTHORITIES",
    "CANONICAL_OWNER",
    "DERIVED",
    "EXPIRED",
    "EXTERNAL_AUTHORITY",
    "FRESH",
    "FRESHNESS_STATES",
    "OBSERVED",
    "REVOKED",
    "STALE",
    "AssertionProvenance",
    "IncompleteProvenance",
]
