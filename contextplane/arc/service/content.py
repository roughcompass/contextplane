"""Envelope encryption for ARC's governed content: a per-cell DEK wrapped by
a purpose-bound KEK.

`arc_revisions.source_body_*` and `arc_directives.compact_statement_*` are
wrapped-DEK columns, not directly-encrypted ones. A fresh Data Encryption Key
is generated per cell and used to seal its content; only that 32-byte DEK --
never the content -- is wrapped by a Key Encryption Key this module never
reads in the clear (`ArcContentKeyProvider` holds it). That indirection is
what makes re-keying a column cheap: rotating the KEK re-wraps a DEK, not
however many kilobytes of source body or directive prose it protects. It is
also why the schema carries a `nonce` column alongside `wrapped_dek` rather
than one ciphertext blob: the nonce belongs to the content layer, and the
wrap layer's own nonce travels inside `wrapped_dek`'s bytes, because the
schema gives that layer no column of its own.

**AAD, not payload.** Every seal -- at the content layer and at the DEK-wrap
layer -- binds the envelope profile, which layer it is, the table, the
column, and every part of the row's primary key. An envelope is therefore
inseparable from the exact cell it was written for: lifting
`source_body_ciphertext` (or a rewrapped `wrapped_dek`) out of one row and
pasting it into another, or into a different column of the same row, fails
AEAD verification instead of quietly decrypting under the wrong identity.
`arc_directives`'s primary key is the pair `(directive_id, revision_id)`, so
the same stable directive re-projected onto a different revision is a
different cell, and both halves of that key are bound -- not just one.

**Fail closed.** No plaintext fallback exists here. A missing or
purpose-mismatched key, an envelope profile or algorithm this module does not
recognize, and a failed AEAD tag all raise; none of them return content.

One purpose, one provider class, like every ARC key -- see `signing.py` for
why that separation is structural. `ArcContentKeyProvider` is the same
interface whether it is backed by a single tenant's own key or, eventually, a
deployment-wide one for global content; this module does not distinguish the
two -- it encrypts whatever cell and provider it is given. Turning encrypted
global-scope content on for real, with the custody, recovery, and
rotation-approval story a deployment-wide key demands, is a decision made
elsewhere.
"""

from __future__ import annotations

import dataclasses
import secrets as _secrets_module
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from contextplane.arc.service.signing import KeyPurpose, KeyRecord, KeyUnavailableError, PurposeBoundKeyProvider
from contextplane.exceptions import RegistryError

# Versioned independently of the key purpose. A purpose (`arc_content_v1`,
# declared on `KeyPurpose`) names which keys may encrypt content; a profile
# names how an envelope was built -- the AAD layout, the wrap scheme, the
# algorithm choice. Bumping this is how a format change becomes an explicit,
# stored fact on every new row rather than a silent reinterpretation of old
# rows under new rules.
CONTENT_ENVELOPE_PROFILE = "arc_content_envelope_v1"

CONTENT_ENCRYPTION_ALGORITHM = "AES-256-GCM"

_DEK_BIT_LENGTH = 256
_DEK_BYTES = _DEK_BIT_LENGTH // 8

# AES-GCM's standard nonce length. Never reused under one key -- a repeat
# leaks the XOR of two plaintexts and forfeits integrity for both, which is
# why a fresh nonce is drawn for every content seal and every DEK wrap.
_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16

# `wrapped_dek` has no sibling nonce column of its own (only the content body
# does), so the wrap layer's nonce travels as a fixed-length prefix inside
# the same BYTEA value instead. The total length is therefore fixed and worth
# checking explicitly -- see `_unwrap`.
_WRAPPED_DEK_LENGTH = _NONCE_BYTES + _DEK_BYTES + _GCM_TAG_BYTES

# Domain-separates the AEAD operation an AAD was built for. The content layer
# and the wrap layer run under different keys (the DEK and the KEK), so a
# cross-layer replay is not realistically reachable regardless -- tagging
# each anyway costs nothing and means neither layer's safety depends on
# reasoning about the other.
_CONTENT_LAYER = b"content"
_DEK_WRAP_LAYER = b"dek_wrap"

# The two encrypted cells this profile currently covers, named so a caller
# never hand-writes the table or column string.
TABLE_ARC_REVISIONS = "arc_revisions"
COLUMN_SOURCE_BODY = "source_body"

TABLE_ARC_DIRECTIVES = "arc_directives"
COLUMN_COMPACT_STATEMENT = "compact_statement"


class ContentProtectionError(RegistryError):
    """An envelope did not open: wrong cell, tampered bytes, a truncated
    `wrapped_dek`, or a profile/algorithm this module does not recognize.

    One type for all of them, deliberately -- as with every other ARC AEAD
    boundary, the caller's response is "this content is not readable," not a
    diagnosis of which specific check failed.
    """


@dataclasses.dataclass(frozen=True)
class ContentCell:
    """The fully-qualified location of one encrypted column in one row.

    This -- not the ciphertext, not the key -- is the identity an envelope is
    ultimately bound to. `row_key` carries every column that participates in
    the row's own primary key, in that key's declared order:
    `arc_directives`'s primary key is the pair `(directive_id, revision_id)`,
    and the same stable directive projected onto a different revision is a
    different row, so both parts have to be present or a re-projection could
    replay an envelope that was never written for it.
    """

    table: str
    row_key: tuple[str, ...]
    column: str


def revision_source_body_cell(revision_id: uuid.UUID) -> ContentCell:
    """The cell for `arc_revisions.source_body_*` on one revision."""
    return ContentCell(table=TABLE_ARC_REVISIONS, row_key=(str(revision_id),), column=COLUMN_SOURCE_BODY)


def directive_compact_statement_cell(*, directive_id: uuid.UUID, revision_id: uuid.UUID) -> ContentCell:
    """The cell for `arc_directives.compact_statement_*` on one projection.

    Both halves of `arc_directives`'s composite primary key are required,
    named arguments -- see `ContentCell` for why dropping either one would
    let one projection's envelope be replayed onto another.
    """
    return ContentCell(
        table=TABLE_ARC_DIRECTIVES,
        row_key=(str(directive_id), str(revision_id)),
        column=COLUMN_COMPACT_STATEMENT,
    )


@dataclasses.dataclass(frozen=True)
class ContentEnvelope:
    """Everything needed to decrypt one cell later, and nothing else.

    Self-describing by design: `key_id`, `algorithm`, and `profile` travel
    with the ciphertext, so a stored row states how it was protected instead
    of depending on a deployment default staying fixed forever -- a rotation
    is then a visible fact on the row it touched, not a silent
    reinterpretation of `wrapped_dek` under whatever key happens to be active
    now.

    `nonce` belongs to the content layer. `wrapped_dek` carries the DEK-wrap
    layer's own nonce as an internal prefix, because the schema gives that
    layer no nonce column of its own -- see the module docstring.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_id: str
    algorithm: str
    profile: str


def _length_prefixed(*parts: bytes) -> bytes:
    """Join `parts` so no two different splits of the same bytes can collide.

    Plain concatenation would let `("ab", "c")` and `("a", "bc")` produce
    identical bytes. Prefixing each part with its own 4-byte length makes the
    whole sequence unambiguous regardless of how many parts there are or
    what they contain.
    """
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _cell_aad(cell: ContentCell, *, layer: bytes) -> bytes:
    """The Additional Authenticated Data for one AEAD call on `cell`.

    Binds the envelope profile, which layer this call belongs to (content vs.
    DEK-wrap), the table, the column, and every part of the row's primary
    key. An envelope authenticated under one cell's AAD fails to open under
    any other cell's -- a different row, a different column of the same row,
    or a directive re-projected onto a different revision (a different
    `row_key` under the same table and column) all diverge here.
    """
    return _length_prefixed(
        CONTENT_ENVELOPE_PROFILE.encode("ascii"),
        layer,
        cell.table.encode("utf-8"),
        cell.column.encode("utf-8"),
        *(part.encode("utf-8") for part in cell.row_key),
    )


def _require_length(value: bytes, expected: int, *, field: str) -> None:
    if len(value) != expected:
        msg = f"{field} is {len(value)} bytes, expected {expected}"
        raise ContentProtectionError(msg)


class ArcContentKeyProvider(PurposeBoundKeyProvider):
    """Holds the Key Encryption Keys that wrap per-cell DEKs.

    Bound to `KeyPurpose.CONTENT_ENCRYPTION`, like every ARC key provider is
    bound to exactly one purpose -- see `signing.py`'s module docstring for
    why that separation is structural rather than a naming convention.

    `records` defaults to a healthy `KeyRecord` per configured secret; pass
    it explicitly to represent a key that is retired or compromised. Content
    already wrapped under such a key must stay decryptable (see
    `ArcContentProtectionService`), so `records` is how that state is
    expressed -- removing the key's secret from `secrets` instead would make
    the content it protects permanently unrecoverable.
    """

    purpose = KeyPurpose.CONTENT_ENCRYPTION

    def __init__(
        self,
        secrets: dict[str, bytes],
        *,
        active_key_id: str | None,
        records: dict[str, KeyRecord] | None = None,
    ) -> None:
        self._secrets = secrets
        self._active_key_id = active_key_id
        self._records = (
            records
            if records is not None
            else {
                key_id: KeyRecord(key_id=key_id, purpose=self.purpose, algorithm=CONTENT_ENCRYPTION_ALGORITHM)
                for key_id in secrets
            }
        )

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        """The KEK new envelopes wrap under. Raises when none is configured.

        Fail closed: a deployment or tenant with no active content key must
        not fall back to storing content unencrypted.
        """
        if self._active_key_id is None:
            msg = "no active ARC content-encryption key is configured; refusing to store unencrypted content"
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def get_for_encryption(self, key_id: str) -> KeyRecord:
        """Like `get`, and additionally refuses a retired or compromised key.

        Only for *originating* an envelope -- `protect`, and the re-wrap half
        of `rewrap`. Opening an existing envelope goes through plain `get`
        instead (via `secret`), so a key that is later retired or found
        compromised can still decrypt the content it already wrapped; a
        deployment has to be able to read that content in order to migrate
        it to a new key at all.
        """
        record = self.get(key_id)
        # `usable_for_signing` tests exactly `is_active and not is_compromised`
        # -- the predicate that gates originating new work under a key for any
        # purpose, not only a signature. Reused rather than re-derived here.
        if not record.usable_for_signing:
            state = "compromised" if record.is_compromised else "inactive"
            msg = f"key {key_id!r} is {state} and cannot encrypt new content"
            raise KeyUnavailableError(msg)
        return record

    def secret(self, key_id: str) -> bytes:
        """The raw KEK bytes, after the base class has checked the purpose."""
        self.get(key_id)
        secret = self._secrets.get(key_id)
        if secret is None:
            msg = f"no content-encryption secret for key {key_id!r}"
            raise KeyUnavailableError(msg)
        return secret


class ArcContentProtectionService:
    """Wrapped-DEK envelope encryption over one `ArcContentKeyProvider`.

    A fresh DEK is generated per cell and used to seal its content; only the
    DEK is wrapped by the provider's KEK. Re-keying a column is `rewrap`: it
    unwraps the existing DEK and wraps that same DEK under a new key, leaving
    the content ciphertext and its nonce untouched -- see the module
    docstring for why that is cheap.
    """

    def __init__(self, provider: ArcContentKeyProvider) -> None:
        self._provider = provider

    def protect(self, cell: ContentCell, plaintext: str, *, key_id: str | None = None) -> ContentEnvelope:
        """Encrypt `plaintext` for `cell` under a freshly generated DEK.

        `key_id` defaults to the provider's active key; passing one
        explicitly is for re-keying and tests, not routine use.
        """
        resolved_key_id = key_id or self._provider.active_key_id
        self._provider.get_for_encryption(resolved_key_id)
        kek = self._provider.secret(resolved_key_id)

        dek = AESGCM.generate_key(bit_length=_DEK_BIT_LENGTH)
        content_nonce = _fresh_nonce()
        ciphertext = AESGCM(dek).encrypt(
            content_nonce, plaintext.encode("utf-8"), _cell_aad(cell, layer=_CONTENT_LAYER)
        )
        wrapped_dek = _wrap_dek(dek, kek=kek, cell=cell)

        return ContentEnvelope(
            ciphertext=ciphertext,
            nonce=content_nonce,
            wrapped_dek=wrapped_dek,
            key_id=resolved_key_id,
            algorithm=CONTENT_ENCRYPTION_ALGORITHM,
            profile=CONTENT_ENVELOPE_PROFILE,
        )

    def reveal(self, cell: ContentCell, envelope: ContentEnvelope) -> str:
        """Decrypt `envelope`, refusing anything not written for exactly `cell`."""
        dek = self._unwrap(cell, envelope)
        _require_length(envelope.nonce, _NONCE_BYTES, field="content nonce")
        try:
            plaintext = AESGCM(dek).decrypt(envelope.nonce, envelope.ciphertext, _cell_aad(cell, layer=_CONTENT_LAYER))
        except InvalidTag as exc:
            msg = (
                f"content envelope for {cell.table}.{cell.column} failed to authenticate "
                "-- wrong cell, tampered ciphertext, or wrong key"
            )
            raise ContentProtectionError(msg) from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"content envelope for {cell.table}.{cell.column} decrypted to non-UTF-8 bytes"
            raise ContentProtectionError(msg) from exc

    def rewrap(self, cell: ContentCell, envelope: ContentEnvelope, *, new_key_id: str) -> ContentEnvelope:
        """Re-wrap the same DEK under `new_key_id`; content ciphertext is untouched.

        The old key is read through plain `get`/`secret` (via `_unwrap`), so
        a key that is retired or was found compromised can still be migrated
        away from. The new key goes through `get_for_encryption`: rotating
        onto a key that is itself inactive or compromised would not be a
        rotation worth doing.
        """
        dek = self._unwrap(cell, envelope)
        self._provider.get_for_encryption(new_key_id)
        new_kek = self._provider.secret(new_key_id)
        wrapped_dek = _wrap_dek(dek, kek=new_kek, cell=cell)
        return dataclasses.replace(envelope, wrapped_dek=wrapped_dek, key_id=new_key_id)

    def _unwrap(self, cell: ContentCell, envelope: ContentEnvelope) -> bytes:
        if envelope.profile != CONTENT_ENVELOPE_PROFILE:
            msg = f"unsupported content envelope profile {envelope.profile!r}"
            raise ContentProtectionError(msg)
        if envelope.algorithm != CONTENT_ENCRYPTION_ALGORITHM:
            msg = f"unsupported content envelope algorithm {envelope.algorithm!r}"
            raise ContentProtectionError(msg)
        kek = self._provider.secret(envelope.key_id)
        _require_length(envelope.wrapped_dek, _WRAPPED_DEK_LENGTH, field="wrapped DEK")
        wrap_nonce = envelope.wrapped_dek[:_NONCE_BYTES]
        wrapped = envelope.wrapped_dek[_NONCE_BYTES:]
        try:
            return AESGCM(kek).decrypt(wrap_nonce, wrapped, _cell_aad(cell, layer=_DEK_WRAP_LAYER))
        except InvalidTag as exc:
            msg = f"wrapped DEK for {cell.table}.{cell.column} failed to authenticate -- wrong cell or tampered wrap"
            raise ContentProtectionError(msg) from exc


def _wrap_dek(dek: bytes, *, kek: bytes, cell: ContentCell) -> bytes:
    wrap_nonce = _fresh_nonce()
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, _cell_aad(cell, layer=_DEK_WRAP_LAYER))
    return wrap_nonce + wrapped


def _fresh_nonce() -> bytes:
    return _secrets_module.token_bytes(_NONCE_BYTES)


__all__ = [
    "COLUMN_COMPACT_STATEMENT",
    "COLUMN_SOURCE_BODY",
    "CONTENT_ENCRYPTION_ALGORITHM",
    "CONTENT_ENVELOPE_PROFILE",
    "TABLE_ARC_DIRECTIVES",
    "TABLE_ARC_REVISIONS",
    "ArcContentKeyProvider",
    "ArcContentProtectionService",
    "ContentCell",
    "ContentEnvelope",
    "ContentProtectionError",
    "directive_compact_statement_cell",
    "revision_source_body_cell",
]
