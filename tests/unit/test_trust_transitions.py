"""Which movements count as falling out of a trust class, and which do not.

The ranking logic is the part that decides what ends up in the record, and it is
pure — so it is tested here rather than only against a database, where a wrong
answer would show up as a table that is too full or too empty and look plausible
either way.
"""

from __future__ import annotations

import pytest

from contextplane.service.memory.confidence import (
    BUCKET_CONFIRMED,
    BUCKET_LOWER_BOUNDS,
    BUCKET_MODERATE,
    BUCKET_STRONG,
    BUCKET_UNRELIABLE,
    BUCKET_WEAK,
    bucket_for,
)
from contextplane.service.memory.trust_transitions import fell


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (BUCKET_CONFIRMED, BUCKET_STRONG),
        (BUCKET_STRONG, BUCKET_MODERATE),
        (BUCKET_MODERATE, BUCKET_WEAK),
        (BUCKET_WEAK, BUCKET_UNRELIABLE),
        # Two at once: a claim unread for long enough skips a bucket, and the
        # record has to hold the whole drop rather than the last step of it.
        (BUCKET_CONFIRMED, BUCKET_UNRELIABLE),
    ],
)
def test_a_weaker_bucket_is_a_fall(previous: str, current: str) -> None:
    assert fell(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (BUCKET_STRONG, BUCKET_CONFIRMED),
        (BUCKET_UNRELIABLE, BUCKET_MODERATE),
    ],
)
def test_regaining_trust_is_not_a_fall(previous: str, current: str) -> None:
    """Recorded by the path that caused it, not by a sweep.

    A claim regains trust because something happened — a confirmation, a
    corroborating source, a rescore — and each already leaves a record. Decay is
    the only direction with no event behind it.
    """
    assert not fell(previous, current)


@pytest.mark.parametrize("bucket", [name for name, _ in BUCKET_LOWER_BOUNDS])
def test_staying_in_a_bucket_is_not_a_fall(bucket: str) -> None:
    """The idempotence the sweep depends on, and the database also enforces.

    Two scores inside one bucket differ numerically and mean the same thing.
    Recording that as a transition would make a sweep that ran twice look like a
    claim that decayed twice, which is why `ck_trust_transition_moved` refuses a
    row whose buckets are equal.
    """
    assert not fell(bucket, bucket)


def test_the_ranking_covers_every_published_bucket() -> None:
    """Derived from the published bounds rather than restated.

    A sixth bucket added to `BUCKET_LOWER_BOUNDS` orders itself here; a `fell`
    that carried its own list would raise `KeyError` on the new value, in a
    sweep, in production.
    """
    for name, _ in BUCKET_LOWER_BOUNDS:
        assert fell(BUCKET_CONFIRMED, name) or name == BUCKET_CONFIRMED


def test_the_fall_is_measured_in_buckets_and_not_in_points() -> None:
    """0.86 to 0.85 is a bigger numeric move than 0.84 to 0.71, and only the
    first crosses a boundary. Filtering on the number would record movement no
    consumer can act on."""
    assert fell(bucket_for(0.86), bucket_for(0.85)) is False
    assert fell(bucket_for(0.86), bucket_for(0.84)) is True
    assert fell(bucket_for(0.84), bucket_for(0.71)) is False
