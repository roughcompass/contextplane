"""The ARC front-door contract's source list is the package tree, measured — not a list someone kept up.

`lint-imports` only inspects what a contract names. The layers contract says
`exhaustive = true`, so import-linter itself fails the build on a top-level
package no layer places. The forbidden contract that keeps ARC's internals
private has no such flag: it takes `source_modules` literally, and a package
absent from that list is not "allowed" — it is unexamined, and the run still
reports KEPT.

That distinction has already cost this codebase a real violation. The list
was first written as the packages that imported ARC at the time, and
`contextplane.workspaces` then grew a direct `arc.service.receipt_read`
import that no source line covered. The contract passed over it. Widening
the list to the whole package tree fixed that instance; it does not stop the
next one, because a hand-written list of twenty-three names goes stale the
same way a hand-written list of six did, just later.

So this test is the flag import-linter does not offer: the source list must
equal the top-level names under `contextplane/`, minus two exemptions that
are structural rather than editorial. Adding `contextplane/foo/` fails here
until `foo` is named in the contract, which is the same moment the layers
contract makes somebody decide where `foo` sits.

The exemptions are not a general escape hatch, which is why they are asserted
rather than subtracted silently. `contextplane.arc` cannot be a source of a
contract forbidding ARC's internals — its own submodules import each other,
and a package forbidden from itself cannot be written. `contextplane.storage`
is out because `storage/migrations/env.py` imports `contextplane.arc.models`
so Alembic's autogenerate sees the `arc_`-prefixed tables; naming storage
here and exempting that import again would state the same fact twice.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from checklib import repo_root

_CONTRACT_NAME = "ARC internals are private; consumers use the contextplane.arc front door"

# Named, not derived: an exemption that computes itself is an exemption nobody reviews.
_EXEMPT = {"contextplane.arc", "contextplane.storage"}


def _top_level_modules(package_dir: Path) -> set[str]:
    """Every top-level module and package under `contextplane/`, as import-linter names them."""
    names: set[str] = set()
    for entry in package_dir.iterdir():
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_dir() and (entry / "__init__.py").is_file():
            names.add(f"contextplane.{entry.name}")
        elif entry.suffix == ".py":
            names.add(f"contextplane.{entry.stem}")
    return names


def _arc_contract() -> dict[str, object]:
    root = repo_root()
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    contracts = config["tool"]["importlinter"]["contracts"]
    matches = [c for c in contracts if c["name"] == _CONTRACT_NAME]
    assert len(matches) == 1, f"expected exactly one contract named {_CONTRACT_NAME!r}, found {len(matches)}"
    return matches[0]


def test_the_front_door_contract_watches_every_package_except_its_two_exemptions() -> None:
    """A new top-level package fails this test until the contract names it."""
    root = repo_root()
    expected = _top_level_modules(root / "contextplane") - _EXEMPT
    declared = set(_arc_contract()["source_modules"])  # type: ignore[arg-type]

    unwatched = expected - declared
    assert not unwatched, (
        f"{sorted(unwatched)} exist under contextplane/ but are not source_modules of the ARC "
        "front-door contract, so lint-imports will not look at them. Add them to the contract, "
        "or add a reasoned exemption to this test's _EXEMPT alongside the comment saying why."
    )

    stale = declared - expected
    assert not stale, (
        f"{sorted(stale)} are named as source_modules but no longer exist under contextplane/. "
        "A source that matches nothing is dead weight that outlives its reason."
    )


def test_the_exemptions_are_the_two_that_are_structural() -> None:
    """The exemptions stay reviewed: growing this set is a decision, not an edit that passes quietly."""
    root = repo_root()
    present = _top_level_modules(root / "contextplane")

    assert _EXEMPT <= present, f"exempted names that no longer exist: {sorted(_EXEMPT - present)}"
    assert _EXEMPT == {"contextplane.arc", "contextplane.storage"}
