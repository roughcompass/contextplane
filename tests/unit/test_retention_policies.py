"""Retention dispositions, the two clocks, and the expiry a derivative inherits.

The rules here decide how long anything is kept and what erasing it leaves behind,
so the tests worth having are the ones about refusal: an unknown record class, a
derivative whose sources are all event-bounded, a payload clock that would outlive
the record it belongs to.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.retention import policies

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def test_every_declared_record_class_has_a_disposition() -> None:
    """The list and the lookup are the same set. A class in one and not the other is
    either an unenforced policy or a policy for a table that does not exist."""
    assert policies.RECORD_CLASSES
    for record_class in policies.RECORD_CLASSES:
        assert policies.disposition(record_class).record_class == record_class


def test_an_unknown_record_class_is_refused() -> None:
    """A default disposition is how a new table quietly acquires the most permissive
    policy nobody approved."""
    with pytest.raises(policies.UnknownRecordClass) as refused:
        policies.disposition("table_nobody_wrote_a_policy_for")
    assert "table_nobody_wrote_a_policy_for" in str(refused.value)


def test_every_erasure_mode_is_one_of_the_declared_four() -> None:
    """Closed vocabulary: a mode nothing implements would be written and never acted
    on, which reads as a policy that is being applied."""
    for record_class in policies.RECORD_CLASSES:
        assert policies.disposition(record_class).erasure_mode in policies.ERASURE_MODES


def test_an_exempt_class_holds_nothing_to_withhold_and_says_so() -> None:
    """Exempt means the record carries no values, so erasure passes over it. Such a
    class must not also claim to tombstone, or the two statements contradict."""
    exempt = [rc for rc in policies.RECORD_CLASSES if policies.is_erasure_exempt(rc)]
    assert exempt, "no class is exempt, so this rule is untested rather than satisfied"
    for record_class in exempt:
        disposition = policies.disposition(record_class)
        assert disposition.is_exempt is True
        assert disposition.writes_tombstone is False


def test_writing_a_tombstone_is_decided_by_behaviour_not_by_mode() -> None:
    """A class may be deleted outright and still owe a tombstone — the record that a
    removal happened is what stops it looking like data loss — so the flag is derived
    from the tombstone behaviour rather than from the erasure mode."""
    for record_class in policies.RECORD_CLASSES:
        disposition = policies.disposition(record_class)
        assert disposition.writes_tombstone == (disposition.tombstone_behaviour is not None)


def test_a_class_with_no_retention_period_computes_no_deadline() -> None:
    """None means bounded by tenant or workspace deletion, not unbounded. Returning a
    computed date anyway would invent a duration nobody approved."""
    unbounded = [rc for rc in policies.RECORD_CLASSES if policies.disposition(rc).retention_days is None]
    assert unbounded, "no class is event-bounded, so this branch is untested"
    for record_class in unbounded:
        assert policies.expiry_deadline(record_class, _NOW) is None


def test_a_class_with_a_period_expires_after_it() -> None:
    """The deadline is the anchor plus the approved period, so a period of N days
    cannot silently become N hours."""
    bounded = [rc for rc in policies.RECORD_CLASSES if policies.disposition(rc).retention_days is not None]
    assert bounded, "no class has a duration, so this branch is untested"
    for record_class in bounded:
        days = policies.disposition(record_class).retention_days
        assert days is not None
        assert policies.expiry_deadline(record_class, _NOW) == _NOW + datetime.timedelta(days=days)


def test_the_payload_clock_never_outlives_the_record_clock() -> None:
    """Content reduces before the record goes, or the two are one clock. The reverse
    would minimize a record that had already been deleted."""
    for record_class in policies.RECORD_CLASSES:
        payload = policies.payload_deadline(record_class, _NOW)
        record = policies.expiry_deadline(record_class, _NOW)
        if payload is None:
            assert policies.disposition(record_class).payload_retention_days is None
            continue
        assert record is None or payload <= record


def test_a_derivative_inherits_the_earliest_expiry_of_its_sources() -> None:
    """Never the latest and never an average: a derivative that outlived any source
    keeps that source's content readable after the record itself is gone."""
    early = _NOW + datetime.timedelta(days=10)
    late = _NOW + datetime.timedelta(days=400)

    assert policies.minimum_expiry([late, early]) == early
    # An event-bounded source contributes nothing rather than counting as forever.
    assert policies.minimum_expiry([late, None, early]) == early


def test_a_tenant_horizon_earlier_than_every_source_still_wins() -> None:
    """The fallback is the tenant's own horizon, and nothing may outlive that either,
    so it competes with the sources rather than only filling in for them."""
    source = _NOW + datetime.timedelta(days=100)
    horizon = _NOW + datetime.timedelta(days=5)

    assert policies.minimum_expiry([source], fallback=horizon) == horizon
    # And when the horizon is later, the source is still the binding constraint.
    assert policies.minimum_expiry([horizon], fallback=source) == horizon


def test_wholly_event_bounded_sources_need_an_explicit_fallback() -> None:
    """Refused rather than written with a guessed value: a registration whose expiry
    somebody invented is a derivative that expires on a date no policy chose."""
    assert policies.minimum_expiry([None, None], fallback=_NOW) == _NOW

    with pytest.raises(policies.NoComputableExpiry) as refused:
        policies.minimum_expiry([None, None])
    assert "bounded source" in str(refused.value)

    with pytest.raises(policies.NoComputableExpiry):
        policies.minimum_expiry([])


def test_the_policy_version_is_stamped_and_stable() -> None:
    """Every tombstone records which policy applied. A version that moved without a
    decision would make old tombstones unattributable."""
    assert policies.POLICY_VERSION
    assert policies.disposition(policies.RECORD_CONTEXT_RECEIPT).verifier_disclosure
    assert policies.TENANT_GRACE_DAYS > 0
