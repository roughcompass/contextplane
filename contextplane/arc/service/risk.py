"""`RiskClassificationService`: the ADR 041 complete-rule-set reducer, and
`RiskEnvelopeValidator`, the single collaborator `ArtifactMaterialisation
Service.submit` calls to turn "both risk and envelope prerequisites exist"
into real, persisted rows.

**The reducer classifies over every rule, never a representative one.**
Classifying by the narrowest or first rule would let a revision that
carries a global mandatory rule alongside narrower ones escape the
three-identity actor-separation requirement a global mandatory
classification demands -- exactly the bypass this module exists to close.
The complete algorithm, in order:

1. any rule with ``scope == "global"`` and ``is_mandatory`` -> ``global_mandatory``
2. otherwise any rule with ``scope == "global"`` -> ``global_non_mandatory``
3. otherwise the highest-impact non-global tier, by scope order
   ``tenant > domain > capability > task``, with a mandatory rule
   outranking a non-mandatory one at equal scope.

That yields exactly the ten members ``contextplane.arc.schemas.authoring_
profile_shapes.RISK_CLASSIFICATIONS`` already declares (two global tiers,
plus four scopes crossed with mandatory/non-mandatory) -- this module
imports that tuple as its one source of the closed vocabulary rather than
restating the ten literals a second time.

**Versioning.** The classification result and the algorithm version that
produced it are both bound at submission and stay sticky for that proposal
version: approval, qualification, and activation recompute with the exact
implementation named here, never whatever this module happens to ship at
the time they run. `_REDUCERS` is a version -> callable registry rather
than a single function specifically so a second algorithm version can be
added here without displacing the first -- a nonterminal proposal bound to
an old version keeps calling the old implementation until it reaches a
terminal state; a proposal naming a version this registry no longer
carries is a `stale`-terminalization case for whichever later task owns
that lifecycle transition, not something this module guesses how to
reinterpret under the current algorithm.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.schemas.authoring_profile_shapes import RISK_CLASSIFICATIONS
from contextplane.arc.service.envelope import EnvelopeInvalid, ExpectedImpactEnvelopeService
from contextplane.arc.service.queries import risk as queries
from contextplane.exceptions import RegistryError

#: The algorithm version this module currently implements. Bound into the
#: sticky `arc_risk_classifications` row and the summary columns on
#: `arc_authoring_proposal_versions` at submission time.
CURRENT_RISK_ALGORITHM_VERSION = "arc_risk_reducer_v1"

# ADR 041's scope order, highest impact first. `global` is handled as its
# own two-tier branch above this table rather than ranked alongside it,
# because a global rule's mandatory/non-mandatory split does not compete
# against the non-global scopes at all -- any global rule always outranks
# every non-global one regardless of mandatory status.
_SCOPE_ORDER: dict[str, int] = {"tenant": 4, "domain": 3, "capability": 2, "task": 1}


class RiskClassificationError(RegistryError):
    """The reducer cannot classify the given rule set.

    Raised for a candidate with zero applicability rules -- a frozen
    candidate is supposed to always carry at least one rule, rejected
    earlier in the authoring flow, so reaching this exception means that
    upstream check did not fire; refusing loudly here is the defensive
    backstop, not the primary enforcement point (`arc_proposal_
    validation_failed`, 422).
    """


class UnknownRiskAlgorithmVersion(RiskClassificationError):
    """*reducer_version* names no reducer implementation this deployment
    still carries.

    Only reachable once a second algorithm version exists and an old one is
    retired while a proposal still names it -- see the module docstring.
    Recomputation under a different, unrequested version is exactly the
    silent reinterpretation ADR 041 §2 forbids, so this refuses rather than
    falling back to whatever is current (`arc_proposal_validation_failed`,
    422).
    """


@dataclasses.dataclass(frozen=True)
class RiskClassificationResult:
    classification: str
    algorithm_version: str


def _reduce_v1(rules: Sequence[Mapping[str, Any]]) -> str:
    if not rules:
        raise RiskClassificationError(
            "cannot classify a candidate with zero applicability rules -- a rule always exists once "
            "proposal validation has rejected an empty applicability list"
        )

    if any(rule.get("scope") == "global" and bool(rule.get("is_mandatory")) for rule in rules):
        return "global_mandatory"
    if any(rule.get("scope") == "global" for rule in rules):
        return "global_non_mandatory"

    best_rank = -1
    best_mandatory = False
    for rule in rules:
        rank = _SCOPE_ORDER.get(str(rule.get("scope")))
        if rank is None:
            # `global` is already handled above; any other unrecognized
            # scope literal would already have been refused by the closed
            # `_APPLICABILITY_RULE_SCHEMA` enum before this candidate could
            # ever be persisted, so this branch is unreachable in practice
            # and deliberately does not contribute to the ranking.
            continue
        mandatory = bool(rule.get("is_mandatory"))
        if rank > best_rank or (rank == best_rank and mandatory and not best_mandatory):
            best_rank = rank
            best_mandatory = mandatory

    scope_name = next(name for name, rank in _SCOPE_ORDER.items() if rank == best_rank)
    return f"{scope_name}_{'mandatory' if best_mandatory else 'non_mandatory'}"


#: Version -> reducer. See the module docstring for why this is a registry
#: rather than one function: a retired version is never replaced in place.
_REDUCERS: dict[str, Callable[[Sequence[Mapping[str, Any]]], str]] = {
    CURRENT_RISK_ALGORITHM_VERSION: _reduce_v1,
}


class RiskClassificationService:
    """Computes, but never persists, the ADR 041 risk classification.

    Pure with respect to storage: `classify` takes a candidate's already-
    validated `arc_artifact_semantics_v1` document and returns a result;
    persistence and the sticky-version binding are `RiskEnvelopeValidator`'s
    job, not this class's, so the reducer itself stays testable with no
    session at all.
    """

    def classify(
        self, artifact_semantics: Mapping[str, Any], *, reducer_version: str = CURRENT_RISK_ALGORITHM_VERSION
    ) -> RiskClassificationResult:
        reducer = _REDUCERS.get(reducer_version)
        if reducer is None:
            raise UnknownRiskAlgorithmVersion(
                f"no reducer implementation for algorithm version {reducer_version!r} is registered in this "
                "deployment"
            )
        rules = artifact_semantics.get("applicability") or ()
        classification = reducer(rules)
        if classification not in RISK_CLASSIFICATIONS:
            # A reducer bug, not a caller error: every registered
            # implementation must only ever emit a member of the closed
            # vocabulary this module's own docstring derives from. Raising
            # here rather than trusting the string is what keeps a future
            # typo in a reducer implementation from silently widening the
            # classification vocabulary past the ten literals every
            # downstream consumer (the wire enum, the DB CHECK, the
            # actor-separation table) closes against.
            msg = f"reducer {reducer_version!r} returned {classification!r}, which is not in RISK_CLASSIFICATIONS"
            raise RiskClassificationError(msg)
        return RiskClassificationResult(classification=classification, algorithm_version=reducer_version)


@dataclasses.dataclass(frozen=True)
class RiskEnvelopeAssessment:
    """What a won `assess_and_persist` hands back -- identity, matching
    every other row-shape dataclass in this package, not the rows
    themselves (a caller that needs more re-reads)."""

    classification: str
    algorithm_version: str
    envelope_id: uuid.UUID
    envelope_digest: str


class RiskEnvelopeValidator:
    """The single collaborator `ArtifactMaterialisationService.submit`
    calls once both this task's prerequisites exist: classifies risk,
    validates and freezes the expected-impact envelope, and persists both
    in the caller's own transaction.

    Composes `RiskClassificationService` and `ExpectedImpactEnvelopeService`
    rather than reimplementing either -- this class owns only the
    submission-time orchestration and the write, matching
    `OperationalChainService.append_event`'s own convention of taking the
    caller's session rather than opening one of its own.
    """

    def __init__(
        self,
        *,
        risk: RiskClassificationService | None = None,
        envelope: ExpectedImpactEnvelopeService | None = None,
    ) -> None:
        self._risk = risk if risk is not None else RiskClassificationService()
        self._envelope = envelope if envelope is not None else ExpectedImpactEnvelopeService()

    async def assess_and_persist(
        self,
        session: AsyncSession,
        *,
        proposal_id: uuid.UUID,
        proposal_version: int,
        artifact_semantics: Mapping[str, Any],
        expected_impact_envelope: Mapping[str, Any],
        now: datetime.datetime,
    ) -> RiskEnvelopeAssessment:
        """Validate, classify, and persist -- in that order, so a rejected
        envelope or an unclassifiable candidate never reaches a write.

        Everything below runs against *session* without opening or
        committing a transaction of its own: the caller's `session.begin()`
        block is what makes this atomic with the draft-revision insert and
        bijection freeze that precede it and the operational-chain append
        that follows it. Any exception here rolls back all of it together.
        """
        envelope_assessment = self._envelope.validate(
            expected_impact_envelope, proposal_id=proposal_id, proposal_version=proposal_version
        )
        risk_result = self._risk.classify(artifact_semantics)

        await queries.insert_risk_classification(
            session,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            classification=risk_result.classification,
            algorithm_version=risk_result.algorithm_version,
            computed_at=now,
        )
        await queries.set_proposal_version_risk(
            session,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            classification=risk_result.classification,
            algorithm_version=risk_result.algorithm_version,
        )
        envelope_id = uuid.UUID(str(envelope_assessment.envelope["envelope_id"]))
        await queries.insert_envelope(
            session,
            envelope_id=envelope_id,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            envelope_digest=envelope_assessment.envelope_digest,
            author_issuer=str(envelope_assessment.envelope["author_issuer"]),
            author_subject=str(envelope_assessment.envelope["author_subject"]),
            created_at=now,
            items=[
                queries.EnvelopeItemRow(
                    item_id=str(item["item_id"]),
                    delta_code=str(item["delta_code"]),
                    class_predicate=dict(item["class_predicate"]),
                    minimum_count=int(item["minimum_count"]),
                    maximum_count=(None if item["maximum_count"] is None else int(item["maximum_count"])),
                    rationale_code=str(item["rationale_code"]),
                )
                for item in envelope_assessment.envelope["items"]
            ],
        )

        return RiskEnvelopeAssessment(
            classification=risk_result.classification,
            algorithm_version=risk_result.algorithm_version,
            envelope_id=envelope_id,
            envelope_digest=envelope_assessment.envelope_digest,
        )


__all__ = [
    "CURRENT_RISK_ALGORITHM_VERSION",
    "RiskClassificationError",
    "RiskClassificationResult",
    "RiskClassificationService",
    "RiskEnvelopeAssessment",
    "RiskEnvelopeValidator",
    "UnknownRiskAlgorithmVersion",
    # Re-exported so a caller that only imports `risk.py` (the collaborator
    # `submission.py` actually holds) can still catch the one envelope
    # failure mode without a second import.
    "EnvelopeInvalid",
]
