"""Re-export of the template-database layer, which lives under ``scripts/``.

Fingerprinting, the canonical schema digest, and the migrated template a run
clones from are shared by the parent-side runner and this suite's helpers and
tests. It lives under ``scripts/`` because ``scripts/`` must not import from
``tests/`` — the dependency runs the other way, the same direction as
``tests/helpers/pg_provider.py`` importing ``scripts.devstack``. This module
keeps the test-facing import path stable so callers need not know where the
implementation sits.
"""

from __future__ import annotations

from scripts.pg_template import (
    DateRolloverError,
    SchemaDigestMismatch,
    SchemaEnvironment,
    ServerVersions,
    TemplateError,
    TemplateIdentity,
    advisory_lock_key,
    alembic_heads,
    assert_no_rollover,
    canonical_schema_digest,
    catalog_queries,
    compute_fingerprint,
    fingerprint_inputs,
    migration_environment,
    migration_transitive_sources,
    revision_chain,
    revision_heads,
    run_migrations,
    template_name,
    utc_date,
)

__all__ = [
    "DateRolloverError",
    "SchemaDigestMismatch",
    "SchemaEnvironment",
    "ServerVersions",
    "TemplateError",
    "TemplateIdentity",
    "advisory_lock_key",
    "alembic_heads",
    "assert_no_rollover",
    "canonical_schema_digest",
    "catalog_queries",
    "compute_fingerprint",
    "fingerprint_inputs",
    "migration_environment",
    "migration_transitive_sources",
    "revision_chain",
    "revision_heads",
    "run_migrations",
    "template_name",
    "utc_date",
]
