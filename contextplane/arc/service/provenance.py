"""Per-field provenance and the transient candidate-semantics checks a
`PATCH {PV}` runs before persisting them.

Each semantic field instance in a proposal has exactly one valid
`field_provenance_v1` record, in one of three mutually exclusive shapes:
`source_backed` (an authorized source excerpt backs the value),
`human_judgment` (the authenticated caller of the `PATCH` is asserting it),
or `server_derived` (the server computed it, naming the profile that did).
The three column groups are mutually exclusive by construction -- a field
cannot be simultaneously backed by a source and asserted by judgment,
because then neither claim is falsifiable against the other.

**Per-field, not per-document.** The primary key is `(proposal_id,
proposal_version, field_path)`. `edit()` upserts exactly the entries a
caller supplies in one `PATCH`; a caller touching only `$.directives[2]`
does not, and must not, disturb the already-recorded provenance for every
other field on the same version. `queries/provenance.py`'s own docstring
states the same rule from the SQL side.

**The author is never client-supplied.** For a `human_judgment` field, the
author is the authenticated `{issuer, subject}` that called `PATCH`,
written server-side in the same transaction. Appendix A.6's own text names
`author` as a field a request can never carry; this module treats that as
a defense the *service* also enforces, not only the closed wire schema
upstream of it -- `_assert_no_injected_actor_fields` runs against the raw
entry mapping a caller of this module supplies, independent of whichever
wire type produced it, so a caller that reaches this service some other
way than the one route wired today still cannot smuggle an actor field
through.

**Where the candidate document lives.** `ProposalPatchRequest.semantics`
names the full candidate `arc_artifact_semantics_v1` document.
`arc_authoring_proposal_versions.semantics` (a nullable `JSONB` column) is
where `edit()` persists it, once `validate_candidate_semantics` accepts the
given payload -- closed-schema, duplicate-identifier, and ambiguous-
selector checks, exactly as before. Persistence is what lets a later,
separate call with no body (`POST {PV}/validate`) revalidate *that same*
candidate rather than a transient one that existed only for the duration
of one `PATCH` -- see `ProvenanceService.revalidate_stored`'s own docstring
for how it reads the persisted document back and re-runs the same checks
against it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas import authoring_profiles
from contextplane.arc.schemas.authoring_profiles import AuthoringProfileError
from contextplane.arc.service import audit_outbox
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.proposal import ProposalStateConflict
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.service.queries import provenance as queries
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, RegistryError
from contextplane.types import Clock

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProvenanceInvalid(RegistryError):
    """A `field_provenance_v1` entry's shape is missing or contradictory
    (`arc_provenance_invalid`, 422)."""


class ActorNotCallerSupplied(RegistryError):
    """A request carried a reserved actor field the caller must never
    supply (`arc_actor_not_caller_supplied`, 400)."""


class SemanticsValidationFailed(RegistryError):
    """The candidate semantics failed closed-schema, duplicate-identifier,
    or ambiguous-selector validation (`arc_proposal_validation_failed`,
    422)."""


# ---------------------------------------------------------------------------
# The three mutually exclusive column groups (Appendix B.2). Keyed by the
# wire `FieldProvenanceInput` field names, which is why this table is not
# the same as `authoring_profiles._PROVENANCE_GROUPS`: that one governs the
# separately-shaped `arc_field_provenance_v1` canonical profile (different
# field names, no `source_evidence_id`, a `quoted_excerpt_digest` instead
# of `excerpt_digest`, and a looser forbidden set) used for the later
# review-package digest computation -- a related but distinct rule over a
# distinct shape, not a second copy of this one.
# ---------------------------------------------------------------------------

_PROVENANCE_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "source_backed": {
        "required": ("source_evidence_id", "source_anchor", "excerpt_digest"),
        "forbidden": ("author_role", "derivation_profile"),
    },
    "human_judgment": {
        "required": ("author_role",),
        "forbidden": ("source_evidence_id", "source_anchor", "excerpt_digest", "derivation_profile"),
    },
    "server_derived": {
        "required": ("derivation_profile",),
        "forbidden": ("source_evidence_id", "source_anchor", "excerpt_digest", "author_role"),
    },
}

# Actor identity fields no `field_provenance` entry may carry from a
# caller, regardless of which wire type produced the mapping this module
# receives. `author`/`author_issuer`/`author_subject` are always
# server-derived from the authenticated `PATCH` caller; the rest mirror
# `arc_authoring_enums.RESERVED_ACTOR_FIELDS` so a defense at this layer
# does not silently narrow the wire-layer rule.
_FORBIDDEN_ACTOR_KEYS: frozenset[str] = frozenset(
    {
        "author",
        "author_issuer",
        "author_subject",
        "actor_id",
        "actor_issuer",
        "actor_subject",
        "caller_issuer",
        "caller_subject",
        "authenticated_issuer",
        "authenticated_subject",
        "acting_principal",
        "role",
        "roles",
        "on_behalf_of",
    }
)


def _entry_dict(entry: Mapping[str, Any] | Any) -> dict[str, Any]:  # noqa: ANN401 - accepts a pydantic model or a plain mapping
    if isinstance(entry, Mapping):
        return dict(entry)
    return dict(entry.model_dump())


def _assert_no_injected_actor_fields(entry: Mapping[str, Any]) -> None:
    hit = _FORBIDDEN_ACTOR_KEYS & set(entry.keys())
    if hit:
        field_path = entry.get("field_path", "<unknown>")
        msg = (
            f"field_provenance entry for {field_path!r} carries caller-supplied actor field(s) "
            f"{sorted(hit)!r}; the author is always the authenticated caller of the PATCH, never "
            "supplied in the request body"
        )
        raise ActorNotCallerSupplied(msg)


def _check_conditional(entry: Mapping[str, Any]) -> None:
    field_path = entry.get("field_path", "<unknown>")
    provenance_class = entry.get("provenance_class")
    spec = _PROVENANCE_GROUPS.get(provenance_class)  # type: ignore[arg-type]
    if spec is None:
        msg = f"field_path {field_path!r}: unknown provenance_class {provenance_class!r}"
        raise ProvenanceInvalid(msg)
    for name in spec["required"]:
        if entry.get(name) is None:
            msg = f"field_path {field_path!r}: {provenance_class} requires {name!r} to be set"
            raise ProvenanceInvalid(msg)
    for name in spec["forbidden"]:
        if entry.get(name) is not None:
            msg = f"field_path {field_path!r}: {provenance_class} forbids {name!r} from being set"
            raise ProvenanceInvalid(msg)


# ---------------------------------------------------------------------------
# Candidate-semantics checks a PATCH runs on the given payload. See this
# module's own docstring for why these validate the given object rather
# than a persisted one.
# ---------------------------------------------------------------------------


def validate_candidate_semantics(semantics: Mapping[str, Any], *, stored: bool = False) -> None:
    """Closed-schema and ambiguous-selector checks.

    `stored` picks which half of the version split applies, and the two
    callers genuinely differ. An edit in progress is a new write, so it is
    held to the active profile and a candidate still spelled the old way is
    refused -- that refusal is the cutover. Re-validating a persisted
    version is the opposite: it was written under whatever profile was
    active at the time, so it is checked under the version it declares,
    and holding it to today's would fail every row written before the
    rename for saying exactly what it was supposed to say.

    Reuses `authoring_profiles`' own validators for the
    closed-schema half rather than re-declaring the profile's field set a
    second time -- and that reuse already gives duplicate-identifier
    rejection for free: `directives`/`applicability` are both declared
    `x-array-kind: ordered` with `directive_id`/`rule_id` as the order key,
    so the profile validator's own "strictly ascending" check refuses any
    submission containing two equal ids, wherever in the list they sit
    (two equal values can never both satisfy strict ascending order, so a
    second, separate duplicate-scan here would only ever run on input the
    profile validator had already accepted as duplicate-free -- dead code
    checking for something that can no longer be true). The one check
    below is not redundant: two *different* rule_ids can carry the exact
    same selector, which the ordering check has no reason to reject and
    the profile's per-object schema cannot express either (it validates
    one rule's shape, not uniqueness of the selector across every rule in
    the list).

    Capability- and domain-visibility validation (this task's own contract
    text: "validates target tenant/capability/domain visibility") is not
    implemented here: this module's callers have no capability/domain
    existence registry in scope to check against, only
    `ArcAuthorizationService.visible_capability_ids`, which answers "can
    *this caller* see it", not "does this deployment's registry recognize
    it" -- a real, separate check this task does not have the tools to
    build correctly. Left as a residual gap rather than a guess.
    """
    obj = dict(semantics)
    try:
        if stored:
            authoring_profiles.canonicalize_stored(obj)
        else:
            authoring_profiles.validate_artifact_semantics_v2(obj)
    except AuthoringProfileError as exc:
        raise SemanticsValidationFailed(str(exc)) from exc

    selectors_seen: dict[tuple[Any, ...], str] = {}
    for rule in obj.get("applicability") or []:
        rule_id = str(rule.get("rule_id"))
        selector = _selector_tuple(rule)
        if selector in selectors_seen:
            msg = (
                f"applicability rules {selectors_seen[selector]!r} and {rule_id!r} share an identical "
                "selector, which is ambiguous: nothing distinguishes which one applies"
            )
            raise SemanticsValidationFailed(msg)
        selectors_seen[selector] = rule_id


def _selector_tuple(rule: Mapping[str, Any]) -> tuple[Any, ...]:
    def _frozen(value: Any) -> Any:  # noqa: ANN401 - normalizes a JSON list/None into a hashable key component
        return frozenset(value) if isinstance(value, list) else value

    return (
        rule.get("scope"),
        rule.get("target_tenant_id"),
        _frozen(rule.get("capability_ids")),
        _frozen(rule.get("capability_labels")),
        _frozen(rule.get("domain_ids")),
        _frozen(rule.get("intent_kinds")),
        _frozen(rule.get("action_classes")),
        _frozen(rule.get("environments")),
        _frozen(rule.get("data_sensitivity_tiers")),
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FieldProvenanceRecord:
    field_path: str
    provenance_class: str
    source_evidence_id: uuid.UUID | None
    source_anchor: str | None
    excerpt_digest: str | None
    author_role: str | None
    derivation_profile: str | None
    author_issuer: str | None
    author_subject: str | None


@dataclasses.dataclass(frozen=True)
class ValidationIssue:
    field_path: str
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[ValidationIssue, ...]


def _record(row: queries.FieldProvenanceRow) -> FieldProvenanceRecord:
    return FieldProvenanceRecord(
        field_path=row.field_path,
        provenance_class=row.provenance_class,
        source_evidence_id=row.source_evidence_id,
        source_anchor=row.source_anchor,
        excerpt_digest=row.excerpt_digest,
        author_role=row.author_role,
        derivation_profile=row.derivation_profile,
        author_issuer=row.author_issuer,
        author_subject=row.author_subject,
    )


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProvenanceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    async def edit(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        semantics: Mapping[str, Any] | None = None,
        entries: Sequence[Any],
    ) -> tuple[FieldProvenanceRecord, ...]:
        """Validate and persist one `PATCH`'s candidate semantics and
        `field_provenance` entries, atomically.

        Legal only while the version is `open` -- matching `AvailableAction.
        EDIT`'s own state. `semantics` is optional here only so a caller
        with nothing new to say about the candidate can still edit
        `field_provenance` alone; the real `PATCH {PV}` route always
        supplies both, since `ProposalPatchRequest.semantics` is a required
        wire field. Every check -- the candidate's own closed-schema and
        ambiguous-selector rules, then every entry's conditional shape --
        runs before a single write happens, so a `PATCH` that fails on
        either half writes neither: not a stale candidate beside fresh
        provenance, and not the reverse.
        """
        semantics_obj = dict(semantics) if semantics is not None else None
        if semantics_obj is not None:
            validate_candidate_semantics(semantics_obj)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await self._authorize_write(session, ctx, proposal_id, proposal_version)

            normalized = [_entry_dict(entry) for entry in entries]
            for entry in normalized:
                _assert_no_injected_actor_fields(entry)
                _check_conditional(entry)

            if semantics_obj is not None:
                await proposal_queries.update_semantics(
                    session, proposal_id=proposal_id, proposal_version=proposal_version, semantics=semantics_obj
                )

            results: list[FieldProvenanceRecord] = []
            for entry in normalized:
                provenance_class = entry["provenance_class"]
                author_issuer = ctx.oidc_issuer if provenance_class == "human_judgment" else None
                author_subject = ctx.oidc_subject if provenance_class == "human_judgment" else None
                await queries.upsert_field_provenance(
                    session,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    field_path=entry["field_path"],
                    provenance_class=provenance_class,
                    source_evidence_id=entry.get("source_evidence_id"),
                    source_anchor=entry.get("source_anchor"),
                    excerpt_digest=entry.get("excerpt_digest"),
                    author_issuer=author_issuer,
                    author_subject=author_subject,
                    author_role=entry.get("author_role"),
                    derivation_profile=entry.get("derivation_profile"),
                    created_at=now,
                )
                results.append(
                    FieldProvenanceRecord(
                        field_path=entry["field_path"],
                        provenance_class=provenance_class,
                        source_evidence_id=entry.get("source_evidence_id"),
                        source_anchor=entry.get("source_anchor"),
                        excerpt_digest=entry.get("excerpt_digest"),
                        author_role=entry.get("author_role"),
                        derivation_profile=entry.get("derivation_profile"),
                        author_issuer=author_issuer,
                        author_subject=author_subject,
                    )
                )

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_FIELD_PROVENANCE_UPDATED,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "field_paths": sorted(entry["field_path"] for entry in normalized),
                },
            )
            return tuple(results)

    async def list_for_version(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> tuple[FieldProvenanceRecord, ...]:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            rows = await queries.load_field_provenance(session, proposal_id, proposal_version)
        return tuple(_record(row) for row in rows)

    async def revalidate_stored(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> ValidationResult:
        """Re-run every check against what is currently persisted for this
        version: the candidate `semantics` document (if a `PATCH` has ever
        written one) and every `field_provenance` row.

        Both halves are read fresh from `arc_authoring_proposal_versions`/
        `arc_authoring_field_provenance` on this call, not carried over
        from whatever was true when a prior `PATCH` wrote them -- this
        proves the persisted rows still hold the invariant now, not merely
        that they held it once. A version with no candidate yet (`semantics
        IS NULL`) has nothing to re-check on that half, matching `edit()`'s
        own treatment of an absent candidate.

        Authorization is write-level, matching `edit()` and matching what
        `available_actions` already promises: `validate` is only listed
        there when the caller can write, so the check here must agree.
        """
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
            rows = await queries.load_field_provenance(session, proposal_id, proposal_version)

        errors: list[ValidationIssue] = []
        if version.semantics is not None:
            try:
                validate_candidate_semantics(version.semantics, stored=True)
            except SemanticsValidationFailed as exc:
                errors.append(ValidationIssue(field_path="$", code="arc_proposal_validation_failed", message=str(exc)))

        for row in rows:
            entry = {
                "field_path": row.field_path,
                "provenance_class": row.provenance_class,
                "source_evidence_id": row.source_evidence_id,
                "source_anchor": row.source_anchor,
                "excerpt_digest": row.excerpt_digest,
                "author_role": row.author_role,
                "derivation_profile": row.derivation_profile,
            }
            try:
                _check_conditional(entry)
            except ProvenanceInvalid as exc:
                errors.append(
                    ValidationIssue(field_path=row.field_path, code="arc_provenance_invalid", message=str(exc))
                )
        return ValidationResult(valid=not errors, errors=tuple(errors))

    async def _authorize_write(
        self, session: AsyncSession, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> None:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
        if version is None:
            raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
        family = await proposal_queries.load_family(session, version.artifact_id)
        if family is None:
            raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
        self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
        if version.state != "open":
            msg = f"proposal version {proposal_id}/{proposal_version} is not open for editing (state={version.state!r})"
            raise ProposalStateConflict(msg)


__all__ = [
    "ActorNotCallerSupplied",
    "FieldProvenanceRecord",
    "ProvenanceInvalid",
    "ProvenanceService",
    "SemanticsValidationFailed",
    "ValidationIssue",
    "ValidationResult",
    "validate_candidate_semantics",
]
