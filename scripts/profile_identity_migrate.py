#!/usr/bin/env python3
"""Drive the identity migration one phase at a time, and never past a refusal.

Expand, backfill, dual-read, dual-write, cut over. Each step is a subcommand, and
each refuses to run unless the tenant is in the phase immediately before it. That
is not ceremony: backfilling before expanding writes into columns that do not
exist, and cutting over before dual reading means nobody ever compared the two
answers. A driver that let an operator skip would turn every safety check into
one they had the option not to run.

**Nothing here rewrites an opaque ID.** Expansion and backfill only add rows to
the handle table. Every external mapping and dependent reference keeps resolving,
because the column they point at is never touched. This is the property the whole
migration is judged on, and the way it is achieved is by not having any code that
could violate it.

**Backfill is append-only and idempotent.** Re-running it adds nothing it has
already added, so an interrupted run is resumed rather than restarted, and a
resumed run cannot produce a second primary handle for a row that already has
one. The database enforces the same thing with a partial unique index; both exist
because an idempotence bug and an index are different failure modes.

**Rollback restores behaviour without deleting identifiers.** It moves the tenant
back to reading legacy names and leaves every handle, alias and mapping in place.
A rollback that deleted what it had created would make the second attempt start
from a different place than the first, and would throw away the alias somebody
had already begun using.

Every subcommand is `--dry-run` by default in the sense that matters: it reports
what it would write and writes nothing unless `--apply` is given. A migration
tool whose default is to act is one that acts by accident.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from contextplane.entities.identity import (
    HandleKind,
    Phase,
    assert_phase_transition,
    lookup_key_for,
)

# The namespace a tenant's own handles are published into. A single value rather
# than per-entity, because the namespace identifies the tenant's vocabulary and a
# row-level one would let two rows of the same type disagree about whose
# vocabulary they belong to.
DEFAULT_NAMESPACE = "tenant"


@dataclass(frozen=True)
class PlannedHandle:
    """One handle the backfill would write, before anything is written."""

    entity_id: str
    entity_type: str
    namespace: str
    handle_name: str

    @property
    def qualified(self) -> str:
        return f"{self.namespace}:{self.entity_type}/{self.handle_name}"

    @property
    def lookup_key(self) -> str:
        return lookup_key_for(self.namespace, self.entity_type, self.handle_name)


def plan_backfill(
    rows: Sequence[tuple[str, str, str]],
    *,
    existing_keys: Sequence[str] = (),
    namespace: str = DEFAULT_NAMESPACE,
) -> list[PlannedHandle]:
    """What the backfill would add, given the entities and what already exists.

    Pure, and separated from the writing for two reasons: it is the part with the
    interesting behaviour, and `--apply` should execute exactly what a dry run
    printed rather than recompute it and hope the two agree.

    Idempotence lives here. A row whose key is already present is skipped, so a
    resumed run adds only the remainder — and because the skip is by key rather
    than by position, resuming after an interruption at any point converges on
    the same set.
    """
    already = {key.lower() for key in existing_keys}
    planned: list[PlannedHandle] = []
    seen: set[str] = set()
    for entity_id, entity_type, name in rows:
        if not entity_type.strip() or not name.strip():
            # The preflight blocks on these. Skipping rather than raising keeps a
            # partially-dirty tenant migratable for the rows that are clean, and
            # the preflight is where the operator is told what was left behind.
            continue
        handle = PlannedHandle(entity_id=entity_id, entity_type=entity_type, namespace=namespace, handle_name=name)
        if handle.lookup_key in already or handle.lookup_key in seen:
            continue
        seen.add(handle.lookup_key)
        planned.append(handle)
    return planned


_SELECT_ENTITIES = """
SELECT entity_id::text, entity_type, name
  FROM entities
 WHERE tenant_id = %(tenant)s AND is_active
 ORDER BY entity_id
"""

_SELECT_ACTIVE_KEYS = """
SELECT lookup_key FROM entity_handles WHERE tenant_id = %(tenant)s AND valid_to IS NULL
"""

# Append-only: no UPDATE, no DELETE, and `ON CONFLICT DO NOTHING` so a concurrent
# writer that got there first is not an error. The partial unique index is what
# makes that conflict detectable at all.
_INSERT_HANDLE = """
INSERT INTO entity_handles (
    handle_id, tenant_id, entity_id, entity_type, namespace, handle_name,
    qualified_handle, lookup_key, kind, valid_from, source, recorded_at
) VALUES (
    %(handle_id)s, %(tenant)s, %(entity_id)s, %(entity_type)s, %(namespace)s, %(handle_name)s,
    %(qualified)s, %(lookup_key)s, %(kind)s, now(), %(source)s, now()
)
ON CONFLICT DO NOTHING
"""


def _connect(database_url: str):  # type: ignore[no-untyped-def]  # driver is untyped; see preflight for the same shape
    import psycopg2  # type: ignore[import-untyped]  # noqa: PLC0415 - deferred so a missing driver is a message, not a traceback

    return psycopg2.connect(database_url)


def _read_state(cursor, tenant: str) -> tuple[list[tuple[str, str, str]], list[str]]:  # type: ignore[no-untyped-def]
    cursor.execute(_SELECT_ENTITIES, {"tenant": tenant})
    rows = [(str(row[0]), str(row[1] or ""), str(row[2] or "")) for row in cursor.fetchall()]
    cursor.execute(_SELECT_ACTIVE_KEYS, {"tenant": tenant})
    keys = [str(row[0]) for row in cursor.fetchall()]
    return rows, keys


def backfill(
    database_url: str,
    tenant: str,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    apply: bool = False,
) -> list[PlannedHandle]:
    """Plan, then optionally write, the primary handles for one tenant.

    The plan is computed from a single read of both tables so the decision and
    the write see one state. Reading entities, deciding, then reading handles
    would leave a window where a handle appeared between the two and the write
    tried to add it a second time.
    """
    connection = _connect(database_url)
    try:
        with connection, connection.cursor() as cursor:
            rows, keys = _read_state(cursor, tenant)
            planned = plan_backfill(rows, existing_keys=keys, namespace=namespace)
            if not apply:
                return planned
            for handle in planned:
                cursor.execute(
                    _INSERT_HANDLE,
                    {
                        "handle_id": str(uuid.uuid4()),
                        "tenant": tenant,
                        "entity_id": handle.entity_id,
                        "entity_type": handle.entity_type,
                        "namespace": handle.namespace,
                        "handle_name": handle.handle_name,
                        "qualified": handle.qualified,
                        "lookup_key": handle.lookup_key,
                        "kind": HandleKind.PRIMARY.value,
                        "source": "identity-backfill",
                    },
                )
    finally:
        connection.close()
    return planned


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write. Without it the command reports what it would write and writes nothing.",
    )
    parser.add_argument(
        "--from-phase",
        required=True,
        choices=[phase.value for phase in Phase],
        help="The phase the tenant is in now. Checked against the step requested.",
    )
    parser.add_argument("step", choices=["expand", "backfill", "dual-read", "dual-write", "cut-over", "rollback"])
    return parser


_STEP_TARGET = {
    "expand": Phase.EXPANDED,
    "backfill": Phase.BACKFILLED,
    "dual-read": Phase.DUAL_READ,
    "dual-write": Phase.DUAL_WRITE,
    "cut-over": Phase.CUT_OVER,
    "rollback": Phase.ROLLED_BACK,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    current = Phase(arguments.from_phase)
    target = _STEP_TARGET[arguments.step]

    try:
        assert_phase_transition(current, target)
    except ValueError as error:
        print(f"identity migrate: refusing {arguments.step}: {error}", file=sys.stderr)
        return 2

    if arguments.step == "backfill":
        planned = backfill(
            arguments.database_url,
            arguments.tenant,
            namespace=arguments.namespace,
            apply=arguments.apply,
        )
        verb = "wrote" if arguments.apply else "would write"
        print(f"identity migrate: {verb} {len(planned)} primary handle(s)")
        for handle in planned[:20]:
            print(f"  {handle.qualified} -> {handle.entity_id}")
        if len(planned) > 20:
            print(f"  ... and {len(planned) - 20} more")
        return 0

    # The remaining steps change no rows: expansion is the migration that already
    # ran, and the read/write/cutover phases are runtime state a deployment
    # carries. The command exists so the phase order is checked in one place
    # rather than trusted to whoever is following a runbook.
    print(f"identity migrate: {current.value} -> {target.value} is a permitted transition; no rows change here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
