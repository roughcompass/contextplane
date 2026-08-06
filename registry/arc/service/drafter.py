"""The model-backed drafting route's service, and reach confirmations.

**The model is disabled, and `draft()`'s first branch proves it.** Per the
committed decision (`registry/arc/drafter/model_decision.json`, `outcome:
human_only`) and its startup guard
(`registry.wiring.services._assert_drafter_decision_permits_serving`),
`ARC_DRAFTER_MODEL_ENABLED` defaults false and cannot be more permissive
than that artifact. `draft()` re-checks both the flag and the decision's
own `outcome` itself, before opening a database session, reading a source,
or spawning a sandboxed process -- so the disabled path costs nothing
beyond the check itself, and refusing to serve is provably the *first*
thing that happens, not a guard bolted on after other work already ran.
Everything below the disabled-state check exists so that a future accepted
model has real, tested infrastructure to serve through; it is not reachable
in this deployment's committed state.

**The sandbox pipeline is two hops, both real subprocesses, reusing
`registry.arc.sandbox.ipc` unchanged for both.** The API process never
parses admitted-but-unreviewed source bytes in-process (the same rule
`source_admission.py`'s own sandboxed parser exists to hold), so `draft()`
cannot hand raw content straight to the drafter sandbox: it first calls the
existing parser sandbox (`registry.arc.sandbox.parser_main`, built for
`AAS-T08` and never previously given a production caller) to obtain a
sanitized `ParsedSourceEnvelope`, verifies that envelope's binding against
the admitted source, and only then hands the envelope -- structured,
already-validated data, not raw bytes -- to the drafter sandbox
(`registry.arc.sandbox.drafter_main`). Each sandbox gets its own socket
path and peer UID; nothing here lets one authenticate as the other (see
`tests/conformance/test_arc_drafter_sandbox.py`).

**Reach confirmations live in this module, not a separate one.** The TDD's
own module inventory names no dedicated module for
`arc_authoring_reach_confirmations`, and both of this task's routes
(`POST {PV}/draft`, `POST {PV}/reach-confirmations`) land in the same task
for the same reason: neither is reachable until a human (or, once accepted,
a model) is actively drafting a version. `confirm_reach` follows the exact
persistence shape `provenance.py::edit` already established for the sibling
`arc_authoring_field_provenance` table -- per-field upsert, never a
delete-then-reinsert of the whole set.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import os
import subprocess  # noqa: S404 - the sandboxed processes are real, separate OS processes by design; fixed argv below
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.sandbox import ipc
from registry.arc.schemas import drafter_output
from registry.arc.schemas import parser_output as po
from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries import drafter as queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.source_admission import SourceAdmissionService
from registry.arc.service.source_status import SourceStatusService
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.config import Settings
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock, JSONValue

# `load_drafter_model_decision` is deliberately *not* imported here. It is
# defined in `registry.wiring.services`, which already imports this module
# to construct `DrafterService` -- importing it back from here would be a
# service-module-depends-on-its-own-wiring cycle. `_wire_arc` passes its own
# `load_drafter_model_decision` in as `decision_loader` below instead, the
# same seam every unit test in `tests/unit/test_arc_drafter.py` uses to
# supply a fixture decision without touching the filesystem.

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DrafterModelDisabled(RegistryError):
    """Model-backed drafting is not enabled by an accepted decision
    artifact (`arc_drafter_model_disabled`, 409)."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CitationRecord:
    field_path: str
    source_evidence_id: uuid.UUID
    source_anchor: str
    excerpt_digest: str


@dataclasses.dataclass(frozen=True)
class DraftResult:
    patch: dict[str, Any]
    citations: tuple[CitationRecord, ...]
    declined_field_paths: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ReachConfirmationRecord:
    field_path: str
    confirmed: bool
    confirmed_at: datetime.datetime | None
    confirmed_by_issuer: str | None
    confirmed_by_subject: str | None


def _record(row: queries.ReachConfirmationRow) -> ReachConfirmationRecord:
    return ReachConfirmationRecord(
        field_path=row.field_path,
        confirmed=row.confirmed,
        confirmed_at=row.confirmed_at,
        confirmed_by_issuer=row.confirmed_by_issuer,
        confirmed_by_subject=row.confirmed_by_subject,
    )


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Sandbox pipeline -- real subprocesses, reusing `ipc.py` unchanged for both
# hops. Module-level and injectable (see `DrafterService.__init__`) so unit
# tests can substitute a fake without spawning a process, while integration
# tests exercise this exact function against real ones.
# ---------------------------------------------------------------------------

_SANDBOX_DEADLINE_SECONDS = 30.0
_SANDBOX_CPU_SECONDS = 30
_SANDBOX_MEMORY_BYTES = 512 * 1024 * 1024
_SOCKET_WAIT_TIMEOUT_SECONDS = 10.0

#: What every declined-everything outcome below returns -- the safe,
#: bounded fallback for any sandbox-transport failure, refusal, or binding
#: mismatch: propose nothing rather than guess. Never raised as an error,
#: because a sandbox being unavailable is not a caller error -- it is
#: exactly the outcome `patch={}` / `citations=()` already represents.
_SandboxPipeline = Callable[
    [bytes, str, uuid.UUID, str, Sequence[str]],
    Awaitable[tuple[dict[str, Any], tuple[CitationRecord, ...], tuple[str, ...]]],
]


def _decline_all(
    target_field_paths: Sequence[str],
) -> tuple[dict[str, Any], tuple[CitationRecord, ...], tuple[str, ...]]:
    return {}, (), tuple(dict.fromkeys(target_field_paths))


def _wait_for_socket(proc: subprocess.Popen[bytes], sock_path: Path, *, timeout: float) -> bool:
    """Poll for `sock_path` to appear. Returns `False` (never raises) on
    timeout or early process exit -- the caller treats either as "sandbox
    unavailable" and declines rather than raising a 500 for a condition
    that is a legitimate, bounded outcome of this endpoint."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.02)
    return False


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _run_sandbox_pipeline_sync(
    content: bytes,
    media_type: str,
    source_evidence_id: uuid.UUID,
    source_content_digest: str,
    target_field_paths: Sequence[str],
) -> tuple[dict[str, Any], tuple[CitationRecord, ...], tuple[str, ...]]:
    target_field_paths = list(target_field_paths)
    caller_uid = os.getuid()

    # A short, real path directly under `/tmp`: pytest's own `tmp_path`
    # nests several directories deep, which easily exceeds the kernel's
    # `sun_path` limit (104 bytes on BSD/macOS, 108 on Linux) for an
    # `AF_UNIX` socket -- the same reason `scripts/run_parser_sandbox.sh`
    # and `tests/conformance/test_arc_parser_sandbox.py` both bind under a
    # fresh top-level temp directory rather than a nested one.
    with tempfile.TemporaryDirectory(dir="/tmp") as workdir_str:
        workdir = Path(workdir_str)
        read_root = workdir / "read_root"
        parser_scratch = workdir / "parser_scratch"
        drafter_scratch = workdir / "drafter_scratch"
        read_root.mkdir()
        parser_scratch.mkdir()
        drafter_scratch.mkdir()
        content_path = read_root / "content"
        content_path.write_bytes(content)
        content_path.chmod(0o400)
        read_root.chmod(0o500)
        parser_scratch.chmod(0o700)
        drafter_scratch.chmod(0o700)

        # -- hop 1: parser sandbox ------------------------------------------------
        parser_sock = workdir / "parser.sock"
        parser_proc = subprocess.Popen(  # noqa: S603 - fixed argv naming this repo's own sandbox module; no caller input reaches it
            [
                sys.executable,
                "-m",
                "registry.arc.sandbox.parser_main",
                "--content-path",
                str(content_path),
                "--sock-path",
                str(parser_sock),
                "--media-type",
                media_type,
                "--source-evidence-id",
                str(source_evidence_id),
                "--expected-peer-uid",
                str(caller_uid),
                "--scratch-dir",
                str(parser_scratch),
                "--deadline-seconds",
                str(_SANDBOX_DEADLINE_SECONDS),
                "--cpu-seconds",
                str(_SANDBOX_CPU_SECONDS),
                "--memory-bytes",
                str(_SANDBOX_MEMORY_BYTES),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if not _wait_for_socket(parser_proc, parser_sock, timeout=_SOCKET_WAIT_TIMEOUT_SECONDS):
                return _decline_all(target_field_paths)
            try:
                parser_response = ipc.request_json(
                    parser_sock, {}, expected_peer_uid=caller_uid, deadline_seconds=_SANDBOX_DEADLINE_SECONDS
                )
            except ipc.SandboxRefusal:
                return _decline_all(target_field_paths)
        finally:
            _terminate(parser_proc)

        parsed = po.parse_parser_result(parser_response)
        if isinstance(parsed, po.ParserRefusal):
            return _decline_all(target_field_paths)
        try:
            po.verify_source_binding(
                parsed.envelope, source_evidence_id=source_evidence_id, source_content_digest=source_content_digest
            )
        except po.ParserBindingError:
            return _decline_all(target_field_paths)

        # -- hop 2: drafter sandbox -----------------------------------------------
        drafter_sock = workdir / "drafter.sock"
        drafter_proc = subprocess.Popen(  # noqa: S603 - fixed argv naming this repo's own sandbox module; no caller input reaches it
            [
                sys.executable,
                "-m",
                "registry.arc.sandbox.drafter_main",
                "--content-path",
                str(content_path),
                "--sock-path",
                str(drafter_sock),
                "--source-content-digest",
                source_content_digest,
                "--expected-peer-uid",
                str(caller_uid),
                "--scratch-dir",
                str(drafter_scratch),
                "--deadline-seconds",
                str(_SANDBOX_DEADLINE_SECONDS),
                "--cpu-seconds",
                str(_SANDBOX_CPU_SECONDS),
                "--memory-bytes",
                str(_SANDBOX_MEMORY_BYTES),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if not _wait_for_socket(drafter_proc, drafter_sock, timeout=_SOCKET_WAIT_TIMEOUT_SECONDS):
                return _decline_all(target_field_paths)
            try:
                drafter_request: dict[str, JSONValue] = {
                    "envelope": cast("dict[str, JSONValue]", parsed.envelope.model_dump(mode="json")),
                    "target_field_paths": cast("list[JSONValue]", list(target_field_paths)),
                }
                drafter_response = ipc.request_json(
                    drafter_sock,
                    drafter_request,
                    expected_peer_uid=caller_uid,
                    deadline_seconds=_SANDBOX_DEADLINE_SECONDS,
                )
            except ipc.SandboxRefusal:
                return _decline_all(target_field_paths)
        finally:
            _terminate(drafter_proc)

    drafted = drafter_output.parse_drafter_result(drafter_response)
    if isinstance(drafted, drafter_output.DrafterRefusal):
        return _decline_all(target_field_paths)

    citations = tuple(
        CitationRecord(
            field_path=c.field_path,
            source_evidence_id=c.source_evidence_id,
            source_anchor=c.source_anchor,
            excerpt_digest=c.excerpt_digest,
        )
        for c in drafted.citations
    )
    return dict(drafted.patch), citations, tuple(drafted.declined_field_paths)


async def _run_sandbox_pipeline(
    content: bytes,
    media_type: str,
    source_evidence_id: uuid.UUID,
    source_content_digest: str,
    target_field_paths: Sequence[str],
) -> tuple[dict[str, Any], tuple[CitationRecord, ...], tuple[str, ...]]:
    """Runs the synchronous, blocking two-hop pipeline off the event loop
    thread -- `subprocess.Popen`/`socket.recv` calls would otherwise stall
    every other request this process is serving concurrently. Matches this
    codebase's existing `asyncio.to_thread` convention for blocking calls
    (see e.g. `registry/service/retrieval/search.py`)."""
    return await asyncio.to_thread(
        _run_sandbox_pipeline_sync, content, media_type, source_evidence_id, source_content_digest, target_field_paths
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DrafterService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        source_admission: SourceAdmissionService,
        source_status: SourceStatusService,
        clock: Clock,
        settings: Settings,
        decision_loader: Callable[[], dict[str, Any]],
        sandbox_pipeline: _SandboxPipeline | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._source_admission = source_admission
        self._source_status = source_status
        self._clock = clock
        self._settings = settings
        self._decision_loader = decision_loader
        self._sandbox_pipeline = sandbox_pipeline or _run_sandbox_pipeline

    async def draft(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        source_evidence_id: uuid.UUID,
        target_field_paths: Sequence[str],
    ) -> DraftResult:
        """Ask the drafter sandbox for a citation-bound patch.

        The disabled-state check runs first, before any database session
        opens: a deployment with the model disabled (the committed
        default) never reads the proposal, never checks source status,
        and never spawns a process for this call. See this module's own
        docstring for why that ordering is load-bearing, not incidental.
        """
        if not self._settings.arc_drafter_model_enabled:
            raise DrafterModelDisabled(
                "model-backed drafting is disabled on this deployment (ARC_DRAFTER_MODEL_ENABLED is false)"
            )
        decision = self._decision_loader()
        if decision["outcome"] != "accepted":
            raise DrafterModelDisabled(
                f"model-backed drafting is disabled: the committed decision artifact records "
                f"outcome={decision['outcome']!r}, not 'accepted'"
            )

        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
            if version.state != "open":
                msg = (
                    f"proposal version {proposal_id}/{proposal_version} is not open for drafting "
                    f"(state={version.state!r})"
                )
                raise ProposalStateConflict(msg)

        if not target_field_paths:
            patch, citations, declined = _decline_all(())
            return DraftResult(patch=patch, citations=citations, declined_field_paths=declined)

        # Source status is checked fresh here (not merely at admission),
        # matching the freshness rule every other selection/authorization
        # chokepoint enforces: a source that was revoked or expired since
        # admission must not be readable for drafting either.
        await self._source_status.check_status(source_evidence_id)
        evidence = await self._source_admission.get_evidence(ctx, source_evidence_id)
        content, media_type = await self._source_admission.get_body(ctx, source_evidence_id)

        patch, citations, declined = await self._sandbox_pipeline(
            content, media_type, source_evidence_id, evidence.source_content_digest, target_field_paths
        )
        return DraftResult(patch=patch, citations=citations, declined_field_paths=declined)

    async def confirm_reach(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        field_paths: Sequence[str],
    ) -> tuple[ReachConfirmationRecord, ...]:
        """Record that the authenticated caller has reviewed each named
        field path's reach, for this frozen candidate.

        Legal only while the version is `open`, matching every other
        `PATCH`-adjacent write on this aggregate (`provenance.py::edit`,
        `semantic_tests.py::run`): reach is confirmed against a candidate
        that can still change, not retroactively against one already
        submitted.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
            if version.state != "open":
                msg = (
                    f"proposal version {proposal_id}/{proposal_version} is not open for reach confirmation "
                    f"(state={version.state!r})"
                )
                raise ProposalStateConflict(msg)

            deduplicated = list(dict.fromkeys(field_paths))
            for field_path in deduplicated:
                await queries.upsert_reach_confirmation(
                    session,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    field_path=field_path,
                    confirmed=True,
                    confirmed_at=now,
                    confirmed_by_issuer=ctx.oidc_issuer,
                    confirmed_by_subject=ctx.oidc_subject,
                )

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_REACH_CONFIRMATION_UPDATED,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "field_paths": sorted(deduplicated),
                },
            )
            rows = await queries.load_reach_confirmations_for_paths(
                session, proposal_id, proposal_version, deduplicated
            )
        return tuple(_record(row) for row in rows)

    async def list_reach_confirmations(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> tuple[ReachConfirmationRecord, ...]:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            rows = await queries.load_reach_confirmations(session, proposal_id, proposal_version)
        return tuple(_record(row) for row in rows)


__all__ = [
    "CitationRecord",
    "DraftResult",
    "DrafterModelDisabled",
    "DrafterService",
    "ReachConfirmationRecord",
]
