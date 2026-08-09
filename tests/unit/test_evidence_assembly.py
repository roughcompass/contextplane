"""What the assembler selects, refuses, and hands to the claim writer.

The integration suite proves the predicates against a real database. These prove
the parts that are decisions rather than queries — which chains are checkable,
how a chain becomes provenance, and what ceiling it licenses — and they prove the
predicates' *shape*, which is the half a live query cannot check: a `WHERE` clause
that forgot the tenant still returns rows, and the rows it returns look fine.

Every test here asserts a behaviour. None exists to move a coverage number: a
test with no claim would leave the module exactly as unverified as no test at all,
while making the gate say otherwise.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_UNATTRIBUTED,
)
from contextplane.service.memory.derivation import Evidence
from contextplane.service.memory.evidence import (
    KIND_DIAGNOSTIC,
    PROVENANCE_KINDS,
    EligibleFeedback,
    EvidenceAssembler,
    EvidenceRefused,
    as_provenance,
    ceiling_for,
    validate_chain,
)
from contextplane.types import TenantContext

_TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _ctx(tenant_id: uuid.UUID = _TENANT) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


def _signal(authority: str = AUTHORITY_OBSERVER_EXTRACTION, **overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "kind": "signal",
        "source_authority": authority,
        "classification": "internal",
        "signal_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return Evidence(**fields)


# --- Test doubles -------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _RecordingSession:
    """Records the statement text and bound parameters of every read.

    The assembler's correctness is mostly in its `WHERE` clause, and a live query
    cannot tell a missing condition from one that happened not to matter for the
    rows present. Reading the statement is what checks the condition is there.
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        self.statements.append(str(statement))
        self.params.append(dict(params or {}))
        return _Result(self.rows)

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _factory(session: _RecordingSession) -> Any:
    def make() -> _RecordingSession:
        return session

    return make


def _row(**overrides: Any) -> Any:
    from types import SimpleNamespace

    fields: dict[str, Any] = {
        "feedback_id": uuid.uuid4(),
        "kind": "item_specific",
        "rating": "stale",
        "receipt_id": uuid.uuid4(),
        "receipt_item_id": "item-1",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --- The predicates carry their conditions ------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("tenant_id = :tid", id="tenant-scoped"),
        pytest.param("learning_eligible", id="eligible-only"),
        pytest.param("kind <> :diagnostic", id="no-diagnostics"),
    ],
)
def test_the_feedback_read_carries_every_eligibility_condition(condition: str) -> None:
    """Each condition asserted by name, so dropping one fails here rather than in production.

    A live query with a missing condition returns rows that look entirely
    reasonable; only the statement says whether the condition was ever there.
    """
    session = _RecordingSession([])
    import asyncio

    asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(_ctx()))
    assert condition in session.statements[0]


def test_the_feedback_read_binds_the_callers_own_tenant() -> None:
    """The tenant comes from the context, never from an argument a caller could set."""
    session = _RecordingSession([])
    import asyncio

    ctx = _ctx()
    asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(ctx))
    assert session.params[0]["tid"] == ctx.tenant_id
    assert session.params[0]["diagnostic"] == KIND_DIAGNOSTIC


def test_filtering_by_receipt_adds_the_condition_rather_than_a_second_pass() -> None:
    """The optional filter is part of the query, not a list comprehension afterwards."""
    session = _RecordingSession([])
    import asyncio

    receipt_id = uuid.uuid4()
    asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(_ctx(), receipt_id=receipt_id))
    assert "receipt_id = :rid" in session.statements[0]
    assert session.params[0]["rid"] == receipt_id


def test_the_unfiltered_read_does_not_mention_a_receipt() -> None:
    """Two whole statements, so neither carries the other's parameters."""
    session = _RecordingSession([])
    import asyncio

    asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(_ctx()))
    assert "receipt_id = :rid" not in session.statements[0]
    assert "rid" not in session.params[0]


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("tenant_id = :tid", id="tenant-scoped"),
        pytest.param("revoked_at IS NULL", id="not-revoked"),
        pytest.param("NOT superseded_for_learning", id="not-superseded"),
    ],
)
def test_the_signal_read_excludes_withdrawn_and_overtaken_evidence(condition: str) -> None:
    """Revocation and supersession are separate conditions because they mean different things.

    A revoked signal was withdrawn; a superseded one is still true and merely
    overtaken. One flag covering both would lose which happened.
    """
    session = _RecordingSession([])
    import asyncio

    asyncio.run(EvidenceAssembler(_factory(session)).eligible_signals(_ctx()))
    assert condition in session.statements[0]


def test_selected_feedback_is_returned_as_typed_rows() -> None:
    """The caller gets a shape it can read, not a raw row it has to know the columns of."""
    row = _row()
    session = _RecordingSession([row])
    import asyncio

    selected = asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(_ctx()))
    assert len(selected) == 1
    assert isinstance(selected[0], EligibleFeedback)
    assert selected[0].feedback_id == row.feedback_id
    assert selected[0].receipt_item_id == "item-1"


def test_a_read_that_finds_nothing_returns_an_empty_tuple() -> None:
    """No eligible evidence is an ordinary answer, not an error."""
    session = _RecordingSession([])
    import asyncio

    assert asyncio.run(EvidenceAssembler(_factory(session)).eligible_feedback(_ctx())) == ()


# --- Chain validation ---------------------------------------------------------


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(EvidenceRefused, match="nothing in it"):
        validate_chain([])


def test_a_chain_of_valid_evidence_is_accepted() -> None:
    """The validator must not refuse everything, which a too-eager check would."""
    assert _accepts([_signal(), _signal(AUTHORITY_OWNER_HUMAN)]) is True


def test_every_provenance_kind_validates() -> None:
    """The set the validator accepts is the set provenance can spell.

    A kind accepted here but unspellable in `_ref_for` would produce a ref of the
    wrong shape, which is provenance nobody can resolve.
    """
    assert PROVENANCE_KINDS == {"signal", "receipt", "receipt_item", "external_reference", "checkpoint"}


# --- Provenance ---------------------------------------------------------------


def test_a_signal_becomes_a_resolvable_ref() -> None:
    signal_id = uuid.uuid4()
    provenance = as_provenance([_signal(signal_id=signal_id)])
    assert provenance[0].ref == f"signal:{signal_id}"


def test_a_receipt_item_is_spelled_as_the_pair() -> None:
    """The item id means nothing without the receipt it sits on."""
    receipt_id = uuid.uuid4()
    provenance = as_provenance(
        [
            Evidence(
                kind="receipt_item",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                receipt_id=receipt_id,
                receipt_item_id="item-9",
            )
        ]
    )
    assert provenance[0].ref == f"receipt_item:{receipt_id}:item-9"


def test_a_checkpoint_ref_carries_the_digest_it_was_read_at() -> None:
    """Without the digest the ref names a checkpoint that may since have changed."""
    checkpoint_id = uuid.uuid4()
    digest = "sha256:" + "c" * 64
    provenance = as_provenance(
        [
            Evidence(
                kind="checkpoint",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                checkpoint_id=checkpoint_id,
                checkpoint_digest=digest,
            )
        ]
    )
    assert provenance[0].ref == f"checkpoint:{checkpoint_id}@{digest}"


def test_an_external_reference_becomes_a_ref() -> None:
    reference_id = uuid.uuid4()
    provenance = as_provenance(
        [
            Evidence(
                kind="external_reference",
                source_authority=AUTHORITY_OBSERVER_HUMAN,
                classification="internal",
                reference_id=reference_id,
            )
        ]
    )
    assert provenance[0].ref == f"external_reference:{reference_id}"


def test_a_receipt_becomes_a_ref() -> None:
    receipt_id = uuid.uuid4()
    provenance = as_provenance(
        [
            Evidence(
                kind="receipt",
                source_authority=AUTHORITY_OBSERVER_HUMAN,
                classification="internal",
                receipt_id=receipt_id,
            )
        ]
    )
    assert provenance[0].ref == f"receipt:{receipt_id}"


def test_provenance_preserves_every_item_in_order() -> None:
    """A chain is evidence as a whole; dropping or reordering one item changes what it shows."""
    chain = [_signal(), _signal(AUTHORITY_OWNER_HUMAN), _signal(AUTHORITY_OBSERVER_HUMAN)]
    provenance = as_provenance(chain)
    assert len(provenance) == 3
    assert [p.ref for p in provenance] == [f"signal:{item.signal_id}" for item in chain]


def test_converting_an_empty_chain_is_refused() -> None:
    """The conversion validates first, so an unusable chain cannot become provenance."""
    with pytest.raises(EvidenceRefused):
        as_provenance([])


# --- The ceiling --------------------------------------------------------------


def test_the_ceiling_is_the_weakest_link() -> None:
    assert ceiling_for([_signal(AUTHORITY_OWNER_HUMAN), _signal(AUTHORITY_OBSERVER_EXTRACTION)]) == (
        AUTHORITY_OBSERVER_EXTRACTION
    )


def test_the_ceiling_of_a_single_item_is_that_item() -> None:
    assert ceiling_for([_signal(AUTHORITY_OWNER_HUMAN)]) == AUTHORITY_OWNER_HUMAN


def test_an_empty_chain_licenses_nothing() -> None:
    assert ceiling_for([]) == AUTHORITY_UNATTRIBUTED


def _accepts(chain: list[Evidence]) -> bool:
    """Whether the validator admits this chain, as a value a test can assert on.

    The validator signals refusal by raising, so turning that into a boolean is
    what lets the accepting cases make a positive claim rather than relying on
    the absence of an exception.
    """
    try:
        validate_chain(chain)
    except EvidenceRefused:
        return False
    return True


def _bypassing_construction(item: Evidence, **overrides: Any) -> Evidence:
    """An `Evidence` with a field set past its own validation.

    `Evidence.__post_init__` refuses a bad kind or authority, so these states are
    unreachable through the constructor — which is exactly why `validate_chain`
    re-checks them. A chain can be built by a future path that does not go through
    `__init__`, and a validator that assumed otherwise would pass it.
    """
    clone = Evidence(**{**vars(item)})
    for name, value in overrides.items():
        object.__setattr__(clone, name, value)
    return clone


def test_the_validator_does_not_trust_the_item_constructor_to_have_run() -> None:
    """An authority off the ladder is refused even when it arrived past `__post_init__`."""
    smuggled = _bypassing_construction(_signal(), source_authority="supreme_authority")
    with pytest.raises(EvidenceRefused, match="outside the ladder"):
        validate_chain([smuggled])


def test_a_kind_with_no_provenance_spelling_is_refused_by_the_chain_validator() -> None:
    """A kind the ref builder cannot spell would produce provenance nobody can resolve."""
    smuggled = _bypassing_construction(_signal(), kind="rumour")
    with pytest.raises(EvidenceRefused, match="no provenance spelling"):
        validate_chain([smuggled])
