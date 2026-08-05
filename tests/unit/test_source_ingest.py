"""Unit tests for `registry.service.memory.source_ingest`.

`ClaimService`, `SourceGovernanceService`, and the catalog are all bare
`MagicMock`/`AsyncMock` collaborators (each has its own unit suite; this
file only asserts *how* `SourceIngestService` calls them). No Postgres, no
`AsyncMock`-router session -- this module never opens a session itself, it
only orchestrates other services.

Coverage:
- The three connector parsers (`parse_document`, `parse_work_item`,
  `parse_incident`): the regex extraction patterns, the page@revision
  citation shape, and the incident timestamp's UTC/"Z" formatting.
- `ingest`'s governed-bridge sequencing: the empty-batch short-circuit
  (never calls governance at all), governance admits or refuses the *whole*
  batch before any claim is staged (all-or-nothing), and refusal means zero
  writes.
- The admission call is content-blind: `governance.admit` receives only
  `source_id` and `count`, never the candidates themselves -- proven here by
  asserting the exact call, and by showing that a candidate whose value
  looks like PII is staged verbatim, because this module has no scanning
  step to intercept it (that is a deliberate posture of the governed-source
  path, not an oversight to fix).
- The provisioning branch: reached only when `policy.may_provision_entities`
  is set *and* the staged claim came back unlinked; skipped when either half
  is false; `policy_for` is read once per batch, not once per candidate; the
  provisioning write runs under an elevated role scoped to that one call and
  never mutates or reuses the caller's own context; and the explicit
  misconfiguration guard (opted in, but wired without a catalog) fails
  loudly rather than silently dropping the entity.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.memory.claim_authority import STATUS_STAGED, STATUS_UNLINKED, Evidence, StagedClaim
from registry.service.memory.source_governance import Admission, SourcePolicy
from registry.service.memory.source_ingest import (
    EVIDENCE_DOCUMENT,
    EVIDENCE_INCIDENT,
    EVIDENCE_WORK_ITEM,
    Candidate,
    IngestResult,
    SourceIngestService,
    parse_document,
    parse_incident,
    parse_work_item,
)
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# parse_document
# ---------------------------------------------------------------------------


def test_parse_document_extracts_each_pattern_and_cites_the_page_and_revision() -> None:
    body = "Owner: platform-team\nRTO: 300 seconds\nrunbook: https://runbooks.example/x"
    candidates = parse_document(subject_reference="cap:checkout", page_id="p-1", revision="r-9", body=body)

    by_predicate = {c.predicate: c for c in candidates}
    assert by_predicate["owned_by_team"].value == "platform-team"
    assert by_predicate["recovery_time_objective_seconds"].value == 300
    assert isinstance(by_predicate["recovery_time_objective_seconds"].value, int)
    assert by_predicate["runbook_url"].value == "https://runbooks.example/x"
    for candidate in candidates:
        assert candidate.evidence[0].kind == EVIDENCE_DOCUMENT
        assert candidate.evidence[0].ref == "p-1@r-9"


def test_parse_document_finds_nothing_in_prose_that_matches_no_pattern() -> None:
    candidates = parse_document(
        subject_reference="cap:checkout", page_id="p-1", revision="r-1", body="just some unrelated prose"
    )

    assert candidates == ()


def test_parse_document_skips_a_match_whose_captured_value_is_only_whitespace() -> None:
    """`owner:` followed by nothing but trailing spaces still satisfies the
    pattern's `[^\\n]+` (one or more non-newline characters), but the
    stripped value is empty -- that match is dropped rather than staged as
    an owner of the empty string."""
    candidates = parse_document(subject_reference="cap:x", page_id="p", revision="1", body="Owner:    ")

    assert candidates == ()


def test_parse_document_emits_one_candidate_per_match_when_a_pattern_repeats() -> None:
    body = "escalate to: on-call-a\nescalate to: on-call-b"
    candidates = parse_document(subject_reference="cap:x", page_id="p", revision="1", body=body)

    escalations = [c for c in candidates if c.predicate == "escalation_contact"]
    assert [c.value for c in escalations] == ["on-call-a", "on-call-b"]


# ---------------------------------------------------------------------------
# parse_work_item
# ---------------------------------------------------------------------------


def test_parse_work_item_produces_exactly_one_candidate_without_reading_the_summary_as_content() -> None:
    candidates = parse_work_item(
        subject_reference="cap:checkout", item_key="ENG-42", url="https://tracker/ENG-42", summary="fix the thing"
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.predicate == "work_item_url"
    assert candidate.value == "https://tracker/ENG-42"
    assert candidate.evidence == (Evidence(kind=EVIDENCE_WORK_ITEM, ref="ENG-42", excerpt="fix the thing"),)


# ---------------------------------------------------------------------------
# parse_incident
# ---------------------------------------------------------------------------


def test_parse_incident_produces_two_claims_sharing_evidence_and_the_occurred_at_valid_from() -> None:
    occurred_at = datetime.datetime(2026, 3, 1, 8, 30, 0, tzinfo=datetime.UTC)
    candidates = parse_incident(
        subject_reference="cap:checkout",
        incident_id="INC-1",
        report_url="https://postmortems/INC-1",
        occurred_at=occurred_at,
        summary="checkout went down",
    )

    assert len(candidates) == 2
    by_predicate = {c.predicate: c for c in candidates}
    assert by_predicate["incident_occurred_at"].value == "2026-03-01T08:30:00Z"
    assert by_predicate["incident_report_url"].value == "https://postmortems/INC-1"
    for candidate in candidates:
        assert candidate.asserted_valid_from == occurred_at
        assert candidate.evidence[0].kind == EVIDENCE_INCIDENT
        assert candidate.evidence[0].ref == "INC-1"


def test_parse_incident_converts_a_non_utc_offset_to_the_z_suffixed_utc_stamp() -> None:
    occurred_at = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
    candidates = parse_incident(
        subject_reference="cap:x", incident_id="INC-2", report_url="u", occurred_at=occurred_at, summary="s"
    )

    stamp = next(c for c in candidates if c.predicate == "incident_occurred_at").value
    assert stamp == "2026-03-01T07:00:00Z"
    assert "+00:00" not in stamp


# ---------------------------------------------------------------------------
# ingest() -- the governed bridge
# ---------------------------------------------------------------------------


def _ctx(roles: tuple[str, ...] = ("sync_worker",)) -> Any:
    return tenant_context(roles=list(roles))


def _candidate(**overrides: Any) -> Candidate:
    base: dict[str, Any] = {
        "subject_reference": "cap:checkout",
        "predicate": "owned_by_team",
        "value": "platform",
        "evidence": (Evidence(kind=EVIDENCE_DOCUMENT, ref="p@1"),),
        "asserted_valid_from": None,
    }
    base.update(overrides)
    return Candidate(**base)


def _staged(status: str, **overrides: Any) -> StagedClaim:
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "subject_entity_id": None,
        "predicate": "owned_by_team",
        "value": "platform",
        "status": status,
        "visibility": "private",
        "owning_tenant_id": None,
        "source_authority": "owner_extraction",
    }
    base.update(overrides)
    return StagedClaim(**base)


def _governance(*, permitted: bool = True, reason: str | None = None, may_provision: bool = False) -> Any:
    governance = MagicMock()
    governance.admit = AsyncMock(return_value=Admission(permitted=permitted, reason=reason, remaining=100))
    governance.policy_for = AsyncMock(
        return_value=SourcePolicy(
            source_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            authority_tier="owner_extraction",
            ingest_ceiling=1000,
            window_seconds=3600,
            breaker_open_until=None,
            breach_count=0,
            may_provision_entities=may_provision,
        )
    )
    return governance


def _claims(stage_result: StagedClaim | list[StagedClaim] | None = None) -> Any:
    claims = MagicMock()
    if isinstance(stage_result, list):
        claims.stage_claim = AsyncMock(side_effect=stage_result)
    else:
        claims.stage_claim = AsyncMock(return_value=stage_result or _staged(STATUS_STAGED))
    claims.link_subject = AsyncMock(return_value=_staged(STATUS_STAGED))
    return claims


@pytest.mark.asyncio
async def test_ingest_of_an_empty_batch_never_calls_governance_at_all() -> None:
    governance = _governance()
    service = SourceIngestService(claims=_claims(), governance=governance)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=())

    assert result == IngestResult(admitted=True, written=0)
    governance.admit.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_asks_governance_by_count_only_never_by_candidate_content() -> None:
    """Pins the content-blind admission call: `admit` is invoked with the
    source id and the batch size, and nothing else -- the candidates
    themselves are never passed to governance."""
    source_id = uuid.uuid4()
    candidates = (_candidate(), _candidate(predicate="runbook_url", value="https://x"))
    governance = _governance()
    service = SourceIngestService(claims=_claims(), governance=governance)

    await service.ingest(_ctx(), source_id=source_id, candidates=candidates)

    governance.admit.assert_awaited_once_with(source_id, count=2)


@pytest.mark.asyncio
async def test_ingest_refuses_the_whole_batch_and_writes_nothing_when_governance_refuses() -> None:
    governance = _governance(permitted=False, reason="ingest ceiling of 10 per 3600s reached")
    claims = _claims()
    service = SourceIngestService(claims=claims, governance=governance)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(_candidate(), _candidate()))

    assert result == IngestResult(admitted=False, written=0, refused_reason="ingest ceiling of 10 per 3600s reached")
    claims.stage_claim.assert_not_called()
    governance.policy_for.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_stages_every_admitted_candidate_with_its_own_fields() -> None:
    candidates = (
        _candidate(predicate="owned_by_team", value="platform"),
        _candidate(predicate="runbook_url", value="https://runbooks/1", asserted_valid_from=_NOW),
    )
    governance = _governance()
    claims = _claims([_staged(STATUS_STAGED), _staged(STATUS_STAGED)])
    service = SourceIngestService(claims=claims, governance=governance)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=candidates)

    assert result == IngestResult(admitted=True, written=2)
    assert claims.stage_claim.await_count == 2
    first_kwargs = claims.stage_claim.await_args_list[0].kwargs
    assert first_kwargs["predicate"] == "owned_by_team"
    assert first_kwargs["value"] == "platform"
    second_kwargs = claims.stage_claim.await_args_list[1].kwargs
    assert second_kwargs["predicate"] == "runbook_url"
    assert second_kwargs["asserted_valid_from"] == _NOW


@pytest.mark.asyncio
async def test_ingest_does_not_scan_or_redact_candidate_content() -> None:
    """The governed-source path has no PII/containment scan of its own; a
    candidate carrying an SSN-shaped value is staged byte-for-byte. The
    read-path trust label is this posture's defense, not a scan here."""
    pii_shaped = _candidate(predicate="escalation_contact", value="call 123-45-6789 for help")
    governance = _governance()
    claims = _claims()
    service = SourceIngestService(claims=claims, governance=governance)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(pii_shaped,))

    assert result.written == 1
    staged_value = claims.stage_claim.await_args_list[0].kwargs["value"]
    assert staged_value == "call 123-45-6789 for help"


# ---------------------------------------------------------------------------
# ingest() -- the provisioning branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_provisions_and_links_an_unresolved_subject_when_the_policy_allows_it() -> None:
    source_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    unlinked = _staged(STATUS_UNLINKED)
    governance = _governance(may_provision=True)
    claims = _claims(unlinked)
    catalog = MagicMock()
    catalog.create_entity = AsyncMock(return_value=MagicMock(entity_id=entity_id))
    service = SourceIngestService(claims=claims, governance=governance, catalog=catalog)
    ctx = _ctx(roles=("sync_worker",))

    result = await service.ingest(ctx, source_id=source_id, candidates=(_candidate(subject_reference="cap:new"),))

    assert result.written == 1
    catalog.create_entity.assert_awaited_once()
    create_kwargs = catalog.create_entity.await_args.kwargs
    assert create_kwargs["entity_type"] == "capability"
    assert create_kwargs["name"] == "cap:new"
    claims.link_subject.assert_awaited_once()
    link_kwargs = claims.link_subject.await_args.kwargs
    assert link_kwargs["claim_id"] == unlinked.claim_id
    assert link_kwargs["subject_reference"] == str(entity_id)


@pytest.mark.asyncio
async def test_ingest_provisioning_uses_an_elevated_role_scoped_to_that_call_only() -> None:
    """The caller's own context (a sync connector) correctly fails
    `link_subject`'s curator-only check on its own; provisioning elevates
    the role for the one internal call and must not mutate the caller's own
    context object."""
    governance = _governance(may_provision=True)
    claims = _claims(_staged(STATUS_UNLINKED))
    catalog = MagicMock()
    catalog.create_entity = AsyncMock(return_value=MagicMock(entity_id=uuid.uuid4()))
    service = SourceIngestService(claims=claims, governance=governance, catalog=catalog)
    ctx = _ctx(roles=("sync_worker",))

    await service.ingest(ctx, source_id=uuid.uuid4(), candidates=(_candidate(),))

    linking_ctx = claims.link_subject.await_args.args[0]
    assert linking_ctx.roles == ["producer"]
    assert ctx.roles == ["sync_worker"]
    assert linking_ctx.tenant_id == ctx.tenant_id
    assert linking_ctx.actor_id == ctx.actor_id


@pytest.mark.asyncio
async def test_ingest_never_provisions_when_the_policy_has_not_opted_in() -> None:
    governance = _governance(may_provision=False)
    claims = _claims(_staged(STATUS_UNLINKED))
    catalog = MagicMock()
    catalog.create_entity = AsyncMock()
    service = SourceIngestService(claims=claims, governance=governance, catalog=catalog)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(_candidate(),))

    assert result.written == 1
    catalog.create_entity.assert_not_called()
    claims.link_subject.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_never_provisions_a_claim_that_already_resolved() -> None:
    """Even with the policy opted in, provisioning is gated on the staged
    claim coming back unlinked -- a resolved claim is never re-provisioned."""
    governance = _governance(may_provision=True)
    claims = _claims(_staged(STATUS_STAGED))
    catalog = MagicMock()
    catalog.create_entity = AsyncMock()
    service = SourceIngestService(claims=claims, governance=governance, catalog=catalog)

    result = await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(_candidate(),))

    assert result.written == 1
    catalog.create_entity.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_reads_policy_for_once_per_batch_not_once_per_candidate() -> None:
    governance = _governance(may_provision=True)
    claims = _claims([_staged(STATUS_UNLINKED), _staged(STATUS_UNLINKED), _staged(STATUS_UNLINKED)])
    catalog = MagicMock()
    catalog.create_entity = AsyncMock(return_value=MagicMock(entity_id=uuid.uuid4()))
    service = SourceIngestService(claims=claims, governance=governance, catalog=catalog)

    await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(_candidate(), _candidate(), _candidate()))

    governance.policy_for.assert_awaited_once()
    assert catalog.create_entity.await_count == 3


@pytest.mark.asyncio
async def test_ingest_raises_when_provisioning_is_opted_in_but_no_catalog_was_wired() -> None:
    governance = _governance(may_provision=True)
    claims = _claims(_staged(STATUS_UNLINKED))
    service = SourceIngestService(claims=claims, governance=governance, catalog=None)

    with pytest.raises(RuntimeError, match="may_provision_entities"):
        await service.ingest(_ctx(), source_id=uuid.uuid4(), candidates=(_candidate(),))


def test_ctx_replace_used_by_provisioning_is_a_real_dataclass_field_swap() -> None:
    """Sanity check on the fixture itself: `tenant_context(...)` really is a
    plain dataclass, so `dataclasses.replace(ctx, roles=[...])` (what
    `_provision_and_link` does) produces an independent object rather than
    mutating the original -- if this ever stopped being true the provisioning
    test above would be trusting a fixture that no longer models production."""
    ctx = _ctx(roles=("sync_worker",))
    replaced = dataclasses.replace(ctx, roles=["producer"])
    assert replaced.roles == ["producer"]
    assert ctx.roles == ["sync_worker"]
