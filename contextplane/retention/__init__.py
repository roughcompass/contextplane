"""Retention, erasure propagation, and the derivatives that inherit both.

What outlives what is policy, and policy is versioned rather than edited: a
correction is a new policy version plus re-propagation, so every tombstone and
every registered derivative names the version it was decided under and stays
readable when the values move.

This package holds the storage those decisions are recorded in. It imports
`storage` and below and nothing else — the erasure writers, propagation workers,
and aggregate readers that consume these tables all live above it and import
downward.
"""

from __future__ import annotations

from contextplane.retention.models import (
    DerivativeRegistration,
    DerivativeSourceLink,
    DerivativeWorkItem,
    PrivacyAggregate,
    RetentionPolicy,
    SourceTombstone,
)

__all__ = [
    "DerivativeRegistration",
    "DerivativeSourceLink",
    "DerivativeWorkItem",
    "PrivacyAggregate",
    "RetentionPolicy",
    "SourceTombstone",
]
