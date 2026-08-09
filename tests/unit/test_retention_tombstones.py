"""Keyed tombstones: proving a removal happened without republishing what was removed.

The proof commits to the erased content so a holder of the key can confirm a
tombstone describes the record it claims to, while a reader without the key learns
nothing — not even whether two tombstones cover identical content. These tests are
mostly about that asymmetry, and about the refusals that keep it true when there is
no key.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.retention import policies, tombstones

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SUBJECT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

_KEY_ID = "k1"
_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


def _resolver(**overrides: object) -> tombstones.KeyedTenantSalt:
    fields: dict[str, object] = {"secrets": {_KEY_ID: _KEY}, "active_key_id": _KEY_ID}
    fields.update(overrides)
    secrets = fields.pop("secrets")
    return tombstones.KeyedTenantSalt(secrets, **fields)  # type: ignore[arg-type]


def test_each_tenant_derives_an_independent_salt_from_one_root_key() -> None:
    """Domain separation is what stops one tenant's markers being checkable against
    another's, and one root key is what makes destruction tractable."""
    resolver = _resolver()

    assert resolver.salt_for(_TENANT) != resolver.salt_for(_OTHER_TENANT)
    # Deterministic, or a re-run of a minimization would produce a different marker.
    assert resolver.salt_for(_TENANT) == resolver.salt_for(_TENANT)
    assert len(resolver.salt_for(_TENANT)) == 32


def test_a_destroyed_tenant_salt_stays_destroyed() -> None:
    """Offboarding has to be irreversible to be worth performing, and the refusal is
    the same exception as having no key at all — a caller that could tell them apart
    would be tempted to treat one as recoverable."""
    resolver = _resolver(destroyed=frozenset({_OTHER_TENANT}))

    assert resolver.salt_for(_TENANT)
    with pytest.raises(tombstones.TenantSaltUnavailable, match="destroyed at offboarding"):
        resolver.salt_for(_OTHER_TENANT)


def test_no_active_key_refuses_rather_than_deriving_an_unkeyed_salt() -> None:
    """An unkeyed salt makes every tenant's markers derivable by anyone, so a removal
    reported under one is a removal whose proof means nothing."""
    with pytest.raises(tombstones.TenantSaltUnavailable, match="no active retention key"):
        tombstones.KeyedTenantSalt({}, active_key_id=None).salt_for(_TENANT)


def test_an_active_key_id_with_no_material_refuses_and_names_the_key() -> None:
    """The misconfiguration worth diagnosing: a key id was configured and its material
    was not, which is indistinguishable from no key unless the message says which."""
    with pytest.raises(tombstones.TenantSaltUnavailable) as refused:
        tombstones.KeyedTenantSalt({}, active_key_id="rotated-out").salt_for(_TENANT)
    assert "rotated-out" in str(refused.value)

    with pytest.raises(tombstones.TenantSaltUnavailable):
        tombstones.KeyedTenantSalt({_KEY_ID: b""}, active_key_id=_KEY_ID).salt_for(_TENANT)


def test_a_proof_commits_to_the_content_without_carrying_it() -> None:
    """Two records differing only in their content digest must produce different
    proofs, and no proof may contain the digest it committed to."""
    salt = _resolver().salt_for(_TENANT)
    common = {"record_class": policies.RECORD_TASK_CHECKPOINT, "subject_id": _SUBJECT, "effective_at": _NOW}

    first = tombstones.mint_proof(salt, content_digest="sha256:aaaa", **common)  # type: ignore[arg-type]
    second = tombstones.mint_proof(salt, content_digest="sha256:bbbb", **common)  # type: ignore[arg-type]

    assert first != second
    assert "aaaa" not in first and "bbbb" not in second
    # Same inputs, same proof: a verifier re-derives it rather than storing a copy.
    assert first == tombstones.mint_proof(salt, content_digest="sha256:aaaa", **common)  # type: ignore[arg-type]


def test_the_proof_varies_with_the_record_class_the_subject_and_the_instant() -> None:
    """Each is part of the message, which is why two tombstones over identical content
    are not comparable: a reader cannot tell that they cover the same thing."""
    salt = _resolver().salt_for(_TENANT)
    base = {
        "record_class": policies.RECORD_CONTEXT_RECEIPT,
        "subject_id": _SUBJECT,
        "content_digest": "sha256:abcd",
        "effective_at": _NOW,
    }
    proof = tombstones.mint_proof(salt, **base)  # type: ignore[arg-type]

    assert proof != tombstones.mint_proof(salt, **{**base, "record_class": policies.RECORD_TASK_CHECKPOINT})  # type: ignore[arg-type]
    assert proof != tombstones.mint_proof(salt, **{**base, "subject_id": uuid.uuid4()})  # type: ignore[arg-type]
    assert proof != tombstones.mint_proof(salt, **{**base, "effective_at": _NOW + datetime.timedelta(seconds=1)})  # type: ignore[arg-type]
    # A different tenant's salt over identical inputs is a different proof too.
    assert proof != tombstones.mint_proof(_resolver().salt_for(_OTHER_TENANT), **base)  # type: ignore[arg-type]


def test_a_minimized_item_key_is_recognisable_and_keeps_none_of_the_original() -> None:
    """Recognisable so a second pass leaves its own output alone, which is what makes
    a retried minimization idempotent instead of a value that changes every run."""
    salt = _resolver().salt_for(_TENANT)
    marker = tombstones.erased_item_key(salt, "capability:billing/notes")

    assert tombstones.is_erased_key(marker)
    assert "billing" not in marker and "notes" not in marker
    assert marker.startswith(tombstones.ERASED_KEY_PREFIX)
    assert tombstones.erased_item_key(salt, "capability:billing/notes") == marker
    assert not tombstones.is_erased_key("capability:billing/notes")


def test_minimizing_an_already_minimized_key_is_detectable_before_it_happens() -> None:
    """The guard the idempotence rests on: keying a marker again would produce a
    different marker, so callers ask first."""
    salt = _resolver().salt_for(_TENANT)
    once = tombstones.erased_item_key(salt, "receipt:item-1")
    twice = tombstones.erased_item_key(salt, once)

    assert tombstones.is_erased_key(once)
    # Proof that the check is load-bearing rather than decorative.
    assert twice != once


def test_two_different_keys_produce_distinguishable_markers() -> None:
    """So a receipt's remaining lines stay distinguishable from each other after
    minimization, rather than collapsing into one repeated value."""
    salt = _resolver().salt_for(_TENANT)
    assert tombstones.erased_item_key(salt, "item-a") != tombstones.erased_item_key(salt, "item-b")


def test_a_disclosure_carries_structure_and_proof_and_the_rule_that_was_applied() -> None:
    """The verifier's whole answer. The policy sentence travels with it so the reader
    sees the rule rather than inferring it from what is missing."""
    salt = _resolver().salt_for(_TENANT)
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

    assert disclosure.proof_hmac == proof
    assert disclosure.erased_at == _NOW
    assert disclosure.verifier_disclosure == policies.disposition(policies.RECORD_CONTEXT_RECEIPT).verifier_disclosure
    # Nothing derived from the content's size or shape, and no identity beyond the id.
    assert set(vars(disclosure)) == {
        "record_class",
        "subject_id",
        "erased_at",
        "policy_version",
        "proof_hmac",
        "verifier_disclosure",
    }


def test_the_proof_is_withheld_once_the_salt_is_gone_and_the_structure_survives() -> None:
    """The proof is still stored and stops being publishable: nothing can re-derive
    it, so offering it would assert a check nobody can perform. Withholding it must
    not also withhold that the record existed and was erased under a named policy."""
    disclosure = tombstones.disclose(
        record_class=policies.RECORD_TASK_CHECKPOINT,
        subject_id=_SUBJECT,
        erased_at=_NOW,
        policy_version=policies.POLICY_VERSION,
        proof_hmac="deadbeef",
        salt_available=False,
    )

    assert disclosure.proof_hmac is None
    assert (disclosure.record_class, disclosure.erased_at) == (policies.RECORD_TASK_CHECKPOINT, _NOW)
    assert disclosure.policy_version == policies.POLICY_VERSION
    assert disclosure.verifier_disclosure
