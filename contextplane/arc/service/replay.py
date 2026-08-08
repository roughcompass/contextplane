"""Response-replay sealing: the AEAD boundary an exact retry opens.

`ResolutionService` answers an exact retry of a resolution from the receipt
a prior attestation already produced, instead of re-running selection (see
`ResolutionService._replay`). What it would hand back is
`arc_receipts.response_replay_*`: an envelope this module seals when the
receipt is created and opens when a retry is served.

**A separate module from `continuation.py`, not a new method on
`ContinuationTokenProvider`.** Both are AEAD sealing with AAD binding, and
that is where the resemblance ends. A continuation token is state the
*caller* holds and must present again -- self-describing, five minutes,
single-use, refused outright once its digest is recorded. A replay envelope
never leaves the deployment: it lives inside the receipt row it protects,
is opened by ARC itself keyed off the receipt's own identity, and stays
valid for as long as the receipt is retained, with no separate expiry of
its own. Sharing one key between the two would mean a leaked page token
could also decrypt a retained response body -- exactly the cross-purpose
failure `KeyPurpose` exists to make structurally impossible, see
`signing.py`'s module docstring. So this gets its own purpose, its own
provider, and its own module, like every other ARC key.

**Ciphertext, because the receipt table must not become a second copy of
governed content.** `ReplayEnvelope` (see `receipt.py`) is deliberately
opaque for the same reason `arc_directives.compact_statement` is: the
sealed bundle carries resolved directive content, and a receipt table an
operator can query must not also be a plaintext archive of everything any
agent was ever shown. `receipt.py` records whatever envelope it is handed
and never encrypts one itself -- this module is the key-provider it was
written expecting.

**Bound to the receipt it was sealed for, and nothing else.** The AAD
covers a profile tag and `receipt_id`, length-prefixed the way
`PageBinding.as_aad()` is in `continuation.py`. An envelope produced for
one receipt therefore fails to open under any other receipt's id, rather
than decrypting to a bundle nobody asked for.
"""

from __future__ import annotations

import json
import secrets as _secrets_module
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from contextplane.arc.service.bundle import ContextBundle
from contextplane.arc.service.receipt import ReplayEnvelope
from contextplane.arc.service.signing import KeyPurpose, KeyRecord, KeyUnavailableError, PurposeBoundKeyProvider
from contextplane.arc.types import ResolutionStatus
from contextplane.exceptions import RegistryError

# Independent of `KeyPurpose.RESPONSE_REPLAY_ENCRYPTION`, on purpose: the
# purpose names which keys may seal a replay envelope at all, and changing
# it is a key-custody event. This profile names how the envelope's
# plaintext is laid out -- right now, a JSON object carrying the whole
# bundle -- and bumping it is how a future format change becomes an
# explicit, versioned fact instead of a silent reinterpretation of an
# older envelope under new rules. See `content.py`'s module docstring for
# the same distinction made at more length.
RESPONSE_REPLAY_PROFILE = "arc_response_replay_envelope_v1"

# AES-GCM's standard nonce length. Never reused under one key -- see
# `ResponseReplayProvider.seal` for why a fresh one is drawn every call
# rather than one derived from `receipt_id`.
_NONCE_BYTES = 12


class ResponseReplayError(RegistryError):
    """An envelope did not open: wrong receipt, tampered bytes, an
    unavailable key, or a payload that decrypted but does not parse.

    One type for all of them, deliberately -- as with every other ARC AEAD
    boundary, the caller's answer is "this cannot be replayed," not a
    diagnosis of which check failed.
    """


class ResponseReplayProvider(PurposeBoundKeyProvider):
    """Holds the AEAD key that seals a receipt's retained response.

    Its own purpose and its own provider class, like every other ARC key --
    see `signing.py`'s module docstring for why sharing key material across
    purposes is structural, not a naming convention.

    Shaped like `ContinuationTokenProvider`: a healthy `KeyRecord` per
    configured secret, and no `records` override. Nothing here yet needs to
    express a retired or compromised replay key the way
    `ArcContentKeyProvider` does for content -- that refinement can be
    added if a rotation story ever needs it, rather than carried
    unused from the start.
    """

    purpose = KeyPurpose.RESPONSE_REPLAY_ENCRYPTION

    def __init__(self, secrets: dict[str, bytes], *, active_key_id: str | None) -> None:
        self._secrets = secrets
        self._active_key_id = active_key_id
        self._records = {
            key_id: KeyRecord(key_id=key_id, purpose=self.purpose, algorithm="AES-256-GCM") for key_id in secrets
        }

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        """The key new envelopes seal under. Raises when none is configured.

        Fail closed, matching every other ARC key provider: a deployment
        with no response-replay key must not produce a receipt whose
        retained response is stored unencrypted, and must not silently skip
        retaining one either -- both would be a quieter failure than
        refusing outright.
        """
        if self._active_key_id is None:
            msg = (
                "no active ARC response-replay key is configured; refusing to "
                "seal a retained response without encrypting it"
            )
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def secret(self, key_id: str) -> bytes:
        """The raw key bytes, after the base class has checked the purpose."""
        self.get(key_id)
        secret = self._secrets.get(key_id)
        if secret is None:
            msg = f"no response-replay secret for key {key_id!r}"
            raise KeyUnavailableError(msg)
        return secret

    def seal(self, receipt_id: uuid.UUID, bundle: ContextBundle) -> ReplayEnvelope:
        """Seal `bundle` so `receipt_id`'s exact retry can replay it.

        The nonce is drawn fresh from the CSPRNG on every call -- the same
        choice `ContinuationTokenProvider.issue` and
        `ArcContentProtectionService.protect` make. All AES-GCM actually
        requires is that one (key, nonce) pair never encrypt two different
        messages, and a fresh random 96-bit nonce satisfies that without
        this method having to reason about whether it could ever be called
        twice for the same plaintext.

        It can be, though, which is why a *derived* nonce -- keyed off
        `receipt_id`, the way `ChallengeNonceDeriver` derives a nonce from a
        challenge id so nothing has to be stored -- was considered and set
        aside. `ResolutionService._attempt` retries the whole resolution
        transaction on a lost race, with the same preallocated `receipt_id`
        every time, and calls this before it knows whether that attempt's
        transaction will actually commit. Only the attempt that commits
        ever persists a ciphertext; the rest are discarded with their
        transaction. A derived nonce would make every discarded call a
        repeat of one `(key, nonce)` pair under this `receipt_id`, and
        would need this method to prove every one of them also carried
        identical plaintext -- true today because selection is pure, but
        not a fact this method should have to depend on to stay safe. A
        random nonce needs no such proof: it is correct regardless of how
        many times this is called for one receipt, or with what.
        """
        key_id = self.active_key_id
        secret = self.secret(key_id)
        nonce = _fresh_nonce()
        ciphertext = AESGCM(secret).encrypt(nonce, _encode_bundle(bundle), _aad(receipt_id))
        return ReplayEnvelope(ciphertext=ciphertext, nonce=nonce, key_id=key_id)

    def open_envelope(self, receipt_id: uuid.UUID, envelope: ReplayEnvelope) -> ContextBundle:
        """Unseal `envelope`, refusing anything not sealed for `receipt_id`.

        Named `open_envelope`, not `open`, for the same reason
        `continuation.py` names its inverse `open_token`: the builtin
        already owns that name.
        """
        try:
            secret = self.secret(envelope.key_id)
        except KeyUnavailableError as exc:
            # A key this deployment no longer holds at all is
            # indistinguishable, to the caller, from an envelope that was
            # never valid -- see `continuation.py`'s `open_token` for the
            # same call.
            msg = "response-replay envelope cannot be opened"
            raise ResponseReplayError(msg) from exc

        try:
            plaintext = AESGCM(secret).decrypt(envelope.nonce, envelope.ciphertext, _aad(receipt_id))
        except InvalidTag as exc:
            # Wrong receipt, tampered ciphertext, and a wrong key all land
            # here, and all mean the same thing to the caller.
            msg = (
                "response-replay envelope failed to authenticate -- wrong " "receipt, tampered ciphertext, or wrong key"
            )
            raise ResponseReplayError(msg) from exc

        return _decode_bundle(plaintext)


def _aad(receipt_id: uuid.UUID) -> bytes:
    """Additional authenticated data for one seal/open call.

    Length-prefixed like `PageBinding.as_aad()` in `continuation.py`, even
    though only one value is bound here: it is `receipt_id.bytes` behind a
    fixed profile tag, and both are fixed-length, so plain concatenation
    could not actually collide today. The prefix costs nothing and makes
    this safe by construction rather than by an argument that happens to
    hold given today's field list -- the same unconditional guarantee
    every other ARC AAD in this codebase gives itself, and the one a
    second bound field added here later would need without anyone having
    to remember to add it then.
    """
    parts = (RESPONSE_REPLAY_PROFILE.encode("ascii"), receipt_id.bytes)
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _fresh_nonce() -> bytes:
    return _secrets_module.token_bytes(_NONCE_BYTES)


def _encode_bundle(bundle: ContextBundle) -> bytes:
    """The bundle as canonical-enough JSON bytes: deterministic key order,
    no incidental whitespace. Determinism is not a security requirement
    here -- unlike `schemas/canonical.py`'s profiles, nothing external ever
    recomputes this digest -- but a stable encoding makes the ciphertext
    itself a useful diagnostic (two sealings of an identical bundle differ
    only in nonce and ciphertext, never in a shuffled plaintext).
    """
    payload = {
        "profile": RESPONSE_REPLAY_PROFILE,
        "bundle": {
            "status": str(bundle.status),
            "directives": list(bundle.directives),
            "cap_facts": list(bundle.cap_facts),
            "rendered_content_bytes": bundle.rendered_content_bytes,
            "budget_limit_bytes": bundle.budget_limit_bytes,
            "blocked_reasons": list(bundle.blocked_reasons),
            "degraded_reasons": list(bundle.degraded_reasons),
            "omission_reasons": list(bundle.omission_reasons),
            "offending_artifact_ids": list(bundle.offending_artifact_ids),
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_bundle(plaintext: bytes) -> ContextBundle:
    """The inverse of `_encode_bundle`.

    A payload that fails to parse here decrypted correctly -- AEAD already
    refused anything tampered -- so a failure in this function means a
    format this build does not understand, not an attack. Reported the
    same way regardless: a receipt that cannot be replayed is a receipt
    that cannot be replayed either way.
    """
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        msg = "response-replay envelope payload is malformed"
        raise ResponseReplayError(msg) from exc
    if not isinstance(decoded, dict) or decoded.get("profile") != RESPONSE_REPLAY_PROFILE:
        msg = "response-replay envelope profile is unsupported"
        raise ResponseReplayError(msg)

    body = decoded.get("bundle")
    try:
        if not isinstance(body, dict):
            raise TypeError("response-replay envelope is missing its bundle body")
        return ContextBundle(
            status=ResolutionStatus(body["status"]),
            directives=_as_object_tuple(body["directives"], field="directives"),
            cap_facts=_as_cap_fact_tuple(body["cap_facts"], field="cap_facts"),
            rendered_content_bytes=_as_int(body["rendered_content_bytes"], field="rendered_content_bytes"),
            budget_limit_bytes=_as_int(body["budget_limit_bytes"], field="budget_limit_bytes"),
            blocked_reasons=_as_str_tuple(body["blocked_reasons"], field="blocked_reasons"),
            degraded_reasons=_as_str_tuple(body["degraded_reasons"], field="degraded_reasons"),
            omission_reasons=_as_str_tuple(body["omission_reasons"], field="omission_reasons"),
            offending_artifact_ids=_as_str_tuple(body["offending_artifact_ids"], field="offending_artifact_ids"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "response-replay envelope payload is malformed"
        raise ResponseReplayError(msg) from exc


def _as_int(value: object, *, field: str) -> int:
    """Accept only a real integer from a decoded payload.

    `int("7")` would happily accept a string here. `rendered_content_bytes`
    and `budget_limit_bytes` are compared against each other elsewhere in
    ARC, so a loose parse here would make that comparison meaningless.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"expected an integer for {field!r} in a replay payload, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _as_str_tuple(value: object, *, field: str) -> tuple[str, ...]:
    """A JSON array of strings, as one of `ContextBundle`'s reason tuples."""
    if not isinstance(value, list):
        msg = f"expected a list for {field!r} in a replay payload, got {type(value).__name__}"
        raise TypeError(msg)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            msg = f"expected a string in {field!r}, got {type(item).__name__}"
            raise TypeError(msg)
        items.append(item)
    return tuple(items)


def _as_object_tuple(value: object, *, field: str) -> tuple[dict[str, object], ...]:
    """A JSON array of JSON objects, as `ContextBundle.directives` holds them."""
    if not isinstance(value, list):
        msg = f"expected a list for {field!r} in a replay payload, got {type(value).__name__}"
        raise TypeError(msg)
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            msg = f"expected an object in {field!r}, got {type(item).__name__}"
            raise TypeError(msg)
        items.append(item)
    return tuple(items)


def _as_cap_fact_tuple(value: object, *, field: str) -> tuple[dict[str, str | None], ...]:
    """A JSON array of string-or-null objects, as `ContextBundle.cap_facts` holds them."""
    if not isinstance(value, list):
        msg = f"expected a list for {field!r} in a replay payload, got {type(value).__name__}"
        raise TypeError(msg)
    facts: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            msg = f"expected an object in {field!r}, got {type(item).__name__}"
            raise TypeError(msg)
        fact: dict[str, str | None] = {}
        for key, val in item.items():
            if not isinstance(key, str):
                msg = f"expected a string key in {field!r}, got {type(key).__name__}"
                raise TypeError(msg)
            if val is not None and not isinstance(val, str):
                msg = f"expected a string or null value in {field!r}, got {type(val).__name__}"
                raise TypeError(msg)
            fact[key] = val
        facts.append(fact)
    return tuple(facts)


__all__ = [
    "RESPONSE_REPLAY_PROFILE",
    "ResponseReplayError",
    "ResponseReplayProvider",
]
