"""Unit tests for `contextplane/arc/service/source_admission_graph.py`.

No database: the three query functions this service uses are monkeypatched,
and the shared admission transaction is a spy. What is under test is the
service's own judgement -- which promotions it refuses, and what claim it
builds from one it accepts -- not the insert path, which
`test_arc_source_admission.py` already covers against the same
`finish_admission` this delegates to.

The refusal cases matter more than the happy path here. This module exists
to let a governed revision cite something no verifier signed, so every check
that keeps that from becoming "anything at all" is load-bearing: a reversed
promotion, a self-promotion, a claim owned by another tenant, and evidence
that names an event rather than a revision.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from contextplane.arc.service import source_admission_graph as sag
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.queries.source_admission_graph import PromotedClaimRow, ProvenanceRow
from contextplane.arc.service.source_admission import SourceAdmissionRefused
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_PROMOTED_AT = datetime.datetime(2025, 12, 1, 9, 30, tzinfo=datetime.UTC)
_REVIEW = datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_TENANT = uuid.UUID("b0000000-0000-4000-8000-000000000001")
_AUTHOR = uuid.UUID("a0000000-0000-4000-8000-00000000000a")
_PROMOTER = uuid.UUID("a0000000-0000-4000-8000-00000000000b")


class _AllowAll:
    """A permissive `CapabilityVisibility`: the real authorization service
    runs, so what is exercised is this module's scope-building rather than
    a stand-in for the chokepoint.
    """

    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


class _FakeClock:
    def now(self) -> datetime.datetime:
        return _NOW


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _session_factory() -> _FakeSession:
    return _FakeSession()


def _ctx(tenant_id: uuid.UUID = _TENANT) -> ArcRequestContext:
    return ArcRequestContext(
        tenant=TenantContext(
            tenant_id=tenant_id,
            actor_id=uuid.uuid4(),
            roles=["admin"],
            oidc_subject="operator",
        ),
        oidc_issuer=_ISSUER,
    )


def _promoted(**overrides: Any) -> PromotedClaimRow:
    row = PromotedClaimRow(
        claim_id=uuid.UUID("c0000000-0000-4000-8000-00000000000c"),
        owning_tenant_id=_TENANT,
        author_actor_id=_AUTHOR,
        subject_entity_id=uuid.UUID("e0000000-0000-4000-8000-00000000000e"),
        subject_reference="adr-0042",
        predicate="governs_deployment",
        value_jsonb={"rule": "production deploys reference an approved change ticket"},
        claim_status="staged",
        source_authority="human",
        asserted_valid_from=datetime.datetime(2025, 11, 1, tzinfo=datetime.UTC),
        asserted_valid_to=None,
        promotion_id=uuid.UUID("d0000000-0000-4000-8000-00000000000d"),
        promoted_at=_PROMOTED_AT,
        promoted_by=_PROMOTER,
        reversed_at=None,
        created_row_id=uuid.UUID("f0000000-0000-4000-8000-00000000000f"),
        target_kind="attribute",
    )
    return dataclasses.replace(row, **overrides)


_COMMIT = ProvenanceRow(
    evidence_kind="commit",
    evidence_ref="bitbucket.org/acme/adr@9f3c1ad",
    evidence_excerpt="All production deploys must reference an approved change ticket.",
    derivation="human",
)
_DOC = ProvenanceRow(
    evidence_kind="document_revision",
    evidence_ref="adr-0042@rev7",
    evidence_excerpt=None,
    derivation="human",
)
_SESSION_EVENT = ProvenanceRow(
    evidence_kind="session_event",
    evidence_ref="session:abc",
    evidence_excerpt=None,
    derivation="extraction",
)


class _SpyAdmission:
    """Stands in for `SourceAdmissionService`, capturing one call."""

    def __init__(self) -> None:
        self.call: dict[str, Any] | None = None

    async def finish_admission(self, ctx: ArcRequestContext, **kwargs: Any) -> str:
        self.call = {"ctx": ctx, **kwargs}
        return "evidence"


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim: PromotedClaimRow | None,
    provenance: tuple[ProvenanceRow, ...] = (_COMMIT,),
    subject: str | None = "rina@acme.example",
) -> tuple[sag.GraphPromotionAdmissionService, _SpyAdmission]:
    async def load_promoted_claim(_session: Any, **_: Any) -> PromotedClaimRow | None:
        return claim

    async def load_claim_provenance(_session: Any, _claim_id: Any) -> tuple[ProvenanceRow, ...]:
        return provenance

    async def load_actor_subject(_session: Any, _actor_id: Any) -> str | None:
        return subject

    monkeypatch.setattr(sag.queries, "load_promoted_claim", load_promoted_claim)
    monkeypatch.setattr(sag.queries, "load_claim_provenance", load_claim_provenance)
    monkeypatch.setattr(sag.queries, "load_actor_subject", load_actor_subject)

    spy = _SpyAdmission()
    service = sag.GraphPromotionAdmissionService(
        _session_factory,
        admission=spy,  # type: ignore[arg-type]
        authorization=ArcAuthorizationService(visibility=_AllowAll()),
        clock=_FakeClock(),
    )
    return service, spy


def _request(**overrides: Any) -> sag.GraphPromotionAdmission:
    base = sag.GraphPromotionAdmission(
        claim_id=uuid.UUID("c0000000-0000-4000-8000-00000000000c"),
        source_system="bitbucket.org/acme/adr",
        review_expires_at=_REVIEW,
        idempotency_key="idem-1",
    )
    return dataclasses.replace(base, **overrides)


class TestRefusals:
    async def test_refuses_a_claim_that_is_not_promoted_for_this_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, spy = _service(monkeypatch, claim=None)
        with pytest.raises(SourceAdmissionRefused, match="not a promoted claim"):
            await service.admit_promoted_claim(_ctx(), _request())
        assert spy.call is None

    async def test_refuses_a_reversed_promotion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reversed_at = datetime.datetime(2025, 12, 20, tzinfo=datetime.UTC)
        service, spy = _service(monkeypatch, claim=_promoted(reversed_at=reversed_at))
        with pytest.raises(SourceAdmissionRefused, match="was reversed"):
            await service.admit_promoted_claim(_ctx(), _request())
        assert spy.call is None

    async def test_refuses_when_the_author_promoted_their_own_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, spy = _service(monkeypatch, claim=_promoted(promoted_by=_AUTHOR))
        with pytest.raises(SourceAdmissionRefused, match="second actor"):
            await service.admit_promoted_claim(_ctx(), _request())
        assert spy.call is None

    async def test_refuses_a_promotion_with_no_promoting_actor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _service(monkeypatch, claim=_promoted(promoted_by=None))
        with pytest.raises(SourceAdmissionRefused, match="no promoting actor"):
            await service.admit_promoted_claim(_ctx(), _request())

    async def test_refuses_evidence_that_names_an_event_rather_than_a_revision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _ = _service(monkeypatch, claim=_promoted(), provenance=(_SESSION_EVENT,))
        with pytest.raises(SourceAdmissionRefused, match="source revision locator"):
            await service.admit_promoted_claim(_ctx(), _request())

    async def test_refuses_a_review_deadline_that_has_already_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _service(monkeypatch, claim=_promoted())
        past = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
        with pytest.raises(SourceAdmissionRefused, match="not in the future"):
            await service.admit_promoted_claim(_ctx(), _request(review_expires_at=past))


class TestAdmittedClaim:
    async def test_builds_a_claim_attributing_approval_to_the_promotion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, spy = _service(monkeypatch, claim=_promoted())
        await service.admit_promoted_claim(_ctx(), _request())

        assert spy.call is not None
        claim = spy.call["claim"]
        assert claim["source_system"] == "bitbucket.org/acme/adr"
        assert claim["source_revision_locator"] == "commit:bitbucket.org/acme/adr@9f3c1ad"
        assert claim["approval_locator"] == "promotion:d0000000-0000-4000-8000-00000000000d"
        assert claim["approving_authority_subject"] == "rina@acme.example"
        assert claim["approval_scope"] == "claim:c0000000-0000-4000-8000-00000000000c"
        # The promotion's own timestamp, not the admission's: what was approved
        # happened when a second actor promoted the claim.
        assert claim["approved_at"] == "2025-12-01T09:30:00Z"
        assert claim["expires_at"] == "2026-04-01T00:00:00Z"

    async def test_admits_under_the_graph_authority_with_no_proof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, spy = _service(monkeypatch, claim=_promoted())
        await service.admit_promoted_claim(_ctx(), _request())

        assert spy.call is not None
        assert spy.call["admission_method"] == "graph_promotion"
        assert spy.call["proof"].verification_method == "graph_promotion"
        # Neither representation: the DB constraint requires both NULL for this
        # method, and the proof carries nothing that could populate either.
        assert spy.call["proof"].signature_base64 is None
        assert spy.call["proof"].assertion_base64 is None
        assert spy.call["connector_id"] is None
        assert spy.call["policy_id"] is None
        assert spy.call["verifier_id"] == "promotion:d0000000-0000-4000-8000-00000000000d"

    async def test_digest_is_computed_over_the_admitted_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, spy = _service(monkeypatch, claim=_promoted())
        await service.admit_promoted_claim(_ctx(), _request())

        assert spy.call is not None
        body = spy.call["content_bytes"]
        assert spy.call["content_bytes_len"] == len(body)
        assert spy.call["claim"]["source_content_digest"] == spy.call["content_digest"]
        # The projection round-trips, so a directive can anchor into it.
        projection = json.loads(body)
        assert projection["promotion"]["promotion_id"] == "d0000000-0000-4000-8000-00000000000d"
        assert projection["claim"]["predicate"] == "governs_deployment"
        assert projection["evidence"][0]["ref"] == "bitbucket.org/acme/adr@9f3c1ad"

    async def test_falls_back_to_the_actor_id_when_no_subject_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, spy = _service(monkeypatch, claim=_promoted(), subject=None)
        await service.admit_promoted_claim(_ctx(), _request())

        assert spy.call is not None
        assert spy.call["claim"]["approving_authority_subject"] == str(_PROMOTER)


class TestLocatorSelection:
    def test_prefers_a_commit_over_a_document_revision(self) -> None:
        assert sag._locator(_promoted(), (_DOC, _COMMIT)) == "commit:bitbucket.org/acme/adr@9f3c1ad"

    def test_accepts_a_document_revision_when_no_commit_exists(self) -> None:
        assert sag._locator(_promoted(), (_SESSION_EVENT, _DOC)) == "document_revision:adr-0042@rev7"


class TestProjectionDeterminism:
    def test_two_projections_of_one_promotion_are_byte_identical(self) -> None:
        first = sag._canonical_projection(_promoted(), (_COMMIT, _DOC), promoted_by_subject="rina")
        second = sag._canonical_projection(_promoted(), (_COMMIT, _DOC), promoted_by_subject="rina")
        assert first == second

    def test_a_changed_promotion_changes_the_bytes(self) -> None:
        base = sag._canonical_projection(_promoted(), (_COMMIT,), promoted_by_subject="rina")
        moved = sag._canonical_projection(
            _promoted(promotion_id=uuid.UUID("d0000000-0000-4000-8000-000000000099")),
            (_COMMIT,),
            promoted_by_subject="rina",
        )
        assert base != moved
