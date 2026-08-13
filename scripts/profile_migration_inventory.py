#!/usr/bin/env python
"""Report what a profile migration must account for, and refuse an incomplete count.

The inventory's value is entirely in its completeness. A report listing what it
found is indistinguishable from one that never looked, so this script enumerates
every category the migration defines and fails when one is missing — `--check` is
that refusal in machine-readable form.

    python scripts/profile_migration_inventory.py           # the inventory
    python scripts/profile_migration_inventory.py --check   # exit 1 if incomplete
"""

from __future__ import annotations

import argparse
import json
import sys

from contextplane.profile.migration import (
    INVENTORY_CATEGORIES,
    IncompleteInventory,
    empty_inventory,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit non-zero if the inventory is incomplete")
    parser.add_argument("--json", action="store_true", help="machine-readable inventory")
    args = parser.parse_args(argv)

    # No database is contacted here. The counts a real run needs come from the
    # deployment being migrated, and this command's job in CI is to prove the
    # category set itself is complete and self-consistent — the part that can be
    # wrong in the repository rather than in the data.
    inventory = empty_inventory()

    try:
        inventory.assert_complete()
    except IncompleteInventory as incomplete:
        print(f"migration inventory: {incomplete}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"categories": sorted(inventory.counts), "counts": dict(inventory.counts)}, indent=2))
    else:
        print(f"migration inventory: {len(INVENTORY_CATEGORIES)} categor(ies) accounted for")
        for category in sorted(inventory.counts):
            print(f"  {category}: {inventory.counts[category]}")
        print(
            "\nCounts are taken from the deployment being migrated. This run proves the category set is "
            "complete; it does not claim the graph is empty."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
