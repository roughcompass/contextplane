#!/usr/bin/env python
"""Dry-run a profile migration forward or back, and refuse to activate on unresolved findings.

Dry run is the default and the only mode this script offers. Executing a migration
is an operator action taken with a plan in hand; a script that could do it with one
flag is one that eventually does it with one typo.

    python scripts/profile_migration_execute.py --direction forward
    python scripts/profile_migration_execute.py --direction rollback
"""

from __future__ import annotations

import argparse
import datetime
import sys

from contextplane.profile.migration import (
    MigrationPlan,
    MigrationRefused,
    empty_inventory,
)


def plan_for(direction: str) -> MigrationPlan:
    """The plan a run of this direction would execute.

    Findings come from the deployment. With none supplied the plan is empty, which
    is a plan that may activate — and the report says so explicitly rather than
    letting a clean run be read as a thorough one.
    """
    if direction not in {"forward", "rollback"}:
        msg = f"unknown direction {direction!r}; expected 'forward' or 'rollback'"
        raise MigrationRefused(msg)
    return MigrationPlan(inventory=empty_inventory(), findings=())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--direction", default="forward", choices=("forward", "rollback"))
    parser.add_argument(
        "--at",
        default=None,
        help="the instant to evaluate expiries against, ISO-8601; defaults to now",
    )
    args = parser.parse_args(argv)

    at = datetime.datetime.fromisoformat(args.at) if args.at else datetime.datetime.now(tz=datetime.UTC)

    try:
        plan = plan_for(args.direction)
        plan.assert_may_activate(at)
    except MigrationRefused as refused:
        print(f"migration {args.direction}: blocked — {refused}", file=sys.stderr)
        return 1

    warnings = plan.warnings(at)
    print(f"migration {args.direction}: dry run only; {plan.inventory.total} row(s) in scope")
    for warning in warnings:
        print(f"  warning: {warning}")
    print(
        "\nNo findings were supplied, so this run proves the plan's own consistency rather than the "
        "deployment's readiness. Supply the deployment's findings to evaluate that."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
