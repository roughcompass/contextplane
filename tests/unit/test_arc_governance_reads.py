"""What "in force" means, decided once.

Both tables carry the columns the answer is made of and neither carries the
answer, so this is the only place it is computed. Testing it here rather than
only through a route is the point: four screens would otherwise each decide,
and the one that got `valid_to IS NULL` backwards would show revoked verifiers
as live.
"""

from __future__ import annotations

import dataclasses
import datetime

import pytest

from contextplane.arc.service.governance_reads import _exception_in_force, _verifier_in_force

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)


@dataclasses.dataclass(frozen=True)
class _Verifier:
    valid_from: datetime.datetime = _NOW - _HOUR
    valid_to: datetime.datetime | None = None
    revoked_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class _Exception:
    effective_from: datetime.datetime = _NOW - _HOUR
    effective_until: datetime.datetime | None = None
    revoked_at: datetime.datetime | None = None


def test_a_verifier_with_no_end_date_is_in_force() -> None:
    """`valid_to IS NULL` means "no end", not "expired".

    The one a reader gets wrong once, and the reason this is computed in a
    single place rather than by each caller reading three columns.
    """
    assert _verifier_in_force(_Verifier(), _NOW)


def test_a_revoked_verifier_is_not_in_force_however_valid_its_window() -> None:
    """Revocation beats the window. A verifier revoked this morning with a
    validity window running to next year must not read as live."""
    assert not _verifier_in_force(
        _Verifier(valid_to=_NOW + datetime.timedelta(days=365), revoked_at=_NOW - _HOUR), _NOW
    )


def test_a_verifier_not_yet_valid_is_not_in_force() -> None:
    """Enrolment can be future-dated, and a future verifier cannot approve now."""
    assert not _verifier_in_force(_Verifier(valid_from=_NOW + _HOUR), _NOW)


def test_a_verifier_past_its_window_is_not_in_force() -> None:
    assert not _verifier_in_force(_Verifier(valid_to=_NOW - _HOUR), _NOW)


def test_an_exception_with_no_end_is_in_force_indefinitely() -> None:
    """A real state, not a missing value.

    An open-ended exception is a policy change wearing a smaller word, and the
    surface showing it should make that visible rather than hand it an invented
    expiry to tidy the column.
    """
    assert _exception_in_force(_Exception(), _NOW)


def test_a_revoked_exception_is_not_in_force() -> None:
    assert not _exception_in_force(_Exception(revoked_at=_NOW - _HOUR), _NOW)


@pytest.mark.parametrize(
    "row",
    [
        _Exception(effective_from=_NOW + _HOUR),
        _Exception(effective_until=_NOW - _HOUR),
    ],
)
def test_an_exception_outside_its_window_is_not_in_force(row: _Exception) -> None:
    assert not _exception_in_force(row, _NOW)


def test_the_boundary_is_exclusive_at_the_end_and_inclusive_at_the_start() -> None:
    """An exception effective exactly now is in force; one ending exactly now is
    not. Stated because a reader hitting the boundary should not have to guess,
    and because the two tables have to agree on it."""
    assert _exception_in_force(_Exception(effective_from=_NOW), _NOW)
    assert not _exception_in_force(_Exception(effective_until=_NOW), _NOW)
    assert _verifier_in_force(_Verifier(valid_from=_NOW), _NOW)
    assert not _verifier_in_force(_Verifier(valid_to=_NOW), _NOW)
