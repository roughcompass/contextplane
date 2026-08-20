"""Unit tests for ``ClaimService``, assembled from ``claim_writer.py``
(construction, the machine/system write path, and the lifecycle helpers),
``claim_authority.py`` (predicate/subject resolution, authority derivation,
value conformance), and ``claim_curator_actions.py`` (the two curator
decisions) -- three cooperating modules composed into one class by
``claim_writer.py``'s mixin inheritance. Still one test file, because
``ClaimService`` is still one class with one behavior contract; only where
each method's body physically lives changed.

All DB interaction is mocked at ``session.execute`` via an SQL-string-keyed
router, mirroring ``tests/unit/test_promotion_sweep_worker.py``'s own pattern --
no Postgres is required. Four collaborators that live in their own modules with
their own dedicated suites are patched directly at the point each of the three
modules above imports them, rather than re-simulated through SQL:

- ``resolve_visible_entity`` (the cross-tenant visibility chokepoint) --
  called only inside ``claim_authority.py``'s ``_resolve_subject``, so it is
  patched there once.
- ``subject_change_profile`` (confidence_read's volatility read) and
  ``detect_for_claim`` (contest detection) -- each called directly from two
  places, ``claim_writer.py``'s ``stage_claim`` and
  ``claim_curator_actions.py``'s ``link_subject``, so each is patched in both
  modules; whichever path a given test exercises is the one that reads the
  patched value.
- ``project_claim`` (the embedding-index projection hook) -- called only from
  ``claim_writer.py`` (``close_superseded``, ``mark_consolidated``).

What is under test here is ``ClaimService``'s own logic: predicate resolution,
value conformance, authority derivation, visibility derivation, and the two
curator decisions (``link_subject``, ``discard``), plus the lifecycle helpers
that take an open session directly (``close_superseded``,
``set_promotion_state``). ``contextplane.service.memory.confidence``'s own scoring
arithmetic is exercised for real (not mocked) wherever it runs, since it is a
pure function and using the real thing is what makes an authority/status/
visibility assertion here mean anything -- but this file never asserts on a
specific confidence *value*, since that arithmetic already has its own suite.

Coverage:
- Predicate/ontology refusal: unknown predicate, deprecated predicate.
- Value conformance: provenance-required, the interval check, null value,
  prose outside its one permitted category, boolean-vs-integer, malformed
  decimal (and a well-formed one accepted as a string), a malformed version
  range, and a timestamp with an offset.
- Authority derivation (the branch table in ``_derive_authority`` /
  ``_evidence_derivation``): curator evidence from a human actor, a service
  principal refused as curator evidence, a deterministic connector run, a
  non-deterministic one, an unresolvable connector ref (costs, never buys),
  a generic evidence kind (inference, not extraction), the weakest-link rule
  over mixed evidence, the observer-vs-owner flip, and the unattributed floor
  for an unresolved subject.
- Visibility/ownership: narrower-than-subject refusal, default-to-subject,
  and the owning tenant being the subject's rather than the author's.
- Subject resolution: the external-id branch claim_authority.py owns directly,
  and the unresolvable-reference-stores-unlinked path.
- ``link_subject``: not-found, the role guard, the same-tenant-queue guard,
  the already-staged guard, a reference that still does not resolve, the
  visibility-narrows-fresh-from-the-subject behaviour, the authority flip on
  the resolved subject's owner, contest detection re-running against the new
  neighbourhood (and the counterparty rescore that follows), and the audit row.
- ``discard``: not-found, the role guard, the same-tenant-queue guard, the
  not-staged-or-unlinked guard, the staged->rejected path with its audit
  reason, and the unlinked-claim terminal shape (rejected, subject and
  confidence both still NULL) via the author-tenant queue fallback.
- ``close_superseded``: the reason-membership guard and the UPDATE's
  status='staged' WHERE guard, plus the embedding-index hook it calls.
- ``set_promotion_state``: the closed-set guard (carried forward, not fixed,
  per this phase's exception-tree bookkeeping -- see the module docstring
  above the test) and the UPDATE it issues for a valid state.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.catalog.global_vocabulary import CARDINALITY_SINGLE
from contextplane.service.memory import claim_authority as claim_authority_module
from contextplane.service.memory import claim_curator_actions as claim_curator_actions_module
from contextplane.service.memory import claim_writer as claim_writer_module
from contextplane.service.memory.claim_authority import (
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
    REJECT_DEPRECATED_PREDICATE,
    REJECT_EVIDENCE_KIND,
    REJECT_INTERVAL,
    REJECT_NULL_VALUE,
    REJECT_PROSE,
    REJECT_UNKNOWN_PREDICATE,
    REJECT_VALUE_TYPE,
    REJECT_VISIBILITY,
    STATUS_STAGED,
    STATUS_UNLINKED,
    ClaimRejected,
    Evidence,
)
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.contest import ContestOutcome, Disagreement
from contextplane.storage.models import Entity
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _producer_ctx(tenant_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None) -> Any:
    return tenant_context(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _predicate_row(
    *,
    value_type: str = "string",
    claim_category: str = "ownership_stewardship",
    value_cardinality: str = CARDINALITY_SINGLE,
    deprecated_at: datetime.datetime | None = None,
) -> MagicMock:
    return MagicMock(
        value_type=value_type,
        claim_category=claim_category,
        value_cardinality=value_cardinality,
        deprecated_at=deprecated_at,
    )


def _entity(tenant_id: uuid.UUID, entity_id: uuid.UUID | None = None) -> MagicMock:
    e = MagicMock(spec=Entity)
    e.entity_id = entity_id or uuid.uuid4()
    e.tenant_id = tenant_id
    return e


def _patch_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_entity: Any | None = None,
    change_profile: tuple[float | None, int] = (None, 0),
    contest_outcome: ContestOutcome | None = None,
) -> AsyncMock:
    """Patch the split write path's four collaborator imports; return the
    detect_for_claim mock so a test can assert on how it was called. See the
    module docstring for which module each collaborator is patched in and why."""
    monkeypatch.setattr(claim_authority_module, "resolve_visible_entity", AsyncMock(return_value=resolved_entity))
    change_mock = AsyncMock(return_value=change_profile)
    monkeypatch.setattr(claim_writer_module, "subject_change_profile", change_mock)
    monkeypatch.setattr(claim_curator_actions_module, "subject_change_profile", change_mock)
    detect_mock = AsyncMock(
        return_value=contest_outcome or ContestOutcome(detected=(), neighbourhood_size=0, truncated=False)
    )
    monkeypatch.setattr(claim_writer_module, "detect_for_claim", detect_mock)
    monkeypatch.setattr(claim_curator_actions_module, "detect_for_claim", detect_mock)
    monkeypatch.setattr(claim_writer_module, "project_claim", AsyncMock(return_value=False))
    return detect_mock


# ---------------------------------------------------------------------------
# stage_claim: session router
# ---------------------------------------------------------------------------


_UNSET = object()


def _stage_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    predicate_row: Any = _UNSET,
    resolved_entity: Any | None = None,
    subject_visibility: str = "tenant-shared",
    actor_kind: str | None = "human",
    sync_run_visible: bool = False,
    sync_run_source_type: str | None = None,
    external_id_row: Any | None = None,
    contest_outcome: ContestOutcome | None = None,
) -> tuple[ClaimService, list[str], list[tuple[str, dict]]]:
    """A ClaimService wired to a SQL-string-keyed router for stage_claim tests.

    ``predicate_row`` defaults to a well-formed row; pass ``None`` explicitly
    to simulate an unknown predicate (the real "not found" shape), which is
    why the default is a distinct sentinel rather than ``None`` itself.
    """
    if predicate_row is _UNSET:
        predicate_row = _predicate_row()

    executed: list[str] = []
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        captured.append((sql, dict(params or {})))
        result = MagicMock()

        if "FROM vocabulary_values" in sql:
            result.one_or_none = MagicMock(return_value=predicate_row)
            return result
        if "SELECT visibility FROM entities" in sql:
            result.scalar_one = MagicMock(return_value=subject_visibility)
            return result
        if "SELECT actor_kind FROM actors" in sql:
            result.scalar_one_or_none = MagicMock(return_value=actor_kind)
            return result
        if "SELECT s.source_type FROM sync_runs" in sql:
            row = MagicMock(source_type=sync_run_source_type) if sync_run_visible else None
            result.one_or_none = MagicMock(return_value=row)
            return result
        if "SELECT source_id FROM sync_runs" in sql:
            row = MagicMock(source_id=uuid.uuid4()) if sync_run_visible else None
            result.one_or_none = MagicMock(return_value=row)
            return result
        if "FROM memory_session_events" in sql:
            result.one_or_none = MagicMock(return_value=None)
            return result
        if "FROM entity_external_ids" in sql:
            result.one_or_none = MagicMock(return_value=external_id_row)
            return result
        if "INSERT INTO memory_claims (" in sql:
            return result
        if "INSERT INTO memory_claim_provenance" in sql:
            return result
        if "SELECT status, source_authority, is_contested" in sql:
            result.one_or_none = MagicMock(
                return_value=MagicMock(status="staged", source_authority="owner_inference", is_contested=False)
            )
            return result
        if "SELECT independence_key, independence_group" in sql:
            result.all = MagicMock(return_value=[])
            return result
        if "FROM memory_predicate_churn" in sql:
            # No inspected per-predicate rate, which is every deployment until
            # somebody inspects a fit. The write path then decays on the authored
            # category figure, which is what the assertions below expect.
            result.all = MagicMock(return_value=[])
            return result
        if "UPDATE memory_claims SET" in sql and "confidence" in sql:
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    _patch_collaborators(
        monkeypatch,
        resolved_entity=resolved_entity,
        contest_outcome=contest_outcome,
    )

    service = ClaimService(factory, clock=FakeClock(_NOW))
    return service, executed, captured


def _evidence(kind: str, ref: str | None = None) -> tuple[Evidence, ...]:
    return (Evidence(kind=kind, ref=ref or "evt-1"),)


# ---------------------------------------------------------------------------
# Predicate / value conformance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signature_has_no_authority_parameter() -> None:
    """The caller cannot declare its own authority: there is no parameter for
    it, which is the whole reason it is derived rather than accepted."""
    import inspect

    params = inspect.signature(ClaimService.stage_claim).parameters
    assert "source_authority" not in params
    assert "derivation" not in params


@pytest.mark.asyncio
async def test_unknown_predicate_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, executed, _ = _stage_service(monkeypatch, predicate_row=None)

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="vibes_with",
            value="x",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_UNKNOWN_PREDICATE
    assert not any("INSERT INTO memory_claims (" in s for s in executed)


@pytest.mark.asyncio
async def test_deprecated_predicate_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, executed, _ = _stage_service(
        monkeypatch, predicate_row=_predicate_row(deprecated_at=_NOW - datetime.timedelta(days=1))
    )

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="platform",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_DEPRECATED_PREDICATE
    assert not any("INSERT INTO memory_claims (" in s for s in executed)


@pytest.mark.asyncio
async def test_a_claim_without_provenance_is_refused_before_any_query(monkeypatch: pytest.MonkeyPatch) -> None:
    service, executed, _ = _stage_service(monkeypatch)

    with pytest.raises(ValidationError, match="provenance"):
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="platform",
            evidence=(),
        )
    assert executed == []


@pytest.mark.asyncio
async def test_asserted_valid_to_before_valid_from_is_refused_before_any_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, executed, _ = _stage_service(monkeypatch)

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="platform",
            evidence=_evidence("session_event"),
            asserted_valid_from=_NOW,
            asserted_valid_to=_NOW - datetime.timedelta(seconds=1),
        )
    assert exc.value.reason == REJECT_INTERVAL
    assert executed == []


@pytest.mark.asyncio
async def test_null_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch)

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value=None,
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_NULL_VALUE


@pytest.mark.asyncio
async def test_prose_outside_session_summary_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(
        monkeypatch, predicate_row=_predicate_row(value_type="prose", claim_category="ownership_stewardship")
    )

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="a paragraph",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_PROSE


@pytest.mark.asyncio
async def test_boolean_is_not_accepted_as_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch, predicate_row=_predicate_row(value_type="duration_seconds"))

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="recovery_time_objective_seconds",
            value=True,
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_malformed_decimal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch, predicate_row=_predicate_row(value_type="decimal"))

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="target_availability",
            value="banana",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_well_formed_decimal_string_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed-point string, not a float -- the whole reason the type exists."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(
        monkeypatch,
        predicate_row=_predicate_row(value_type="decimal"),
        resolved_entity=_entity(tid),
    )

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="target_availability",
        value="0.999",
        evidence=_evidence("session_event"),
    )
    assert claim.status == STATUS_STAGED


@pytest.mark.asyncio
async def test_relative_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch, predicate_row=_predicate_row(value_type="url"))

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="runbook_url",
            value="/relative/path",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_malformed_version_predicate_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch, predicate_row=_predicate_row(value_type="version_predicate"))

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="interface_version",
            value="not a version range",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


@pytest.mark.asyncio
async def test_timestamp_with_an_offset_is_rejected_not_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _stage_service(monkeypatch, predicate_row=_predicate_row(value_type="timestamp_utc"))

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(),
            subject_reference=str(uuid.uuid4()),
            predicate="decided_at",
            value="2026-08-05T12:00:00+02:00",
            evidence=_evidence("session_event"),
        )
    assert exc.value.reason == REJECT_VALUE_TYPE


# ---------------------------------------------------------------------------
# Authority derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_evidence_from_a_human_actor_earns_owner_human(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid), actor_kind="human")

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("curator", str(aid)),
    )
    assert claim.source_authority == AUTHORITY_OWNER_HUMAN


@pytest.mark.asyncio
async def test_a_service_principal_cannot_produce_curator_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, executed, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid), actor_kind="sync_worker")

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(tid, aid),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="platform",
            evidence=_evidence("curator", str(aid)),
        )
    assert exc.value.reason == REJECT_EVIDENCE_KIND
    # Refused before the row is ever written.
    assert not any("INSERT INTO memory_claims (" in s for s in executed)


@pytest.mark.asyncio
async def test_deterministic_connector_run_earns_owner_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(
        monkeypatch,
        resolved_entity=_entity(tid),
        sync_run_visible=True,
        sync_run_source_type="openapi",
    )

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("connector_run", str(uuid.uuid4())),
    )
    assert claim.source_authority == AUTHORITY_OWNER_EXTRACTION


@pytest.mark.asyncio
async def test_non_deterministic_connector_run_earns_owner_inference_not_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproducibility comes from the connector contract, not the evidence
    kind. An unregistered source type has made no such promise."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(
        monkeypatch,
        resolved_entity=_entity(tid),
        sync_run_visible=True,
        sync_run_source_type="scraped_wiki",
    )

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("connector_run", str(uuid.uuid4())),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_unresolvable_connector_ref_costs_authority_rather_than_buying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid))

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("connector_run", "not-a-uuid"),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_a_generic_evidence_kind_is_inference_not_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The artefact is real, but the step from it to a typed triple is a model
    reading text -- not reproducible, and must not rank as if it were."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid))

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event", "evt-1"),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_mixed_evidence_takes_the_weakest_link_not_the_strongest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Max would be a privilege-escalation primitive: one connector run
    attached to a model inference must not outrank the owner's real parse."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(
        monkeypatch,
        resolved_entity=_entity(tid),
        sync_run_visible=True,
        sync_run_source_type="openapi",
    )

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=(
            Evidence(kind="connector_run", ref=str(uuid.uuid4())),
            Evidence(kind="session_event", ref="evt-9"),
        ),
    )
    assert claim.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_observer_tier_when_the_author_does_not_own_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identical evidence earns a lower tier once the subject belongs to
    somebody else's tenant -- standing, not the evidence, decides the axis."""
    owner_tid = uuid.uuid4()
    author_tid, author_aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(owner_tid))

    claim = await service.stage_claim(
        _producer_ctx(author_tid, author_aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event", "evt-1"),
    )
    assert claim.source_authority == AUTHORITY_OBSERVER_INFERENCE
    assert claim.owning_tenant_id == owner_tid


@pytest.mark.asyncio
async def test_an_unresolved_subject_is_unattributed_not_an_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No owner to compare the author against, so standing is undefined
    rather than low -- an observer tier would assert a determination nobody
    made."""
    service, _, _ = _stage_service(monkeypatch, resolved_entity=None)

    claim = await service.stage_claim(
        _producer_ctx(),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event", "evt-1"),
    )
    assert claim.source_authority == AUTHORITY_UNATTRIBUTED
    assert claim.status == STATUS_UNLINKED
    assert claim.subject_entity_id is None


# ---------------------------------------------------------------------------
# Visibility / ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_cannot_be_more_visible_than_its_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, executed, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid), subject_visibility="private")

    with pytest.raises(ClaimRejected) as exc:
        await service.stage_claim(
            _producer_ctx(tid, aid),
            subject_reference=str(uuid.uuid4()),
            predicate="owned_by_team",
            value="platform",
            evidence=_evidence("session_event"),
            visibility="public",
        )
    assert exc.value.reason == REJECT_VISIBILITY
    assert not any("INSERT INTO memory_claims (" in s for s in executed)


@pytest.mark.asyncio
async def test_visibility_defaults_to_the_subjects_own(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(tid), subject_visibility="private")

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event"),
    )
    assert claim.visibility == "private"


@pytest.mark.asyncio
async def test_the_owning_tenant_is_the_subjects_not_the_authors(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_tid = uuid.uuid4()
    author_tid, author_aid = uuid.uuid4(), uuid.uuid4()
    service, _, _ = _stage_service(monkeypatch, resolved_entity=_entity(owner_tid), subject_visibility="public")

    claim = await service.stage_claim(
        _producer_ctx(author_tid, author_aid),
        subject_reference=str(uuid.uuid4()),
        predicate="owned_by_team",
        value="someone else",
        evidence=_evidence("session_event"),
    )
    assert claim.owning_tenant_id == owner_tid
    assert claim.owning_tenant_id != author_tid


# ---------------------------------------------------------------------------
# Subject resolution claim_authority.py owns directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unresolvable_subject_stores_unlinked_rather_than_dropping(monkeypatch: pytest.MonkeyPatch) -> None:
    service, executed, _ = _stage_service(monkeypatch, resolved_entity=None)

    claim = await service.stage_claim(
        _producer_ctx(),
        subject_reference="github:acme/not-in-the-catalog",
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event"),
    )
    assert claim.status == STATUS_UNLINKED
    assert claim.subject_entity_id is None
    assert claim.owning_tenant_id is None
    # No visibility query for an unresolved subject -- there is nothing to
    # derive from, so the row is skipped rather than queried and discarded.
    assert not any("SELECT visibility FROM entities" in s for s in executed)


@pytest.mark.asyncio
async def test_an_external_id_reference_resolves_through_its_own_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `system:identifier` fallback is claim_authority.py's own SQL, not
    the visibility chokepoint's -- exercised here without mocking it away."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    subject_id = uuid.uuid4()
    row = MagicMock(entity_id=subject_id, tenant_id=tid)
    service, _, _ = _stage_service(monkeypatch, resolved_entity=None, external_id_row=row)

    claim = await service.stage_claim(
        _producer_ctx(tid, aid),
        subject_reference="github:acme/billing",
        predicate="owned_by_team",
        value="platform",
        evidence=_evidence("session_event"),
    )
    assert claim.status == STATUS_STAGED
    assert claim.subject_entity_id == subject_id
    assert claim.owning_tenant_id == tid


# ---------------------------------------------------------------------------
# link_subject
# ---------------------------------------------------------------------------


def _link_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim_row: Any | None,
    resolved_entity: Any | None = None,
    subject_visibility: str = "tenant-shared",
    provenance_rows: tuple[Any, ...] = (),
    contest_outcome: ContestOutcome | None = None,
) -> tuple[ClaimService, list[str], list[tuple[str, dict]], AsyncMock]:
    executed: list[str] = []
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        captured.append((sql, dict(params or {})))
        result = MagicMock()

        if "author_tenant_id, status, predicate, value_jsonb" in sql:
            result.one_or_none = MagicMock(return_value=claim_row)
            return result
        if "SELECT visibility FROM entities" in sql:
            result.scalar_one = MagicMock(return_value=subject_visibility)
            return result
        if "SELECT evidence_kind, derivation FROM memory_claim_provenance" in sql:
            result.all = MagicMock(return_value=list(provenance_rows))
            return result
        if "subject_entity_id = :sid" in sql:
            return result
        if "SELECT status, source_authority, is_contested" in sql:
            result.one_or_none = MagicMock(
                return_value=MagicMock(status="staged", source_authority="owner_inference", is_contested=False)
            )
            return result
        if "SELECT independence_key, independence_group" in sql:
            result.all = MagicMock(return_value=[])
            return result
        if "FROM memory_predicate_churn" in sql:
            # No inspected per-predicate rate, so decay falls back to the
            # authored category figure -- what these assertions were written
            # against and what every deployment does until a fit is inspected.
            result.all = MagicMock(return_value=[])
            return result
        if "UPDATE memory_claims SET" in sql and "confidence" in sql:
            return result
        if "INSERT INTO audit_log" in sql:
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    detect_mock = _patch_collaborators(monkeypatch, resolved_entity=resolved_entity, contest_outcome=contest_outcome)

    service = ClaimService(factory, clock=FakeClock(_NOW))
    return service, executed, captured, detect_mock


@pytest.mark.asyncio
async def test_link_subject_raises_not_found_for_a_missing_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _, _ = _link_service(monkeypatch, claim_row=None)

    with pytest.raises(NotFoundError):
        await service.link_subject(
            tenant_context(roles=["admin"]), claim_id=uuid.uuid4(), subject_reference="github:acme/x"
        )


@pytest.mark.asyncio
async def test_link_subject_requires_the_producer_or_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _, _ = _link_service(monkeypatch, claim_row=None)

    with pytest.raises(PermissionError):
        await service.link_subject(
            tenant_context(roles=["consumer"]), claim_id=uuid.uuid4(), subject_reference="github:acme/x"
        )
    # Refused before any session is even opened.
    service._session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_link_subject_refuses_a_curator_from_another_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    service, _, _, _ = _link_service(monkeypatch, claim_row=claim_row)

    with pytest.raises(PermissionError):
        await service.link_subject(
            tenant_context(roles=["admin"]),  # a different, freshly-generated tenant than tid
            claim_id=uuid.uuid4(),
            subject_reference="github:acme/x",
        )


@pytest.mark.asyncio
async def test_link_subject_refuses_a_claim_that_is_already_staged(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_STAGED, predicate="owned_by_team", value="platform")
    service, _, _, _ = _link_service(monkeypatch, claim_row=claim_row)

    with pytest.raises(ConflictError):
        await service.link_subject(
            tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
            claim_id=uuid.uuid4(),
            subject_reference="github:acme/x",
        )


@pytest.mark.asyncio
async def test_link_subject_refuses_a_reference_that_still_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    service, executed, _, _ = _link_service(monkeypatch, claim_row=claim_row, resolved_entity=None)

    with pytest.raises(ValidationError):
        await service.link_subject(
            tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
            claim_id=uuid.uuid4(),
            subject_reference=str(uuid.uuid4()),
        )
    # A failed link must not have flipped anything.
    assert not any("subject_entity_id = :sid" in s for s in executed)


@pytest.mark.asyncio
async def test_link_subject_narrows_visibility_to_the_subjects_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever visibility was requested before the subject resolved was
    already discarded at stage time; linking derives fresh from the subject
    rather than reviving a request nobody could evaluate then."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    service, _, _, _ = _link_service(
        monkeypatch,
        claim_row=claim_row,
        resolved_entity=_entity(tid),
        subject_visibility="tenant-shared",
    )

    linked = await service.link_subject(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=uuid.uuid4(),
        subject_reference=str(uuid.uuid4()),
    )
    assert linked.visibility == "tenant-shared"
    assert linked.status == STATUS_STAGED


@pytest.mark.asyncio
async def test_link_subject_derives_owner_authority_when_the_linking_tenant_owns_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    service, _, _, _ = _link_service(
        monkeypatch,
        claim_row=claim_row,
        resolved_entity=_entity(tid),
        provenance_rows=(MagicMock(evidence_kind="session_event", derivation="inference"),),
    )

    linked = await service.link_subject(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=uuid.uuid4(),
        subject_reference=str(uuid.uuid4()),
    )
    assert linked.source_authority == AUTHORITY_OWNER_INFERENCE


@pytest.mark.asyncio
async def test_link_subject_derives_observer_authority_when_the_linking_tenant_does_not_own_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority flips on who owns the *resolved* subject, not on who
    authored the claim: identical evidence earns a lower tier once the
    subject turns out to belong to somebody else's tenant."""
    owner_tid = uuid.uuid4()
    author_tid, author_aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(
        author_tenant_id=author_tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform"
    )
    service, _, _, _ = _link_service(
        monkeypatch,
        claim_row=claim_row,
        resolved_entity=_entity(owner_tid),
        provenance_rows=(MagicMock(evidence_kind="session_event", derivation="inference"),),
    )

    linked = await service.link_subject(
        tenant_context(tenant_id=author_tid, actor_id=author_aid, roles=["admin"]),
        claim_id=uuid.uuid4(),
        subject_reference=str(uuid.uuid4()),
    )
    assert linked.owning_tenant_id == owner_tid
    assert linked.source_authority == AUTHORITY_OBSERVER_INFERENCE


@pytest.mark.asyncio
async def test_link_subject_reruns_contest_detection_and_rescopes_the_counterparty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim excluded from every neighbourhood query while unlinked can,
    the moment it gets a subject, disagree with something already there --
    and the counterparty's own score has to be recomputed too."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    outcome = ContestOutcome(
        detected=(
            Disagreement(
                lower_claim_id=min(claim_id, counterparty_id, key=str),
                upper_claim_id=max(claim_id, counterparty_id, key=str),
                predicate="owned_by_team",
                lower_value="platform",
                upper_value="core-services",
            ),
        ),
        neighbourhood_size=1,
        truncated=False,
    )
    service, executed, _, detect_mock = _link_service(
        monkeypatch, claim_row=claim_row, resolved_entity=_entity(tid), contest_outcome=outcome
    )

    linked = await service.link_subject(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=claim_id,
        subject_reference=str(uuid.uuid4()),
    )

    assert linked.is_contested is True
    detect_mock.assert_awaited_once()
    # The counterparty's own rescore ran: its own row lookup fired.
    assert any("SELECT status, source_authority, is_contested" in s for s in executed)


@pytest.mark.asyncio
async def test_link_subject_writes_an_audit_row_naming_the_action_and_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_id = uuid.uuid4()
    claim_row = MagicMock(author_tenant_id=tid, status=STATUS_UNLINKED, predicate="owned_by_team", value="platform")
    service, _, captured, _ = _link_service(monkeypatch, claim_row=claim_row, resolved_entity=_entity(tid))

    await service.link_subject(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=claim_id,
        subject_reference=str(uuid.uuid4()),
    )

    audit_calls = [(sql, params) for sql, params in captured if "INSERT INTO audit_log" in sql]
    assert len(audit_calls) == 1
    _, params = audit_calls[0]
    assert params["action"] == actions.CLAIM_LINKED
    assert params["tid"] == tid
    assert params["target"] == claim_id


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------


def _discard_service(
    monkeypatch: pytest.MonkeyPatch, *, claim_row: Any | None
) -> tuple[ClaimService, list[str], list[tuple[str, dict]]]:
    executed: list[str] = []
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        captured.append((sql, dict(params or {})))
        result = MagicMock()

        if "owning_tenant_id, author_tenant_id, subject_entity_id, status" in sql:
            result.one_or_none = MagicMock(return_value=claim_row)
            return result
        if "status = 'rejected'" in sql:
            return result
        if "INSERT INTO audit_log" in sql:
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    service = ClaimService(factory, clock=FakeClock(_NOW))
    return service, executed, captured


@pytest.mark.asyncio
async def test_discard_raises_not_found_for_a_missing_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _discard_service(monkeypatch, claim_row=None)

    with pytest.raises(NotFoundError):
        await service.discard(tenant_context(roles=["admin"]), claim_id=uuid.uuid4(), reason="x")


@pytest.mark.asyncio
async def test_discard_requires_the_producer_or_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _discard_service(monkeypatch, claim_row=None)

    with pytest.raises(PermissionError):
        await service.discard(tenant_context(roles=["consumer"]), claim_id=uuid.uuid4(), reason="nope")
    service._session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_discard_refuses_a_curator_from_another_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()
    claim_row = MagicMock(owning_tenant_id=tid, author_tenant_id=tid, subject_entity_id=uuid.uuid4(), status="staged")
    service, _, _ = _discard_service(monkeypatch, claim_row=claim_row)

    with pytest.raises(PermissionError):
        await service.discard(tenant_context(roles=["admin"]), claim_id=uuid.uuid4(), reason="nope")


@pytest.mark.asyncio
async def test_discard_refuses_a_claim_that_is_not_staged_or_unlinked(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_row = MagicMock(owning_tenant_id=tid, author_tenant_id=tid, subject_entity_id=uuid.uuid4(), status="rejected")
    service, _, _ = _discard_service(monkeypatch, claim_row=claim_row)

    with pytest.raises(ConflictError):
        await service.discard(
            tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]), claim_id=uuid.uuid4(), reason="again"
        )


@pytest.mark.asyncio
async def test_discard_rejects_a_staged_claim_and_audits_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_id = uuid.uuid4()
    claim_row = MagicMock(owning_tenant_id=tid, author_tenant_id=tid, subject_entity_id=uuid.uuid4(), status="staged")
    service, executed, captured = _discard_service(monkeypatch, claim_row=claim_row)

    await service.discard(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=claim_id,
        reason="wrong team, corrected verbally",
    )

    assert any("status = 'rejected'" in s for s in executed)
    audit_calls = [(sql, params) for sql, params in captured if "INSERT INTO audit_log" in sql]
    assert len(audit_calls) == 1
    _, params = audit_calls[0]
    assert params["action"] == actions.CLAIM_DISCARDED
    assert params["tid"] == tid
    assert '"reason": "wrong team, corrected verbally"' in params["after"]


@pytest.mark.asyncio
async def test_discard_rejects_an_unlinked_claim_via_the_author_tenant_queue_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlinked claim has no owner yet, so it sits in the author's queue --
    the claim_tenant_id COALESCE fallback -- and the terminal shape it lands
    in leaves subject and confidence exactly as staging left them (both NULL),
    which this test proves by never routing a confidence-bearing UPDATE
    through the SQL router at all."""
    tid, aid = uuid.uuid4(), uuid.uuid4()
    claim_id = uuid.uuid4()
    claim_row = MagicMock(owning_tenant_id=None, author_tenant_id=tid, subject_entity_id=None, status="unlinked")
    service, executed, captured = _discard_service(monkeypatch, claim_row=claim_row)

    await service.discard(
        tenant_context(tenant_id=tid, actor_id=aid, roles=["admin"]),
        claim_id=claim_id,
        reason="dead reference, will never resolve",
    )

    assert any("status = 'rejected'" in s for s in executed)
    audit_calls = [(sql, params) for sql, params in captured if "INSERT INTO audit_log" in sql]
    _, params = audit_calls[0]
    # The author tenant, not the (absent) owning tenant, is who this claim's
    # queue belonged to.
    assert params["tid"] == tid


# ---------------------------------------------------------------------------
# close_superseded / set_promotion_state: caller-supplied session
# ---------------------------------------------------------------------------


def _bare_service(monkeypatch: pytest.MonkeyPatch) -> ClaimService:
    monkeypatch.setattr(claim_writer_module, "project_claim", AsyncMock(return_value=False))
    return ClaimService(MagicMock(), clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_close_superseded_refuses_an_unknown_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bare_service(monkeypatch)
    session = AsyncMock()
    session.execute = AsyncMock()

    with pytest.raises(ValidationError):
        await service.close_superseded(
            session, claim_id=uuid.uuid4(), survivor=uuid.uuid4(), reason="deleted_because_i_felt_like_it", now=_NOW
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_superseded_updates_status_and_projects_the_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bare_service(monkeypatch)
    claim_id, survivor = uuid.uuid4(), uuid.uuid4()
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        captured.append((" ".join(str(stmt).split()), dict(params or {})))
        return MagicMock()

    session = AsyncMock()
    session.execute = _execute

    await service.close_superseded(session, claim_id=claim_id, survivor=survivor, reason="lost_conflict", now=_NOW)

    assert len(captured) == 1
    sql, params = captured[0]
    assert "status = 'superseded'" in sql
    assert "WHERE claim_id = :cid AND status = 'staged'" in sql
    assert params == {"cid": claim_id, "survivor": survivor, "reason": "lost_conflict", "now": _NOW}
    claim_writer_module.project_claim.assert_awaited_once_with(session, claim_id=claim_id, now=_NOW)


@pytest.mark.asyncio
async def test_set_promotion_state_refuses_an_unknown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``ValueError``, not yet rebased onto the unified error tree --
    pinned as it behaves today; carried forward, not fixed, in this task."""
    service = _bare_service(monkeypatch)
    session = AsyncMock()
    session.execute = AsyncMock()

    with pytest.raises(ValueError):
        await service.set_promotion_state(session, claim_id=uuid.uuid4(), state="on_the_fence")
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_promotion_state_writes_the_new_state(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bare_service(monkeypatch)
    claim_id = uuid.uuid4()
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        captured.append((" ".join(str(stmt).split()), dict(params or {})))
        return MagicMock()

    session = AsyncMock()
    session.execute = _execute

    await service.set_promotion_state(session, claim_id=claim_id, state="promoted")

    assert len(captured) == 1
    sql, params = captured[0]
    assert "SET promotion_state = :state" in sql
    assert params == {"cid": claim_id, "state": "promoted"}
