"""Proof that a revision authored through the real authoring surface --
submit() through approval, checkpoint export, and activation, never a
`seed_arc` SQL fixture -- resolves through the real `ResolutionService`,
and that a *mandatory* obligation it carries is what the resulting receipt
records.

Two existing suites each prove half of this and neither proves both
together. `test_arc_post_activation_serving.py` authors a revision through
this exact real pipeline and shows `corpus.py`/`selection.py` can select
its directive -- but it deliberately keeps that directive's applicability
rule non-mandatory (see that file's own module docstring for why: a
mandatory rule requires observation qualification before activation, and
standing that up was out of that file's own scope). `test_arc_resolution.
py` proves `ResolutionService.resolve()` writes a receipt -- but every one
of its `SelectionInput`s carries an empty `candidates` tuple by
construction (see that file's own module docstring: it exists to prove the
resolution *transaction*'s ordering and atomicity, not the content of any
one directive), and the revision underneath it comes from `seed_arc`'s raw
SQL fixture, never from this surface's own writer. This file drives one
revision through the real pipeline, with a mandatory applicability rule,
into the real `ResolutionService`, and asserts the receipt it produces
names that exact directive as the mandatory obligation it satisfied.

**Why the applicability rule can be mandatory here, when `test_arc_post_
activation_serving.py`'s own candidate deliberately is not.** ADR 041's
risk reducer classifies any `is_mandatory=True` rule, at any scope, as
requiring observation qualification before activation (`contextplane.arc.
service.risk`'s own module docstring; `qualification.py`'s private
`_requires_observation` is the identical rule, transcribed there rather
than imported for the reason its own docstring gives). This file pays that
cost directly: `_mandatory_qualification_provider` seeds a cohort and an
already-`qualified` qualification row -- the same way `test_arc_
observation.py` seeds them for its own, unrelated constraint proofs,
because nothing in this codebase evaluates real observed traffic inline
(`qualification.py`'s own module docstring calls that wiring explicitly
out of scope) -- and then accepts it through the real `QualificationService.
accept()`, so activation's `observation_qualified` and `actor_separation`
predicates run for real rather than being stubbed out.

**Why `ResolutionService` is built here rather than read off `wired_app`.**
The wired app's own `arc_resolution` service is `None` on every deployment
today: ARC's key-material hierarchy starts empty and resolution is wired
only once there is key material behind it (`contextplane/wiring/services.py`'s
own comment on `arc_resolution`). `test_arc_resolution.py` already
establishes the pattern this file follows -- construct a real
`ResolutionService` with test key material -- because that is the only way
any test in this codebase exercises it; this file does the same, injecting
the wired app's own `arc_integrity`/`arc_corpus` so the one thing under
proof (a real, activated revision reaching a real receipt) uses genuine
collaborators throughout, not a second stand-in for machinery this file
does not need to fake.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas.canonical import canonicalize_host_attestation_envelope
from contextplane.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from contextplane.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    HostSignerKeyRegistry,
    ManifestClaims,
)
from contextplane.arc.service.challenge import CHALLENGE_TTL, ChallengeNonceDeriver, ChallengeService
from contextplane.arc.service.queries import observation as obs_queries
from contextplane.arc.service.receipt import ReceiptService, ReplayEnvelope
from contextplane.arc.service.resolution import ResolutionRequest, ResolutionService, parse_manifest
from contextplane.arc.types import ArcRequestContext, ResolutionStatus
from contextplane.main import create_app
from contextplane.types import TenantContext
from tests.helpers.arc_authoring_pipeline import AUTHORING_NOW, ISSUER, seed_and_activate
from tests.helpers.arc_fixtures import SIGNING_KEY_ID, provenance, signing_provider
from tests.helpers.auth_harness import default_settings
from tests.helpers.clock import FakeClock

_HOST_ID = "resolution-host-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"
_NONCE_KEY = "nk1"
_ACCEPTER = "accepter-1"


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    """The real app, through its own lifespan -- matching every sibling
    activation/approval integration test's own `wired_app` fixture."""
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def _mandatory_applicability(rule_id: uuid.UUID) -> list[dict[str, object]]:
    """One task-scoped, selector-free rule -- matches any manifest naming
    the `code_change` task kind and `merge` action class -- with
    `is_mandatory=True`, the one field `test_arc_post_activation_serving.
    py`'s own candidate deliberately keeps `False`."""
    return [
        {
            "rule_id": str(rule_id),
            "scope": "intent",
            "target_tenant_id": None,
            "capability_ids": None,
            "capability_labels": None,
            "domain_ids": None,
            "intent_kinds": None,
            "action_classes": None,
            "environments": None,
            "data_sensitivity_tiers": None,
            "effective_from": None,
            "effective_until": None,
            "is_mandatory": True,
        }
    ]


def _mandatory_qualification_provider(
    factory: async_sessionmaker[AsyncSession], services: Any
) -> Callable[[uuid.UUID, uuid.UUID, uuid.UUID, int], Awaitable[uuid.UUID]]:
    """Build the `qualification_id_provider` `seed_and_activate` calls once
    submit() has produced `revision_id`/`proposal_id`/`proposal_version`.

    Seeds a cohort and a `computed_decision='qualified'` qualification row
    directly -- standing in for the real observation window the same way
    `test_arc_observation.py` already does for its own, unrelated
    constraint proofs (see this module's own docstring for why nothing in
    this codebase can produce that decision from real traffic in a test) --
    then accepts it through the real `QualificationService.accept()`, so
    activation's `observation_qualified`/`actor_separation` predicates see
    a genuinely accepted qualification, not a hand-written one.
    """

    async def _provider(
        tenant_id: uuid.UUID, revision_id: uuid.UUID, proposal_id: uuid.UUID, proposal_version: int
    ) -> uuid.UUID:
        cohort_id = uuid.uuid4()
        qualification_id = uuid.uuid4()
        window_started_at = AUTHORING_NOW
        window_deadline = window_started_at + datetime.timedelta(hours=24)
        async with factory() as session, session.begin():
            await obs_queries.insert_cohort(
                session,
                cohort_id=cohort_id,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                candidate_revision_id=revision_id,
                risk_classification="intent_mandatory",
                scope_predicate_digest="a" * 64,
                tenant_membership_digest="b" * 64,
                eligibility_predicate_digest="c" * 64,
                frozen_at=window_started_at,
                window_started_at=window_started_at,
                window_deadline=window_deadline,
            )
            await session.execute(
                text(
                    "UPDATE arc_observation_cohorts SET closed_at = :closed, window_ended_at = :ended "
                    "WHERE cohort_id = :cid"
                ),
                {"closed": window_deadline, "ended": window_deadline, "cid": cohort_id},
            )
            await session.execute(
                text(
                    "INSERT INTO arc_observation_qualifications ("
                    " qualification_id, idempotency_key_digest, candidate_review_package_digest,"
                    " candidate_revision_id, proposal_id, proposal_version, risk_classification,"
                    " risk_algorithm_version, baseline_revision_id, selection_engine_version,"
                    " engine_configuration_version, cohort_id, cohort_digest, window_started_at, window_ended_at,"
                    " eligible_count, observed_count, expected_impact_envelope_digest, counters_by_delta_code,"
                    " unexplained_count, out_of_envelope_count, replay_corpus_digest, replay_result_digest,"
                    " qualification_algorithm_version, computed_decision, computed_at, reason_codes"
                    ") VALUES ("
                    " :qid, :ikd, :crpd, :rid, :pid, :pv, :rc, :rav, NULL, :sev, :ecv, :cid, :cd, :wsa, :wea,"
                    " :ec, :oc, :eied, CAST('[]' AS JSONB), 0, 0, NULL, NULL, :qav, 'qualified', :computed_at,"
                    " ARRAY['window_met']"
                    ")"
                ),
                {
                    "qid": qualification_id,
                    "ikd": uuid.uuid4().hex + uuid.uuid4().hex,
                    "crpd": uuid.uuid4().hex + uuid.uuid4().hex,
                    "rid": revision_id,
                    "pid": proposal_id,
                    "pv": proposal_version,
                    "rc": "intent_mandatory",
                    "rav": "arc_risk_reducer_v1",
                    "sev": "arc_selection_v1",
                    "ecv": "arc_selection_config_v1",
                    "cid": cohort_id,
                    "cd": uuid.uuid4().hex + uuid.uuid4().hex,
                    "wsa": window_started_at,
                    "wea": window_deadline,
                    "ec": 100,
                    "oc": 100,
                    "eied": uuid.uuid4().hex + uuid.uuid4().hex,
                    "qav": "arc_observation_qualification_v1",
                    "computed_at": window_deadline,
                },
            )

        accepter_ctx = ArcRequestContext(
            tenant=TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=_ACCEPTER),
            oidc_issuer=ISSUER,
        )
        accepted = await services.arc_qualification.accept(
            accepter_ctx, qualification_id=qualification_id, acknowledged_reason_codes=["window_met"]
        )
        assert accepted.decision == "qualified"
        assert accepted.accepted_at is not None
        return qualification_id

    return _provider


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


async def _register_receipt_signing_key(factory: async_sessionmaker[AsyncSession]) -> None:
    """`arc_receipt_events.signer_key_id` is a real foreign key into
    `arc_receipt_signing_keys` -- a signer that exists only in `signing_
    provider()`'s in-process `ReceiptSigningProvider` has no durable row on
    the other side of that key, so the event insert fails without one.
    `tests/helpers/arc_fixtures.py::seed_arc` registers the identical row
    for its own callers; this file has no `seed_arc` call to ride along
    with, so it registers the row itself.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_receipt_signing_keys ("
                "  signer_key_id, algorithm, public_key, purpose, valid_from, manifest_digest"
                ") VALUES (:kid, 'Ed25519', :pub, 'arc_receipt_event_v1', :vfrom, :digest) "
                "ON CONFLICT (signer_key_id) DO NOTHING"
            ),
            {
                "kid": SIGNING_KEY_ID,
                "pub": base64.b64encode(b"x" * 32).decode("ascii"),
                "vfrom": AUTHORING_NOW - datetime.timedelta(days=1),
                "digest": "d" * 64,
            },
        )


async def _register_host_key(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, signer_key_id: str, public: bytes
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_host_attestation_keys ("
                "  signer_key_id, host_id, tenant_id, attestation_profile, public_key,"
                "  valid_from, created_by_operator"
                ") VALUES (:kid, :host, :tid, :profile, :pub, :vfrom, 'test')"
            ),
            {
                "kid": signer_key_id,
                "host": _HOST_ID,
                "tid": tenant_id,
                "profile": _PROFILE,
                "pub": base64.b64encode(public).decode("ascii"),
                "vfrom": AUTHORING_NOW - datetime.timedelta(days=1),
            },
        )


def _manifest_claims() -> ManifestClaims:
    """Matches the mandatory rule's own task-scoped, selector-free match:
    `code_change`/`merge` is the one dimension the rule actually names
    (via task-scope membership), and every other field is free to be
    anything valid since the rule's own selectors are empty."""
    return ManifestClaims(
        session_id="authored-resolution-test",
        intent_kind="code_change",
        requested_action_classes=("merge",),
        capability_ids=(),
        domain_ids=("payments",),
        environment="production",
        data_sensitivity="confidential",
        repository_identity="git@example.test:org/repo.git",
        supported_context_bundle_content_profiles=("arc_context_bundle_content_v1",),
    )


def _sign_envelope(
    *, manifest: ManifestClaims, nonce: bytes, signer_key_id: str, private: Ed25519PrivateKey
) -> AttestationEnvelope:
    payload = {
        "host_id": _HOST_ID,
        "repository_identity": manifest.repository_identity,
        "immutable_source_revision": "deadbeef",
        "environment": manifest.environment,
        "data_sensitivity": manifest.data_sensitivity,
        "session_id": manifest.session_id,
        "manifest_claims_digest": compute_manifest_claims_digest(manifest.as_claims_dict()),
        "arc_nonce": base64.b64encode(nonce).decode("ascii"),
    }
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    envelope_dict: dict[str, object] = {
        "profile": _PROFILE,
        "signer_key_id": signer_key_id,
        "attestation_id": attestation_id,
        "issued_at": AUTHORING_NOW,
        "expires_at": AUTHORING_NOW + CHALLENGE_TTL,
        "payload": payload,
    }
    signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
    signature = private.sign(signing_input)
    return AttestationEnvelope(
        profile=_PROFILE,
        signer_key_id=signer_key_id,
        attestation_id=attestation_id,
        issued_at=AUTHORING_NOW,
        expires_at=AUTHORING_NOW + CHALLENGE_TTL,
        payload=payload,
        signature=base64.b64encode(signature).decode("ascii"),
    )


@pytest.mark.asyncio
async def test_a_surface_authored_mandatory_directive_resolves_with_a_receipt(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The terminal clause of "a revision authored through the authoring
    surface activates through all ten predicates, and mandatory context
    then resolves with a receipt" -- exercised together for the first time.

    Authors and activates a revision through the real pipeline
    (`seed_and_activate`) with a mandatory, task-scoped applicability rule;
    assembles the real candidate corpus for it (`arc_corpus.assemble`, the
    same call `test_arc_post_activation_serving.py` already proves reads
    back what `submit` wrote); drives that corpus through a real
    `ResolutionService.resolve()`; and asserts the resulting receipt names
    this exact directive as the mandatory obligation it satisfied -- not
    merely that a receipt row exists, but that its own selected-directives
    row is *this* directive, marked mandatory, off a `ready` resolution.
    """
    services = wired_app.state.services
    factory = services.session_factory

    rule_id = uuid.uuid4()
    qualification_provider = _mandatory_qualification_provider(factory, services)
    tenant_id, revision_id = await seed_and_activate(
        wired_app,
        pg_container,
        slug=f"authored-resolution-{uuid.uuid4().hex[:8]}",
        applicability=_mandatory_applicability(rule_id),
        qualification_id_provider=qualification_provider,
    )

    async with factory() as session:
        directive_id = (
            await session.execute(
                text("SELECT directive_id FROM arc_directives WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()

    task_manifest = parse_manifest(_manifest_claims())
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)
    candidates = await services.arc_corpus.assemble(tenant_id=tenant_id, manifest=task_manifest, as_of=as_of)
    assert len(candidates.candidates) == 1, "the corpus must offer exactly the one directive this revision carries"
    offered_directive, offered_rule, _effective_from = candidates.candidates[0]
    assert offered_directive.directive_id == directive_id
    assert offered_rule.is_mandatory, "the applicability rule this revision carries must read back as mandatory"

    private, public = _keypair()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await _register_host_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public=public)
    await _register_receipt_signing_key(factory)

    clock = FakeClock(AUTHORING_NOW)
    deriver = ChallengeNonceDeriver({_NONCE_KEY: b"nonce-secret"}, active_key_id=_NONCE_KEY)
    challenges = ChallengeService(factory, deriver, clock)
    resolution = ResolutionService(
        factory,
        attestation=AttestationService(HostSignerKeyRegistry(), clock=clock),
        challenges=challenges,
        receipts=ReceiptService(signing_provider(), clock),
        provenance=provenance(),
        clock=clock,
        # The real, wired integrity assessor -- the same instance `arc_corpus`
        # above just used to build `candidates` -- so this resolve() call
        # rechecks the identical, genuinely-activated revision `select_and_
        # verify`'s own authoritative recheck is for, not a stand-in that
        # always says valid.
        integrity=services.arc_integrity,
        seal=lambda rid, bundle: ReplayEnvelope(
            ciphertext=f"sealed:{rid}".encode(), nonce=b"nonce-12-byt", key_id="replay-1"
        ),
    )

    # `arc_receipts.actor_id` is a real foreign key into `actors` (unlike
    # the authoring surface's own `ArcRequestContext.actor_id`, which
    # `seed_and_activate` mints fresh per call and nothing constrains) --
    # the resolving identity has to be the one actor row `seed_tenant_and_
    # actor` created for this tenant.
    async with factory() as session:
        actor_id = (
            await session.execute(text("SELECT actor_id FROM actors WHERE tenant_id = :tid"), {"tid": tenant_id})
        ).scalar_one()

    manifest_claims = _manifest_claims()
    claims_digest = compute_manifest_claims_digest(manifest_claims.as_claims_dict())
    resolver_ctx = ArcRequestContext(
        tenant=TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["consumer"], oidc_subject="resolver-1"),
        oidc_issuer=ISSUER,
        host_id=_HOST_ID,
    )
    issued = await challenges.issue_challenge(
        resolver_ctx,
        session_id=manifest_claims.session_id,
        manifest_claims_digest=claims_digest,
        idempotency_key=uuid.uuid4().hex,
    )
    envelope = _sign_envelope(
        manifest=manifest_claims, nonce=issued.arc_nonce, signer_key_id=signer_key_id, private=private
    )
    request = ResolutionRequest(
        ctx=resolver_ctx,
        host_id=_HOST_ID,
        manifest=manifest_claims,
        envelope=envelope,
        manifest_fingerprint="f" * 64,
        candidates=candidates,
        budget_limit_bytes=12288,
    )

    outcome = await resolution.resolve(request, as_of=as_of)

    assert outcome.status is ResolutionStatus.READY, "one satisfied mandatory obligation with nothing conflicting"
    assert outcome.bundle is not None
    assert len(outcome.bundle.directives) == 1
    assert outcome.bundle.directives[0]["directive_id"] == str(directive_id)

    async with factory() as session:
        receipt_row = (
            await session.execute(
                text("SELECT resolution_status FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": outcome.receipt_id},
            )
        ).one()
        selected = (
            await session.execute(
                text(
                    "SELECT revision_id, directive_id, is_mandatory FROM arc_receipt_selected_directives "
                    "WHERE receipt_id = :rid"
                ),
                {"rid": outcome.receipt_id},
            )
        ).all()

    assert receipt_row.resolution_status == "ready"
    assert len(selected) == 1, "the receipt must record exactly the one directive this revision carried"
    assert selected[0].revision_id == revision_id
    assert selected[0].directive_id == directive_id
    assert selected[0].is_mandatory is True, "this obligation was owed, not merely offered"
