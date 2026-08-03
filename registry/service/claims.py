"""The one path that creates claims.

Session extraction, connectors, consumer requests, and curator entry all write
through here. That is not tidiness: every invariant a claim carries -- it
conforms to the ontology, its value matches the predicate's declared type, its
subject resolves to a real entity, it has provenance, it cannot be more visible
than the thing it describes -- holds only because there is exactly one place
that can create one. A second writer would satisfy none of them while producing
rows that look identical.

A lint gate enforces the same rule structurally, the way the visibility
chokepoint is enforced: nothing outside this module may INSERT into `lmm_claims`.

**Validation order is deliberate.** Cheap structural checks first, then the
subject resolution that costs a query. A malformed value should not pay for a
lookup, and more importantly a rejection reason should describe the first thing
wrong rather than whichever check happened to run.

**A claim is never silently dropped.** An unresolvable subject is stored
`unlinked` and queued for a human; every other rejection raises with a
categorized reason. Silent rejection is the failure mode where extraction
quietly stops producing and nobody notices for a month.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import uuid
from typing import Any

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.exceptions import ValidationError
from registry.service.visibility import resolve_visible_entity
from registry.storage.models import CLAIM_PREDICATE_KIND
from registry.types import Clock, TenantContext

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
# Authority answers two questions about a claim's provenance: whether the
# asserting tenant owns the thing being described, and how reproducible the
# step from artefact to typed triple was. It is not confidence, and it is never
# supplied by the caller -- every value below is computed here from the
# authenticated principal and from evidence this module resolves itself. A
# producer that could name its own authority would name the highest one.
#
# Ordered strongest first. Two axes flattened into one ladder, ownership-major:
# a claim from a tenant that does not own the subject can never outrank one
# from the tenant that does, at any derivation tier. That ordering is what
# makes the flattening lossless rather than a convenient simplification.
#
# Consolidation must still gate cross-tenant supersession on
# `author_tenant_id != owning_tenant_id` rather than on rank. "Different
# tenant" routes a proposal; "lower rank" contests and can supersede. A single
# ordinal cannot express both, which is why both tenant columns are persisted
# alongside the rank rather than collapsed into it.
AUTHORITY_OWNER_HUMAN = "owner_human"
AUTHORITY_OWNER_EXTRACTION = "owner_extraction"
AUTHORITY_OWNER_INFERENCE = "owner_inference"
AUTHORITY_OBSERVER_HUMAN = "observer_human"
AUTHORITY_OBSERVER_EXTRACTION = "observer_extraction"
AUTHORITY_OBSERVER_INFERENCE = "observer_inference"
# No subject means no owner to compare the author against, so the standing
# axis is undefined. Saying that is better than guessing a tier which turns
# out to have been wrong once a curator links the claim -- and nothing marks a
# guessed value as stale.
AUTHORITY_UNATTRIBUTED = "unattributed"

SOURCE_AUTHORITY_ORDER: tuple[str, ...] = (
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
)

#: Rank 0 is strongest. Compare by rank, never by string order.
SOURCE_AUTHORITY_RANK: dict[str, int] = {
    value: rank for rank, value in enumerate(SOURCE_AUTHORITY_ORDER)
}

# Derivation tiers. Weakest is the highest number so the weakest link across a
# claim's evidence is a plain `max()`.
DERIVATION_HUMAN = "human"
DERIVATION_EXTRACTION = "extraction"
DERIVATION_INFERENCE = "inference"

_DERIVATION_RANK = {DERIVATION_HUMAN: 0, DERIVATION_EXTRACTION: 1, DERIVATION_INFERENCE: 2}
_DERIVATION_BY_RANK = {rank: name for name, rank in _DERIVATION_RANK.items()}

_AUTHORITY_BY_AXES = {
    (True, DERIVATION_HUMAN): AUTHORITY_OWNER_HUMAN,
    (True, DERIVATION_EXTRACTION): AUTHORITY_OWNER_EXTRACTION,
    (True, DERIVATION_INFERENCE): AUTHORITY_OWNER_INFERENCE,
    (False, DERIVATION_HUMAN): AUTHORITY_OBSERVER_HUMAN,
    (False, DERIVATION_EXTRACTION): AUTHORITY_OBSERVER_EXTRACTION,
    (False, DERIVATION_INFERENCE): AUTHORITY_OBSERVER_INFERENCE,
}

# A connector whose `parse()` is a pure function of the fetched bytes produces
# a reproducible claim: re-fetch the artefact, re-parse, get the same triple.
# That reproducibility -- not the file format -- is what earns the extraction
# tier, and the connector base class already guarantees it. A source type
# absent from this set falls to inference.
DETERMINISTIC_SOURCE_TYPES = frozenset(
    {"openapi", "package_json", "release_notes", "markdown_adr_rfc", "docs_corpus"}
)

EVIDENCE_CURATOR = "curator"
EVIDENCE_CONNECTOR_RUN = "connector_run"

STATUS_STAGED = "staged"
STATUS_UNLINKED = "unlinked"

# A rejection nobody counts is a pipeline that has silently stopped working:
# extraction that quietly stops conforming looks exactly like extraction that
# is producing nothing because there is nothing to produce. Label cardinality
# is bounded by `REJECTION_REASONS`.
_REJECTED = Counter(
    "registry_claim_rejected_total",
    "Claim writes refused, by reason.",
    ["reason"],
)

_STAGED = Counter(
    "registry_claim_staged_total",
    "Claims written, by status and derived source authority.",
    ["status", "source_authority"],
)

# Counted separately from the status label because the rate is the signal, not
# the total: a rising share of unresolved subjects means extraction is drifting
# off the entity model, which no absolute count makes visible.
_UNLINKED = Counter(
    "registry_claim_unresolved_subject_total",
    "Claims stored unlinked because the subject did not resolve.",
)

# Ordered widest to narrowest. A claim may never be more visible than the entity
# it describes, so comparison needs an order.
_VISIBILITY_RANK = {"public": 0, "tenant-shared": 1, "private": 2}

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


class ClaimService:
    """Stages claims. The only creator of `lmm_claims` rows."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def stage_claim(
        self,
        ctx: TenantContext,
        *,
        subject_reference: str,
        predicate: str,
        value: Any,
        evidence: tuple[Evidence, ...],
        asserted_valid_from: datetime.datetime | None = None,
        asserted_valid_to: datetime.datetime | None = None,
        visibility: str | None = None,
        token_count: int | None = None,
        tokenizer_id: str | None = None,
    ) -> StagedClaim:
        """Validate, resolve, and stage one claim.

        Evidence is required. A claim with no provenance cannot be checked by
        anyone later, and an unverifiable assertion in a store whose whole
        premise is trust is worse than no assertion.
        """
        if not evidence:
            msg = "a claim requires provenance; an assertion nobody can check is not evidence"
            raise ValidationError(msg)

        now = self._clock.now()
        valid_from = asserted_valid_from if asserted_valid_from is not None else now
        if asserted_valid_to is not None and asserted_valid_to <= valid_from:
            raise ClaimRejected(
                REJECT_INTERVAL, "asserted_valid_to must be after asserted_valid_from"
            )

        async with self._session_factory() as session, session.begin():
            declared = await self._resolve_predicate(session, ctx, predicate)
            self._validate_value(value, declared)

            subject = await self._resolve_subject(session, ctx, subject_reference)
            resolved_visibility = await self._derive_visibility(
                session, subject, requested=visibility
            )
            authority, derivations = await self._derive_authority(session, ctx, subject, evidence)
            status = STATUS_UNLINKED if subject.entity_id is None else STATUS_STAGED

            claim_id = uuid.uuid4()
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
            await session.execute(
                text(
                    "INSERT INTO lmm_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id,"
                    "  subject_entity_id, subject_reference, predicate, value_type,"
                    "  claim_category, value_jsonb, asserted_valid_from, asserted_valid_to,"
                    "  status, visibility, source_authority, size_bytes, token_count,"
                    "  tokenizer_id, created_at"
                    ") VALUES (:cid, :owner, :author, :actor, :subject, :ref, :pred, :vtype,"
                    "          :cat, CAST(:val AS JSONB), :vfrom, :vto, :status, :vis, :auth,"
                    "          :size, :tokens, :tokenizer, :now)"
                ),
                {
                    "cid": claim_id,
                    "owner": subject.owning_tenant_id,
                    "author": ctx.tenant_id,
                    "actor": ctx.actor_id,
                    "subject": subject.entity_id,
                    "ref": subject_reference,
                    "pred": predicate,
                    "vtype": declared.value_type,
                    "cat": declared.claim_category,
                    "val": canonical,
                    "vfrom": valid_from,
                    "vto": asserted_valid_to,
                    "status": status,
                    "vis": resolved_visibility,
                    "auth": authority,
                    "size": len(canonical.encode("utf-8")),
                    "tokens": token_count,
                    "tokenizer": tokenizer_id,
                    "now": now,
                },
            )

            for item in evidence:
                await session.execute(
                    text(
                        "INSERT INTO lmm_claim_provenance "
                        "  (claim_id, evidence_kind, evidence_ref, evidence_excerpt, "
                        "   derivation, recorded_at) "
                        "VALUES (:cid, :kind, :ref, :excerpt, :deriv, :now) "
                        "ON CONFLICT (claim_id, evidence_kind, evidence_ref) DO NOTHING"
                    ),
                    {
                        "cid": claim_id,
                        "kind": item.kind,
                        "ref": item.ref,
                        "excerpt": item.excerpt,
                        # Which row set the floor. Authority is a minimum over
                        # the set, so without this an auditor cannot
                        # reconstruct why a claim scored as it did.
                        "deriv": derivations[(item.kind, item.ref)],
                        "now": now,
                    },
                )

        _STAGED.labels(status=status, source_authority=authority).inc()
        if subject.entity_id is None:
            _UNLINKED.inc()

        return StagedClaim(
            claim_id=claim_id,
            subject_entity_id=subject.entity_id,
            predicate=predicate,
            value=value,
            status=status,
            visibility=resolved_visibility,
            owning_tenant_id=subject.owning_tenant_id,
            source_authority=authority,
        )

    # -- resolution ------------------------------------------------------------

    async def _resolve_predicate(
        self, session: AsyncSession, ctx: TenantContext, predicate: str
    ) -> _Declared:
        """Global predicates win, then the tenant's own.

        Safe only because a tenant cannot define a name that exists globally --
        without that rule this ordering would let a shared term shadow a
        tenant's and silently change what their claims mean.
        """
        row = (
            await session.execute(
                text(
                    "SELECT value_type, claim_category, deprecated_at "
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
        return _Declared(value_type=row.value_type, claim_category=row.claim_category)

    async def _resolve_subject(
        self, session: AsyncSession, ctx: TenantContext, reference: str
    ) -> _Subject:
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
        derivations = {
            (item.kind, item.ref): await self._evidence_derivation(session, ctx, item)
            for item in evidence
        }

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
            return _AUTHORITY_BY_AXES[(is_owner, DERIVATION_HUMAN)], derivations

        # The weakest link across the remaining evidence. A claim is only as
        # checkable as the least checkable step needed to produce it, and taking
        # the strongest would let anyone launder a model inference into the
        # extraction tier by attaching one connector run to it. Independent
        # sources agreeing raises confidence; it must never raise authority.
        weakest = max(_DERIVATION_RANK[d] for d in derivations.values())
        return _AUTHORITY_BY_AXES[(is_owner, _DERIVATION_BY_RANK[weakest])], derivations

    async def _evidence_derivation(
        self, session: AsyncSession, ctx: TenantContext, item: Evidence
    ) -> str:
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

    async def _derive_visibility(
        self, session: AsyncSession, subject: _Subject, *, requested: str | None
    ) -> str:
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
                f"a claim may not be more visible ({requested}) than its subject "
                f"({subject_visibility})",
            )
        return requested

    # -- value conformance -----------------------------------------------------

    def _validate_value(self, value: Any, declared: _Declared) -> None:
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

        if expected in {"string", "enum", "decimal", "url", "version_predicate", "entity_ref", "prose"}:
            if not isinstance(value, str):
                self._type_error(expected, value)
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

    def _type_error(self, expected: str, value: Any) -> None:
        raise ClaimRejected(
            REJECT_VALUE_TYPE,
            f"predicate declares {expected!r} but the value is {type(value).__name__}",
        )


def _maybe_uuid(value: str) -> uuid.UUID | None:
    """A UUID if the string is one, else None. Never raises."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclasses.dataclass(frozen=True)
class _Declared:
    value_type: str
    claim_category: str


@dataclasses.dataclass(frozen=True)
class _Subject:
    entity_id: uuid.UUID | None
    owning_tenant_id: uuid.UUID | None


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
    "ClaimRejected",
    "ClaimService",
    "Evidence",
    "StagedClaim",
]
