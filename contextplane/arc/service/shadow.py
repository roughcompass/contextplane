"""`ShadowService`: the ADR 041 Sec.9 overlay -- active revisions plus
exactly one authorized draft substituted for its own artifact-family
baseline.

**This is a distinct code path, not a widened production query.**
`corpus.py::CorpusReader.assemble` is called completely unmodified -- this
module never edits its SQL or its `_SELECTABLE_LIFECYCLE` tuple (`active`,
`expired`). The overlay is a pure, post-hoc substitution performed on the
tuple `assemble` already returned: `overlay_candidate_set` drops whichever
entries belong to the one revision being replaced (the candidate's own
family baseline) and adds entries built from the candidate's own frozen
`arc_artifact_semantics_v1` document -- a `draft` revision that could never
have been produced by `corpus.py`'s own query, because that query's
lifecycle filter was never touched. `tests/conformance/test_arc_corpus_
lifecycle_filter.py` asserts both halves of that claim: the filter tuple is
unchanged, and the overlay function is what (and the only thing that)
injects the draft.

**Directive/rule reconstruction duplicates three small helpers from
`corpus.py`** (`_uuid_set`/`_str_set`/`_vocab_set`-shaped conversions and
the mandatory-first/widest-scope governing-rule tie-break) rather than
importing them: those names are private to a sibling module this task's
concurrency note forbids editing, and each is a three-to-five-line
conversion -- the same "duplicate a small helper rather than a cross-module
private import" convention `review_package.py`'s own `_rfc3339` already
follows in this package.

Shadow output is diagnostic only: nothing here writes a receipt, satisfies
an obligation, or is reachable from the live `/v1/arc/resolve` path --
wiring shadow evaluation into request-serving is explicitly a later
phase's work, not this task's.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.corpus import CorpusReader
from contextplane.arc.service.selection import SelectionInput, SelectionResult, select
from contextplane.arc.types import (
    ActionClass,
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    NormalizedConstraint,
    ResolutionStatus,
    SatisfactionMode,
    TaskKind,
    TaskManifest,
    parse_wire_directive_type,
)
from contextplane.exceptions import RegistryError

#: One entry of `SelectionInput.candidates`: a directive, the rule that
#: governs it, and the revision's own effective-from timestamp.
_CandidateEntry = tuple[Directive, ApplicabilityRule, datetime.datetime]


class ShadowError(RegistryError):
    """The candidate has no usable frozen semantics to build an overlay
    from (`arc_proposal_validation_failed`, 422) -- reachable only if a
    caller invokes shadow evaluation before submission has frozen a
    candidate document, which the qualification service never does."""


# ---------------------------------------------------------------------------
# Small closed-vocabulary conversions, duplicated from `corpus.py` -- see
# this module's own docstring for why.
# ---------------------------------------------------------------------------


def _uuid_set(raw: list[Any] | None) -> frozenset[uuid.UUID]:
    return frozenset(v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in raw or ())


def _str_set(raw: list[Any] | None) -> frozenset[str]:
    return frozenset(str(v) for v in raw or ())


def _vocab_set(raw: list[Any] | None, vocab: type[TaskKind] | type[ActionClass]) -> frozenset[Any]:
    return frozenset(vocab(v) for v in raw or ())


def _parse_ts(value: str | datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _governing_rule(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """The one rule every directive in a single-revision candidate pairs
    with -- mirrors `corpus.py`'s own `_governing_rank` tie-break (mandatory
    first, then widest authority scope) for the case that module handles by
    joining a revision's directives against its own multiple rules with no
    column tying one to the other. A candidate with zero rules cannot reach
    here: proposal validation rejects an empty applicability list before
    submission, the same invariant `risk.py`'s reducer relies on.
    """
    return min(
        rules,
        key=lambda rule: (
            0 if rule.get("is_mandatory") else 1,
            AuthorityScope(rule["scope"]).rank,
            str(rule["rule_id"]),
        ),
    )


def _rule_from_dict(rule: dict[str, Any], revision_id: uuid.UUID) -> ApplicabilityRule:
    target = rule.get("target_tenant_id")
    return ApplicabilityRule(
        rule_id=uuid.UUID(str(rule["rule_id"])),
        revision_id=revision_id,
        scope=AuthorityScope(rule["scope"]),
        is_mandatory=bool(rule.get("is_mandatory")),
        target_tenant_id=uuid.UUID(str(target)) if target else None,
        capability_ids=_uuid_set(rule.get("capability_ids")),
        capability_labels=_str_set(rule.get("capability_labels")),
        domain_ids=_str_set(rule.get("domain_ids")),
        task_kinds=_vocab_set(rule.get("task_kinds"), TaskKind),
        action_classes=_vocab_set(rule.get("action_classes"), ActionClass),
        environments=_str_set(rule.get("environments")),
        data_sensitivity_tiers=_str_set(rule.get("data_sensitivity_tiers")),
        effective_from=_parse_ts(rule.get("effective_from")),
        effective_until=_parse_ts(rule.get("effective_until")),
    )


def _directive_from_dict(entry: dict[str, Any], revision_id: uuid.UUID) -> Directive:
    """Build the domain directive from one candidate `directives[]`
    element -- the shadow-overlay counterpart of `corpus.py::_directive_
    from_row`, reading a still-`draft` candidate's own frozen JSON instead
    of a persisted `arc_directives` row.

    `directive_type` goes through `parse_wire_directive_type`, the same
    definition `submission.py::_directive_row` translates through when
    writing the persisted row -- so a candidate this overlay can build a
    domain object for and one submission can materialise are always the
    same set. It fails the same way on the same input for the same reason:
    a wire literal with no persisted counterpart is not a value this
    module can guess a mapping for.
    """
    subject: ConflictSubjectKey | None = None
    constraint: NormalizedConstraint | None = None
    if entry.get("conflict_subject_digest") is not None:
        subject = ConflictSubjectKey(
            schema_version=str(entry["conflict_key_schema_version"]),
            namespace=entry["conflict_key_namespace"],
            subject_selector=entry["conflict_key_subject_selector"],
            operation=entry["conflict_key_operation"],
            action_class=entry["conflict_key_action_class"],
            target_selector=entry["conflict_key_target_selector"],
        )
        constraint = NormalizedConstraint.parse(
            entry["conflict_key_modality"],
            entry["conflict_key_constraint_operator"],
            entry["conflict_key_constraint_value"],
        )
    return Directive(
        directive_id=uuid.UUID(str(entry["directive_id"])),
        revision_id=revision_id,
        directive_type=parse_wire_directive_type(str(entry["directive_type"])),
        source_anchor=entry["source_anchor"],
        conflict_subject=subject,
        constraint=constraint,
        satisfaction_mode=(SatisfactionMode(entry["satisfaction_mode"]) if entry.get("satisfaction_mode") else None),
        verification_max_age_seconds=entry.get("verification_max_age_seconds"),
        accepted_verifier_classes=_str_set(entry.get("accepted_verifier_classes")),
        required_evidence_type=entry.get("required_evidence_type"),
        delegable_exception=bool(entry.get("delegable_exception", False)),
    )


def candidate_entries(
    semantics: dict[str, Any], *, revision_id: uuid.UUID, effective_from: datetime.datetime
) -> tuple[_CandidateEntry, ...]:
    """Build the candidate's own `(Directive, ApplicabilityRule, datetime)`
    entries from its frozen `arc_artifact_semantics_v1` document -- the
    overlay's substitute for whatever `corpus.py` would have loaded for an
    *active* revision, built instead from JSON because this revision's
    lifecycle state (`draft`) means that query never returns it.
    """
    rules = list(semantics.get("applicability") or ())
    if not rules:
        msg = (
            "candidate semantics carry no applicability rules -- proposal validation "
            "should have rejected this before submission"
        )
        raise ShadowError(msg)
    governing = _governing_rule(rules)
    rule = _rule_from_dict(governing, revision_id)
    directives = list(semantics.get("directives") or ())
    return tuple((_directive_from_dict(d, revision_id), rule, effective_from) for d in directives)


def overlay_candidate_set(
    candidates: tuple[_CandidateEntry, ...],
    *,
    baseline_revision_id: uuid.UUID | None,
    overlay_entries: tuple[_CandidateEntry, ...],
) -> tuple[_CandidateEntry, ...]:
    """The overlay itself: drop every entry belonging to *baseline_
    revision_id* (the candidate's own family's current baseline, so it is
    never counted alongside the candidate it is being compared against),
    then append *overlay_entries* (the candidate's own directives).

    Pure and DB-free by design -- this is the one function `tests/
    conformance/test_arc_corpus_lifecycle_filter.py` exercises directly to
    prove the overlay adds exactly the substituted draft and nothing a
    lifecycle-widened production query would also have let in: every other
    entry's `revision_id` is untouched, and no entry here is ever inspected
    for a `lifecycle_state` this tuple shape does not even carry.
    """
    if baseline_revision_id is None:
        kept = candidates
    else:
        kept = tuple(entry for entry in candidates if entry[0].revision_id != baseline_revision_id)
    return kept + overlay_entries


@dataclasses.dataclass(frozen=True)
class ShadowDelta:
    """One manifest's worth of ADR 041 Sec.4 delta-code occurrences,
    comparing the overlay (candidate active) selection against the
    baseline (current production) selection over the identical manifest.

    `delta_codes` may repeat a code once per affected directive (`newly_
    selected`/`no_longer_selected`) or carry it at most once
    (`conflict_changed`/`mandatory_block_added`/`mandatory_block_removed`,
    each a manifest-level event rather than a per-directive one). All
    codes in one `ShadowDelta` share the same manifest, and therefore the
    same `arc_observation_class_predicate_v1` class -- `qualification.py`
    matches every occurrence against that one class.
    """

    delta_codes: tuple[str, ...]


def diff_selection(baseline: SelectionResult, overlay: SelectionResult) -> ShadowDelta:
    """Compare two `select()` outcomes over the same manifest and reduce
    the difference to ADR 041's closed delta-code vocabulary.

    Mandatory-block transitions are read off the reduced `status` rather
    than the raw `blocked_reasons` set: `select()` reduces exactly once
    (any blocking reason yields `blocked`), so the overall status flipping
    is the manifest-level fact an envelope item's `mandatory_block_added`/
    `_removed` predicate is meant to describe. Directive-level appearance/
    disappearance is read off the mandatory+optional directive-id sets,
    independent of that reduction.
    """
    baseline_ids = frozenset(s.directive.directive_id for s in (*baseline.mandatory, *baseline.optional))
    overlay_ids = frozenset(s.directive.directive_id for s in (*overlay.mandatory, *overlay.optional))
    codes: list[str] = []
    codes.extend("newly_selected" for _ in (overlay_ids - baseline_ids))
    codes.extend("no_longer_selected" for _ in (baseline_ids - overlay_ids))
    if baseline.conflicts != overlay.conflicts:
        codes.append("conflict_changed")
    if overlay.status is ResolutionStatus.BLOCKED and baseline.status is not ResolutionStatus.BLOCKED:
        codes.append("mandatory_block_added")
    if baseline.status is ResolutionStatus.BLOCKED and overlay.status is not ResolutionStatus.BLOCKED:
        codes.append("mandatory_block_removed")
    return ShadowDelta(delta_codes=tuple(codes))


def _predicate_matches(class_predicate: dict[str, Any], manifest_class: dict[str, Any]) -> bool:
    """Whether *manifest_class* (an `arc_observation_class_predicate_v1`
    object with every field a concrete singleton set) falls inside
    *class_predicate* (an envelope item's own predicate, where a `null`
    field is unconstrained). Every field must match; there is no
    "any field matches" shortcut, because ADR 041 Sec.4 items are
    conjunctive predicates, not disjunctive ones.
    """
    for field in (
        "task_kind",
        "requested_action_classes",
        "environment",
        "data_sensitivity_tier",
        "capability_ids",
        "domain_ids",
    ):
        allowed = class_predicate.get(field)
        if allowed is None:
            continue
        allowed_set = {str(v) for v in allowed}
        observed = {str(v) for v in (manifest_class.get(field) or ())}
        if not observed.issubset(allowed_set):
            return False
    return True


@dataclasses.dataclass(frozen=True)
class DeltaMatch:
    """One delta occurrence's classification: `item_id` is the single
    envelope item it explained, or `None` if it matched zero or more than
    one -- both count as unexplained per ADR 041 Sec.4 ("matches exactly
    one envelope item")."""

    delta_code: str
    item_id: str | None


def match_deltas_to_envelope(
    delta: ShadowDelta, manifest_class: dict[str, Any], envelope_items: list[dict[str, Any]]
) -> tuple[DeltaMatch, ...]:
    """Classify every occurrence in *delta* against *envelope_items*,
    given the one manifest class every occurrence shares. Item-level
    minimum/maximum range checking is the caller's job once every
    manifest in a window (or corpus) has been folded in -- a single
    occurrence cannot know whether the *cumulative* count for its item is
    in range.
    """
    matches: list[DeltaMatch] = []
    for code in delta.delta_codes:
        candidates = [
            item
            for item in envelope_items
            if item["delta_code"] == code and _predicate_matches(item["class_predicate"], manifest_class)
        ]
        matches.append(DeltaMatch(delta_code=code, item_id=candidates[0]["item_id"] if len(candidates) == 1 else None))
    return tuple(matches)


class ShadowService:
    """Evaluates one manifest under the ADR 041 Sec.9 overlay.

    Composes the deployment's own `CorpusReader` rather than opening a
    second read path to `arc_revisions`/`arc_directives`/`arc_
    applicability_rules` -- see this module's own docstring for why that
    matters (the overlay must be provably built on top of the unmodified
    production query, not a parallel one that could quietly drift from it).
    """

    def __init__(self, corpus: CorpusReader, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._corpus = corpus
        self._session_factory = session_factory

    async def build_overlay_input(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest: TaskManifest,
        as_of: datetime.datetime,
        baseline_revision_id: uuid.UUID | None,
        candidate_revision_id: uuid.UUID,
        candidate_semantics: dict[str, Any],
    ) -> tuple[SelectionInput, SelectionInput]:
        """Returns `(baseline_input, overlay_input)` -- the same manifest,
        exceptions, and obligations, differing only in `candidates`."""
        baseline_input = await self._corpus.assemble(tenant_id=tenant_id, manifest=manifest, as_of=as_of)
        overlay_entries = candidate_entries(
            candidate_semantics, revision_id=candidate_revision_id, effective_from=as_of
        )
        overlay_candidates = overlay_candidate_set(
            baseline_input.candidates, baseline_revision_id=baseline_revision_id, overlay_entries=overlay_entries
        )
        overlay_input = dataclasses.replace(baseline_input, candidates=overlay_candidates)
        return baseline_input, overlay_input

    async def evaluate(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest: TaskManifest,
        as_of: datetime.datetime,
        baseline_revision_id: uuid.UUID | None,
        candidate_revision_id: uuid.UUID,
        candidate_semantics: dict[str, Any],
    ) -> ShadowDelta:
        """One eligible manifest, evaluated under both selections and
        reduced to its delta-code occurrences."""
        baseline_input, overlay_input = await self.build_overlay_input(
            tenant_id=tenant_id,
            manifest=manifest,
            as_of=as_of,
            baseline_revision_id=baseline_revision_id,
            candidate_revision_id=candidate_revision_id,
            candidate_semantics=candidate_semantics,
        )
        return diff_selection(select(baseline_input), select(overlay_input))


__all__ = [
    "DeltaMatch",
    "ShadowDelta",
    "ShadowError",
    "ShadowService",
    "candidate_entries",
    "diff_selection",
    "match_deltas_to_envelope",
    "overlay_candidate_set",
]
