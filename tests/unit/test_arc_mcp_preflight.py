"""The MCP preflight state machine.

REST re-authenticates every request; a long-lived MCP connection does not.
Without this, a credential that changed mid-connection would keep working
until the connection dropped. Every negative path below is a way that could
happen, and each one has to refuse.

These are the tests that matter most in this file — a preflight that only
ever accepts is indistinguishable from no preflight at all.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from registry.arc.service.preflight import (
    PREFLIGHT_REQUIRED,
    PreflightError,
    PreflightRecord,
    PreflightRegistry,
    credential_fingerprint,
    new_connection_id,
    restriction_digest,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_EXPIRES = _NOW + datetime.timedelta(hours=1)
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ACTOR = uuid.UUID("33333333-3333-3333-3333-333333333333")
_TOKEN = "header.payload.signature"
_RESTRICTIONS = {"scope": "read"}


def _registry_with_record(
    *,
    connection_id: str,
    token: str = _TOKEN,
    tenant_id: uuid.UUID = _TENANT,
    restrictions: object = None,
    expires_at: datetime.datetime = _EXPIRES,
) -> PreflightRegistry:
    registry = PreflightRegistry()
    registry.record(
        connection_id=connection_id,
        credential_fingerprint=credential_fingerprint(token),
        tenant_id=tenant_id,
        actor_id=_ACTOR,
        oidc_issuer="https://idp.example.test",
        oidc_subject="svc-agent",
        roles=("consumer",),
        token_restriction_digest=restriction_digest(restrictions if restrictions is not None else _RESTRICTIONS),
        authentication_expires_at=expires_at,
        completed_at=_NOW,
    )
    return registry


def _require(
    registry: PreflightRegistry,
    connection_id: str | None,
    *,
    token: str = _TOKEN,
    tenant_id: uuid.UUID = _TENANT,
    restrictions: object = None,
    now: datetime.datetime = _NOW,
) -> PreflightRecord:
    return registry.require(
        connection_id=connection_id,
        credential_fingerprint=credential_fingerprint(token),
        tenant_id=tenant_id,
        token_restriction_digest=restriction_digest(restrictions if restrictions is not None else _RESTRICTIONS),
        now=now,
    )


# --- the happy path, so the negatives mean something --------------------------


def test_a_completed_preflight_admits_a_matching_call() -> None:
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    record = _require(registry, conn)
    assert record.tenant_id == _TENANT
    assert record.actor_id == _ACTOR


def test_the_record_carries_everything_a_later_check_needs() -> None:
    conn = new_connection_id()
    record = _registry_with_record(connection_id=conn).require(
        connection_id=conn,
        credential_fingerprint=credential_fingerprint(_TOKEN),
        tenant_id=_TENANT,
        token_restriction_digest=restriction_digest(_RESTRICTIONS),
        now=_NOW,
    )
    assert record.oidc_issuer == "https://idp.example.test"
    assert record.oidc_subject == "svc-agent"
    assert record.roles == ("consumer",)
    assert record.completed_at == _NOW


# --- the negatives, which are the point ----------------------------------------


def test_a_connection_that_never_preflighted_is_refused() -> None:
    with pytest.raises(PreflightError) as exc:
        _require(PreflightRegistry(), new_connection_id())
    assert exc.value.code == PREFLIGHT_REQUIRED


def test_a_call_with_no_connection_identity_is_refused() -> None:
    """A tool that could not determine which connection it was serving must
    not fall back to serving all of them."""
    with pytest.raises(PreflightError, match="no server connection identity"):
        _require(PreflightRegistry(), None)


def test_an_empty_connection_id_is_refused() -> None:
    """Empty string is not a connection; treating it as one would make a
    single shared record for every caller that failed to supply one."""
    with pytest.raises(PreflightError):
        _require(PreflightRegistry(), "")


def test_a_changed_credential_is_refused() -> None:
    """The fingerprint covers the token itself, so a swapped token refuses
    even when every claim inside it is identical."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="credential .* has changed"):
        _require(registry, conn, token="a.different.token")


def test_a_changed_tenant_selector_is_refused() -> None:
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="tenant selection .* has changed"):
        _require(registry, conn, tenant_id=_OTHER_TENANT)


def test_changed_restrictions_are_refused() -> None:
    """A narrowed or widened token must not keep operating under the
    preflight it completed before the change."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="restrictions .* have changed"):
        _require(registry, conn, restrictions={"scope": "write"})


def test_expired_authentication_is_refused() -> None:
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="expired"):
        _require(registry, conn, now=_EXPIRES + datetime.timedelta(seconds=1))


def test_expiry_is_checked_at_the_boundary_not_after_it() -> None:
    """Exactly-at-expiry is expired. An off-by-one here is a window in
    which a dead credential still works."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="expired"):
        _require(registry, conn, now=_EXPIRES)
    # One second earlier still works, so this is a boundary and not a
    # blanket refusal.
    assert _require(_registry_with_record(connection_id=conn), conn, now=_EXPIRES - datetime.timedelta(seconds=1))


# --- a refusal must not leave reusable state ------------------------------------


@pytest.mark.parametrize(
    ("changed", "match"),
    [("credential", "credential"), ("tenant", "tenant"), ("restrictions", "restrictions")],
)
def test_a_failed_check_invalidates_the_record(changed: str, match: str) -> None:
    """The second attempt must not get a different answer from the first.

    Leaving the record would mean a caller who changed their credential
    could change it back and resume -- which is exactly the replay the
    fingerprint check exists to stop.
    """
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)

    with pytest.raises(PreflightError, match=match):
        if changed == "credential":
            _require(registry, conn, token="a.different.token")
        elif changed == "tenant":
            _require(registry, conn, tenant_id=_OTHER_TENANT)
        else:
            _require(registry, conn, restrictions={"scope": "write"})

    # Even the originally-correct identity is now refused: the record is gone.
    with pytest.raises(PreflightError, match="not completed"):
        _require(registry, conn)


def test_an_expired_record_is_dropped_rather_than_left_to_be_resurrected() -> None:
    """A clock that moved backwards must not bring a dead record back."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    with pytest.raises(PreflightError, match="expired"):
        _require(registry, conn, now=_EXPIRES + datetime.timedelta(seconds=1))
    assert len(registry) == 0
    with pytest.raises(PreflightError, match="not completed"):
        _require(registry, conn, now=_NOW)


# --- lifecycle -------------------------------------------------------------------


def test_re_preflighting_replaces_the_record() -> None:
    """A client that refreshed its token legitimately re-preflights; the
    new record is simply the current truth."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    registry.record(
        connection_id=conn,
        credential_fingerprint=credential_fingerprint("refreshed.token.here"),
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        oidc_issuer="https://idp.example.test",
        oidc_subject="svc-agent",
        roles=("consumer",),
        token_restriction_digest=restriction_digest(_RESTRICTIONS),
        authentication_expires_at=_EXPIRES,
        completed_at=_NOW,
    )

    assert _require(registry, conn, token="refreshed.token.here")
    with pytest.raises(PreflightError, match="credential"):
        _require(registry, conn, token=_TOKEN)


def test_invalidating_a_connection_refuses_its_later_calls() -> None:
    """What disconnect and logout call."""
    conn = new_connection_id()
    registry = _registry_with_record(connection_id=conn)
    registry.invalidate(conn)
    with pytest.raises(PreflightError):
        _require(registry, conn)


def test_invalidating_an_unknown_connection_is_harmless() -> None:
    """Disconnect can fire for a connection that never preflighted."""
    PreflightRegistry().invalidate("never-seen")


def test_connections_are_isolated_from_each_other() -> None:
    """One connection's preflight must never admit another's calls."""
    mine = new_connection_id()
    theirs = new_connection_id()
    registry = _registry_with_record(connection_id=mine)

    assert _require(registry, mine)
    with pytest.raises(PreflightError, match="not completed"):
        _require(registry, theirs)


def test_invalidating_one_connection_leaves_others_alone(caplog: pytest.LogCaptureFixture) -> None:
    first = new_connection_id()
    second = new_connection_id()
    registry = _registry_with_record(connection_id=first)
    registry.record(
        connection_id=second,
        credential_fingerprint=credential_fingerprint(_TOKEN),
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        oidc_issuer="https://idp.example.test",
        oidc_subject="svc-agent",
        roles=("consumer",),
        token_restriction_digest=restriction_digest(_RESTRICTIONS),
        authentication_expires_at=_EXPIRES,
        completed_at=_NOW,
    )

    registry.invalidate(first)
    assert _require(registry, second)


# --- identifiers and digests -------------------------------------------------------


def test_connection_ids_are_unguessable_and_unique() -> None:
    ids = {new_connection_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) >= 32 for i in ids)


def test_the_fingerprint_does_not_contain_the_token() -> None:
    """It is stored in memory and may appear in a diagnostic; it must not
    be a credential anybody could replay."""
    fingerprint = credential_fingerprint(_TOKEN)
    assert _TOKEN not in fingerprint
    assert len(fingerprint) == 64


def test_restriction_digest_is_order_independent() -> None:
    """An unordered set of scopes must not appear to change merely because
    it serialized in a different order."""
    assert restriction_digest(["b", "a"]) == restriction_digest(["a", "b"])
    assert restriction_digest({"x": 1, "y": 2}) == restriction_digest({"y": 2, "x": 1})


def test_restriction_digest_distinguishes_different_restrictions() -> None:
    """The negative control: order-independence must not have flattened
    everything to one value."""
    assert restriction_digest(["a"]) != restriction_digest(["b"])
    assert restriction_digest({"scope": "read"}) != restriction_digest({"scope": "write"})


def test_absent_restrictions_have_a_stable_digest() -> None:
    """A credential carrying none must compare equal to itself across
    calls, rather than being treated as changed every time."""
    assert restriction_digest(None) == restriction_digest(None)
