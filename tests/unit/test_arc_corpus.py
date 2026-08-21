"""Unit tests for `CorpusReader._drop_integrity_failed` -- the §6.3
"mandatory corpus assembly" integrity prefilter.

No database: this method's only session use is forwarding it, unread, into
`self._integrity.assess`, so a bare `object()` sentinel and a fake
integrity collaborator are enough to exercise the filtering logic itself.
The SQL-backed halves of `CorpusReader` (`_candidates`/`_exceptions`/
`_obligations`) are covered against real Postgres in
`tests/integration/test_arc_corpus_assembly.py`.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.arc.service.corpus import CorpusReader, _obligation_rule
from contextplane.arc.service.integrity import PURPOSE_CORPUS_ASSEMBLY
from contextplane.arc.types import ApplicabilityRule, AuthorityScope, Directive, DirectiveType

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _FakeIntegrity:
    def __init__(self, outcomes: dict[uuid.UUID, bool] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def assess(self, session: object, revision_id: uuid.UUID, purpose: str) -> _FakeAssessment:
        self.calls.append((revision_id, purpose))
        return _FakeAssessment(valid=self.outcomes.get(revision_id, True))


class _FakeAssessment:
    def __init__(self, *, valid: bool) -> None:
        self.valid = valid
        self.reason_code = None if valid else "arc_operational_integrity_failed"


def _candidate(revision_id: uuid.UUID) -> tuple[Directive, ApplicabilityRule, datetime.datetime]:
    directive = Directive(
        directive_id=uuid.uuid4(),
        revision_id=revision_id,
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="a#1",
    )
    rule = ApplicabilityRule(rule_id=uuid.uuid4(), revision_id=revision_id, scope=AuthorityScope.GLOBAL)
    return (directive, rule, _NOW)


def _reader(integrity: _FakeIntegrity) -> CorpusReader:
    return CorpusReader(None, integrity=integrity)  # type: ignore[arg-type]


async def test_an_empty_candidate_tuple_short_circuits_with_no_assessment() -> None:
    integrity = _FakeIntegrity()
    result = await _reader(integrity)._drop_integrity_failed(object(), ())
    assert result == ()
    assert integrity.calls == []


async def test_every_candidate_survives_when_every_revision_passes() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    candidates = (_candidate(a), _candidate(b))
    integrity = _FakeIntegrity()

    result = await _reader(integrity)._drop_integrity_failed(object(), candidates)

    assert result == candidates
    assert sorted(integrity.calls) == sorted([(a, PURPOSE_CORPUS_ASSEMBLY), (b, PURPOSE_CORPUS_ASSEMBLY)])


async def test_a_candidate_whose_revision_fails_integrity_is_excluded() -> None:
    kept, dropped = uuid.uuid4(), uuid.uuid4()
    candidates = (_candidate(kept), _candidate(dropped))
    integrity = _FakeIntegrity({dropped: False})

    result = await _reader(integrity)._drop_integrity_failed(object(), candidates)

    assert result == (candidates[0],)


async def test_a_revision_shared_by_two_candidates_is_assessed_exactly_once() -> None:
    """Several directives from the same revision must not charge a second
    `assess` call for a fact that does not change between them."""
    shared = uuid.uuid4()
    directive_one, rule, effective = _candidate(shared)
    directive_two = Directive(
        directive_id=uuid.uuid4(), revision_id=shared, directive_type=DirectiveType.CITATION_ONLY, source_anchor="a#2"
    )
    candidates = ((directive_one, rule, effective), (directive_two, rule, effective))
    integrity = _FakeIntegrity()

    result = await _reader(integrity)._drop_integrity_failed(object(), candidates)

    assert result == candidates
    assert integrity.calls == [(shared, PURPOSE_CORPUS_ASSEMBLY)]


async def test_dropping_every_candidate_returns_an_empty_tuple_not_none() -> None:
    revision_id = uuid.uuid4()
    candidates = (_candidate(revision_id),)
    integrity = _FakeIntegrity({revision_id: False})

    result = await _reader(integrity)._drop_integrity_failed(object(), candidates)

    assert result == ()


# --- obligation snapshot rehydration ---------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "why"),
    (
        ({"scope": "entity", "entity_ids": []}, "entity scope with no entity"),
        ({"scope": "tenant", "target_tenant_id": None}, "tenant scope with no target tenant"),
        ({"scope": "domain", "domain_ids": []}, "domain scope with no domain"),
        ({"scope": "intent", "intent_kinds": []}, "intent scope with no selectors"),
    ),
)
def test_a_snapshot_the_rule_constructor_refuses_degrades_rather_than_raising(
    snapshot: dict[str, object], why: str
) -> None:
    """The fallback has to hold for every shape `__post_init__` refuses.

    `ApplicabilityRule.__post_init__` raises `ArcVocabularyError`, which is a
    `RegistryError` -- neither a `ValueError` nor a `TypeError`, which was all
    `_obligation_rule` caught. So a snapshot in any of these shapes threw
    straight past the handler *and* past `_obligations`, which wraps nothing,
    and every context resolution for that tenant failed until somebody deleted
    the row. The "an unreadable obligation must still block" path never ran,
    because the failure did not arrive as unreadable.

    Returning `None` is what puts it back on that path: the caller keeps the
    obligation rather than skipping it, so an unreadable tombstone still blocks.
    """
    assert _obligation_rule(snapshot, uuid.uuid4()) is None, why


def test_a_readable_snapshot_still_rehydrates() -> None:
    """The widened `except` must not have turned the happy path into `None`."""
    rule = _obligation_rule({"scope": "global"}, uuid.uuid4())

    assert rule is not None
    assert rule.scope is AuthorityScope.GLOBAL
