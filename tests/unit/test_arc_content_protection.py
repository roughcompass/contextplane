"""Content envelopes are bound to their exact cell, not merely encrypted."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable

import pytest

from registry.arc.service.content import (
    COLUMN_COMPACT_STATEMENT,
    COLUMN_SOURCE_BODY,
    CONTENT_ENCRYPTION_ALGORITHM,
    CONTENT_ENVELOPE_PROFILE,
    TABLE_ARC_DIRECTIVES,
    TABLE_ARC_REVISIONS,
    ArcContentKeyProvider,
    ArcContentProtectionService,
    ContentCell,
    ContentProtectionError,
    directive_compact_statement_cell,
    revision_source_body_cell,
)
from registry.arc.service.signing import KeyPurpose, KeyPurposeMismatchError, KeyRecord, KeyUnavailableError


def _secret(label: bytes) -> bytes:
    """A distinguishable 32-byte AES-256 key."""
    return (label * 32)[:32]


def _record(key_id: str, **overrides: object) -> KeyRecord:
    defaults: dict[str, object] = {
        "key_id": key_id,
        "purpose": KeyPurpose.CONTENT_ENCRYPTION,
        "algorithm": CONTENT_ENCRYPTION_ALGORITHM,
    }
    defaults.update(overrides)
    return KeyRecord(**defaults)  # type: ignore[arg-type]


def _provider(
    secrets: dict[str, bytes] | None = None,
    *,
    active: str | None = "k1",
    records: dict[str, KeyRecord] | None = None,
) -> ArcContentKeyProvider:
    return ArcContentKeyProvider(
        secrets if secrets is not None else {"k1": _secret(b"1"), "k2": _secret(b"2")},
        active_key_id=active,
        records=records,
    )


def _revision_cell() -> ContentCell:
    return revision_source_body_cell(uuid.uuid4())


def _directive_cell() -> ContentCell:
    return directive_compact_statement_cell(directive_id=uuid.uuid4(), revision_id=uuid.uuid4())


_ANY_CELL: tuple[Callable[[], ContentCell], ...] = (_revision_cell, _directive_cell)


def _flip_last_byte(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 0xFF])


# ---------------------------------------------------------------------------
# Purpose separation
# ---------------------------------------------------------------------------


def test_provider_is_bound_to_the_content_encryption_purpose() -> None:
    assert ArcContentKeyProvider.purpose is KeyPurpose.CONTENT_ENCRYPTION


def test_a_key_recorded_for_a_different_purpose_is_refused() -> None:
    """A key some other purpose's provider recorded must never be usable here
    -- see `signing.py` for why sharing key material across purposes is a
    real vulnerability, not an untidiness."""
    wrong_purpose = _record("k1", purpose=KeyPurpose.CONTINUATION_TOKEN)
    provider = _provider(records={"k1": wrong_purpose})
    with pytest.raises(KeyPurposeMismatchError):
        provider.get("k1")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_cell", _ANY_CELL, ids=["revision", "directive"])
def test_round_trip_recovers_the_original_plaintext(make_cell: Callable[[], ContentCell]) -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = make_cell()
    envelope = service.protect(cell, "governed content body", key_id="k1")
    assert service.reveal(cell, envelope) == "governed content body"


@pytest.mark.parametrize("make_cell", _ANY_CELL, ids=["revision", "directive"])
def test_round_trip_preserves_non_ascii_content(make_cell: Callable[[], ContentCell]) -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = make_cell()
    text = "non-ascii content: café, 日本語, emoji \U0001f512"
    envelope = service.protect(cell, text, key_id="k1")
    assert service.reveal(cell, envelope) == text


@pytest.mark.parametrize("make_cell", _ANY_CELL, ids=["revision", "directive"])
def test_round_trip_handles_empty_content(make_cell: Callable[[], ContentCell]) -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = make_cell()
    envelope = service.protect(cell, "", key_id="k1")
    assert service.reveal(cell, envelope) == ""


def test_envelope_records_key_id_algorithm_and_profile() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    envelope = service.protect(_revision_cell(), "x", key_id="k2")
    assert envelope.key_id == "k2"
    assert envelope.algorithm == CONTENT_ENCRYPTION_ALGORITHM
    assert envelope.profile == CONTENT_ENVELOPE_PROFILE


def test_profile_is_versioned() -> None:
    """A format change must be an explicit version bump, not a silent shift."""
    assert CONTENT_ENVELOPE_PROFILE.endswith("_v1")


def test_repeated_encryption_of_the_same_cell_and_plaintext_yields_different_bytes() -> None:
    """A fresh DEK and fresh nonces every call -- ciphertext must not leak
    which rows hold equal content."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    first = service.protect(cell, "same text", key_id="k1")
    second = service.protect(cell, "same text", key_id="k1")
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.wrapped_dek != second.wrapped_dek


# ---------------------------------------------------------------------------
# AAD binding -- the property this module exists for
# ---------------------------------------------------------------------------


def test_an_envelope_encrypted_for_one_row_fails_to_decrypt_for_another_row() -> None:
    """The single most important property here: an envelope is not portable
    to any other row, even of the same table and column."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    row_a = revision_source_body_cell(uuid.uuid4())
    row_b = revision_source_body_cell(uuid.uuid4())

    envelope = service.protect(row_a, "row A's governed body", key_id="k1")

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(row_b, envelope)

    # And it still opens correctly for the row it was actually written for.
    assert service.reveal(row_a, envelope) == "row A's governed body"


def test_an_envelope_fails_to_decrypt_under_a_different_column_of_the_same_row() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    row_key = (str(uuid.uuid4()),)
    cell = ContentCell(table=TABLE_ARC_REVISIONS, row_key=row_key, column=COLUMN_SOURCE_BODY)
    other_column = ContentCell(table=TABLE_ARC_REVISIONS, row_key=row_key, column="some_other_column")

    envelope = service.protect(cell, "x", key_id="k1")

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(other_column, envelope)


def test_a_directive_envelope_does_not_decrypt_under_a_different_revision_projection() -> None:
    """`arc_directives`'s primary key is `(directive_id, revision_id)`; both
    parts must be bound, or a directive re-projected onto a new revision
    could replay the old projection's envelope."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    directive_id = uuid.uuid4()
    revision_1 = uuid.uuid4()
    revision_2 = uuid.uuid4()
    cell_1 = directive_compact_statement_cell(directive_id=directive_id, revision_id=revision_1)
    cell_2 = directive_compact_statement_cell(directive_id=directive_id, revision_id=revision_2)

    envelope = service.protect(cell_1, "prohibit direct production writes", key_id="k1")

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(cell_2, envelope)


def test_table_and_column_boundaries_cannot_be_shifted_to_collide() -> None:
    """Length-prefixed AAD: a cell named `("ab", "c")` must not authenticate
    like one named `("a", "bc")`. Plain concatenation of table and column
    would make those two indistinguishable."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    row_key = (str(uuid.uuid4()),)
    cell_a = ContentCell(table="ab", row_key=row_key, column="c")
    cell_b = ContentCell(table="a", row_key=row_key, column="bc")

    envelope = service.protect(cell_a, "x", key_id="k1")

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(cell_b, envelope)


def test_row_key_boundaries_cannot_be_shifted_to_collide() -> None:
    """Same collision shape as the table/column test, for the row key parts."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell_a = ContentCell(table=TABLE_ARC_DIRECTIVES, row_key=("ab", "c"), column=COLUMN_COMPACT_STATEMENT)
    cell_b = ContentCell(table=TABLE_ARC_DIRECTIVES, row_key=("a", "bc"), column=COLUMN_COMPACT_STATEMENT)

    envelope = service.protect(cell_a, "x", key_id="k1")

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(cell_b, envelope)


# ---------------------------------------------------------------------------
# AEAD integrity and malformed envelopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_cell", _ANY_CELL, ids=["revision", "directive"])
def test_tampered_ciphertext_fails_to_decrypt(make_cell: Callable[[], ContentCell]) -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = make_cell()
    envelope = service.protect(cell, "governed content", key_id="k1")
    tampered = dataclasses.replace(envelope, ciphertext=_flip_last_byte(envelope.ciphertext))

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(cell, tampered)


@pytest.mark.parametrize("make_cell", _ANY_CELL, ids=["revision", "directive"])
def test_tampered_wrapped_dek_fails_to_decrypt(make_cell: Callable[[], ContentCell]) -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = make_cell()
    envelope = service.protect(cell, "governed content", key_id="k1")
    tampered = dataclasses.replace(envelope, wrapped_dek=_flip_last_byte(envelope.wrapped_dek))

    with pytest.raises(ContentProtectionError, match="authenticate"):
        service.reveal(cell, tampered)


def test_truncated_wrapped_dek_fails_closed() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")
    truncated = dataclasses.replace(envelope, wrapped_dek=envelope.wrapped_dek[:10])

    with pytest.raises(ContentProtectionError, match="wrapped DEK"):
        service.reveal(cell, truncated)


def test_truncated_content_nonce_fails_closed() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")
    truncated = dataclasses.replace(envelope, nonce=envelope.nonce[:5])

    with pytest.raises(ContentProtectionError, match="content nonce"):
        service.reveal(cell, truncated)


def test_reveal_refuses_an_envelope_with_an_unrecognized_profile() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")
    bad = dataclasses.replace(envelope, profile="arc_content_envelope_v2")

    with pytest.raises(ContentProtectionError, match="profile"):
        service.reveal(cell, bad)


def test_reveal_refuses_an_envelope_with_an_unrecognized_algorithm() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")
    bad = dataclasses.replace(envelope, algorithm="ChaCha20-Poly1305")

    with pytest.raises(ContentProtectionError, match="algorithm"):
        service.reveal(cell, bad)


# ---------------------------------------------------------------------------
# Fail closed on a missing or unhealthy provider
# ---------------------------------------------------------------------------


def test_no_active_key_refuses_to_protect() -> None:
    provider = _provider(active=None)
    service = ArcContentProtectionService(provider)
    with pytest.raises(KeyUnavailableError, match="no active"):
        service.protect(_revision_cell(), "governed body")


def test_protect_refuses_an_unknown_key_id() -> None:
    provider = _provider()
    service = ArcContentProtectionService(provider)
    with pytest.raises(KeyUnavailableError):
        service.protect(_revision_cell(), "x", key_id="does-not-exist")


def test_protect_refuses_a_compromised_key() -> None:
    provider = _provider(records={"k1": _record("k1", is_compromised=True)})
    service = ArcContentProtectionService(provider)
    with pytest.raises(KeyUnavailableError, match="compromised"):
        service.protect(_revision_cell(), "x", key_id="k1")


def test_protect_refuses_an_inactive_key() -> None:
    provider = _provider(records={"k1": _record("k1", is_active=False)})
    service = ArcContentProtectionService(provider)
    with pytest.raises(KeyUnavailableError, match="inactive"):
        service.protect(_revision_cell(), "x", key_id="k1")


def test_protect_defaults_to_the_providers_active_key() -> None:
    provider = _provider(active="k2")
    service = ArcContentProtectionService(provider)
    envelope = service.protect(_revision_cell(), "x")
    assert envelope.key_id == "k2"


def test_reveal_fails_closed_when_the_wrapping_key_is_unavailable() -> None:
    """A key this deployment does not hold at all -- the sharpest form of a
    missing provider -- must raise, never fall back to returning the
    ciphertext or a guessed plaintext."""
    provider = _provider()
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")

    keyless_service = ArcContentProtectionService(ArcContentKeyProvider({}, active_key_id=None))
    with pytest.raises(KeyUnavailableError):
        keyless_service.reveal(cell, envelope)


def test_compromised_key_can_still_reveal_previously_protected_content() -> None:
    """Verification-style asymmetry: a compromised key never originates new
    work again, but content it already wrapped must stay readable, or a
    deployment could never migrate that content to a new key."""
    cell = _revision_cell()
    envelope = ArcContentProtectionService(_provider()).protect(cell, "secret body", key_id="k1")

    now_compromised = _provider(records={"k1": _record("k1", is_compromised=True), "k2": _record("k2")})
    assert ArcContentProtectionService(now_compromised).reveal(cell, envelope) == "secret body"


# ---------------------------------------------------------------------------
# Re-keying
# ---------------------------------------------------------------------------


def test_rewrap_changes_the_key_but_not_the_content_ciphertext() -> None:
    """Re-keying a column means re-wrapping its DEK, not re-encrypting the
    content the DEK protects."""
    provider = _provider(active="k1")
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    original = service.protect(cell, "governed body text")

    rewrapped = service.rewrap(cell, original, new_key_id="k2")

    assert rewrapped.ciphertext == original.ciphertext
    assert rewrapped.nonce == original.nonce
    assert rewrapped.wrapped_dek != original.wrapped_dek
    assert rewrapped.key_id == "k2"
    assert service.reveal(cell, rewrapped) == "governed body text"


def test_rewrap_refuses_an_inactive_new_key() -> None:
    provider = _provider(records={"k1": _record("k1"), "k2": _record("k2", is_active=False)})
    service = ArcContentProtectionService(provider)
    cell = _revision_cell()
    envelope = service.protect(cell, "x", key_id="k1")

    with pytest.raises(KeyUnavailableError, match="inactive"):
        service.rewrap(cell, envelope, new_key_id="k2")


def test_rewrap_migrates_content_away_from_a_newly_compromised_key() -> None:
    """Rotation in response to a compromise: the DEK was wrapped under k1
    while it was healthy; k1 is now compromised; `rewrap` must still open it
    (through plain `get`, not `get_for_encryption`) in order to move it."""
    cell = _revision_cell()
    envelope = ArcContentProtectionService(_provider()).protect(cell, "x", key_id="k1")

    now_compromised = _provider(records={"k1": _record("k1", is_compromised=True), "k2": _record("k2")})
    migrated_service = ArcContentProtectionService(now_compromised)
    migrated = migrated_service.rewrap(cell, envelope, new_key_id="k2")

    assert migrated.key_id == "k2"
    assert migrated_service.reveal(cell, migrated) == "x"


# ---------------------------------------------------------------------------
# Cell constructors
# ---------------------------------------------------------------------------


def test_revision_source_body_cell_binds_the_revision_id() -> None:
    revision_id = uuid.uuid4()
    cell = revision_source_body_cell(revision_id)
    assert cell.table == TABLE_ARC_REVISIONS
    assert cell.column == COLUMN_SOURCE_BODY
    assert cell.row_key == (str(revision_id),)


def test_directive_compact_statement_cell_binds_both_halves_of_the_composite_key() -> None:
    directive_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    cell = directive_compact_statement_cell(directive_id=directive_id, revision_id=revision_id)
    assert cell.table == TABLE_ARC_DIRECTIVES
    assert cell.column == COLUMN_COMPACT_STATEMENT
    assert cell.row_key == (str(directive_id), str(revision_id))
