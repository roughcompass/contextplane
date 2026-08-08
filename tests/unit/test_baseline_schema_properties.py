"""Schema properties the pre-squash migration chain asserted, preserved.

`tests/unit/test_migrations.py` used to load five phase-named migration
modules (0006, 0007, 0009, 0014, 0018, 0019 — spread across
`TestP7*`/`TestMig0014*`/`TestMig0018*`/`TestMig0019*` classes) directly from
their file paths and captured the SQL their `upgrade()`/`downgrade()`
functions emitted through a mocked `op`. That whole mechanism — per-migration
module loading, SQL-string capture, drop-order assertions on a chain of ADD
COLUMN statements — is obsolete by construction once there is one baseline
revision instead of 47: there is no discrete "0007's upgrade()" to load
anymore, because 0007's tables are just part of the baseline's one CREATE
TABLE each.

What is not obsolete is the *shape* those tests were protecting: constraints,
partitioning, bi-temporal columns, and null-pairing rules that are still true
of the schema today. Each is re-asserted here against the baseline module's
DDL text — the same no-DB, import-and-regex approach
`tests/conformance/test_usage_schema.py` already uses for the same reason:
this runs in CI with no container.

Two migrations' tests are *not* carried forward at all, because the feature
they tested was withdrawn and nothing in the final schema corresponds to it:
`capability_annotations` (0018) and `workspace_shares` /
`workspace_share_acceptances` (created in 0019, dropped again in 0020) do not
exist in the baseline. There is no property to preserve for a table that was
never in the schema this migration produces.
"""

from __future__ import annotations

import importlib
import re

_MODULE_NAME = "contextplane.storage.migrations.versions.0001_baseline_schema"
_mig = importlib.import_module(_MODULE_NAME)


# ---------------------------------------------------------------------------
# Graph primitives (formerly 0007)
# ---------------------------------------------------------------------------


def test_pii_detection_log_is_partitioned_by_range() -> None:
    assert "PARTITION BY RANGE (ts)" in _mig._PII_DETECTION_LOG_DDL


def test_closure_cache_direction_is_check_constrained() -> None:
    assert "CHECK (direction IN ('forward', 'reverse'))" in _mig._CLOSURE_CACHE_DDL


def test_edge_property_schemas_is_bitemporal() -> None:
    for column in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
        assert column in _mig._EDGE_PROPERTY_SCHEMAS_DDL


def test_entity_external_ids_has_no_bitemporal_invalidation_column() -> None:
    """Hard-delete only — a mapping to another system's identifier is removed,
    not soft-invalidated, so there is no t_invalidated_at to preserve."""
    assert "t_invalidated_at" not in _mig._ENTITY_EXTERNAL_IDS_DDL


def test_pii_patterns_carries_is_system_and_detector_module() -> None:
    ddl = _mig._PII_PATTERNS_DDL
    assert "is_system" in ddl
    assert "detector_module" in ddl
    # An entropy-based pattern (no single regex) is the only kind allowed a
    # detector_module.
    assert "detector_module IS NULL OR regex = '__entropy__'" in ddl


def test_edge_rel_seeds_present() -> None:
    seeded = {value for kind, value in _mig._VOCAB_SEEDS if kind == "edge_rel"}
    for value in ("requires", "conflicts_with", "composes", "provides_to", "instance_of"):
        assert value in seeded


def test_entity_type_integration_seed_present() -> None:
    seeded = {value for kind, value in _mig._VOCAB_SEEDS if kind == "entity_type"}
    assert "integration" in seeded


def test_all_pii_category_seeds_present() -> None:
    seeded = {value for kind, value in _mig._VOCAB_SEEDS if kind == "pii_category"}
    assert seeded == {
        "email",
        "phone",
        "ssn",
        "aws_access_key",
        "aws_secret_key",
        "jwt_token",
        "credit_card",
    }


def test_all_seven_system_pii_patterns_seeded() -> None:
    assert len(_mig._SYSTEM_PII_PATTERNS) == 7
    assert len(_mig._SYSTEM_PII_PATTERN_IDS) == 7
    assert len(set(_mig._SYSTEM_PII_PATTERN_IDS.values())) == 7, "pattern ids must be unique"


def test_aws_secret_key_uses_entropy_sentinel_and_detector_module() -> None:
    by_name = {name: (regex, module) for name, _category, regex, module in _mig._SYSTEM_PII_PATTERNS}
    regex, module = by_name["aws_secret_key"]
    assert regex == "__entropy__"
    assert module is not None


def test_non_entropy_patterns_have_no_detector_module() -> None:
    for name, _category, _regex, module in _mig._SYSTEM_PII_PATTERNS:
        if name == "aws_secret_key":
            continue
        assert module is None, f"{name} is not entropy-based and must not carry a detector_module"


# ---------------------------------------------------------------------------
# Provider/consumer model (formerly 0009), and the entities.visibility rename
# it and 0014 both touch (formerly TestP7* / TestMig0014*)
# ---------------------------------------------------------------------------


def test_entities_visibility_check_constraint_is_the_post_rename_set() -> None:
    """`public-in-fabric` was renamed to `public` (formerly migration 0014).
    The baseline creates the column with the final three-value set directly —
    there is no separate rename step to test, only the end state."""
    assert "CHECK (visibility IN ('private', 'tenant-shared', 'public'))" in _mig._ENTITIES_DDL


def test_tenants_is_regulated_and_digest_window_columns() -> None:
    ddl = _mig._TENANTS_DDL
    assert "is_regulated" in ddl
    assert "CHECK (\n        notification_digest_window IN ('none', '5m', '15m', '1h', '6h', '24h')\n    )" in ddl or (
        "notification_digest_window IN" in ddl
        and all(v in ddl for v in ("'none'", "'5m'", "'15m'", "'1h'", "'6h'", "'24h'"))
    )


def test_adoption_events_is_bitemporal_with_a_deferrable_unique_constraint() -> None:
    ddl = _mig._ADOPTION_EVENTS_DDL
    for column in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
        assert column in ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl


def test_subscriptions_is_bitemporal_with_a_digest_window_column() -> None:
    ddl = _mig._SUBSCRIPTIONS_DDL
    for column in ("t_valid_from", "t_valid_to", "t_ingested_at", "t_invalidated_at"):
        assert column in ddl
    assert "digest_window" in ddl


def test_notifications_and_deliveries_are_partitioned_monthly() -> None:
    assert "PARTITION BY RANGE (ts)" in _mig._NOTIFICATIONS_DDL
    assert "PARTITION BY RANGE (ts)" in _mig._NOTIFICATION_DELIVERIES_DDL


def test_integration_pairs_enforces_canonical_pair_order() -> None:
    assert "CHECK (capability_a_id < capability_b_id)" in _mig._INTEGRATION_PAIRS_DDL


def test_integration_pairs_trigger_function_has_no_visibility_filter_by_design() -> None:
    """Documented rather than incidental: visibility enforcement belongs at
    the service layer, so every consumer of the data shares one chokepoint."""
    assert "no visibility filter" in _mig._INTEGRATION_PAIRS_TRIGGER_FUNC


def test_integration_pairs_trigger_is_registered_after_insert_on_edges() -> None:
    assert "AFTER INSERT ON edges" in _mig._INTEGRATION_PAIRS_TRIGGER
    assert "populate_integration_pairs" in _mig._INTEGRATION_PAIRS_TRIGGER


# ---------------------------------------------------------------------------
# Workspaces (formerly 0019) — capability_annotations (0018) and
# workspace_shares / workspace_share_acceptances have no equivalent below;
# see the module docstring.
# ---------------------------------------------------------------------------


def test_workspaces_encryption_tier_check_constraint() -> None:
    ddl = _mig._WORKSPACES_DDL
    for tier in ("none", "paas_tenant_key", "aws_kms", "azure_key_vault", "gcp_kms", "hashicorp_vault"):
        assert f"'{tier}'" in ddl


def test_workspaces_owner_kind_constraints() -> None:
    ddl = _mig._WORKSPACES_DDL
    assert "chk_owner_kind" in ddl
    assert "chk_actor_owner" in ddl


def test_workspace_entries_kind_check_excludes_private_annotation() -> None:
    """`private_annotation` was removed from the vocabulary (formerly
    migration 0041) because it never carried behaviour distinct from `note`."""
    ddl = _mig._WORKSPACE_ENTRIES_DDL
    match = re.search(r"CHECK \(\s*kind IN \(([^)]*)\)\s*\)", ddl)
    assert match, "could not locate the chk_entry_kind CHECK body"
    allowed = {v.strip().strip("'") for v in match.group(1).split(",")}
    assert allowed == {"note", "decision", "open_question", "saved_query", "saved_view"}


def test_workspace_entries_fts_index_is_gin_over_tsvector() -> None:
    assert any("USING GIN (to_tsvector" in stmt for stmt in _mig._WORKSPACE_ENTRIES_INDEXES)


def test_workspace_entries_references_index_is_gin() -> None:
    assert any("idx_we_refs" in stmt and "USING GIN" in stmt for stmt in _mig._WORKSPACE_ENTRIES_INDEXES)


def test_no_encryption_retrofit_columns_on_workspace_tables() -> None:
    """The encryption retrofit that was once planned for workspaces never
    shipped. No ciphertext, nonce, or key-reference column should exist on
    either table."""
    forbidden = ("ciphertext", "_nonce", "kek_id", "wrapped_dek")
    for ddl in (_mig._WORKSPACES_DDL, _mig._WORKSPACE_ENTRIES_DDL):
        for token in forbidden:
            assert token not in ddl, f"unexpected encryption-retrofit column fragment {token!r} in workspace DDL"


def test_workspace_tables_are_not_bitemporal() -> None:
    """Unlike entities/attributes/facts, workspaces and workspace_entries were
    deliberately never given t_valid_from/t_valid_to — they use a single
    t_invalidated_at soft-delete instead."""
    for ddl in (_mig._WORKSPACES_DDL, _mig._WORKSPACE_ENTRIES_DDL):
        assert "t_valid_from" not in ddl
        assert "t_valid_to" not in ddl


def test_workspace_owner_kind_is_immutable_after_creation() -> None:
    """The final trigger (workspace sharing's cross-tenant-aware predecessor
    was replaced when workspace_shares was dropped) rejects any change to
    owner_kind, full stop."""
    assert "owner_kind is immutable after creation" in _mig._WORKSPACE_OWNER_KIND_IMMUTABLE_FUNC
    assert "BEFORE UPDATE ON workspaces" in _mig._WORKSPACE_OWNER_KIND_IMMUTABLE_TRIGGER
