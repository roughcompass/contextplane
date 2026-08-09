"""What an extractor may conclude, how much authority that conclusion may carry,
and what gets written when it records the attempt.

The rule this file exists for is the one SQL cannot hold: a derived claim inherits
at most what its weakest source was entitled to assert. The schema stores the
inputs to that comparison and can enforce none of it, because authority is a
source-issued string whose ordering lives in the governance ladder rather than in
the database. So the ceiling is a service-layer obligation, and these are the
tests that make it one.

The recording half is proved against a session the test controls rather than a
real database, because what matters here is not that the SQL executes — the
integration suite runs it against Postgres — but *what is sent and in what
order*. A refusal that happens after the insert has already refused nothing, and
a replay lookup that lost its tenant condition still returns a perfectly
reasonable-looking row belonging to somebody else. Neither failure is visible in
a result set; both are visible in the statements.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_UNATTRIBUTED,
    SOURCE_AUTHORITY_RANK,
)
from contextplane.service.memory.derivation import (
    MAX_EXCERPT_CHARS,
    STATUS_PENDING,
    STATUS_STAGED,
    Assertion,
    DerivationProfile,
    DerivationRefused,
    DerivationService,
    Evidence,
    RecordedDerivation,
    assertion_digest,
    may_promote,
    weakest_authority,
)
from contextplane.types import TenantContext

_PROFILE = DerivationProfile(name="outcome-extractor", version="1.4.0")


def _evidence(authority: str = AUTHORITY_OBSERVER_EXTRACTION, **overrides: object) -> Evidence:
    fields: dict[str, object] = {
        "kind": "signal",
        "source_authority": authority,
        "classification": "internal",
        "signal_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return Evidence(**fields)  # type: ignore[arg-type]


def _assertion(**overrides: object) -> Assertion:
    fields: dict[str, object] = {
        "subject_reference": "capability:billing",
        "predicate": "context_was_stale",
        "value": {"observed": "runbook referenced a removed step"},
        "applicability": "repo:roughcompass/contextplane",
    }
    fields.update(overrides)
    return Assertion(**fields)  # type: ignore[arg-type]


# --- The ceiling --------------------------------------------------------------


def test_the_weakest_source_sets_the_ceiling() -> None:
    """Weakest, not strongest, and not the one that happened to trigger the run."""
    ceiling = weakest_authority(
        [
            _evidence(AUTHORITY_OWNER_HUMAN),
            _evidence(AUTHORITY_OBSERVER_EXTRACTION),
            _evidence(AUTHORITY_OBSERVER_HUMAN),
        ]
    )
    assert ceiling == AUTHORITY_OBSERVER_EXTRACTION


def test_the_ceiling_is_computed_by_rank_not_by_string_order() -> None:
    """Comparing the strings would sort alphabetically and mean nothing.

    `observer_extraction` sorts before `owner_human` alphabetically while being
    the *weaker* of the two, so a string comparison would hand a derived claim
    more authority than its evidence had — silently, and in the dangerous
    direction.
    """
    pair = [_evidence(AUTHORITY_OWNER_HUMAN), _evidence(AUTHORITY_OBSERVER_EXTRACTION)]
    assert weakest_authority(pair) == AUTHORITY_OBSERVER_EXTRACTION
    assert min(AUTHORITY_OWNER_HUMAN, AUTHORITY_OBSERVER_EXTRACTION) == AUTHORITY_OBSERVER_EXTRACTION
    assert SOURCE_AUTHORITY_RANK[AUTHORITY_OBSERVER_EXTRACTION] > SOURCE_AUTHORITY_RANK[AUTHORITY_OWNER_HUMAN]


def test_no_evidence_licenses_nothing() -> None:
    assert weakest_authority([]) == AUTHORITY_UNATTRIBUTED


@pytest.mark.parametrize(
    ("claimed", "evidence_authority"),
    [
        pytest.param(AUTHORITY_OWNER_HUMAN, AUTHORITY_OBSERVER_EXTRACTION, id="owner-from-observer"),
        pytest.param(AUTHORITY_OBSERVER_HUMAN, AUTHORITY_UNATTRIBUTED, id="human-from-unattributed"),
    ],
)
def test_an_attempt_claiming_more_than_its_evidence_is_refused(claimed: str, evidence_authority: str) -> None:
    """Refused rather than clamped.

    Clamping would produce an assertion nobody asked for and leave the caller
    believing the stronger one was recorded. The refusal names the ceiling, so
    the caller learns which evidence was too weak.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    with pytest.raises(DerivationRefused, match="may carry at most"):
        service._assert_within_ceiling(claimed, evidence_authority)


@pytest.mark.parametrize(
    "claimed",
    [
        pytest.param(AUTHORITY_OBSERVER_EXTRACTION, id="exactly-at-the-ceiling"),
        pytest.param(AUTHORITY_UNATTRIBUTED, id="below-the-ceiling"),
    ],
)
def test_an_attempt_at_or_below_the_ceiling_is_allowed(claimed: str) -> None:
    """The ceiling must not refuse everything, which a too-eager check would.

    Asserted positively rather than by calling and hoping: a test whose only
    evidence is the absence of an exception passes just as happily when the
    method under test does nothing at all.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    assert _permits(service, claimed, AUTHORITY_OBSERVER_EXTRACTION) is True


def test_an_authority_off_the_ladder_is_refused() -> None:
    """A tier nobody declared has no rank, so no comparison against it means anything."""
    with pytest.raises(DerivationRefused, match="ladder"):
        _evidence("supreme_authority")


# --- What may be asserted -----------------------------------------------------


def test_causation_is_not_a_predicate_an_extractor_may_assert() -> None:
    """Two observations are not a cause.

    "The run failed" and "this context was served" are both readable from the
    evidence; "this context caused the failure" is a third claim with evidence
    requirements no extractor reading these inputs can meet.
    """
    with pytest.raises(DerivationRefused, match="not one an extractor may assert"):
        _assertion(predicate="caused_failure")


@pytest.mark.parametrize("field", ["subject_reference", "applicability"])
def test_an_assertion_without_subject_or_scope_is_refused(field: str) -> None:
    """An assertion naming neither what it is about nor where it holds cannot be reviewed."""
    with pytest.raises(DerivationRefused):
        _assertion(**{field: "   "})


def test_an_unknown_evidence_kind_is_refused() -> None:
    with pytest.raises(DerivationRefused, match="unknown evidence kind"):
        _evidence(kind="rumour")


def test_checkpoint_evidence_needs_its_digest() -> None:
    """The id says which checkpoint; the digest says it had not changed when read.

    A citation without the digest claims an immutability it never verified.
    """
    with pytest.raises(DerivationRefused, match="digest"):
        Evidence(
            kind="checkpoint",
            source_authority=AUTHORITY_OWNER_HUMAN,
            classification="internal",
            checkpoint_id=uuid.uuid4(),
        )


def test_exact_item_evidence_needs_both_halves() -> None:
    with pytest.raises(DerivationRefused, match="receipt"):
        Evidence(
            kind="receipt_item",
            source_authority=AUTHORITY_OWNER_HUMAN,
            classification="internal",
            receipt_id=uuid.uuid4(),
        )


# --- Excerpts are excerpts ----------------------------------------------------


def test_an_excerpt_longer_than_the_bound_is_refused() -> None:
    """A bounded excerpt that happens to be the whole field is a copy with a shorter name.

    The bound is a length the code enforces, not an intention a docstring states.
    """
    with pytest.raises(DerivationRefused, match="bound is"):
        _evidence(excerpt="x" * (MAX_EXCERPT_CHARS + 1))


def test_an_excerpt_within_the_bound_is_kept() -> None:
    item = _evidence(excerpt="y" * MAX_EXCERPT_CHARS)
    assert item.excerpt is not None
    assert len(item.excerpt) == MAX_EXCERPT_CHARS


def test_the_bound_is_far_below_a_body() -> None:
    """The number matters less than the property: no body fits.

    A bound generous enough to hold a checkpoint payload would let a copy pass as
    a quotation, which is the failure this bound exists to prevent.
    """
    assert MAX_EXCERPT_CHARS <= 2048


# --- Identity -----------------------------------------------------------------


def test_the_same_conclusion_from_the_same_profile_digests_alike() -> None:
    assert assertion_digest(_PROFILE, _assertion()) == assertion_digest(_PROFILE, _assertion())


def test_a_later_extractor_version_is_a_different_attempt() -> None:
    """Folding the version out would make an upgrade look like a replay."""
    later = DerivationProfile(name=_PROFILE.name, version="1.5.0")
    assert assertion_digest(_PROFILE, _assertion()) != assertion_digest(later, _assertion())


def test_a_changed_conclusion_digests_differently() -> None:
    assert assertion_digest(_PROFILE, _assertion()) != assertion_digest(
        _PROFILE, _assertion(predicate="context_was_incomplete")
    )


# --- Supersession -------------------------------------------------------------


def test_promotion_is_barred_when_every_input_was_superseded() -> None:
    """Both runs happened; the superseded one is no longer the thing to learn from.

    The attempt is still recorded — dropping it would lose the fact that the
    derivation was made — but promoting on evidence a later run has overtaken
    would canonicalize a conclusion that evidence may already contradict.
    """
    recorded = _recorded(superseded_only=True)
    assert may_promote(recorded) is False


def test_promotion_is_allowed_when_any_input_still_stands() -> None:
    assert may_promote(_recorded(superseded_only=False)) is True


# --- Recording the attempt ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_derivation_with_no_evidence_never_reaches_a_session() -> None:
    """Nothing licenses it, so there is nothing to write.

    Asserted as "no session was opened" rather than merely "it raised": a refusal
    that happens after the transaction has begun has already spent the write it
    was supposed to prevent, and only the call log distinguishes the two.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    with pytest.raises(DerivationRefused, match="nothing licenses"):
        await service.derive(_ctx(), profile=_PROFILE, assertion=_assertion(), evidence=[])


@pytest.mark.asyncio
async def test_an_over_claiming_attempt_is_refused_before_anything_is_written() -> None:
    """The ceiling is checked ahead of the insert, not alongside it.

    If the order were reversed the attempt would exist in the table carrying an
    authority its evidence never licensed, and the caller's exception would be a
    report about a row that had already been written.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    with pytest.raises(DerivationRefused, match="may carry at most"):
        await service.derive(
            _ctx(),
            profile=_PROFILE,
            assertion=_assertion(),
            evidence=[_evidence(AUTHORITY_OBSERVER_EXTRACTION)],
            claimed_authority=AUTHORITY_OWNER_HUMAN,
        )


@pytest.mark.asyncio
async def test_a_claimed_authority_off_the_ladder_is_refused_through_the_public_path() -> None:
    """An unranked tier cannot be compared, so it cannot be admitted.

    Exercised through `derive` rather than the check alone, because a refusal the
    public entry point swallows is not a refusal.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    with pytest.raises(DerivationRefused, match="not on the ladder"):
        await service.derive(
            _ctx(),
            profile=_PROFILE,
            assertion=_assertion(),
            evidence=[_evidence(AUTHORITY_OWNER_HUMAN)],
            claimed_authority="supreme_authority",
        )


@pytest.mark.asyncio
async def test_the_stored_attempt_carries_the_ceiling_its_evidence_licensed() -> None:
    """The attempt's authority is a fact about its evidence, not about its caller.

    So it is the weakest source, recorded whether or not the caller named one —
    the ceiling is what a later reader needs to judge the conclusion, and a
    caller-supplied value would make that judgement depend on who asked.
    """
    factory, executed = _recording_session_factory()
    recorded = await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(),
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[_evidence(AUTHORITY_OWNER_HUMAN), _evidence(AUTHORITY_OBSERVER_EXTRACTION)],
    )
    assert recorded.source_authority == AUTHORITY_OBSERVER_EXTRACTION
    assert _params_of(executed, "INSERT INTO claim_derivations")["auth"] == AUTHORITY_OBSERVER_EXTRACTION


@pytest.mark.asyncio
async def test_the_attempt_is_stored_pending_so_the_extractor_does_not_approve_its_own_output() -> None:
    """`pending`, never `staged`.

    Whether a conclusion becomes a claim is decided against the curation path's
    evidence rules; an extractor writing its own output as staged would be
    approving its own work, and nothing downstream would know it had.
    """
    factory, executed = _recording_session_factory()
    recorded = await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(), profile=_PROFILE, assertion=_assertion(), evidence=[_evidence()]
    )
    assert recorded.status == STATUS_PENDING
    assert _params_of(executed, "INSERT INTO claim_derivations")["st"] == STATUS_PENDING
    assert STATUS_STAGED not in [params.get("st") for _, params in executed]


@pytest.mark.asyncio
async def test_every_evidence_item_becomes_a_link_carrying_its_own_authority() -> None:
    """Per-item authority, not the ceiling repeated.

    The ceiling is a `max()` over the links, so it stays recomputable only while
    each link records what its own source carried. Stamping the ceiling onto every
    row would make the strongest evidence indistinguishable from the weakest and
    the ceiling impossible to re-derive from what was stored.
    """
    factory, executed = _recording_session_factory()
    await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(),
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[_evidence(AUTHORITY_OWNER_HUMAN), _evidence(AUTHORITY_OBSERVER_EXTRACTION)],
    )
    links = _all_params(executed, "INSERT INTO derivation_evidence_links")
    assert [link["auth"] for link in links] == [AUTHORITY_OWNER_HUMAN, AUTHORITY_OBSERVER_EXTRACTION]


@pytest.mark.asyncio
async def test_a_link_records_the_pointers_its_kind_requires() -> None:
    """Provenance has to resolve back, so the pair is stored as a pair.

    A checkpoint keeps the digest it was read at and a receipt item keeps the
    receipt it sits on; dropping either half leaves a citation that names
    something nobody can look up.
    """
    checkpoint_id, digest = uuid.uuid4(), "sha256:deadbeef"
    receipt_id = uuid.uuid4()
    factory, executed = _recording_session_factory()
    await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(),
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[
            Evidence(
                kind="checkpoint",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                checkpoint_id=checkpoint_id,
                checkpoint_digest=digest,
            ),
            Evidence(
                kind="receipt_item",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                receipt_id=receipt_id,
                receipt_item_id="item-7",
            ),
        ],
    )
    checkpoint_link, item_link = _all_params(executed, "INSERT INTO derivation_evidence_links")
    assert (checkpoint_link["cid"], checkpoint_link["cdig"]) == (checkpoint_id, digest)
    assert (item_link["r"], item_link["i"]) == (receipt_id, "item-7")


@pytest.mark.asyncio
async def test_the_attempt_and_its_links_commit_together() -> None:
    """One transaction, because half a chain is worse than none.

    An attempt whose links did not land reads as a derivation from no evidence,
    which is exactly the state `derive` refuses to be handed.
    """
    factory, executed = _recording_session_factory()
    await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(), profile=_PROFILE, assertion=_assertion(), evidence=[_evidence(), _evidence()]
    )
    kinds = [sql.split(" (")[0] if sql.startswith("INSERT") else sql.split()[0] for sql, _ in executed]
    assert kinds == [
        "SELECT",
        "INSERT INTO claim_derivations",
        "INSERT INTO derivation_evidence_links",
        "INSERT INTO derivation_evidence_links",
        "COMMIT",
    ]


@pytest.mark.asyncio
async def test_a_replay_returns_the_stored_attempt_and_writes_nothing() -> None:
    """The same conclusion twice is one attempt, not two.

    Proved by the absence of any insert rather than by the returned flag alone: a
    service that wrote a second row and then reported `replayed=True` would look
    identical to its caller and would double-count the derivation.
    """
    stored = _stored_row(status=STATUS_PENDING, evidence_count=3)
    factory, executed = _recording_session_factory(existing=stored)
    recorded = await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(), profile=_PROFILE, assertion=_assertion(), evidence=[_evidence()]
    )
    assert recorded.replayed is True
    assert recorded.derivation_id == stored.derivation_id
    assert recorded.evidence_count == 3
    assert [sql for sql, _ in executed if sql.startswith("INSERT")] == []


@pytest.mark.asyncio
async def test_the_replay_lookup_is_scoped_by_tenant_profile_version_and_digest() -> None:
    """Asserted by condition, which is the half a returned row cannot show.

    A lookup that lost its tenant condition still finds a row, and the row looks
    entirely reasonable — it is simply another tenant's attempt, returned as this
    tenant's replay, so the caller is handed a conclusion drawn from evidence it
    may not even be allowed to see. Dropping the profile version instead makes an
    extractor upgrade look like a replay of the old version's work. Both fail here
    rather than in production.
    """
    ctx = _ctx()
    factory, executed = _recording_session_factory()
    await DerivationService(factory, clock=_FrozenClock()).derive(
        ctx, profile=_PROFILE, assertion=_assertion(), evidence=[_evidence()]
    )
    lookup, params = next((sql, p) for sql, p in executed if sql.startswith("SELECT"))
    for condition in ("d.tenant_id = :tid", "d.profile = :p", "d.profile_version = :v", "d.assertion_digest = :dig"):
        assert condition in lookup
    assert params == {
        "tid": ctx.tenant_id,
        "p": _PROFILE.name,
        "v": _PROFILE.version,
        "dig": assertion_digest(_PROFILE, _assertion()),
    }


@pytest.mark.asyncio
async def test_an_attempt_on_superseded_only_evidence_is_stored_and_barred_from_promotion() -> None:
    """Kept because it happened; barred because the evidence was overtaken.

    Both halves in one test, because either alone is the wrong behaviour: dropping
    the attempt loses the record that the derivation was ever made, and promoting
    it canonicalizes a conclusion the later evidence may already contradict.
    """
    factory, executed = _recording_session_factory()
    recorded = await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(),
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[_evidence(superseded_for_learning=True), _evidence(superseded_for_learning=True)],
    )
    assert _params_of(executed, "INSERT INTO claim_derivations")["dig"] == recorded.assertion_digest
    assert recorded.superseded_only is True
    assert may_promote(recorded) is False


@pytest.mark.asyncio
async def test_one_input_still_standing_leaves_promotion_open() -> None:
    """Every input, not any input — the bar is supersession of the whole chain."""
    factory, _ = _recording_session_factory()
    recorded = await DerivationService(factory, clock=_FrozenClock()).derive(
        _ctx(),
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[_evidence(superseded_for_learning=True), _evidence(superseded_for_learning=False)],
    )
    assert recorded.superseded_only is False
    assert may_promote(recorded) is True


# --- Test doubles -------------------------------------------------------------


class _FrozenClock:
    def now(self) -> object:  # pragma: no cover - never read by the paths under test
        raise AssertionError("these tests exercise pure decisions and must not need a clock")


def _unused_factory() -> object:
    """A session factory the ceiling checks never call.

    Passed rather than mocked so a test that accidentally reaches the database
    fails loudly instead of quietly exercising a stub.
    """

    def factory() -> object:
        raise AssertionError("this test must not open a session")

    return factory


def _permits(service: DerivationService, claimed: str, ceiling: str) -> bool:
    """Whether the ceiling check admits this pairing, as a value a test can assert on.

    The check signals refusal by raising, so turning that into a boolean is what
    lets the allowed cases make a positive claim instead of relying on silence.
    """
    try:
        service._assert_within_ceiling(claimed, ceiling)
    except DerivationRefused:
        return False
    return True


class _AsyncCM:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"], oidc_subject="extractor")


def _recording_session_factory(
    *, existing: SimpleNamespace | None = None
) -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
    """A session that records every statement and returns `existing` for the lookup.

    Not an `AsyncMock` with a generic return: the replay branch turns on whether the
    lookup found a row, so the test has to own that answer, and every other
    statement must be visible in order rather than merely counted. `COMMIT` is
    recorded in the same list so ordering assertions can see where it fell.
    """
    executed: list[tuple[str, dict[str, Any]]] = []

    async def _execute(statement: Any, params: dict[str, Any] | None = None) -> SimpleNamespace:
        sql = " ".join(str(statement).split())
        executed.append((sql, params or {}))
        if sql.startswith("SELECT"):
            return SimpleNamespace(one_or_none=lambda: existing)
        return SimpleNamespace()

    async def _commit() -> None:
        executed.append(("COMMIT", {}))

    session = MagicMock()
    session.execute = _execute
    session.commit = _commit

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    return factory, executed


def _all_params(executed: list[tuple[str, dict[str, Any]]], prefix: str) -> list[dict[str, Any]]:
    return [params for sql, params in executed if sql.startswith(prefix)]


def _params_of(executed: list[tuple[str, dict[str, Any]]], prefix: str) -> dict[str, Any]:
    """The one statement matching `prefix`, failing loudly if there is not exactly one.

    A test that silently read the first of several inserts would pass while the
    service wrote a duplicate.
    """
    matches = _all_params(executed, prefix)
    assert len(matches) == 1, f"expected exactly one {prefix!r} statement, got {len(matches)}"
    return matches[0]


def _stored_row(*, status: str, evidence_count: int) -> SimpleNamespace:
    """The row the replay lookup returns when this conclusion is already recorded."""
    return SimpleNamespace(
        derivation_id=uuid.uuid4(),
        assertion_digest=assertion_digest(_PROFILE, _assertion()),
        source_authority=AUTHORITY_OBSERVER_EXTRACTION,
        classification="internal",
        status=status,
        evidence_count=evidence_count,
    )


def _recorded(*, superseded_only: bool) -> RecordedDerivation:
    return RecordedDerivation(
        derivation_id=uuid.uuid4(),
        assertion_digest="sha256:abc",
        source_authority=AUTHORITY_OBSERVER_EXTRACTION,
        classification="internal",
        status="pending",
        evidence_count=1,
        superseded_only=superseded_only,
        replayed=False,
    )
