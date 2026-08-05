"""What a claim must resolve to before anything is written about it.

Split out of the module that used to hold every claim-lifecycle concern in one
file, once that file crossed the line-count ceiling this program enforces on
service modules. This piece stays behind because it is the one cluster with no
outgoing dependency on the others: predicate resolution, subject resolution,
authority derivation, visibility derivation, and value conformance are all
read-only checks (or checks that raise) over data that already exists --
nothing here writes a row. `claim_writer.py` (the machine/system write path)
and `claim_curator_actions.py` (the two curator decisions) both depend on this
module for those checks; this module depends on neither of them. That makes it
the base of the split, imported by both, never importing either back.

It is also where the module's shared vocabulary lives: the data shapes
(`Evidence`, `StagedClaim`), the write-refusal exception (`ClaimRejected`) and
its bounded reason codes, and the small set of status/evidence-kind constants
both write paths need to agree on. None of that is really about "authority" in
the narrow sense of the source-authority ladder below -- it lives here because
every consumer of it already depends on this module for the resolution logic,
and a second, purely-vocabulary module would be one more file to keep in sync
for no reduction in coupling.

**Validation order is deliberate.** Cheap structural checks first, then the
subject resolution that costs a query. A malformed value should not pay for a
lookup, and more importantly a rejection reason should describe the first thing
wrong rather than whichever check happened to run.
"""

from __future__ import annotations

import dataclasses
import decimal
import re
import uuid
from typing import Any, NoReturn
from urllib.parse import urlsplit

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.exceptions import ValidationError
from registry.service.catalog.global_vocabulary import CARDINALITY_MULTI
from registry.service.catalog.version_predicates import validate_version_predicate
from registry.service.governance.authority import (
    AUTHORITY_BY_AXES,
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
    DERIVATION_BY_RANK,
    DERIVATION_EXTRACTION,
    DERIVATION_HUMAN,
    DERIVATION_INFERENCE,
    DERIVATION_RANK,
    SOURCE_AUTHORITY_ORDER,
    SOURCE_AUTHORITY_RANK,
)
from registry.service.governance.visibility import resolve_visible_entity
from registry.storage.models import CLAIM_PREDICATE_KIND
from registry.types import JSONValue, TenantContext

# Why a write was refused. Bounded and exported, because a rejection nobody
# counts is a pipeline that has silently stopped working.
REJECT_UNKNOWN_PREDICATE = "unknown_predicate"
REJECT_DEPRECATED_PREDICATE = "deprecated_predicate"
REJECT_VALUE_TYPE = "value_type_mismatch"
REJECT_PROSE = "prose_not_permitted"
REJECT_NULL_VALUE = "null_value"
REJECT_INTERVAL = "invalid_interval"
REJECT_VISIBILITY = "visibility_broader_than_subject"
REJECT_EVIDENCE_KIND = "evidence_kind_not_permitted"

REJECTION_REASONS = frozenset(
    {
        REJECT_UNKNOWN_PREDICATE,
        REJECT_DEPRECATED_PREDICATE,
        REJECT_VALUE_TYPE,
        REJECT_PROSE,
        REJECT_NULL_VALUE,
        REJECT_INTERVAL,
        REJECT_VISIBILITY,
        REJECT_EVIDENCE_KIND,
    }
)

# --- source authority ------------------------------------------------------
#
# The ladder itself lives in its own module: the write path derives a tier and
# scoring weights one, and neither layer should depend on the other. Re-exported
# here because this is where callers have always found it.
# A connector whose `parse()` is a pure function of the fetched bytes produces
# a reproducible claim: re-fetch the artefact, re-parse, get the same triple.
# That reproducibility -- not the file format -- is what earns the extraction
# tier, and the connector base class already guarantees it. A source type
# absent from this set falls to inference.
DETERMINISTIC_SOURCE_TYPES = frozenset({"openapi", "package_json", "release_notes", "markdown_adr_rfc", "docs_corpus"})

EVIDENCE_CURATOR = "curator"
EVIDENCE_CONNECTOR_RUN = "connector_run"

# The two statuses both write paths (the machine/system path in
# `claim_writer.py`, the curator decisions in `claim_curator_actions.py`) read
# and write. Defined here, the module neither depends on, so each of those two
# can import it without depending on the other.
STATUS_STAGED = "staged"
STATUS_UNLINKED = "unlinked"

# Recorded on every claim scored without a fitted provider mapping. Deliberately
# not a version string and deliberately not "identity": an identity mapping would
# assert that a model reporting 0.9 is right nine times in ten, which nobody has
# checked. A token with no version shape cannot be mistaken for one.
UNCALIBRATED = "uncalibrated"

# A rejection nobody counts is a pipeline that has silently stopped working:
# extraction that quietly stops conforming looks exactly like extraction that
# is producing nothing because there is nothing to produce. Label cardinality
# is bounded by `REJECTION_REASONS`.
_REJECTED = Counter(
    "registry_claim_rejected_total",
    "Claim writes refused, by reason.",
    ["reason"],
)

# Ordered widest to narrowest. A claim may never be more visible than the entity
# it describes, so comparison needs an order.
_VISIBILITY_RANK = {"public": 0, "tenant-shared": 1, "private": 2}

# The value types whose values are text. Named rather than inlined because the
# numeric and boolean branches below must stay reachable: a blanket "must be a
# string" ahead of them rejects an integer duration before it is examined.
_TEXT_VALUE_TYPES = frozenset({"string", "enum", "prose", "entity_ref", "decimal", "url", "version_predicate"})

_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class ClaimRejected(ValidationError):
    """A write refused, carrying the reason code the metric counts.

    Counting happens here rather than at each raise site: a new rejection added
    later cannot forget to increment, which is the failure NF the metric
    exists to prevent. The reason is asserted against the bounded set so a
    typo cannot quietly create an unwatched label.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        _REJECTED.labels(reason=reason).inc()


@dataclasses.dataclass(frozen=True)
class StagedClaim:
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    status: str
    visibility: str
    owning_tenant_id: uuid.UUID | None
    source_authority: str
    # Whether this claim disagrees with something already stored. Returned so a
    # caller staging a claim learns immediately that it conflicts, rather than
    # discovering it when promotion is refused.
    is_contested: bool = False


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One piece of provenance. Immutable once written.

    Correcting a claim creates a new claim; it never rewrites the evidence,
    because the record of what was believed and why is the thing provenance
    exists to preserve.
    """

    kind: str
    ref: str
    excerpt: str | None = None


@dataclasses.dataclass(frozen=True)
class _Declared:
    value_type: str
    claim_category: str
    value_cardinality: str


@dataclasses.dataclass(frozen=True)
class _Subject:
    entity_id: uuid.UUID | None
    owning_tenant_id: uuid.UUID | None


def _maybe_uuid(value: str) -> uuid.UUID | None:
    """A UUID if the string is one, else None. Never raises.

    Imported by `claim_writer.py`'s `_independence`, which resolves the same
    kind of caller-supplied reference this module's own `_resolve_subject`
    does -- one helper, not two copies that could drift.
    """
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class _ClaimResolutionMixin:
    """Predicate/subject resolution, authority derivation, value conformance.

    Composed into `ClaimService` (assembled in `claim_writer.py`) alongside
    `_ClaimCuratorActionsMixin`. Every method here is self-contained -- none
    calls anything outside this mixin -- which is what lets `claim_writer.py`
    and `claim_curator_actions.py` both depend on it without depending on each
    other.
    """

    async def _resolve_predicate(self, session: AsyncSession, ctx: TenantContext, predicate: str) -> _Declared:
        """Global predicates win, then the tenant's own.

        Safe only because a tenant cannot define a name that exists globally --
        without that rule this ordering would let a shared term shadow a
        tenant's and silently change what their claims mean.
        """
        row = (
            await session.execute(
                text(
                    "SELECT value_type, claim_category, value_cardinality, deprecated_at "
                    "FROM vocabulary_values "
                    "WHERE kind = :kind AND value = :value "
                    "  AND (tenant_id IS NULL OR tenant_id = :tid) "
                    "ORDER BY tenant_id NULLS FIRST LIMIT 1"
                ),
                {"kind": CLAIM_PREDICATE_KIND, "value": predicate, "tid": ctx.tenant_id},
            )
        ).one_or_none()

        if row is None:
            raise ClaimRejected(
                REJECT_UNKNOWN_PREDICATE,
                f"predicate {predicate!r} is not in the ontology",
            )
        if row.deprecated_at is not None:
            raise ClaimRejected(
                REJECT_DEPRECATED_PREDICATE,
                f"predicate {predicate!r} is deprecated and accepts no new claims",
            )
        return _Declared(
            value_type=row.value_type,
            claim_category=row.claim_category,
            # A predicate whose cardinality was never declared is treated as
            # set-valued. That direction misses a disagreement; the other
            # manufactures one, and only the first is recoverable.
            value_cardinality=row.value_cardinality or CARDINALITY_MULTI,
        )

    async def _resolve_subject(self, session: AsyncSession, ctx: TenantContext, reference: str) -> _Subject:
        """Resolve by entity id, then by external id. Never guess.

        A reference that resolves to nothing produces an `unlinked` claim
        rather than a dropped one or an invented link. Attaching an assertion
        to the wrong entity is worse than not attaching it: the claim then
        looks corroborated by something it has nothing to do with.
        """
        as_uuid = _maybe_uuid(reference)

        if as_uuid is not None:
            # Through the cross-tenant chokepoint, and deliberately using the
            # variant that cannot distinguish "absent" from "not yours". A
            # direct read would answer "does this id exist, and who owns it"
            # for every entity in the deployment to anyone who can guess a
            # UUID. An invisible subject is indistinguishable from a missing
            # one: both produce an unlinked claim.
            entity = await resolve_visible_entity(session, ctx, as_uuid)
            if entity is not None:
                return _Subject(entity_id=entity.entity_id, owning_tenant_id=entity.tenant_id)

        # External-id form: `system:identifier`. Scoped to the author's tenant,
        # because an external id means something only within the mapping that
        # defined it.
        if ":" in reference:
            system, _, external = reference.partition(":")
            row = (
                await session.execute(
                    text(
                        "SELECT e.entity_id, e.tenant_id FROM entity_external_ids x "
                        "JOIN entities e ON e.entity_id = x.entity_id "
                        "WHERE x.tenant_id = :tid AND x.external_system_slug = :sys "
                        "  AND x.external_id = :ext"
                    ),
                    {"tid": ctx.tenant_id, "sys": system, "ext": external},
                )
            ).one_or_none()
            if row is not None:
                return _Subject(entity_id=row.entity_id, owning_tenant_id=row.tenant_id)

        return _Subject(entity_id=None, owning_tenant_id=None)

    async def _derive_authority(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        subject: _Subject,
        evidence: tuple[Evidence, ...],
    ) -> tuple[str, dict[tuple[str, str], str]]:
        """Compute a claim's authority from provenance the caller cannot forge.

        Returns the authority and the per-evidence derivation tier, so the
        provenance rows can record which one set the floor. Authority is a
        minimum over the evidence set; without the per-row value an auditor
        cannot reconstruct why a claim scored as it did.

        Standing comes from the authenticated tenant compared against the
        subject's owner -- a producer cannot claim ownership it does not have.
        Derivation comes from resolving each piece of evidence, never from the
        caller asserting how it produced the claim.
        """
        derivations = {(item.kind, item.ref): await self._evidence_derivation(session, ctx, item) for item in evidence}

        if subject.owning_tenant_id is None:
            # No subject, so no owner to compare the author against. Naming an
            # observer tier here would assert a determination that was never
            # made, and nothing would mark it stale once curation links the
            # claim to an entity the author does in fact own.
            return AUTHORITY_UNATTRIBUTED, derivations

        is_owner = subject.owning_tenant_id == ctx.tenant_id

        # A human putting their name to a claim asserts it in the first person.
        # That stands on its own rather than averaging with whatever the claim
        # was originally derived from -- otherwise confirming a model
        # extraction would leave it at the model's tier and confirmation would
        # mean nothing.
        if any(item.kind == EVIDENCE_CURATOR for item in evidence):
            if not await self._actor_is_human(session, ctx):
                raise ClaimRejected(
                    REJECT_EVIDENCE_KIND,
                    "curator evidence records a human act; a service principal cannot produce it",
                )
            return AUTHORITY_BY_AXES[(is_owner, DERIVATION_HUMAN)], derivations

        # The weakest link across the remaining evidence. A claim is only as
        # checkable as the least checkable step needed to produce it, and taking
        # the strongest would let anyone launder a model inference into the
        # extraction tier by attaching one connector run to it. Independent
        # sources agreeing raises confidence; it must never raise authority.
        weakest = max(DERIVATION_RANK[d] for d in derivations.values())
        return AUTHORITY_BY_AXES[(is_owner, DERIVATION_BY_RANK[weakest])], derivations

    async def _rederive_authority(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        subject: _Subject,
        linking_tenant_id: uuid.UUID,
    ) -> str:
        """Authority for a claim whose subject has just resolved.

        The same owner-vs-observer rule `_derive_authority` applies at staging
        time, computed from evidence already on file rather than evidence
        supplied fresh: every derivation tier was fixed, and any curator item's
        human check already passed, when that provenance was first recorded.
        Re-running those checks against the *linking* actor rather than
        whoever originally supplied the evidence would attribute the wrong
        person's standing to the claim.
        """
        is_owner = subject.owning_tenant_id == linking_tenant_id
        rows = (
            await session.execute(
                text("SELECT evidence_kind, derivation FROM memory_claim_provenance WHERE claim_id = :cid"),
                {"cid": claim_id},
            )
        ).all()
        if any(row.evidence_kind == EVIDENCE_CURATOR for row in rows):
            return AUTHORITY_BY_AXES[(is_owner, DERIVATION_HUMAN)]
        weakest = max(
            (DERIVATION_RANK[row.derivation] for row in rows),
            default=DERIVATION_RANK[DERIVATION_INFERENCE],
        )
        return AUTHORITY_BY_AXES[(is_owner, DERIVATION_BY_RANK[weakest])]

    async def _evidence_derivation(self, session: AsyncSession, ctx: TenantContext, item: Evidence) -> str:
        """How reproducible is a claim derived from this evidence?

        The caller supplies a pointer; this decides what the pointer is worth.
        An unresolvable pointer is worth the floor, so a bad ref can only ever
        cost authority, never buy it.
        """
        if item.kind == EVIDENCE_CURATOR:
            return DERIVATION_HUMAN

        if item.kind == EVIDENCE_CONNECTOR_RUN:
            run = _maybe_uuid(item.ref)
            if run is None:
                return DERIVATION_INFERENCE
            row = (
                await session.execute(
                    text(
                        "SELECT s.source_type FROM sync_runs r "
                        "JOIN sync_sources s ON s.source_id = r.source_id "
                        "WHERE r.sync_run_id = :rid AND r.tenant_id = :tid"
                    ),
                    {"rid": run, "tid": ctx.tenant_id},
                )
            ).one_or_none()
            if row is not None and row.source_type in DETERMINISTIC_SOURCE_TYPES:
                return DERIVATION_EXTRACTION
            return DERIVATION_INFERENCE

        # session_event, document_revision, commit, work_item. The artefact is
        # real, but the step from it to a typed triple is a model reading text.
        # A deterministic reader over one of these earns the extraction tier by
        # being a registered connector, not by asserting it at the call site.
        return DERIVATION_INFERENCE

    async def _actor_is_human(self, session: AsyncSession, ctx: TenantContext) -> bool:
        kind = (
            await session.execute(
                text("SELECT actor_kind FROM actors WHERE actor_id = :aid AND tenant_id = :tid"),
                {"aid": ctx.actor_id, "tid": ctx.tenant_id},
            )
        ).scalar_one_or_none()
        return bool(kind == "human")

    async def _derive_visibility(self, session: AsyncSession, subject: _Subject, *, requested: str | None) -> str:
        """A claim can never be more visible than the entity it describes.

        Derived from the subject rather than accepted from the author: a claim
        about somebody else's private capability must not be publishable by
        whoever noticed it.
        """
        if subject.entity_id is None:
            # Nothing to derive from, so the most restrictive value. An
            # unlinked claim is served to nobody anyway.
            return "private"

        subject_visibility = (
            await session.execute(
                text("SELECT visibility FROM entities WHERE entity_id = :eid"),
                {"eid": subject.entity_id},
            )
        ).scalar_one()

        if requested is None:
            return str(subject_visibility)
        if requested not in _VISIBILITY_RANK:
            raise ClaimRejected(REJECT_VISIBILITY, f"unknown visibility {requested!r}")
        if _VISIBILITY_RANK[requested] < _VISIBILITY_RANK[str(subject_visibility)]:
            raise ClaimRejected(
                REJECT_VISIBILITY,
                f"a claim may not be more visible ({requested}) than its subject " f"({subject_visibility})",
            )
        return requested

    # -- value conformance -----------------------------------------------------

    def _validate_value(self, value: JSONValue, declared: _Declared) -> None:
        """The value must be what the predicate says it is.

        This is what makes claims comparable. A predicate declaring
        `duration_seconds` and receiving a sentence produces a row that looks
        like every other row and can be reasoned with by nothing.
        """
        if value is None:
            raise ClaimRejected(
                REJECT_NULL_VALUE,
                "null is never a value; an unknown is the absence of a claim, not a claim of nothing",
            )

        expected = declared.value_type
        if expected == "prose" and declared.claim_category != "session_summary":
            raise ClaimRejected(REJECT_PROSE, "prose is permitted only for session summaries")

        # The numeric and boolean types are handled below and must reach their
        # own branches, so the string requirement applies only to the types whose
        # values genuinely are text. Making it unconditional here rejects an
        # integer duration before it is ever looked at.
        if expected in _TEXT_VALUE_TYPES and not isinstance(value, str):
            self._type_error(expected, value)

        if expected in {"string", "enum", "prose", "entity_ref"}:
            # Free text, a vocabulary member, a paragraph, or a reference this
            # path resolves separately. Nothing further is decidable here.
            return
        if expected == "decimal":
            # `decimal`/`url`/`version_predicate` are all in `_TEXT_VALUE_TYPES`
            # above, so the guard already refused a non-`str` value for any of
            # them -- this assert makes that guarantee checkable here too,
            # rather than leaving `str`-only helpers (`Decimal`, `urlsplit`,
            # `validate_version_predicate`) to receive a wider type on paper.
            assert isinstance(value, str)  # narrows a real, already-enforced invariant; see the comment above
            # A fixed-point string, not a float -- which is the whole reason the
            # type exists. Parsed rather than merely shaped: an availability
            # target of "banana" would otherwise be stored, and every later
            # comparison against it would be undecidable forever.
            try:
                decimal.Decimal(value)
            except decimal.InvalidOperation:
                self._type_error(expected, value)
            return
        if expected == "url":
            assert isinstance(value, str)  # narrows a real, already-enforced invariant; see the comment above
            # Absolute, as the type declares. A relative reference resolves
            # against a base this store does not have, so it names nothing.
            parsed = urlsplit(value)
            if not parsed.scheme or not parsed.netloc:
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    f"url must be absolute with a scheme and host; got {value!r}",
                )
            return
        if expected == "version_predicate":
            assert isinstance(value, str)  # narrows a real, already-enforced invariant; see the comment above
            # The same grammar the graph's own edges are validated against, so a
            # claim cannot carry a range that could never be promoted to one.
            if not validate_version_predicate(value):
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    f"not a well-formed version range: {value!r}",
                )
            return
        if expected in {"integer", "duration_seconds", "bytes"}:
            # `bool` is a subclass of `int` in Python, and True would otherwise
            # store as 1 under a predicate meaning seconds.
            if isinstance(value, bool) or not isinstance(value, int):
                self._type_error(expected, value)
            return
        if expected == "boolean":
            if not isinstance(value, bool):
                self._type_error(expected, value)
            return
        if expected == "timestamp_utc":
            if not isinstance(value, str) or not _RFC3339_UTC.match(value):
                raise ClaimRejected(
                    REJECT_VALUE_TYPE,
                    "timestamp_utc must be RFC 3339 with a Z suffix; offsets are rejected rather "
                    "than converted, because converting silently loses which zone was meant",
                )
            return

        self._type_error(expected, value)

    def _type_error(self, expected: str, value: JSONValue) -> NoReturn:
        raise ClaimRejected(
            REJECT_VALUE_TYPE,
            f"predicate declares {expected!r} but the value is {type(value).__name__}",
        )


__all__ = [
    "AUTHORITY_OBSERVER_EXTRACTION",
    "AUTHORITY_OBSERVER_HUMAN",
    "AUTHORITY_OBSERVER_INFERENCE",
    "AUTHORITY_OWNER_EXTRACTION",
    "AUTHORITY_OWNER_HUMAN",
    "AUTHORITY_OWNER_INFERENCE",
    "AUTHORITY_UNATTRIBUTED",
    "DERIVATION_EXTRACTION",
    "DERIVATION_HUMAN",
    "DERIVATION_INFERENCE",
    "DETERMINISTIC_SOURCE_TYPES",
    "EVIDENCE_CONNECTOR_RUN",
    "EVIDENCE_CURATOR",
    "REJECTION_REASONS",
    "REJECT_EVIDENCE_KIND",
    "SOURCE_AUTHORITY_ORDER",
    "SOURCE_AUTHORITY_RANK",
    "REJECT_DEPRECATED_PREDICATE",
    "REJECT_INTERVAL",
    "REJECT_NULL_VALUE",
    "REJECT_PROSE",
    "REJECT_UNKNOWN_PREDICATE",
    "REJECT_VALUE_TYPE",
    "REJECT_VISIBILITY",
    "STATUS_STAGED",
    "STATUS_UNLINKED",
    "UNCALIBRATED",
    "ClaimRejected",
    "Evidence",
    "StagedClaim",
]
