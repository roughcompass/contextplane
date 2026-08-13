"""Every container field belongs to exactly one area, and the areas agree with it.

The composition root no longer enumerates services. It expands each area's
frozen container into `Services` by field name, which buys the property the
whole decomposition exists for -- adding a service to an area touches that
area's `wiring.py` and the container's field list, and nothing in
`contextplane/wiring/services.py` -- at the cost of the one thing enumeration
gave for free: mypy checking that every field is supplied, exactly once, with
the right type.

This file buys that back statically. It reads the area containers and the
container declaration and asserts the three things the expansion assumes:

1. **Coverage.** Every `Services` field is supplied by exactly one area, or by
   the short, named set the root passes explicitly. A field nobody supplies is
   a `TypeError` at app startup; a field two areas both supply is a `TypeError`
   about a duplicate keyword. Both are loud, but both are loud at *startup*,
   and this says so at test time with the field's name.
2. **Type agreement.** An area declaring `catalog: CatalogService` where the
   container declares something else would type-check inside the area and fail
   nowhere until a caller used the wrong object.
3. **The exit property itself.** The `Services(...)` call names only the
   fields the root supplies itself and expands every area, so a new service in
   an existing area has nothing to add to `wiring/services.py`.

Comparing annotations rather than runtime types is deliberate: these are the
declarations a reader and mypy both work from, and two dataclasses agreeing on
`RetrievalService` is exactly the claim being checked.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from contextplane.api.container import Services
from contextplane.arc.wiring import ArcServices
from contextplane.ownership.wiring import OwnershipArea
from contextplane.profile.wiring import ProfileArea
from contextplane.service.catalog.wiring import CatalogServices
from contextplane.service.governance.wiring import GovernanceServices
from contextplane.service.memory.wiring import MemoryServices
from contextplane.service.notifications.wiring import NotificationServices
from contextplane.service.retrieval.wiring import RetrievalServices
from contextplane.usage.wiring import UsageServices
from contextplane.wiring.stages import AuthContext
from contextplane.workspaces.wiring import LayeredContextServices

#: Every area container expanded into `Services` by the composition root, by
#: the module a reader would go to in order to add a service to it.
_EXPANDED_AREAS = {
    "contextplane/service/governance/wiring.py": GovernanceServices,
    "contextplane/service/notifications/wiring.py": NotificationServices,
    "contextplane/service/retrieval/wiring.py": RetrievalServices,
    "contextplane/service/catalog/wiring.py": CatalogServices,
    "contextplane/service/memory/wiring.py": MemoryServices,
    "contextplane/arc/wiring.py": ArcServices,
    "contextplane/wiring/stages.py": AuthContext,
    "contextplane/workspaces/wiring.py": LayeredContextServices,
    "contextplane/ownership/wiring.py": OwnershipArea,
    "contextplane/profile/wiring.py": ProfileArea,
}

#: Every area container, including the one the root does not expand. Usage is
#: constructed by its own registration point like every other area, but its
#: single field is threaded back through `contextplane.main`'s lifespan --
#: which owns the writer's drain task, starting it once there is a loop and
#: stopping it with a final flush -- so the root receives the writer as a
#: parameter and names it. The name/type agreement below still applies.
_AREA_CONTAINERS = {**_EXPANDED_AREAS, "contextplane/usage/wiring.py": UsageServices}

#: The fields the composition root passes to `Services(...)` by name, in four
#: categories. Infrastructure it holds itself (`settings`, `engine`,
#: `session_factory`, `scheduler`) plus the one clock every area shares;
#: `usage_writer`, the lifespan-threaded field described above;
#: `workspace_service` and `erasure`, built by
#: `contextplane.wiring.routes.register` after the router table is mounted; and
#: `signal_ingest`, a service whose domain package has no area `wiring.py` at
#: all, where inventing one to hold a single stateless service would add a
#: module for the sake of symmetry.
#:
#: That fourth category is the one to argue with before adding to it. A service
#: that has an area belongs in that area's container, and one whose area is
#: merely inconvenient to build in should get the inconvenience fixed instead --
#: which is why this set is pinned rather than derived: growing it is a decision
#: somebody made and stated, not a line that appeared.
_ROOT_SUPPLIED = frozenset(
    {
        "settings",
        "engine",
        "session_factory",
        "scheduler",
        "clock",
        "usage_writer",
        "workspace_service",
        "erasure",
        "signal_ingest",
    }
)

_SERVICES_PATH = Path(__file__).resolve().parents[2] / "contextplane" / "wiring" / "services.py"


def _annotations(container: type) -> dict[str, str]:
    return {f.name: str(f.type) for f in dataclasses.fields(container)}


def test_every_area_field_is_a_container_field_of_the_same_declared_type() -> None:
    """An area field the container does not declare, or declares differently."""
    declared = _annotations(Services)
    for module, container in _AREA_CONTAINERS.items():
        for name, annotation in _annotations(container).items():
            assert name in declared, f"{module}: {container.__name__}.{name} is not a Services field"
            assert declared[name] == annotation, (
                f"{module}: {container.__name__}.{name} is declared {annotation!r} but the container "
                f"declares {declared[name]!r} -- one of the two is describing a different object"
            )


def test_no_two_areas_supply_the_same_container_field() -> None:
    """A duplicate is a `TypeError` about a repeated keyword at startup, and
    -- worse -- an ambiguity about which area owns the service."""
    seen: dict[str, str] = {}
    for module, container in _AREA_CONTAINERS.items():
        for name in _annotations(container):
            assert name not in seen, f"{name} is supplied by both {seen[name]} and {module}"
            seen[name] = module


def test_every_container_field_is_supplied_by_an_area_or_named_by_the_root() -> None:
    """The coverage half: a field nobody supplies fails app construction."""
    supplied = {name for container in _AREA_CONTAINERS.values() for name in _annotations(container)}
    missing = sorted({f.name for f in dataclasses.fields(Services)} - supplied - _ROOT_SUPPLIED)
    assert not missing, f"no area supplies {missing}; the app would fail to construct its container"


def test_the_root_supplied_set_names_nothing_an_area_already_owns() -> None:
    """Keeps `_ROOT_SUPPLIED` above from silently absorbing an area's field
    and turning this file's coverage check into a tautology."""
    supplied = {name for container in _EXPANDED_AREAS.values() for name in _annotations(container)}
    assert not (_ROOT_SUPPLIED & supplied)
    assert _ROOT_SUPPLIED <= {f.name for f in dataclasses.fields(Services)}


def test_the_container_assembly_names_no_field_an_area_already_owns() -> None:
    """The exit property, asserted on the root's own source.

    Adding a service to an existing area must touch that area's `wiring.py`
    and `contextplane.api.container.Services`, and nothing in
    `contextplane/wiring/services.py`. That holds exactly as long as the
    `Services(...)` call names only the fields the root supplies itself and
    expands the rest: the moment it spells one area-owned field by hand, the
    next service in that area needs a line there too.

    Deliberately scoped to the `Services(...)` call and no wider. Keyword
    arguments elsewhere in the file are the cross-area collaborators the root
    threads into area builders on purpose (`visibility=`, `retrieval=`,
    `subscriptions=`, ...) -- naming those is the design, not a leak.
    """
    tree = ast.parse(_SERVICES_PATH.read_text(encoding="utf-8"), filename=str(_SERVICES_PATH))
    area_owned = {name for container in _EXPANDED_AREAS.values() for name in _annotations(container)}

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Services"
    ]
    assert len(calls) == 1, "the composition root should assemble the container in exactly one place"

    named = {kw.arg for kw in calls[0].keywords if kw.arg is not None}
    leaked = sorted(named & area_owned)
    assert not leaked, f"the Services(...) call names area-owned fields by hand: {leaked}"
    assert named == set(_ROOT_SUPPLIED), (
        f"the Services(...) call names {sorted(named)}; the root-supplied set is "
        f"{sorted(_ROOT_SUPPLIED)} -- everything else must arrive by expansion"
    )
    # ... and the expansion is what supplies the rest.
    assert len(calls[0].keywords) - len(named) == len(
        _EXPANDED_AREAS
    ), "every area container must be expanded into the container assembly exactly once"
