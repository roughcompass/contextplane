"""What survives an erasure, and what a verifier may still say about it.

An erasure that deletes source rows and stops there is the failure this whole
subsystem exists to prevent: a vector, a full-text document, a cached answer, an
export and a summary each hold the erased person's words verbatim, in a place the
record's own table knows nothing about. Deleting the record leaves the copies
searchable, and the person is told their data is gone.

These are contract gates, not behaviour tests. Each one pins a promise that is
cheap to break silently later:

- **Coverage is a registry, so it can be checked.** The refusals that keep the
  registry meaningful live here — an unknown kind, and a second handler for one
  kind. That every kind the schema stores is actually covered is pinned against
  the composition the deployment ships, in the registration conformance module
  beside this one, because a handler that exists and is never registered covers
  nothing.
- **A proof may commit to content; a disclosure may never reveal it.** What a
  verifier gets after a tombstone is structure plus a keyed proof, and only while
  the key exists.
- **Refusing beats improvising.** With no key material and no hold storage, the
  honest answers are a loud refusal and a truthful "nothing is held" — not an
  unkeyed salt or a silently dropped hold.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.config import Settings
from contextplane.retention import derivatives, holds, policies, tombstones

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SUBJECT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: A key id and material a test can configure, so the "configured" direction is
#: exercised as well as the refusal. Hex because that is the wire format
#: `RETENTION_KEYS` accepts.
_KEY_ID = "k1"
_KEY_HEX = "00112233445566778899aabbccddeeff"


# --- Coverage is a registry, so it can be checked -----------------------------


def test_an_empty_registry_reports_every_kind_as_unhandled() -> None:
    """Reporting the gap is what makes the gate above a one-line assertion later.

    A registry that answered "nothing unhandled" when it held nothing would read as
    full coverage, which is the failure mode a coverage gate is least able to detect
    — it would agree.
    """
    assert derivatives.HandlerRegistry().unhandled_kinds() == derivatives.DERIVATIVE_KINDS
    assert derivatives.HandlerRegistry().kinds == ()


def test_a_registry_refuses_an_unknown_kind_and_a_second_handler_for_one() -> None:
    """Both refusals matter and for the same reason: which handler runs would
    otherwise depend on import order, and a kind the schema never stores would
    read as covered."""

    class _Handler:
        kind = derivatives.KIND_VECTOR
        version = "v1"

    class _Unknown:
        kind = "not_a_kind_the_schema_stores"
        version = "v1"

    registry = derivatives.HandlerRegistry()
    registry.register(_Handler())  # type: ignore[arg-type]

    with pytest.raises(derivatives.UnhandledDerivativeKind, match="already has a handler"):
        registry.register(_Handler())  # type: ignore[arg-type]
    with pytest.raises(derivatives.UnhandledDerivativeKind, match="not one the schema stores"):
        registry.register(_Unknown())  # type: ignore[arg-type]
    with pytest.raises(derivatives.UnhandledDerivativeKind, match="no propagation handler is registered"):
        registry.handler_for(derivatives.KIND_EXPORT)


def test_revocation_expiry_and_erasure_are_each_their_own_trigger() -> None:
    """Three different reasons content must go, and the row records which one.
    Collapsing them would make a revoked source indistinguishable from an expired
    record, and only one of those is reversible by policy change."""
    assert derivatives.TRIGGER_REVOCATION in derivatives.TRIGGERS
    assert derivatives.TRIGGER_EXPIRY in derivatives.TRIGGERS
    assert derivatives.TRIGGER_ERASURE in derivatives.TRIGGERS
    assert derivatives.TRIGGER_POLICY_CHANGE in derivatives.TRIGGERS
    # Closed sets: a trigger or operation the schema does not store would be
    # written and never matched.
    assert len(set(derivatives.TRIGGERS)) == len(derivatives.TRIGGERS)
    assert set(derivatives.OPERATIONS) == {
        derivatives.OPERATION_REBUILD,
        derivatives.OPERATION_DELETE,
        derivatives.OPERATION_REDACT,
    }


# --- What the policy says, per record class ------------------------------------


def test_every_record_an_actor_authors_has_an_approved_disposition() -> None:
    """The erasure walks these five classes by name. One without a disposition
    would raise part-way through an erasure that had already deleted rows."""
    from contextplane.context import derivatives as context_derivatives

    for record_class in context_derivatives.ACTOR_RECORD_CLASSES:
        disposition = policies.disposition(record_class)
        assert disposition.record_class == record_class
        assert disposition.legal_basis, f"{record_class} records no legal basis for keeping it"
        assert disposition.verifier_disclosure, f"{record_class} says nothing about what a verifier may see"


def test_an_unknown_record_class_is_refused_rather_than_defaulted() -> None:
    """A default disposition is how a new table acquires a retention policy nobody
    approved — most likely the most permissive one."""
    with pytest.raises(policies.UnknownRecordClass):
        policies.disposition("a_table_nobody_wrote_a_policy_for")


def test_every_class_either_expires_on_a_clock_or_is_bounded_by_deletion() -> None:
    """ "No computable expiry" is a legitimate answer only when the bound is tenant
    or workspace deletion. A class with neither would be kept forever by
    omission."""
    for record_class in policies.RECORD_CLASSES:
        disposition = policies.disposition(record_class)
        if disposition.retention_days is None:
            assert (
                policies.expiry_deadline(record_class, _NOW) is None
            ), f"{record_class} has no retention period but computes a deadline anyway"
            continue
        deadline = policies.expiry_deadline(record_class, _NOW)
        assert deadline is not None and deadline > _NOW, f"{record_class} expires at or before its own anchor"


def test_the_payload_clock_is_never_later_than_the_record_clock() -> None:
    """Content reduces before the record goes, or the two clocks are one. The other
    order would minimize a record that had already been deleted."""
    for record_class in policies.RECORD_CLASSES:
        payload = policies.payload_deadline(record_class, _NOW)
        record = policies.expiry_deadline(record_class, _NOW)
        if payload is None or record is None:
            continue
        assert payload <= record, f"{record_class} reduces its content after the record itself expires"


def test_a_receipt_is_redacted_rather_than_deleted() -> None:
    """The proof a request was served has to outlive the content it cited.
    Deleting the receipt destroys the audit trail; keeping it verbatim keeps the
    erased words. Minimization is the only answer that is both."""
    receipt = policies.disposition(policies.RECORD_CONTEXT_RECEIPT)
    assert receipt.erasure_mode in {policies.MODE_MINIMIZE, policies.MODE_MINIMIZE_AND_TOMBSTONE}
    assert receipt.minimization_action, "a minimized receipt records no statement of what was reduced"

    item = policies.disposition(policies.RECORD_RECEIPT_ITEM)
    assert item.erasure_mode in {policies.MODE_MINIMIZE, policies.MODE_MINIMIZE_AND_TOMBSTONE}


def test_a_tombstoned_class_discloses_structure_only_and_an_exempt_one_says_why() -> None:
    """The disclosure sentence is what a verifier is permitted to publish, so a
    class that writes a tombstone must have one and it must not offer content."""
    for record_class in policies.RECORD_CLASSES:
        disposition = policies.disposition(record_class)
        if disposition.writes_tombstone:
            assert (
                "Never the erased content" in disposition.verifier_disclosure
            ), f"{record_class} writes a tombstone without ruling out disclosing the content"
        if disposition.is_exempt:
            assert not disposition.writes_tombstone, f"{record_class} is exempt from erasure yet tombstones it"


# --- What a verifier may say afterwards ----------------------------------------


def test_a_disclosure_carries_structure_and_proof_and_nothing_else() -> None:
    """Pinned field by field. Any field added here is a channel until somebody
    argues it is not: a content length reveals whether a note was a word or a
    paragraph, and a subject identity beyond the derived id re-identifies the
    person the erasure was performed for."""
    salt = tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID).salt_for(_TENANT)
    proof = tombstones.mint_proof(
        salt,
        record_class=policies.RECORD_CONTEXT_RECEIPT,
        subject_id=_SUBJECT,
        content_digest="sha256:abcd",
        effective_at=_NOW,
    )
    disclosure = tombstones.disclose(
        record_class=policies.RECORD_CONTEXT_RECEIPT,
        subject_id=_SUBJECT,
        erased_at=_NOW,
        policy_version=policies.POLICY_VERSION,
        proof_hmac=proof,
        salt_available=True,
    )

    assert set(vars(disclosure)) == {
        "record_class",
        "subject_id",
        "erased_at",
        "policy_version",
        "proof_hmac",
        "verifier_disclosure",
    }
    assert disclosure.proof_hmac == proof
    assert disclosure.verifier_disclosure == policies.disposition(policies.RECORD_CONTEXT_RECEIPT).verifier_disclosure


def test_the_proof_is_withheld_once_the_salt_is_gone() -> None:
    """The proof stays stored and stops being publishable: nothing can re-derive
    it, so offering it would assert a check no one can perform."""
    disclosure = tombstones.disclose(
        record_class=policies.RECORD_CONTEXT_RECEIPT,
        subject_id=_SUBJECT,
        erased_at=_NOW,
        policy_version=policies.POLICY_VERSION,
        proof_hmac="deadbeef",
        salt_available=False,
    )
    assert disclosure.proof_hmac is None
    # Structure survives: withholding the proof must not also withhold the fact
    # that the record existed and was erased under a named policy.
    assert disclosure.erased_at == _NOW
    assert disclosure.policy_version == policies.POLICY_VERSION


def test_a_proof_commits_to_the_content_without_carrying_it() -> None:
    """Two records differing only in content digest must produce different proofs,
    and neither proof may contain the digest it committed to."""
    salt = b"\x01" * 32
    common = {
        "record_class": policies.RECORD_TASK_CHECKPOINT,
        "subject_id": _SUBJECT,
        "effective_at": _NOW,
    }
    first = tombstones.mint_proof(salt, content_digest="sha256:aaaa", **common)  # type: ignore[arg-type]
    second = tombstones.mint_proof(salt, content_digest="sha256:bbbb", **common)  # type: ignore[arg-type]

    assert first != second
    assert "aaaa" not in first and "bbbb" not in second


def test_a_minimized_item_key_is_recognisable_and_keeps_none_of_the_original() -> None:
    """Recognisable so a second minimization pass leaves its own output alone,
    which is what makes a retried erasure idempotent rather than a value that
    changes on every run."""
    salt = b"\x02" * 32
    marker = tombstones.erased_item_key(salt, "capability:billing/notes")

    assert tombstones.is_erased_key(marker)
    assert "billing" not in marker and "notes" not in marker
    assert tombstones.erased_item_key(salt, "capability:billing/notes") == marker
    assert not tombstones.is_erased_key("capability:billing/notes")


# --- Refusing beats improvising ------------------------------------------------


def test_no_configured_key_refuses_instead_of_deriving_an_unkeyed_salt() -> None:
    """An unkeyed salt would make every tenant's markers derivable by anyone, and
    an erasure would report a removal whose proof means nothing."""
    resolver = tombstones.KeyedTenantSalt({}, active_key_id=None)
    with pytest.raises(tombstones.TenantSaltUnavailable, match="no active retention key"):
        resolver.salt_for(_TENANT)

    named_but_absent = tombstones.KeyedTenantSalt({}, active_key_id=_KEY_ID)
    with pytest.raises(tombstones.TenantSaltUnavailable, match="holds no material"):
        named_but_absent.salt_for(_TENANT)


def test_each_tenant_gets_an_independent_salt_and_a_destroyed_one_stays_gone() -> None:
    """Domain separation is what stops one tenant's markers being checkable against
    another's, and offboarding has to be irreversible to be worth performing."""
    resolver = tombstones.KeyedTenantSalt(
        {_KEY_ID: bytes.fromhex(_KEY_HEX)},
        active_key_id=_KEY_ID,
        destroyed=frozenset({_OTHER_TENANT}),
    )
    assert resolver.salt_for(_TENANT) != resolver.salt_for(
        uuid.UUID("44444444-4444-4444-4444-444444444444"),
    )
    with pytest.raises(tombstones.TenantSaltUnavailable, match="destroyed at offboarding"):
        resolver.salt_for(_OTHER_TENANT)


def test_the_shipped_deployment_configures_no_key_and_says_so() -> None:
    """The default is a refusal, not a placeholder value. A settings default that
    produced a usable key would be a shared secret published in the repository."""
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
    )
    assert settings.retention_active_key_id is None
    assert settings.retention_key_material() == {}


def test_configured_key_material_parses_and_a_malformed_entry_is_refused() -> None:
    """Refused rather than skipped: a skipped key is indistinguishable from one
    never configured, so the deployment that mistyped its active key would get the
    unkeyed-salt refusal and no hint that its material was the problem."""
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
        retention_keys=f"{_KEY_ID}:{_KEY_HEX}, k2:ffee",
        retention_active_key_id=_KEY_ID,
    )
    assert settings.retention_key_material() == {_KEY_ID: bytes.fromhex(_KEY_HEX), "k2": b"\xff\xee"}

    for malformed in ("k1", "k1:", ":ffee", "k1:nothex"):
        broken = Settings(  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://user:pass@localhost:9999/db",
            retention_keys=malformed,
        )
        with pytest.raises(ValueError, match="RETENTION_KEYS"):
            broken.retention_key_material()


async def test_with_no_hold_storage_nothing_is_held_and_no_hold_can_be_placed() -> None:
    """Two different behaviours on purpose. Reads answer truthfully — with nowhere
    to record a hold, none exists — while writes refuse loudly, because a hold
    that silently did not persist is a deletion somebody believes is paused."""
    store = holds.NoHoldStorage()

    assert await store.active_holds(_TENANT, policies.RECORD_CONTEXT_RECEIPT, [_SUBJECT], now=_NOW) == {}
    assert await store.held_overdue(_TENANT, now=_NOW) == ()

    with pytest.raises(holds.HoldStorageUnavailable, match="cannot be placed"):
        await store.place(_TENANT, policies.RECORD_CONTEXT_RECEIPT, _SUBJECT, placed_by="ops", reason="litigation")
    with pytest.raises(holds.HoldStorageUnavailable, match="re-justification"):
        await store.renew(uuid.uuid4(), justification="still needed", approved_by="ops")


async def test_expiry_consults_the_hold_seam_and_reports_what_it_paused() -> None:
    """Held records come back with their holds rather than being dropped: a sweep
    that excluded them silently would make the paused clock invisible, and a
    suspended deletion has to be attributable to something."""
    deletable, held = await holds.partition_by_hold(
        holds.NoHoldStorage(),
        _TENANT,
        policies.RECORD_CONTEXT_RECEIPT,
        [_SUBJECT],
        now=_NOW,
    )
    assert deletable == (_SUBJECT,)
    assert held == {}

    empty_deletable, empty_held = await holds.partition_by_hold(
        holds.NoHoldStorage(), _TENANT, policies.RECORD_CONTEXT_RECEIPT, [], now=_NOW
    )
    assert empty_deletable == () and empty_held == {}
