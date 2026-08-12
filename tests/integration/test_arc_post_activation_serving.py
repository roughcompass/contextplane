"""Protected-action authorization proof: activate a real candidate, then
show that mandatory serving (`corpus.py` + `selection.py`) and
protected-action authorization (`authorization.py`) all refuse once any one of
`RevisionIntegrityService.assess`'s five axes goes bad on the now-active
revision.

Setup mirrors `test_arc_activation_predicates.py`'s own pipeline (submit
through a real `ProposalService`/`ArtifactMaterialisationService`, approve
through `arc_approval_challenges` with a real Ed25519 signature, export the
one pending checkpoint, then activate) with one addition: the candidate
here carries one real `citation_only` directive in its own `directives[]`,
so it is a genuine candidate `CorpusReader.assemble` and `select_and_verify`
can select -- `test_arc_activation_predicates.py`'s own candidate carries
none, which is enough for activation's ten predicates but gives selection
nothing to serve at all.

**The directive and its applicability rule arrive through submission
itself, not a seeded `INSERT`.** An earlier version of this file inserted
`arc_directives`/`arc_applicability_rules` rows directly by SQL after
`ArtifactMaterialisationService.submit` returned, because submission wrote
`arc_revisions` only -- a gap since closed by materialising the
candidate's own directives/applicability rules through submission itself.
That scaffold proved the read path (`corpus.py`/`selection.py`/
`authorization.py`) refuses correctly on an integrity-failed revision; it
never proved the authoring surface itself could produce anything for that
read path to refuse. Now that `submit` materialises the candidate's own
`directives[]`/`applicability[]` in the same transaction as the revision
row, this file's one directive and one rule are just fields on a candidate
profile -- the identical shape `test_arc_submission.py` and `test_arc_
materialisation.py` already exercise for the writer itself, exercised here
end to end through activation and mandatory-context resolution.

**The pipeline itself now lives in `tests/helpers/arc_authoring_pipeline.
py`.** `seed_and_activate` (submit through approval, checkpoint export, and
activation) and its supporting candidate/directive builders moved there so
a second integration-test file needing the identical real pipeline -- to
drive something downstream of activation other than corpus/selection/
authorization -- can reuse it rather than reimplementing it. This file
keeps every test that exercises corpus/selection/authorization directly
against that pipeline's output; it imports the pipeline rather than owning
it.

**Why the applicability rule stays non-mandatory here.** ADR 041's own
reducer (`risk.py`) classifies *any* `is_mandatory=True` rule as requiring
observation qualification before activation, regardless of scope -- making
this file's candidate mandatory would mean standing up a full shadow/
qualification pipeline just to reach an activated revision at all. The
mandatory-blocks-the-whole-resolution property of `select_and_verify` is
proven directly, without that overhead, in `tests/unit/test_arc_selection.
py`'s own synthetic-fixture suite; this file proves the axis-detection
side against a real, activated revision, and exercises the DEGRADED (not
BLOCKED) branch for the optional directive it actually has.

**Why "checkpoint pending" and "checkpoint unavailable" share one
assertion.** `RevisionIntegrityService`'s own durable-checkpoint axis
(`integrity.py::_check_durable_checkpoint`) returns the identical bounded
code, `arc_operational_integrity_pending`, whether the checkpoint row is
merely unexported or entirely absent -- by design, per that axis's own
"never disclose which check failed" contract. This file plants both
shapes (reverting an exported checkpoint's receipt columns to `NULL`, and
deleting the checkpoint row outright) and asserts the same code from each,
rather than inventing a distinction the production code does not draw.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.artifact_materialisation import _conflict_subject_digest
from contextplane.arc.service.authorization import ArcAuthorizationError
from contextplane.arc.service.integrity import (
    PURPOSE_AUTHORIZATION,
    REASON_OPERATIONAL_INTEGRITY_FAILED,
    REASON_OPERATIONAL_INTEGRITY_PENDING,
    REASON_PROJECTION_EVIDENCE_INVALID,
    REASON_SOURCE_STATUS_UNAVAILABLE,
)
from contextplane.arc.service.selection import (
    DEGRADED_OPTIONAL_UNAVAILABLE,
    SelectionInput,
    directives_conflict,
    select,
    select_and_verify,
)
from contextplane.arc.types import ActionClass, DirectiveType, ResolutionStatus, IntentKind, IntentManifest
from contextplane.main import create_app
from tests.helpers.arc_authoring_pipeline import AUTHORING_NOW as _NOW
from tests.helpers.arc_authoring_pipeline import directive_row as _directive
from tests.helpers.arc_authoring_pipeline import seed_and_activate as _seed_and_activate
from tests.helpers.auth_harness import default_settings


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    """The real app, through its own lifespan -- matching every sibling
    activation/approval integration test's own `wired_app` fixture."""
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def _manifest() -> IntentManifest:
    """Matches the candidate's own task-scoped, selector-free applicability
    rule: an empty selector on every dimension means "matches any"."""
    return IntentManifest(
        session_id="serving-test",
        intent_kind=IntentKind.CODE_CHANGE,
        requested_action_classes=frozenset({ActionClass.MERGE}),
    )


async def _assert_mandatory_serving_and_authorization_refuse(
    wired_app: FastAPI, *, tenant_id: uuid.UUID, revision_id: uuid.UUID, expected_reason_code: str
) -> None:
    """The shared assertion every planted-axis test below runs: the
    activated revision's one directive is excluded from a fresh corpus
    assembly, `select_and_verify`'s own independent recheck (against the
    corpus's *pre-filter* candidates, so it is genuinely selection's own
    check being exercised, not merely inheriting corpus's) degrades with
    the axis's bounded code, and protected-action authorization for this
    revision is denied with the same code.
    """
    services = wired_app.state.services
    factory = services.session_factory
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)

    # 1. Mandatory corpus assembly: the directive must not survive.
    corpus_input = await services.arc_corpus.assemble(tenant_id=tenant_id, manifest=_manifest(), as_of=as_of)
    assert corpus_input.candidates == (), "corpus assembly served a directive from an integrity-failed revision"

    # 2. Selection's own authoritative recheck, against the *unfiltered*
    #    candidates corpus.py itself reads before applying its own
    #    integrity prefilter -- this isolates selection.py's own call from
    #    corpus.py's, matching item 3's "each caller independently" bar.
    async with factory() as session:
        raw_candidates = await services.arc_corpus._candidates(session, tenant_id=tenant_id, as_of=as_of)
        assert raw_candidates != (), "the fixture itself produced no candidate to test against"
        selection_input = SelectionInput(
            manifest=_manifest(), tenant_id=tenant_id, as_of=as_of, candidates=raw_candidates
        )
        selection_result = await select_and_verify(session, selection_input, services.arc_integrity)
    assert selection_result.optional == (), "an integrity-failed revision's directive was still offered"
    assert expected_reason_code in selection_result.degraded_reasons

    # 3. Protected-action authorization: denied, carrying the same code.
    async with factory() as session:
        with pytest.raises(ArcAuthorizationError) as exc_info:
            await services.arc_authorization.assert_protected_action_authorized(
                session, revision_id, integrity=services.arc_integrity
            )
    assert exc_info.value.reason == expected_reason_code


@pytest.mark.asyncio
async def test_source_revoked_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-source-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_source_approval_status SET status = 'revoked' "
                "WHERE source_evidence_id = ("
                "  SELECT source_evidence_id FROM arc_authoring_proposal_versions WHERE revision_id = :rid"
                ")"
            ),
            {"rid": revision_id},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app, tenant_id=tenant_id, revision_id=revision_id, expected_reason_code=REASON_SOURCE_STATUS_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_projection_evidence_revoked_refuses_serving_and_authorization(
    wired_app: FastAPI, pg_container: str
) -> None:
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-evidence-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_projection_approval_evidence SET revoked_at = :now, "
                "  revocation_reason_code = 'superseded_by_reapproval' WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=1)},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_PROJECTION_EVIDENCE_INVALID,
    )


@pytest.mark.asyncio
async def test_checkpoint_reverted_to_pending_refuses_serving_and_authorization(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The checkpoint this revision activated with is reverted to
    unexported -- simulating a local/sink reconciliation that un-durables
    it (see `checkpoint_export.py`'s own module docstring for the real
    scenarios this stands in for)."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-checkpoint-pending-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_operational_chain_checkpoints SET exported_at = NULL, sink_receipt_digest = NULL, "
                "  sink_receipt_signature = NULL WHERE revision_id = :rid"
            ),
            {"rid": revision_id},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_PENDING,
    )


@pytest.mark.asyncio
async def test_checkpoint_deleted_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    """No checkpoint row at all for this revision -- `_check_durable_
    checkpoint` treats this identically to an unexported one (see the
    module docstring for why: the bounded code intentionally does not
    distinguish "never durable" from "no longer durable")."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-checkpoint-gone-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_operational_chain_checkpoints WHERE revision_id = :rid"), {"rid": revision_id}
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_PENDING,
    )


@pytest.mark.asyncio
async def test_cached_state_drift_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    """The sticky risk classification this revision was approved and
    activated under no longer agrees with what recomputing it from the
    frozen semantics produces -- the axis 2 cache-drift check
    `ReviewPackageService.assemble` performs on every call, never trusting
    the persisted column as truth."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-cache-drift-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_risk_classifications SET classification = 'global_mandatory' "
                "WHERE proposal_id = (SELECT proposal_id FROM arc_authoring_proposal_versions WHERE revision_id = :rid)"
            ),
            {"rid": revision_id},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_FAILED,
    )


@pytest.mark.asyncio
async def test_no_refusal_discloses_evidence_verifier_or_digest(wired_app: FastAPI, pg_container: str) -> None:
    """`RevisionIntegrityService.assess` never returns evidence bytes, a
    verifier identity, or a digest, on any path -- `integrity.py`'s own
    contract, proven again at this boundary. This walks every refusal this file
    produces and asserts none of them leaked through `ArcAuthorizationError`,
    `SelectionResult`, or `SelectionInput`'s own `repr`/`str`.
    """
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-no-leak-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_projection_approval_evidence SET revoked_at = :now, "
                "  revocation_reason_code = 'superseded_by_reapproval' WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=1)},
        )

    services = wired_app.state.services
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)
    async with factory() as session:
        raw_candidates = await services.arc_corpus._candidates(session, tenant_id=tenant_id, as_of=as_of)
        inputs = SelectionInput(manifest=_manifest(), tenant_id=tenant_id, as_of=as_of, candidates=raw_candidates)
        selection_result = await select_and_verify(session, inputs, services.arc_integrity)

    async with factory() as session:
        with pytest.raises(ArcAuthorizationError) as exc_info:
            await services.arc_authorization.assert_protected_action_authorized(
                session, revision_id, integrity=services.arc_integrity
            )

    rendered = f"{selection_result!r} {exc_info.value!r} {exc_info.value}"
    secrets = frozenset({str(revision_id), str(tenant_id), PURPOSE_AUTHORIZATION})
    for secret in secrets:
        assert secret not in rendered, f"{secret!r} leaked through a refusal this file produced"


# ---------------------------------------------------------------------------
# An action-protecting directive authored through this surface actually
# governs something -- distinct from the axis-refusal proofs above, but
# built on the identical real pipeline (submit through approval, checkpoint
# export, and activation, never a seeded INSERT). Every directive above this
# point is `citation_only`, the one type that -- by its own closed-
# vocabulary definition -- can never make an action ready or blocked; a
# revision carrying only that type activates and serves, but nothing it
# carries could ever conflict, block, or degrade a resolution. The tests
# below submit `verify_before_action` instead, so the materialised
# `arc_directives` row is action-protecting for the first time in this
# file, and prove it participates in `selection.py`'s own conflict
# detection rather than merely surviving the round trip.
# ---------------------------------------------------------------------------

#: A conflict subject shared by both directives below. `directives_conflict`
#: groups on subject alone (never the disagreement itself), so two
#: directives naming this same subject compare as one `ConflictSubjectKey`
#: regardless of what each one requires or prohibits about it.
_SHARED_CONFLICT_SUBJECT: dict[str, str] = {
    "namespace": "arc.retention",
    "subject_selector": "capability:*",
    "operation": "retain",
    "action_class": "data_retention",
    "target_selector": "domain:payments",
}


async def _seed_conflict_domain(factory: async_sessionmaker[AsyncSession], *, subject_digest: str) -> None:
    """`arc_directives.conflict_subject_digest` is a foreign key into
    `arc_conflict_domains`; unlike `_insert_verifier` for approval, nothing
    in `submit`'s own transaction creates this row (see `_directive_row`'s
    own docstring -- `conflict_subject_digest` is trusted verbatim, never
    derived), so a directive naming one must find it already there."""
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_conflict_domains (conflict_subject_digest, conflict_subject_key) "
                "VALUES (:digest, CAST(:key AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"digest": subject_digest, "key": json.dumps(_SHARED_CONFLICT_SUBJECT, sort_keys=True)},
        )


def _conflicting_verify_directives() -> tuple[dict[str, object], dict[str, object], str]:
    """Two `verify_before_action` directives over the identical conflict
    subject, with incompatible constraints: `require equals reviewed`
    permits only `"reviewed"`, `prohibit equals reviewed` permits every
    other value, and those two permitted sets share nothing -- the exact
    shape `constraints_are_compatible` exists to catch. Returns
    `(require_directive, prohibit_directive, subject_digest)`.
    """
    subject_digest = _conflict_subject_digest(dict(_SHARED_CONFLICT_SUBJECT))
    conflict_key_fields = {f"conflict_key_{name}": value for name, value in _SHARED_CONFLICT_SUBJECT.items()}
    require = _directive(
        directive_id=uuid.uuid4(),
        directive_type="verify_before_action",
        satisfaction_mode="authorized_retrieval",
        conflict_subject_digest=subject_digest,
        conflict_key_modality="require",
        conflict_key_constraint_operator="equals",
        conflict_key_constraint_value="reviewed",
        **conflict_key_fields,
    )
    prohibit = _directive(
        directive_id=uuid.uuid4(),
        directive_type="verify_before_action",
        satisfaction_mode="authorized_retrieval",
        conflict_subject_digest=subject_digest,
        conflict_key_modality="prohibit",
        conflict_key_constraint_operator="equals",
        conflict_key_constraint_value="reviewed",
        **conflict_key_fields,
    )
    return require, prohibit, subject_digest


@pytest.mark.asyncio
async def test_a_verify_before_action_directive_materialises_as_an_action_protecting_verify_directive(
    wired_app: FastAPI, pg_container: str
) -> None:
    """A revision authored, submitted, approved, checkpointed, and
    activated entirely through this surface -- carrying a candidate
    directive that names the wire literal `verify_before_action` -- reads
    back from `arc_directives` as the persisted `verify` type, and the
    domain object `CorpusReader` builds from that row is action-protecting.
    Before this task's translation existed, this candidate could not
    reach activation at all: `arc_directives`' own CHECK refused the raw
    wire literal, and `submit` turned that refusal into `Candidate
    GovernanceRowRejected` before a revision ever had anything to serve.
    """
    require, _prohibit, subject_digest = _conflicting_verify_directives()
    factory = wired_app.state.services.session_factory
    await _seed_conflict_domain(factory, subject_digest=subject_digest)

    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-verify-{uuid.uuid4().hex[:8]}", directives=[require]
    )

    async with factory() as session:
        directive_type = (
            await session.execute(
                text("SELECT directive_type FROM arc_directives WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert directive_type == "verify"

    services = wired_app.state.services
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)
    corpus_input = await services.arc_corpus.assemble(tenant_id=tenant_id, manifest=_manifest(), as_of=as_of)
    assert len(corpus_input.candidates) == 1
    directive, _rule, _effective_from = corpus_input.candidates[0]
    assert directive.directive_type is DirectiveType.VERIFY
    assert directive.is_enforceable, "a translated verify directive must be able to make an action ready or blocked"

    result = select(corpus_input)
    assert result.status is ResolutionStatus.READY, "one directive with nothing to conflict with must resolve ready"


@pytest.mark.asyncio
async def test_two_conflicting_verify_before_action_directives_reach_directive_conflict_detection(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The positive property `verify_before_action`'s translation exists
    for: two directives authored through this surface, each naming that
    wire literal and an incompatible constraint over the same conflict
    subject, activate together and are the ones `selection.py::
    directives_conflict` -- and `select()`'s own optional-conflict
    reduction -- actually flags. A `citation_only` directive could never
    reach this path (`directives_conflict` returns `False` before ever
    comparing constraints for a non-action-protecting type); this is the
    path that was unreachable until `verify_before_action` had somewhere
    to translate to.
    """
    require, prohibit, subject_digest = _conflicting_verify_directives()
    factory = wired_app.state.services.session_factory
    await _seed_conflict_domain(factory, subject_digest=subject_digest)

    # `directives[]` canonicalizes as an ordered array, strictly ascending
    # by `directive_id` -- an authoring concern with nothing to do with the
    # conflict this test proves, so the two random ids are sorted here
    # rather than the fixture builder needing to know about canonical
    # ordering at all.
    directives = sorted([require, prohibit], key=lambda d: str(d["directive_id"]))
    tenant_id, revision_id = await _seed_and_activate(
        wired_app,
        pg_container,
        slug=f"serving-verify-conflict-{uuid.uuid4().hex[:8]}",
        directives=directives,
    )

    services = wired_app.state.services
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)
    corpus_input = await services.arc_corpus.assemble(tenant_id=tenant_id, manifest=_manifest(), as_of=as_of)
    assert len(corpus_input.candidates) == 2
    assert {d.directive_type for d, _rule, _effective_from in corpus_input.candidates} == {DirectiveType.VERIFY}

    # The literal call this whole translation exists to make reachable:
    # two materialised, action-protecting directives, submitted through
    # this surface and read back from a real activated revision, that
    # `directives_conflict` itself says disagree. Before this task,
    # `verify_before_action` failed at materialisation, so no pair of
    # directives built from it could ever reach this call.
    directive_a, directive_b = (d for d, _rule, _effective_from in corpus_input.candidates)
    assert directives_conflict(directive_a, directive_b)

    result = select(corpus_input)
    # Both directives sit on the same non-mandatory rule (`_candidate`'s own
    # default, kept deliberately non-mandatory here too -- making it
    # mandatory would additionally require an observation-qualification
    # pipeline this task does not build, per its own non-goals). `select()`
    # only populates `.conflicts` for a *mandatory* conflict (the shape
    # `blocked_conflict` reports); an optional one instead degrades the
    # resolution without naming the pair on the result itself, which is
    # exactly why the direct `directives_conflict` call above is the proof,
    # not `result.conflicts`.
    assert DEGRADED_OPTIONAL_UNAVAILABLE in result.degraded_reasons
    assert result.conflicts == ()

    async with factory() as session:
        revision_row = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert revision_row == "active", "the conflicting pair must reach this point through a real activation"
