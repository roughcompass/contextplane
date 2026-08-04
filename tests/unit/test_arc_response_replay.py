"""Response-replay envelopes are bound to their exact receipt, not merely encrypted."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable

import pytest

from registry.arc.service.bundle import ContextBundle
from registry.arc.service.replay import ResponseReplayError, ResponseReplayProvider
from registry.arc.service.signing import KeyPurpose, KeyPurposeMismatchError, KeyRecord, KeyUnavailableError
from registry.arc.types import ResolutionStatus


def _secret(label: bytes) -> bytes:
    """A distinguishable 32-byte AES-256 key."""
    return (label * 32)[:32]


def _provider(
    secrets: dict[str, bytes] | None = None,
    *,
    active: str | None = "k1",
) -> ResponseReplayProvider:
    return ResponseReplayProvider(
        secrets if secrets is not None else {"k1": _secret(b"1"), "k2": _secret(b"2")},
        active_key_id=active,
    )


def _bundle(**overrides: object) -> ContextBundle:
    defaults: dict[str, object] = {
        "status": ResolutionStatus.READY,
        "directives": (
            {
                "directive_id": str(uuid.uuid4()),
                "revision_id": str(uuid.uuid4()),
                "directive_type": "prohibition",
                "scope": "actor",
                "source_anchor": "policy://retention#s3",
                "constraint": {
                    "modality": "prohibit",
                    "operator": "in_set",
                    "values": ["public-read", "public-read-write"],
                },
            },
        ),
        "cap_facts": (
            {
                "capability_id": str(uuid.uuid4()),
                "owner": "team-storage",
                "lifecycle": "ga",
                "version": "3",
                "interface_reference": None,
            },
        ),
        "rendered_content_bytes": 512,
        "budget_limit_bytes": 65536,
        "blocked_reasons": (),
        "degraded_reasons": (),
        "omission_reasons": (),
        "offending_artifact_ids": (),
    }
    defaults.update(overrides)
    return ContextBundle(**defaults)  # type: ignore[arg-type]


def _blocked_bundle() -> ContextBundle:
    return _bundle(
        status=ResolutionStatus.BLOCKED,
        directives=(),
        cap_facts=(),
        blocked_reasons=("blocked_budget_exceeded",),
        offending_artifact_ids=(str(uuid.uuid4()), str(uuid.uuid4())),
    )


_ANY_BUNDLE: tuple[Callable[[], ContextBundle], ...] = (_bundle, _blocked_bundle)


def _flip_last_byte(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 0xFF])


# ---------------------------------------------------------------------------
# Purpose separation
# ---------------------------------------------------------------------------


def test_provider_is_bound_to_the_response_replay_purpose() -> None:
    assert ResponseReplayProvider.purpose is KeyPurpose.RESPONSE_REPLAY_ENCRYPTION


def test_a_key_recorded_for_a_different_purpose_is_refused() -> None:
    """A key some other purpose's provider recorded must never be usable
    here -- see `signing.py` for why sharing key material across purposes
    is a real vulnerability, not an untidiness."""

    class _FixedRecordProvider(ResponseReplayProvider):
        def _load(self, key_id: str) -> KeyRecord | None:
            return KeyRecord(key_id=key_id, purpose=KeyPurpose.CONTINUATION_TOKEN, algorithm="AES-256-GCM")

    provider = _FixedRecordProvider({"k1": _secret(b"1")}, active_key_id="k1")
    with pytest.raises(KeyPurposeMismatchError):
        provider.get("k1")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_bundle", _ANY_BUNDLE, ids=["ready", "blocked"])
def test_round_trip_recovers_the_original_bundle(make_bundle: Callable[[], ContextBundle]) -> None:
    provider = _provider()
    receipt_id = uuid.uuid4()
    bundle = make_bundle()

    envelope = provider.seal(receipt_id, bundle)

    assert provider.open_envelope(receipt_id, envelope) == bundle


def test_repeated_seal_of_the_same_bundle_yields_different_bytes() -> None:
    """A fresh nonce every call -- ciphertext must not leak which receipts
    retained equal responses, and a repeat seal for one receipt (as a
    resolution retry produces, see `ResponseReplayProvider.seal`) must not
    collide with the seal that came before it."""
    provider = _provider()
    receipt_id = uuid.uuid4()
    bundle = _bundle()

    first = provider.seal(receipt_id, bundle)
    second = provider.seal(receipt_id, bundle)

    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert provider.open_envelope(receipt_id, first) == bundle
    assert provider.open_envelope(receipt_id, second) == bundle


# ---------------------------------------------------------------------------
# AAD binding -- the property this module exists for
# ---------------------------------------------------------------------------


def test_an_envelope_sealed_for_one_receipt_fails_to_open_for_another() -> None:
    """The single most important property here: an envelope is not portable
    to any other receipt, even one sealed moments later under the same key."""
    provider = _provider()
    receipt_a = uuid.uuid4()
    receipt_b = uuid.uuid4()
    bundle = _bundle()

    envelope = provider.seal(receipt_a, bundle)

    with pytest.raises(ResponseReplayError, match="authenticate"):
        provider.open_envelope(receipt_b, envelope)

    # And it still opens correctly for the receipt it was actually sealed for.
    assert provider.open_envelope(receipt_a, envelope) == bundle


# ---------------------------------------------------------------------------
# AEAD integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_bundle", _ANY_BUNDLE, ids=["ready", "blocked"])
def test_tampered_ciphertext_fails_to_open(make_bundle: Callable[[], ContextBundle]) -> None:
    provider = _provider()
    receipt_id = uuid.uuid4()
    envelope = provider.seal(receipt_id, make_bundle())
    tampered = dataclasses.replace(envelope, ciphertext=_flip_last_byte(envelope.ciphertext))

    with pytest.raises(ResponseReplayError, match="authenticate"):
        provider.open_envelope(receipt_id, tampered)


def test_tampered_nonce_fails_to_open() -> None:
    provider = _provider()
    receipt_id = uuid.uuid4()
    envelope = provider.seal(receipt_id, _bundle())
    tampered = dataclasses.replace(envelope, nonce=_flip_last_byte(envelope.nonce))

    with pytest.raises(ResponseReplayError, match="authenticate"):
        provider.open_envelope(receipt_id, tampered)


# ---------------------------------------------------------------------------
# Fail closed on a missing or unhealthy provider
# ---------------------------------------------------------------------------


def test_no_active_key_refuses_to_seal() -> None:
    """No configured active key: the same fail-closed shape every other ARC
    key provider (`ReceiptSigningProvider`, `ArcContentKeyProvider`,
    `ContinuationTokenProvider`) has when constructed with
    `active_key_id=None` -- raise, never seal an unencrypted bundle."""
    provider = ResponseReplayProvider({}, active_key_id=None)

    with pytest.raises(KeyUnavailableError, match="no active"):
        provider.seal(uuid.uuid4(), _bundle())


def test_seal_refuses_an_active_key_id_with_no_matching_secret() -> None:
    """An active key id pointing at nothing configured is a misconfiguration,
    not merely "none configured" -- and fails the same way, closed."""
    provider = ResponseReplayProvider({}, active_key_id="does-not-exist")

    with pytest.raises(KeyUnavailableError):
        provider.seal(uuid.uuid4(), _bundle())


def test_open_fails_closed_when_the_sealing_key_is_unavailable() -> None:
    """A key this deployment does not hold at all -- the sharpest form of a
    missing provider -- must raise, never fall back to returning the
    ciphertext or a guessed bundle."""
    provider = _provider()
    receipt_id = uuid.uuid4()
    envelope = provider.seal(receipt_id, _bundle())

    keyless = ResponseReplayProvider({}, active_key_id=None)
    with pytest.raises(ResponseReplayError):
        keyless.open_envelope(receipt_id, envelope)


# --- wiring: what a keyless deployment must do ---------------------------------


def test_a_keyless_deployment_does_not_wire_resolution() -> None:
    """The route reports "not configured" rather than failing per request.

    A provider with no active key refuses to seal, so wiring resolution
    anyway would turn every call into a 500 that looks like an outage. The
    honest answer is that this deployment cannot resolve -- and it stays
    honest only while `_wire_arc` gates on the key being present.
    """
    import inspect

    from registry.wiring import services

    source = inspect.getsource(services._wire_arc)
    assert (
        "if arc_active_key_id is not None:" in source
    ), "resolution must be wired only when there is key material behind it"
    gate = source.index("if arc_active_key_id is not None:")
    assert source.index("app.state.arc_resolution") > gate, "arc_resolution is assigned outside the key gate"


def test_the_provenance_a_receipt_records_is_not_invented() -> None:
    """Every provenance field has to come from something real.

    A receipt asserting a build revision that was never deployed is worse
    than one admitting the deployment did not say, because a replay years
    later uses exactly these fields to tell tampering from a newer engine.
    """
    import inspect

    from registry.config import Settings
    from registry.wiring import services

    source = inspect.getsource(services._wire_arc)
    assert "registry_build_revision=settings.build_revision" in source
    assert "selection_config_digest()" in source
    assert "CANONICAL_PROFILE_VERSIONS" in source
    # And the default is an admission, not a plausible-looking value.
    assert Settings.model_fields["build_revision"].default == "unknown"
