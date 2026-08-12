"""Re-export of the lifecycle comparison library, which lives under ``scripts/``.

The library is shared by two ``scripts/`` entry points and by this suite's unit
tests. It lives under ``scripts/`` because ``scripts/`` must not import from
``tests/`` — the dependency runs the other way, the same direction as
``tests/helpers/pg_provider.py`` importing ``scripts.devstack``. This module
keeps the test-facing import path stable so callers need not know where the
implementation sits.
"""

from __future__ import annotations

from scripts.lifecycle_measurement import (
    AFTER,
    BEFORE,
    IMPLEMENTATION_ADDITION_PATHS,
    LIFECYCLE_OWNED_PATHS,
    MAX_CRITICAL_PATH_SECONDS,
    MEASUREMENT_ONLY_PATHS,
    MINIMUM_REDUCTION_SECONDS,
    REQUIRED_RUNS_PER_SIDE,
    ChecksumMismatch,
    Classification,
    Comparison,
    DeltaEntry,
    IncompleteEvidence,
    LifecycleError,
    ModuleTiming,
    Outcome,
    Provenance,
    ProvenanceMismatch,
    Run,
    StaleEvidence,
    assignments,
    balanced_critical_path,
    build_comparison,
    canonical_json,
    classify_delta,
    cohort_digest,
    cohort_modules,
    load_bundle,
    manifest_checksum,
    outcome_consistency_failures,
    serial_critical_path,
    source_delta_manifest,
    validate_sides,
    write_bundle,
)

__all__ = [
    "AFTER",
    "BEFORE",
    "ChecksumMismatch",
    "Classification",
    "Comparison",
    "DeltaEntry",
    "IMPLEMENTATION_ADDITION_PATHS",
    "IncompleteEvidence",
    "LIFECYCLE_OWNED_PATHS",
    "LifecycleError",
    "MAX_CRITICAL_PATH_SECONDS",
    "MEASUREMENT_ONLY_PATHS",
    "MINIMUM_REDUCTION_SECONDS",
    "ModuleTiming",
    "Outcome",
    "Provenance",
    "ProvenanceMismatch",
    "REQUIRED_RUNS_PER_SIDE",
    "Run",
    "StaleEvidence",
    "assignments",
    "balanced_critical_path",
    "build_comparison",
    "canonical_json",
    "classify_delta",
    "cohort_digest",
    "cohort_modules",
    "load_bundle",
    "manifest_checksum",
    "outcome_consistency_failures",
    "serial_critical_path",
    "source_delta_manifest",
    "validate_sides",
    "write_bundle",
]
