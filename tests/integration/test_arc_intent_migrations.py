"""The ARC selector rename, both directions, against real Postgres.

Three properties are worth a database rather than a mock. The closed-value
CHECK constraints only exist there, so a value the migration forgot to move is
a constraint violation nothing in Python would notice. The column and index
renames only mean something if the GIN index survives them. And the obligation
snapshot's digest is a dedup key -- recomputing it wrongly does not raise, it
silently splits one obligation into two.

The cycle here is head -> down -> up rather than a hand-seeded pre-cutover
fixture. The downgrade is what produces genuinely pre-cutover-shaped rows, so
upgrading them afterwards exercises the real-world direction on data this test
did not have to spell correctly by hand -- and comparing the far end against
the near end is the "preserved values" check the rollback design asks for.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

_PRE_CUTOVER = "0053_ownership_and_grants"
_CUTOVER = "0049_arc_intent_nomenclature"
#: What a downgrade must be undone *to*. Deliberately `head` rather than
#: `_CUTOVER`: the two were the same revision when this file was written, and
#: `0054`/`0055` landing on top silently turned "come back" into "come back two
#: revisions short". The shared database was then left below head for every
#: later test on the worker, and `0054`'s own downgrade had recreated
#: `audit_log_new` -- which is what failed the partition cutover test 200 nodes
#: later, naming neither this fixture nor this file.
_RESTORE = "head"

_KINDS = ["code_change", "deployment"]


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _alembic(url: str, *args: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env={"DATABASE_URL": url, "PATH": "/usr/bin:/bin"},
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} failed:\n{completed.stdout}\n{completed.stderr}")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


def _digest(snapshot: dict[str, object]) -> str:
    """The production dedup key, recomputed here from the same rule rather
    than imported -- an import would make this test agree with the service by
    construction instead of checking that it does."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def seeded(sync_engine: Engine) -> dict[str, uuid.UUID]:
    """One tenant's worth of ARC scaffolding plus a rule and an obligation,
    seeded at the post-cutover spelling the tree currently carries."""
    ids = {name: uuid.uuid4() for name in ("tenant", "artifact", "revision", "directive", "rule", "obligation")}
    snapshot: dict[str, object] = {
        "scope": "intent",
        "target_tenant_id": None,
        "capability_ids": [],
        "domain_ids": [],
        "intent_kinds": sorted(_KINDS),
        "action_classes": [],
        "environments": [],
        "data_sensitivity_tiers": [],
    }
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, now(), TRUE)"
            ),
            {"t": ids["tenant"], "s": f"idr-{ids['tenant'].hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:a, :t, :s, 'policy', 'Intent rename fixture', now(), 'https://idp.example.test', 'seed')"
            ),
            {"a": ids["artifact"], "t": ids["tenant"], "s": f"a-{ids['artifact'].hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
                ") VALUES (:r, :a, :t, 'test-system', :loc, :rloc, :dig, 'active', now() - interval '1 day',"
                "  now() + interval '365 days', 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "  now() + interval '730 days', 'none', 'body', now())"
            ),
            {
                "r": ids["revision"],
                "a": ids["artifact"],
                "t": ids["tenant"],
                "loc": f"loc://{ids['revision'].hex[:8]}",
                "rloc": f"loc://{ids['revision'].hex[:8]}@1",
                "dig": ids["revision"].hex * 2,
            },
        )
        conn.execute(
            text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:d, :a)"),
            {"d": ids["directive"], "a": ids["artifact"]},
        )
        conn.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  rule_id, revision_id, tenant_id, scope, intent_kinds, effective_from, is_mandatory"
                ") VALUES (:r, :rev, :t, 'intent', CAST(:k AS TEXT[]), now() - interval '1 day', TRUE)"
            ),
            {"r": ids["rule"], "rev": ids["revision"], "t": ids["tenant"], "k": _KINDS},
        )
        conn.execute(
            text(
                "INSERT INTO arc_mandatory_obligations ("
                "  obligation_id, artifact_id, directive_id, current_revision_id,"
                "  applicability_snapshot, applicability_digest, obligation_state, effective_from"
                ") VALUES (:o, :a, :d, :rev, CAST(:snap AS JSONB), :dig, 'satisfied', now() - interval '1 day')"
            ),
            {
                "o": ids["obligation"],
                "a": ids["artifact"],
                "d": ids["directive"],
                "rev": ids["revision"],
                "snap": json.dumps(snapshot),
                "dig": _digest(snapshot),
            },
        )
    return ids


def _rule_row(engine: Engine, rule_id: uuid.UUID, selector: str) -> tuple[str, list[str]]:
    with engine.begin() as conn:
        scope, kinds = conn.execute(
            text(f"SELECT scope, {selector} FROM arc_applicability_rules WHERE rule_id = :r"),
            {"r": rule_id},
        ).one()
    return str(scope), list(kinds)


def _obligation_row(engine: Engine, obligation_id: uuid.UUID) -> tuple[dict[str, object], str]:
    with engine.begin() as conn:
        snapshot, digest = conn.execute(
            text(
                "SELECT applicability_snapshot, applicability_digest "
                "FROM arc_mandatory_obligations WHERE obligation_id = :o"
            ),
            {"o": obligation_id},
        ).one()
    return dict(snapshot), str(digest)


@pytest.fixture
def at_pre_cutover(
    sync_engine: Engine, pg_container: str, seeded: dict[str, uuid.UUID]
) -> Iterator[dict[str, uuid.UUID]]:
    """Downgrade for the duration of one test, and always come back.

    The database is shared with the rest of the session, so leaving it one
    revision behind would fail every later test with an error that names a
    missing column rather than this fixture.
    """
    _alembic(pg_container, "downgrade", _PRE_CUTOVER)
    try:
        yield seeded
    finally:
        _alembic(pg_container, "upgrade", _RESTORE)


def test_downgrade_restores_the_task_spelling_of_the_column_and_its_values(
    sync_engine: Engine, at_pre_cutover: dict[str, uuid.UUID]
) -> None:
    scope, kinds = _rule_row(sync_engine, at_pre_cutover["rule"], "task_kinds")
    assert scope == "task"
    assert sorted(kinds) == sorted(_KINDS)
    assert "intent_kinds" not in {c["name"] for c in inspect(sync_engine).get_columns("arc_applicability_rules")}


def test_the_gin_selector_index_survives_the_rename_as_an_index_not_a_column_copy(
    sync_engine: Engine, at_pre_cutover: dict[str, uuid.UUID]
) -> None:
    """A rename that dropped and recreated the index would still leave *an*
    index here, so this asserts the access method too -- a b-tree on this
    column would satisfy a name check and quietly stop answering `&&`."""
    with sync_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT a.amname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_am a ON a.oid = c.relam "
                "WHERE c.relname = 'ix_arc_rules_task_kinds'"
            )
        ).one_or_none()
    assert row is not None, "the downgraded index name is missing"
    assert row[0] == "gin"


def test_the_downgraded_closed_scope_check_refuses_the_intent_spelling(
    sync_engine: Engine, at_pre_cutover: dict[str, uuid.UUID]
) -> None:
    """The check has to move with the values, not merely be recreated: a
    constraint left admitting both spellings would let the two vocabularies
    coexist, which is the state the one-to-one proof exists to prevent."""
    with sync_engine.begin() as conn, pytest.raises(Exception, match="ck_arc_rules_scope"):
        conn.execute(
            text("UPDATE arc_applicability_rules SET scope = 'intent' WHERE rule_id = :r"),
            {"r": at_pre_cutover["rule"]},
        )


def test_the_obligation_snapshot_and_its_digest_are_respelled_together(
    sync_engine: Engine, at_pre_cutover: dict[str, uuid.UUID]
) -> None:
    snapshot, digest = _obligation_row(sync_engine, at_pre_cutover["obligation"])
    assert "task_kinds" in snapshot
    assert "intent_kinds" not in snapshot
    assert snapshot["scope"] == "task"
    # The stored digest must be the one the service would compute from this
    # snapshot, not the one that was stored before the rewrite.
    assert digest == _digest(dict(snapshot))


def test_the_cycle_preserves_every_value_it_did_not_set_out_to_change(
    sync_engine: Engine, pg_container: str, seeded: dict[str, uuid.UUID]
) -> None:
    """down then up, compared against the near end. Selector membership, the
    obligation's digest and its non-nomenclature snapshot fields all have to
    come back identical; only the spelling was ever supposed to move."""
    before_scope, before_kinds = _rule_row(sync_engine, seeded["rule"], "intent_kinds")
    before_snapshot, before_digest = _obligation_row(sync_engine, seeded["obligation"])

    _alembic(pg_container, "downgrade", _PRE_CUTOVER)
    _alembic(pg_container, "upgrade", _RESTORE)

    after_scope, after_kinds = _rule_row(sync_engine, seeded["rule"], "intent_kinds")
    after_snapshot, after_digest = _obligation_row(sync_engine, seeded["obligation"])

    assert (after_scope, sorted(after_kinds)) == (before_scope, sorted(before_kinds))
    assert after_digest == before_digest
    assert after_snapshot == before_snapshot


def test_the_rename_refuses_rather_than_merging_when_both_spellings_are_present(
    sync_engine: Engine, pg_container: str, at_pre_cutover: dict[str, uuid.UUID]
) -> None:
    """The one-to-one precondition, made to fail on purpose.

    A pre-cutover database is not supposed to be able to hold `intent`
    anywhere -- that is what the closed checks are for -- so the colliding row
    has to be planted through a dropped constraint. Without that, this test
    would assert a refusal against a state it never actually built, which is
    the same as asserting nothing. If the upgrade merged instead of refusing,
    two distinct pre-cutover values would become one and the downgrade could
    not tell them apart.
    """
    with sync_engine.begin() as conn:
        conn.execute(text("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_scope"))
        conn.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  rule_id, revision_id, tenant_id, scope, task_kinds, effective_from, is_mandatory"
                ") VALUES (gen_random_uuid(), :rev, :t, 'intent', CAST(:k AS TEXT[]),"
                "  now() - interval '1 day', TRUE)"
            ),
            {"rev": at_pre_cutover["revision"], "t": at_pre_cutover["tenant"], "k": _KINDS},
        )

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", _CUTOVER],
        capture_output=True,
        text=True,
        env={"DATABASE_URL": pg_container, "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert completed.returncode != 0
    assert "not one-to-one" in completed.stderr + completed.stdout

    # The refusal has to have been total. The column rename is the migration's
    # first write, so if it happened the check ran too late to protect
    # anything -- asserting only on the error message would not catch that.
    columns = {c["name"] for c in inspect(sync_engine).get_columns("arc_applicability_rules")}
    assert "task_kinds" in columns
    assert "intent_kinds" not in columns

    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM arc_applicability_rules WHERE scope = 'intent'"))
        conn.execute(
            text(
                "ALTER TABLE arc_applicability_rules ADD CONSTRAINT ck_arc_rules_scope "
                "CHECK (scope IN ('global', 'tenant', 'domain', 'capability', 'task'))"
            )
        )
