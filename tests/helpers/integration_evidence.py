"""Re-export of the integration evidence layer, which lives under ``scripts/``.

Write-once evidence is shared by the inner runner, the outer performance-gate
controller, the evidence verifier, and this suite's unit tests. It lives under
``scripts/`` because ``scripts/`` must not import from ``tests/`` — the
dependency runs the other way, the same direction as
``tests/helpers/pg_provider.py`` importing ``scripts.devstack``. This module
keeps the test-facing import path stable so callers need not know where the
implementation sits.
"""

from __future__ import annotations

from scripts.integration_evidence import (
    EvidenceError,
    EvidenceWriter,
    RunDirectory,
    SecretLeak,
    assert_no_secrets,
    atomic_write,
    create_run_directory,
    read_manifest,
    run_scoped_digest,
    sha256_file,
    sha256_text,
    verify_manifest,
)

__all__ = [
    "EvidenceError",
    "EvidenceWriter",
    "RunDirectory",
    "SecretLeak",
    "assert_no_secrets",
    "atomic_write",
    "create_run_directory",
    "read_manifest",
    "run_scoped_digest",
    "sha256_file",
    "sha256_text",
    "verify_manifest",
]
