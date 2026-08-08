"""The resolution transaction: one atomic `REPEATABLE READ` unit of work.

Everything a `resolve_context` call does happens here, in one transaction, in
one order. That is not tidiness -- each part of the ordering is load-bearing:

1. **Replay check first**, before touching the signer key. An exact retry is
   answered from the receipt it already produced; it must not re-accept an
   outstanding attestation or consume a second challenge, and it must keep
   working after the key that signed the original was revoked.
2. **Signer key locked `FOR SHARE`**, so a revocation committing mid-flight
   cannot leave a receipt verified against a key that was already gone.
3. **Challenge locked and validated**, so two parallel resolutions cannot
   both consume it.
4. **Every read from one snapshot at one `as_of`.** `REPEATABLE READ` gives
   the snapshot; passing a single `as_of` everywhere gives the logical
   equivalent for time-dependent predicates. Without both, a directive could
   be selected under one instant and its obligation evaluated under another.
5. **Status decided once**, then receipt, selected rows, event, head,
   challenge consumption, audit -- all before the single commit.

A serialization failure retries the whole transaction with the *same*
preallocated identifiers, so a retry produces one receipt rather than a
second one.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import hashlib
import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc import metrics
from contextplane.arc.schemas.canonical import CanonicalizationError
from contextplane.arc.service import audit_outbox
from contextplane.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    AttestationVerificationError,
    ManifestClaims,
)
from contextplane.arc.service.bundle import ContextBundle, assemble
from contextplane.arc.service.challenge import ChallengeService, ChallengeValidationError
from contextplane.arc.service.receipt import (
    ReceiptProvenance,
    ReceiptService,
    ReplayEnvelope,
    SelectedDirective,
    SelectedRevision,
    preallocate_receipt_id,
)
from contextplane.arc.service.selection import (
    IntegrityAssessor,
    ScopedDirective,
    SelectionInput,
    SelectionResult,
    select_and_verify,
)
from contextplane.arc.types import (
    ArcRequestContext,
    ResolutionStatus,
    TaskManifest,
    parse_action_class,
    parse_task_kind,
)
from contextplane.audit import actions
from contextplane.exceptions import RegistryError
from contextplane.types import Clock

_log = logging.getLogger(__name__)

# Postgres reports a serialization failure or deadlock with these SQLSTATEs.
# Both mean "this transaction lost a race and may succeed if retried",
# which is a different thing from a constraint violation -- retrying that
# would fail identically every time.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

MAX_RESOLUTION_ATTEMPTS = 3

BLOCKED_MANIFEST_UNVERIFIED = "blocked_manifest_unverified"

# Governed content that will not canonicalize. Bounded like every other
# reason code: the caller learns it cannot be rendered, not what was wrong
# with it, since the content itself may be attacker-influenced.
BLOCKED_UNRENDERABLE_CONTENT = "blocked_unrenderable_content"


class ManifestUnverified(RegistryError):
    """No trusted, consumable attestation backs this request.

    Distinct from a `blocked` outcome, and the distinction is the point. A
    blocked resolution was properly authenticated and produces a receipt
    saying why it was blocked. This produces no receipt at all: there was
    never a trustworthy request to record.
    """


class IdempotencyConflict(RegistryError):
    """An attestation ID was reused with a different manifest.

    Never resolved automatically. The caller reused a key that identifies
    one request for what is semantically a different one, which is exactly
    what an idempotency key exists to catch.
    """


@dataclasses.dataclass(frozen=True)
class ResolutionOutcome:
    """What `resolve` returns.

    `replayed` distinguishes a fresh resolution from an exact retry served
    from the retained original. The caller needs it: a replay grants no new
    action and consumed no challenge, so treating the two identically would
    let a retry look like fresh authorization.
    """

    receipt_id: uuid.UUID
    status: ResolutionStatus
    bundle: ContextBundle | None
    replayed: bool = False
    attempts: int = 1


@dataclasses.dataclass(frozen=True)
class ResolutionRequest:
    """One `resolve_context` call, already parsed and authenticated."""

    ctx: ArcRequestContext
    host_id: str
    manifest: ManifestClaims
    envelope: AttestationEnvelope
    manifest_fingerprint: str
    candidates: SelectionInput
    budget_limit_bytes: int
    selected_revisions: tuple[SelectedRevision, ...] = ()
    selected_directives: tuple[SelectedDirective, ...] = ()


def _is_retryable(exc: BaseException) -> bool:
    """Whether Postgres said this transaction lost a race.

    Matched on SQLSTATE rather than message text: messages are localized and
    change between versions, and a substring match would either miss real
    serialization failures or retry constraint violations forever.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES


#: Total over `ResolutionStatus`, deliberately. Written as a mapping rather
#: than a conditional so that adding a status without deciding how it is
#: audited raises a `KeyError` on the first resolution that produces it,
#: rather than silently reporting the new state as one of the old ones.
_CONTEXT_EVENT_BY_STATUS: dict[ResolutionStatus, str] = {
    ResolutionStatus.READY: actions.ARC_CONTEXT_RESOLVED,
    ResolutionStatus.DEGRADED: actions.ARC_CONTEXT_DEGRADED,
    ResolutionStatus.BLOCKED: actions.ARC_CONTEXT_BLOCKED,
}


class ResolutionService:
    """Orchestrates the single atomic resolution transaction.

    **`select_and_verify`, not bare `select`.** `_select` below calls the
    §6.3 integrity-aware wrapper (`contextplane.arc.service.selection.
    select_and_verify`), not the pure `select` this class used before --
    `corpus.py`'s own candidate filtering happens before this transaction
    even opens, and this is the authoritative recheck at the actual
    serving instant. See that function's own docstring.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        attestation: AttestationService,
        challenges: ChallengeService,
        receipts: ReceiptService,
        provenance: ReceiptProvenance,
        clock: Clock,
        integrity: IntegrityAssessor,
        seal: Callable[[uuid.UUID, ContextBundle], ReplayEnvelope],
    ) -> None:
        self._session_factory = session_factory
        self._attestation = attestation
        self._challenges = challenges
        self._receipts = receipts
        self._provenance = provenance
        self._clock = clock
        self._integrity = integrity
        self._seal = seal

    async def resolve(self, request: ResolutionRequest, *, as_of: datetime.datetime | None = None) -> ResolutionOutcome:
        """Run the resolution, retrying the whole transaction on a lost race.

        The receipt ID is minted once, outside the retry loop. That is what
        makes a retry a retry: the same identifiers, so a second attempt
        cannot produce a second receipt for one logical resolution.

        `as_of` is accepted rather than always read here because the caller
        has to assemble the candidate corpus before calling, and selection
        re-evaluates every time-dependent predicate against this instant. A
        clock read taken after that assembly would evaluate the manifest at
        an instant the corpus was never read at, so a revision that became
        effective in between would be matched by a rule and then not be
        present to match. Callers with no corpus to assemble omit it.
        """
        receipt_id = preallocate_receipt_id()
        if as_of is None:
            as_of = self._clock.now()
        # Latency is measured on a monotonic timer, not from `as_of`. That
        # value is *domain* time -- the instant the whole resolution is
        # evaluated against, deliberately frozen so every read agrees -- and
        # subtracting it from a later clock read would measure zero under an
        # injected test clock and something meaningless under a clock that
        # stepped. It also covers retries, because a caller waiting through
        # two serialization failures waited for all of it.
        started = time.perf_counter()

        last_error: BaseException | None = None
        for attempt in range(1, MAX_RESOLUTION_ATTEMPTS + 1):
            try:
                outcome = await self._attempt(request, receipt_id=receipt_id, as_of=as_of, attempt=attempt)
            except DBAPIError as exc:
                if not _is_retryable(exc):
                    raise
                last_error = exc
                # Brief, growing pause: retrying instantly against the
                # transaction that just beat us tends to lose the same race
                # again.
                await asyncio.sleep(0.01 * attempt)
            else:
                metrics.observe_resolution_latency(time.perf_counter() - started)
                return outcome

        msg = f"resolution did not converge after {MAX_RESOLUTION_ATTEMPTS} serialization failures"
        raise RuntimeError(msg) from last_error

    async def _attempt(
        self,
        request: ResolutionRequest,
        *,
        receipt_id: uuid.UUID,
        as_of: datetime.datetime,
        attempt: int,
    ) -> ResolutionOutcome:
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

            replayed = await self._replay(session, request)
            if replayed is not None:
                return dataclasses.replace(replayed, attempts=attempt)

            try:
                verified = await self._attestation.verify_attestation(
                    session,
                    tenant_id=request.ctx.tenant_id,
                    host_id=request.host_id,
                    envelope=request.envelope,
                    manifest=request.manifest,
                )
            except AttestationVerificationError as exc:
                await session.rollback()
                await self._audit_failed_attempt(request, reason=str(exc))
                raise ManifestUnverified(str(exc)) from exc

            try:
                challenge = await self._challenges.validate_challenge(
                    session,
                    tenant_id=request.ctx.tenant_id,
                    host_id=request.host_id,
                    session_id=request.manifest.session_id,
                    manifest_claims_digest=verified.manifest_claims_digest,
                    arc_nonce=_decode_nonce(verified.arc_nonce_b64),
                )
            except ChallengeValidationError as exc:
                await session.rollback()
                await self._audit_failed_attempt(request, reason=str(exc))
                raise ManifestUnverified(str(exc)) from exc

            selection = await self._select(session, request, as_of=as_of)
            try:
                bundle = assemble(selection, budget_limit_bytes=request.budget_limit_bytes)
            except CanonicalizationError as exc:
                # Governed content that cannot be canonicalized -- a NUL byte
                # or a non-NFC string that reached the corpus -- is a blocked
                # outcome, not a crash. The request was properly attested, so
                # the caller is owed a receipt saying why it got nothing
                # rather than a 500 that records nothing and looks like an
                # outage. The content is attacker-influenceable upstream, so
                # this is a reachable path, not a defensive nicety.
                bundle = ContextBundle(
                    status=ResolutionStatus.BLOCKED,
                    directives=(),
                    cap_facts=(),
                    rendered_content_bytes=0,
                    budget_limit_bytes=request.budget_limit_bytes,
                    blocked_reasons=(BLOCKED_UNRENDERABLE_CONTENT,),
                    offending_artifact_ids=tuple(sorted({str(s.directive.revision_id) for s in selection.mandatory})),
                )
                _log.warning("arc.resolution.unrenderable_content: %s", exc)

            # Derived from the selection this transaction just computed, not
            # from the request. A caller-supplied selection could disagree
            # with what was actually chosen, and the receipt is the audit
            # record of what the agent was given -- it must not be able to
            # describe a bundle that was never assembled. An explicit
            # selection on the request still wins, so a caller that has
            # already done this work is not forced to repeat it.
            selected_revisions, selected_directives = request.selected_revisions, request.selected_directives
            if not selected_revisions and not selected_directives:
                selected_revisions, selected_directives = await self._record_selection(session, selection)

            await self._receipts.create_receipt(
                session,
                receipt_id=receipt_id,
                challenge_id=challenge.challenge_id,
                tenant_id=request.ctx.tenant_id,
                actor_id=request.ctx.actor_id,
                host_id=request.host_id,
                session_id=request.manifest.session_id,
                manifest_fingerprint=request.manifest_fingerprint,
                attestation_id=verified.attestation_id,
                bundle=bundle,
                provenance=self._provenance,
                replay=self._seal(receipt_id, bundle),
                evaluated_at=as_of,
                freshness_basis="revision_pinned_only",
                selected_revisions=selected_revisions,
                selected_directives=selected_directives,
            )
            await self._challenges.consume_challenge(session, challenge.challenge_id)
            await audit_outbox.emit(
                session,
                tenant_id=request.ctx.tenant_id,
                # One event type per resolution status, not a blocked/other
                # split. A degraded resolution is one where a mandatory
                # obligation could not be served, and collapsing it into
                # `resolved` makes the two indistinguishable to anything that
                # filters or alerts on event type -- which is how an audit
                # stream is normally consumed. The status is in the payload
                # either way, but a reader should not have to parse payloads
                # to notice that governance degraded.
                event_type=_CONTEXT_EVENT_BY_STATUS[bundle.status],
                payload={
                    "receipt_id": str(receipt_id),
                    "host_id": request.host_id,
                    "session_id": request.manifest.session_id,
                    "attestation_id": verified.attestation_id,
                    "resolution_status": str(bundle.status),
                    "blocked_reasons": list(bundle.blocked_reasons),
                },
            )
            await session.commit()

        # Counted after the commit, never inside it. A resolution that hit a
        # serialization failure and rolled back did not consume a challenge
        # and did not produce a receipt; counting it would make the
        # issued-vs-consumed ratio show leakage that never happened, and
        # inflate the resolution count on every retry.
        metrics.observe_challenge_consumed()
        metrics.observe_resolution(bundle.status)

        return ResolutionOutcome(receipt_id=receipt_id, status=bundle.status, bundle=bundle, attempts=attempt)

    async def _record_selection(
        self, session: AsyncSession, selection: SelectionResult
    ) -> tuple[tuple[SelectedRevision, ...], tuple[SelectedDirective, ...]]:
        """Turn what selection chose into what the receipt records.

        Read in the resolution's own transaction and snapshot, so the
        locators and digests written into the receipt are the ones that were
        true at the instant the bundle was assembled.

        The context handle is the directive id. It has to be something the
        agent already holds -- the bundle hands back `directive_id` and
        nothing else that identifies a directive -- and it has to be unique
        within a receipt, which `uq_arc_receipt_directives_handle` requires.
        `source_anchor` satisfies the first and fails the second: two
        directives may cite the same anchor, and the second insert would
        collide and fail an otherwise valid resolution.
        """
        scoped = [*selection.mandatory, *selection.optional]
        if not scoped:
            return (), ()

        revision_ids = sorted({s.directive.revision_id for s in scoped})
        rows = (
            await session.execute(
                text(
                    "SELECT revision_id, artifact_id, source_canonical_locator, "
                    "       source_revision_locator, content_digest, detail_audience "
                    "FROM arc_revisions WHERE revision_id = ANY(:rids)"
                ),
                {"rids": revision_ids},
            )
        ).all()
        by_revision = {row.revision_id: row for row in rows}

        revisions: dict[uuid.UUID, SelectedRevision] = {}
        directives: list[SelectedDirective] = []
        for entry in scoped:
            row = by_revision.get(entry.directive.revision_id)
            if row is None:
                # The revision was selected from this same snapshot, so its
                # absence means the corpus changed underneath a REPEATABLE
                # READ transaction, which cannot happen. Skipping would
                # silently drop an obligation from the audit record.
                msg = f"selected revision {entry.directive.revision_id} vanished mid-resolution"
                raise RuntimeError(msg)

            # A revision reached by both a mandatory and an optional rule is
            # mandatory: the obligation is owed either way.
            existing = revisions.get(row.revision_id)
            revisions[row.revision_id] = SelectedRevision(
                revision_id=row.revision_id,
                artifact_id=row.artifact_id,
                is_mandatory=entry.is_mandatory or (existing is not None and existing.is_mandatory),
            )
            directives.append(
                SelectedDirective(
                    revision_id=row.revision_id,
                    directive_id=entry.directive.directive_id,
                    artifact_id=row.artifact_id,
                    is_mandatory=entry.is_mandatory,
                    visibility_decision_id=f"detail_audience:{row.detail_audience}",
                    source_locator=row.source_canonical_locator,
                    source_revision_locator=row.source_revision_locator,
                    content_digest=row.content_digest,
                    obligation_fields=_obligation_fields(entry),
                    context_handle_digest=hashlib.sha256(str(entry.directive.directive_id).encode("utf-8")).hexdigest(),
                )
            )

        return tuple(revisions[rid] for rid in sorted(revisions)), tuple(directives)

    async def _select(
        self, session: AsyncSession, request: ResolutionRequest, *, as_of: datetime.datetime
    ) -> SelectionResult:
        """The pure decision plus the one integrity recheck every §6.3
        caller performs before it actually serves -- see `select_and_verify`'s
        own docstring. `as_of` is passed in rather than read: reading a
        clock inside the pure half of that function would break the
        determinism guarantee that the same inputs always produce the same
        result.
        """
        return await select_and_verify(session, dataclasses.replace(request.candidates, as_of=as_of), self._integrity)

    async def _replay(self, session: AsyncSession, request: ResolutionRequest) -> ResolutionOutcome | None:
        """Answer an exact retry from the receipt it already produced.

        Deliberately before signer-key validation. This reads an existing
        receipt rather than accepting a new attestation, so it must keep
        working after the key that signed the original has been revoked --
        the original was verified when it was made, and revoking a key does
        not retroactively unmake what it authorized.
        """
        row = (
            await session.execute(
                text(
                    "SELECT receipt_id, manifest_fingerprint, resolution_status, integrity_state "
                    "FROM arc_receipts WHERE host_id = :host AND attestation_id = :att"
                ),
                {"host": request.host_id, "att": request.envelope.attestation_id},
            )
        ).one_or_none()
        if row is None:
            return None

        if row.manifest_fingerprint != request.manifest_fingerprint:
            msg = (
                f"attestation {request.envelope.attestation_id!r} already resolved a different manifest "
                f"for host {request.host_id!r}"
            )
            raise IdempotencyConflict(msg)

        if row.integrity_state != "valid":
            # Replaying a receipt whose chain may have been altered would
            # hand back content ARC can no longer vouch for.
            msg = f"receipt {row.receipt_id} failed integrity verification and cannot be replayed"
            raise ManifestUnverified(msg)

        return ResolutionOutcome(
            receipt_id=row.receipt_id,
            status=ResolutionStatus(row.resolution_status),
            bundle=None,
            replayed=True,
        )

    async def _audit_failed_attempt(self, request: ResolutionRequest, *, reason: str) -> None:
        """Record a rejected attempt in its own bounded transaction.

        The resolution transaction is being abandoned, so an audit row
        written on it would vanish with it -- and a failed authentication
        attempt that leaves no trace is precisely the one an operator most
        needs to see.
        """
        async with self._session_factory() as session, session.begin():
            await audit_outbox.emit(
                session,
                tenant_id=request.ctx.tenant_id,
                event_type=actions.ARC_MANIFEST_UNVERIFIED,
                payload={
                    "host_id": request.host_id,
                    "session_id": request.manifest.session_id,
                    "attestation_id": request.envelope.attestation_id,
                    "reason_code": BLOCKED_MANIFEST_UNVERIFIED,
                    # The message is diagnostic for an operator, never
                    # returned to the caller: which check failed is exactly
                    # the probing signal an attacker wants.
                    "detail": reason[:200],
                },
            )


def _obligation_fields(scoped: ScopedDirective) -> dict[str, object]:
    """The comparable shape of one obligation, as the receipt retains it.

    Identity and constraint only -- never the directive's prose. This row is
    readable by an operator triaging a resolution, and the governed text is
    audience-gated behind JIT detail; copying it here would route around
    that gate for every directive any agent was ever shown.
    """
    constraint = scoped.directive.constraint
    return {
        "directive_type": str(scoped.directive.directive_type),
        "scope": str(scoped.scope),
        "source_anchor": scoped.directive.source_anchor,
        "constraint": (
            None
            if constraint is None
            else {
                "modality": str(constraint.modality),
                "operator": str(constraint.operator),
                "values": sorted(constraint.values),
            }
        ),
    }


def parse_manifest(claims: ManifestClaims) -> TaskManifest:
    """Turn the attested wire claims into the parsed domain manifest.

    Two representations of one manifest, deliberately. `ManifestClaims` is
    exactly what the host canonicalized and signed -- all strings, in the
    profile's field set -- and must stay that way, because re-typing a field
    changes the bytes and the signature would no longer verify.
    `TaskManifest` is what selection matches against: closed vocabularies and
    frozensets, so an unknown task kind fails here rather than silently
    matching nothing.

    Parsing here, in the orchestrator, is what keeps that boundary in one
    place. An `ArcVocabularyError` from this is a malformed request, not an
    authentication failure -- the attestation over it may be perfectly valid.
    """
    return TaskManifest(
        session_id=claims.session_id,
        task_kind=parse_task_kind(claims.task_kind),
        requested_action_classes=frozenset(parse_action_class(a) for a in claims.requested_action_classes),
        capability_ids=frozenset(uuid.UUID(c) for c in claims.capability_ids),
        domain_ids=frozenset(claims.domain_ids),
        environment=claims.environment,
        data_sensitivity=claims.data_sensitivity,
        repository_identity=claims.repository_identity,
    )


def _decode_nonce(nonce_b64: str) -> bytes:
    return base64.b64decode(nonce_b64, validate=True)


__all__ = [
    "BLOCKED_MANIFEST_UNVERIFIED",
    "BLOCKED_UNRENDERABLE_CONTENT",
    "MAX_RESOLUTION_ATTEMPTS",
    "IdempotencyConflict",
    "ManifestUnverified",
    "ResolutionOutcome",
    "ResolutionRequest",
    "ResolutionService",
    "parse_manifest",
]
