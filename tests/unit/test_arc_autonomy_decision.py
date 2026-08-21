"""The authority decision, as a pure function over an envelope and a manifest.

No database: `decide` takes the envelope and the rules it needs, which is what
makes it replayable and what makes these tests about the decision rather than
about two queries. The service that performs those queries -- and the property
that it performs them *every time*, so a suspension takes effect at the next
decision -- is covered in `tests/integration/test_arc_autonomy_decision.py`.

The refusals are the interesting half. Four of the five verdicts are refusals
and they are deliberately not one value, because the stages after this one need
to tell them apart: the advisory stage records what would have been refused, and
the graduation pre-flight counts principals that acted with no envelope at all.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.arc.service.autonomy_decision import AuthorityDecision, EnvelopeVerdict, decide
from contextplane.arc.service.autonomy_envelope import BoundEnvelope, WorkloadIdentity
from contextplane.arc.types import ActionClass, ApplicabilityRule, AuthorityScope, IntentKind, IntentManifest

_TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_ENTITY = uuid.UUID("cccccccc-0000-4000-8000-000000000001")
_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
_AGENT = WorkloadIdentity(issuer="https://iam.example.test", subject="workload/deploy-agent")


def _envelope(**over: object) -> BoundEnvelope:
    fields: dict[str, object] = {
        "binding_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "artifact_id": uuid.uuid4(),
        "principal": _AGENT,
        "state": "active",
        "effective_from": _NOW - datetime.timedelta(days=1),
        "effective_to": None,
        "suspended_at": None,
        "suspension_reason": None,
        "revision_lifecycle_state": "active",
    }
    fields.update(over)
    return BoundEnvelope(**fields)  # type: ignore[arg-type]


def _manifest(**over: object) -> IntentManifest:
    fields: dict[str, object] = {
        "session_id": "s1",
        "intent_kind": IntentKind.DEPLOYMENT,
        "requested_action_classes": frozenset({ActionClass.DEPLOY}),
        "entity_ids": frozenset({_ENTITY}),
        "domain_ids": frozenset({"payments"}),
        "environment": "production",
        "data_sensitivity": "confidential",
    }
    fields.update(over)
    return IntentManifest(**fields)  # type: ignore[arg-type]


def _rule(**over: object) -> ApplicabilityRule:
    fields: dict[str, object] = {
        "rule_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "scope": AuthorityScope.GLOBAL,
    }
    fields.update(over)
    return ApplicabilityRule(**fields)  # type: ignore[arg-type]


def _decide(envelope: BoundEnvelope | None, *rules: ApplicabilityRule, **over: object) -> AuthorityDecision:
    return decide(
        principal=_AGENT,
        envelope=envelope,
        rules=rules,
        manifest=over.get("manifest") or _manifest(),  # type: ignore[arg-type]
        tenant_id=_TENANT,
        as_of=_NOW,
    )


# --- the permit ------------------------------------------------------------------


def test_a_matching_rule_permits_and_names_itself() -> None:
    """The matched rule is recorded, because "was there authority" needs an answer
    an auditor can follow back to a document."""
    rule = _rule(intent_kinds=frozenset({IntentKind.DEPLOYMENT}))
    envelope = _envelope()

    decision = _decide(envelope, rule)

    assert decision.is_permitted
    assert decision.verdict is EnvelopeVerdict.PERMITTED
    assert decision.matched_rule_id == rule.rule_id
    assert decision.binding_id == envelope.binding_id
    assert decision.revision_id == envelope.revision_id


def test_an_envelope_that_permits_everything_says_so_rather_than_omitting_it() -> None:
    """A global rule with no selectors is how "this agent may do anything" is
    written. It is expressible on purpose: the alternative -- inferring blanket
    authority from an *absence* of rules -- is the failure `OUTSIDE_ENVELOPE`
    exists to prevent."""
    assert _decide(_envelope(), _rule()).is_permitted


def test_the_first_matching_rule_wins_deterministically() -> None:
    """Several rules may match; the verdict must not depend on which."""
    first = _rule(rule_id=uuid.UUID(int=1), intent_kinds=frozenset({IntentKind.DEPLOYMENT}))
    second = _rule(rule_id=uuid.UUID(int=2))

    assert _decide(_envelope(), first, second).matched_rule_id == first.rule_id
    assert (
        _decide(_envelope(), second, first).matched_rule_id == second.rule_id
    ), "the caller supplies the order; the service supplies it sorted by rule_id"


# --- the four refusals -----------------------------------------------------------


def test_no_binding_is_its_own_verdict() -> None:
    """The graduation pre-flight counts exactly these, so it cannot be folded in
    with the others."""
    decision = _decide(None, _rule())

    assert decision.verdict is EnvelopeVerdict.NO_ENVELOPE
    assert not decision.is_permitted
    assert decision.binding_id is None


def test_a_no_envelope_verdict_still_names_the_principal() -> None:
    """The one verdict with no envelope to read the principal off, and the one
    where the principal matters most.

    An earlier version took `principal` from `envelope.principal` and substituted
    a placeholder here. That erased the identity the graduation pre-flight counts,
    on precisely the rows it counts them from -- an advisory record saying "some
    principal had no envelope" answers nothing.
    """
    assert _decide(None).principal == _AGENT


def test_a_suspended_envelope_authorises_nothing_even_with_a_matching_rule() -> None:
    """The rule that would have permitted is present and is not consulted."""
    permissive = _rule()
    decision = _decide(_envelope(state="suspended", suspended_at=_NOW, suspension_reason="incident"), permissive)

    assert decision.verdict is EnvelopeVerdict.ENVELOPE_SUSPENDED
    assert decision.matched_rule_id is None
    assert decision.binding_id is not None, "a refusal still says which envelope refused"


@pytest.mark.parametrize("lifecycle", ("draft", "superseded", "revoked", "expired"))
def test_a_revision_not_in_force_authorises_nothing(lifecycle: str) -> None:
    """A binding outlives the revision it names, and that is deliberate -- ending
    bindings as a side effect of revoking a document would be a decision taken
    silently. So the check has to be here: continuing to derive authority from a
    withdrawn governance document is the failure the revocation was meant to
    cause."""
    decision = _decide(_envelope(revision_lifecycle_state=lifecycle), _rule())

    assert decision.verdict is EnvelopeVerdict.ENVELOPE_WITHDRAWN
    assert decision.matched_rule_id is None


def test_an_envelope_with_no_matching_rule_refuses() -> None:
    """Deny by default. This is the ordinary refusal and the only one that means
    the envelope is doing its job."""
    narrow = _rule(intent_kinds=frozenset({IntentKind.READ_ONLY}))

    assert _decide(_envelope(), narrow).verdict is EnvelopeVerdict.OUTSIDE_ENVELOPE


def test_an_envelope_with_no_rules_at_all_refuses() -> None:
    """An envelope nobody has written a matrix into grants nothing, rather than
    everything."""
    assert _decide(_envelope()).verdict is EnvelopeVerdict.OUTSIDE_ENVELOPE


# --- the separation the binding exists to preserve --------------------------------


def test_the_predicate_never_receives_the_principal() -> None:
    """Which principal is asking has already been answered by which envelope was
    resolved, and must not be answerable again inside the matrix.

    A principal encoded as an applicability dimension would sit outside
    `_SCOPE_ORDER`, so precedence would not see it, and a rule meant to narrow
    authority for one agent would widen it for every agent matching the same
    domain. Asserted on the type rather than in prose: no field of
    `ApplicabilityRule` may be principal-shaped.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ApplicabilityRule)}
    principal_shaped = {n for n in fields if any(w in n for w in ("principal", "issuer", "subject", "actor"))}

    assert not principal_shaped, f"a principal has been smuggled into the matrix: {sorted(principal_shaped)}"


def test_two_principals_on_one_envelope_get_the_same_verdict() -> None:
    """The corollary. One template envelope governing a fleet is the ordinary
    case, and the matrix cannot distinguish its members -- if it could, the
    distinction would be invisible to precedence."""
    other = WorkloadIdentity(issuer=_AGENT.issuer, subject="workload/build-agent")
    shared = {"revision_id": uuid.uuid4()}
    rule = _rule(intent_kinds=frozenset({IntentKind.DEPLOYMENT}))

    a = _decide(_envelope(principal=_AGENT, **shared), rule)
    b = _decide(_envelope(principal=other, **shared), rule)

    assert a.verdict is b.verdict
    assert a.matched_rule_id == b.matched_rule_id


# --- replay ----------------------------------------------------------------------


def test_the_decision_is_a_function_of_as_of_not_of_now() -> None:
    """A receipt replayed months later must reach the same verdict, so the rule's
    effective window is evaluated against the instant passed in."""
    future = _rule(effective_from=_NOW + datetime.timedelta(days=30))

    assert (
        decide(
            principal=_AGENT,
            envelope=_envelope(),
            rules=(future,),
            manifest=_manifest(),
            tenant_id=_TENANT,
            as_of=_NOW,
        ).verdict
        is EnvelopeVerdict.OUTSIDE_ENVELOPE
    )
    assert decide(
        principal=_AGENT,
        envelope=_envelope(),
        rules=(future,),
        manifest=_manifest(),
        tenant_id=_TENANT,
        as_of=_NOW + datetime.timedelta(days=60),
    ).is_permitted


def test_the_decision_records_the_instant_it_was_made_for() -> None:
    assert _decide(_envelope(), _rule()).decided_at == _NOW
