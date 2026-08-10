"""The approved retrieval branch, enforced, and the one semantic arm it approves.

Two things live here because they are the same decision seen from two sides: what
the evidence permits, and the only code path that permission names.

**The artifact is the switch.** `workspace_recall_decision.json`, beside this
module, carries the branch a pre-registered campaign landed on. There is no
`SEMANTIC_WORKSPACE_ENABLED` setting and no operator toggle, which is the point --
a toggle would let a deployment turn on a retrieval path the evidence did not
approve, and the evidence is the only thing entitled to turn it on. "Runtime
configuration cannot be more permissive than the artifact" is satisfied here by
runtime configuration having no say at all.

**Loading is fail-closed in both directions.** A missing artifact, an unreadable
one, a branch outside the frozen table, or any digest that disagrees with
`contextplane.context.evaluation.protocol` raises `DecisionUnavailable`. It does
not fall back to lexical and it does not fall back to off: falling back to off
would let deleting a file silently disable task memory, and falling back to
lexical would let editing one silently change which arm answers. Both are
decisions, and neither belongs to whoever edited the file.

**The digests are checked against the code, not merely recorded.** A digest that
is stamped into a document and never compared is a value, not a check -- the
evaluation corpus carried exactly that hole until it was closed, and the same
hole here would let the decision be re-pointed at a protocol nobody re-ran. So
the artifact's protocol, judge, freeze, corpus and world digests are all
recomputed from the committed protocol module and compared. Editing a threshold
in `protocol.py` after this decision was recorded therefore stops the process
rather than quietly re-scoring it.

**The semantic arm is authorized-set-first, and that ordering is the approval.**
`exact_scan` receives candidates the caller is already entitled to see and scores
only those. It cannot widen: there is no fallback to a broader corpus when too few
candidates match, because "too few authorized matches" is a correct answer. A
post-filter design -- score the tenant, drop what the caller may not read -- is
observably different from outside even when it returns the same items: the score
distribution shifts with content the caller cannot read, and the latency tracks
how much of the tenant's corpus is not theirs. Broad ANN was never measured and is
not approved.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from contextplane.context.assembler import ArmOutcome, Exclusion, ordered_items
from contextplane.context.evaluation import protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt

    from contextplane.context.schemas.envelope import ContextItemV1

    #: What an embedder hands back: rows of floats, counted, indexed and sliced.
    #:
    #: A union rather than `Sequence[Sequence[float]]` alone because the two
    #: embedders this arm must accept disagree statically and not at runtime.
    #: The deployment's model returns `NDArray[np.float32]`, which supports
    #: every operation the scan performs but is not a `Sequence` to a type
    #: checker; a deterministic test double returns lists. Naming only the
    #: sequence rejects production -- the arm ships approved and dead -- and
    #: naming only the array forces every double to produce numpy. So this
    #: names both, and stays inside `TYPE_CHECKING`: no numpy at runtime.
    EncodedVectors = Sequence[Sequence[float]] | npt.NDArray[np.float32]

#: Where the committed decision lives. Beside this module rather than under a
#: config directory: the artifact and the code that enforces it are reviewed
#: together or the review is worthless.
DECISION_PATH = Path(__file__).with_name("workspace_recall_decision.json")

#: The branch under which the semantic arm may answer a production request. One
#: value, not a set: three of the four branches disable semantic recall, and
#: naming the permissive one explicitly means a new branch added to the frozen
#: table is disabled here until somebody decides otherwise.
_SEMANTIC_BRANCH = "semantic-adds-value-and-passes-safety"

#: The branch under which the deployment must refuse to serve workspace recall at
#: all. Task memory failing its own baseline is not a reason to serve it anyway.
_REFUSING_BRANCH = "task-memory-fails-baseline"

#: The one approved arm shape. Compared against the artifact rather than read
#: from it: the artifact records which arm the evidence approved, and this
#: constant records which arm this module implements. A mismatch means the code
#: and the decision have come apart, which is exactly when neither should run.
_APPROVED_ARM_KIND = "authorized-set-first-exact-scan"


class DecisionUnavailable(Exception):
    """The approved branch could not be established, so nothing may be served.

    Raised rather than degraded. Every alternative -- serve lexical, serve
    nothing, serve everything -- is a retrieval decision, and the whole reason
    this artifact exists is that retrieval decisions are made by evidence and
    recorded, not made by a missing file.
    """


class SemanticRecallNotApproved(Exception):
    """A semantic scan was requested under a branch that does not approve one."""


@dataclasses.dataclass(frozen=True)
class RecallDecision:
    """The committed branch, as the three questions a caller can ask of it."""

    branch: str
    reviewed_on: str
    arm_kind: str
    similarity_floor: float
    limit: int
    lexical_approved: bool
    semantic_approved: bool
    #: Safety dimensions the campaign did not measure. Carried so a caller that
    #: cites this decision as evidence can be told what it is not evidence of.
    void_safety_dimensions: tuple[str, ...]
    #: Review obligations recorded as still open at the time of the decision.
    open_review_obligations: tuple[str, ...]

    def require_service(self) -> None:
        """Refuse the whole workspace arm when the branch says task memory failed.

        Called on the serving path rather than only at startup, so a deployment
        that hot-loaded a failing branch stops answering rather than continuing
        on the branch it happened to boot with.
        """
        if self.branch == _REFUSING_BRANCH:
            raise DecisionUnavailable(
                f"the recorded branch is {self.branch!r}: task memory did not clear its own baseline, "
                "so workspace recall does not activate on this deployment"
            )

    def require_semantic(self) -> None:
        """Refuse a semantic scan under any branch that did not approve one."""
        if not self.semantic_approved:
            raise SemanticRecallNotApproved(
                f"the recorded branch is {self.branch!r}, which does not approve semantic workspace recall"
            )


def _digests_from_protocol() -> dict[str, str]:
    """What the committed protocol module says its own freeze is, right now."""
    frozen = protocol.freeze()
    return {
        "protocol_version": frozen.protocol_version,
        "judge_version": frozen.judge_version,
        "protocol_digest": frozen.protocol_digest,
        "judge_digest": frozen.judge_digest,
        "freeze_digest": frozen.freeze_digest(),
        "corpus_digest": protocol.FROZEN_CORPUS_DIGEST,
        "world_digest": protocol.FROZEN_WORLD_DIGEST,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecisionUnavailable(message)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """One top-level object of the artifact, or a refusal naming which is missing.

    Narrowing and validation in one step, rather than a boolean check followed by
    an `assert` to convince the type checker. An `assert` disappears under `-O`,
    and a fail-closed gate that a runtime flag can remove is not one.
    """
    section = raw.get(name)
    if not isinstance(section, dict):
        raise DecisionUnavailable(f"{DECISION_PATH} is missing its {name!r} section, or it is not an object")
    return section


def _text(section: dict[str, Any], name: str, *, missing: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value:
        raise DecisionUnavailable(missing)
    return value


def _parse(raw: dict[str, Any]) -> RecallDecision:
    """Validate the artifact against the frozen protocol and this module's arm.

    Every check here answers "could this file have been edited to widen the arm?"
    with no. The branch must be one the protocol froze; the digests must be the
    ones the protocol computes today; the approved arm must be the one this
    module implements; and the floor and limit must be the ones the approved
    configuration was measured at.
    """
    decision = _section(raw, "decision")
    approved = _section(raw, "approved_arm")
    recorded = _section(raw, "protocol")
    arms = _section(raw, "arms")

    branch = decision.get("branch")
    _require(
        isinstance(branch, str) and branch in protocol.BRANCHES,
        f"{DECISION_PATH} records branch {branch!r}, which is not one of the four the protocol froze; "
        "a branch this module does not recognise is not one it may act on",
    )
    branch = str(branch)

    for field, value in _digests_from_protocol().items():
        _require(
            recorded.get(field) == value,
            f"{DECISION_PATH} records {field}={recorded.get(field)!r} but the committed protocol computes "
            f"{value!r}; the decision was taken under a protocol this tree no longer holds, so it cannot be enforced",
        )

    semantic_approved = branch == _SEMANTIC_BRANCH
    _require(
        bool(arms.get("semantic")) is semantic_approved,
        f"{DECISION_PATH} sets arms.semantic={arms.get('semantic')!r} under branch {branch!r}; "
        "the branch decides which arms run and the arm list may not disagree with it",
    )
    _require(
        not arms.get("broad_ann"),
        f"{DECISION_PATH} approves a broad ANN arm, which no configuration in this protocol measured",
    )

    if semantic_approved:
        _require(
            approved.get("kind") == _APPROVED_ARM_KIND,
            f"{DECISION_PATH} approves arm {approved.get('kind')!r} but this module implements "
            f"{_APPROVED_ARM_KIND!r}; a decision and the code enforcing it have come apart",
        )
        _require(
            approved.get("measured_as") == protocol.CONFIG_TREATMENT_B,
            f"{DECISION_PATH} claims the approved arm was measured as {approved.get('measured_as')!r}, "
            f"but the semantic configuration in this protocol is {protocol.CONFIG_TREATMENT_B!r}",
        )

    floor = approved.get("similarity_floor")
    limit = approved.get("limit")
    _require(
        isinstance(floor, int | float) and not isinstance(floor, bool) and 0.0 <= float(floor) <= 1.0,
        f"{DECISION_PATH} records a similarity floor of {floor!r}, which is not a cosine similarity",
    )
    _require(
        isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1,
        f"{DECISION_PATH} records a limit of {limit!r}, which is not a page size",
    )

    reviewed_on = _text(
        decision,
        "reviewed_on",
        missing=f"{DECISION_PATH} carries no review date; an undated decision cannot be revisited",
    )

    gates = raw.get("safety_gates")
    _require(isinstance(gates, list), f"{DECISION_PATH} is missing its 'safety_gates' section")
    gates = gates if isinstance(gates, list) else []
    void = tuple(
        str(gate.get("dimension"))
        for gate in gates
        if isinstance(gate, dict) and str(gate.get("status", "")).startswith("void")
    )
    obligations = tuple(
        str(entry.get("item"))
        for entry in raw.get("open_review_obligations", [])
        if isinstance(entry, dict) and not entry.get("reviewed", False)
    )

    return RecallDecision(
        branch=branch,
        reviewed_on=reviewed_on,
        arm_kind=str(approved.get("kind")),
        similarity_floor=float(str(floor)),
        limit=int(str(limit)),
        # Lexical ships under every branch that did not fail the baseline; the
        # artifact says so and the branch is checked against it above.
        lexical_approved=bool(arms.get("lexical")) and branch != _REFUSING_BRANCH,
        semantic_approved=semantic_approved,
        void_safety_dimensions=void,
        open_review_obligations=obligations,
    )


@functools.cache
def load_decision(path: Path | None = None) -> RecallDecision:
    """The committed decision, validated, cached for the life of the process.

    Cached because the artifact is committed rather than configured: it cannot
    change under a running process without the file being edited underneath it,
    and re-reading it on every request would make that edit take effect silently.
    A deployment that changes the decision restarts, which is what makes the
    change visible.
    """
    target = path if path is not None else DECISION_PATH
    if not target.is_file():
        raise DecisionUnavailable(
            f"no approved retrieval branch is committed at {target}; workspace recall does not activate "
            "without the decision artifact that authorizes it"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DecisionUnavailable(f"the approved retrieval branch at {target} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DecisionUnavailable(f"the approved retrieval branch at {target} is not an object")
    return _parse(raw)


# -- the approved arm ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One authorized checkpoint the semantic scan may consider.

    Carries the item it would become rather than the row it came from. The scan
    decides which candidates to serve, not what a served item looks like, and
    building items here would put a second item-construction path beside the
    production one.
    """

    item_key: str
    text: str
    item: ContextItemV1


class Embedder(Protocol):
    """The narrow slice of an embedding model this arm needs.

    Declared here rather than reused from `contextplane.types.Embedder` so this
    module depends on the two attributes it uses. The production protocol and
    this one differ only in what `encode` returns, and `EncodedVectors` accepts
    both shapes -- so the deployment's embedder satisfies this protocol and a
    deterministic test double, returning plain lists, need satisfy only this.
    """

    model_version: str

    def encode(self, texts: list[str]) -> EncodedVectors:
        """Embed each text, in order."""
        ...


def merge_outcomes(*outcomes: ArmOutcome) -> ArmOutcome:
    """Union two or more arm outcomes into one block's worth of facts.

    Deduplicated by item key, because a checkpoint found both lexically and by
    the semantic scan is one item served once -- counting it twice would inflate
    the denominator of a precision the arm did not actually harm.
    """
    seen: dict[str, ContextItemV1] = {}
    exclusions: dict[str, Exclusion] = {}
    truncated = False
    freshest: datetime.datetime | None = None
    reasons: list[str] = []
    for outcome in outcomes:
        for item in outcome.items:
            seen.setdefault(item.receipt_item_id.item_key, item)
        for exclusion in outcome.exclusions:
            exclusions.setdefault(exclusion.item_key, exclusion)
        truncated = truncated or outcome.truncated
        if outcome.fresh_as_of is not None and (freshest is None or outcome.fresh_as_of > freshest):
            freshest = outcome.fresh_as_of
        if outcome.degraded_reason:
            reasons.append(outcome.degraded_reason)

    # An item that one arm served and another withheld is served: the withholding
    # arm declined to return it, it did not revoke the other arm's authority to.
    for key in seen:
        exclusions.pop(key, None)

    return ArmOutcome(
        items=ordered_items(tuple(seen.values())),
        exclusions=tuple(exclusions.values()),
        truncated=truncated,
        fresh_as_of=freshest,
        degraded_reason="; ".join(reasons) if reasons else None,
    )


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, with a zero vector scoring zero rather than raising.

    A zero vector is what the stub embedder produces, and it genuinely carries no
    direction -- so it is similar to nothing. Raising here would turn a
    misconfigured embedder into a request-failing error, which hides the
    misconfiguration behind a retry.
    """
    if len(left) != len(right):
        raise ValueError(f"cannot compare a {len(left)}-dimension vector with a {len(right)}-dimension one")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm == 0.0:
        return 0.0
    return dot / norm


def exact_scan(
    *,
    query: str,
    candidates: Sequence[Candidate],
    embedder: Embedder,
    decision: RecallDecision,
) -> ArmOutcome:
    """Score every authorized candidate against the query, and serve the best.

    The candidate sequence *is* the authorization boundary. Nothing here
    re-checks it, and nothing here can widen it. The floor and the limit come
    from the decision rather than from a caller, so a request cannot ask for a
    laxer threshold than the one the approved configuration was measured at.
    """
    decision.require_semantic()
    if not candidates:
        return ArmOutcome()

    # Both shapes `EncodedVectors` admits are counted, indexed, sliced and
    # iterated identically at runtime; they disagree only to a type checker.
    # The cast names the one the scan reads, because leaving the union in place
    # erases each row to `object` and the similarity below would quietly stop
    # being checked -- trading a real check for an imaginary one.
    vectors = cast("Sequence[Sequence[float]]", embedder.encode([query, *(c.text for c in candidates)]))
    if len(vectors) != len(candidates) + 1:
        raise ValueError(
            f"the embedder returned {len(vectors)} vector(s) for {len(candidates) + 1} text(s); "
            "a scan cannot align scores to candidates it cannot count"
        )
    query_vector = vectors[0]

    scored = [
        (cosine(query_vector, vector), candidate) for vector, candidate in zip(vectors[1:], candidates, strict=True)
    ]
    # Ties break on item key rather than on input order, so two runs over the
    # same authorized set cannot differ by however the rows were fetched.
    scored.sort(key=lambda pair: (-pair[0], pair[1].item_key))

    kept = [candidate for similarity, candidate in scored if similarity >= decision.similarity_floor]
    return ArmOutcome(
        items=ordered_items(tuple(c.item for c in kept[: decision.limit])),
        truncated=len(kept) > decision.limit,
    )


__all__ = [
    "DECISION_PATH",
    "Candidate",
    "DecisionUnavailable",
    "Embedder",
    "RecallDecision",
    "SemanticRecallNotApproved",
    "cosine",
    "exact_scan",
    "load_decision",
    "merge_outcomes",
]
