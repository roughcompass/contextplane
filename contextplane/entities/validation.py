"""Validate an entity write against the profile its tenant is bound to.

Before this module, an entity's properties were checked against the
`capability_type_schemas` registry and only when the caller supplied a
`capability_type`. Everything else — a generic entity, a sync-worker write, a
promotion landing an attribute in the canonical graph — wrote whatever it was
handed. A profile that declares which types exist and which properties they
carry is not a governance surface if four of the five ways to write an entity
never consult it.

So the seam is here rather than in any one writer: `EntityService`,
`SchemaService` and the promotion writer all resolve through this module, and
`scripts/check_profile_write_coverage.py` fails the build when a new writer
does not.

**The binding decides the mode, not the caller.** A caller that chose its own
enforcement level would be a caller that could opt out, which is the bypass this
module exists to close. The tenant's binding state is what selects it:

- an `active` binding is **mandatory** — the tenant's governance is in force and
  a violation is refused;
- a `validating` binding is **advisory** — the binding exists precisely so that
  current data can be measured against a profile before anyone commits to it, so
  violations are reported and the write proceeds;
- no binding in either state is **unbound** — there is no profile to validate
  against, and a deployment that has not adopted one writes as it always did.

Mandatory therefore begins at the approved `validating → active` transition, and
nothing else moves it.

**Violations are bounded.** An entity carrying two hundred undeclared properties
would otherwise produce a two-hundred-item error payload, which is neither
readable nor safe to put in an HTTP response. The result carries at most
`MAX_REPORTED_VIOLATIONS` and says when it truncated, so a caller can tell "these
are the problems" from "these are the first few".

**Reads here are of `profile_bindings` and `profile_revisions`, never `entities`.**
This module validates values it was handed; it never resolves an entity row, so
it needs no visibility filtering and must not acquire any — a validator that
could read entities would be a second cross-tenant query path.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.profile.compiler import ENTITY_FAMILY

#: Enforcement modes, named by what they do rather than by the binding state
#: that selects them — a reader of a validation result should not have to know
#: the binding state machine to know whether the write was refused.
MANDATORY: Final = "mandatory"
ADVISORY: Final = "advisory"
UNBOUND: Final = "unbound"

#: Binding states that govern a write, most authoritative first. `active` wins
#: over `validating` when a tenant has both: a tenant validating its *next*
#: profile is still governed by its current one, and reporting against the
#: candidate while enforcing nothing would leave the live profile unenforced for
#: the whole validation window.
_GOVERNING_STATES: Final[tuple[str, ...]] = ("active", "validating")

#: The ceiling on reported violations. Chosen to be large enough that an
#: ordinary mistake is reported in full and small enough that a pathological
#: payload cannot be echoed back through an error response.
MAX_REPORTED_VIOLATIONS: Final = 20

#: How a declared value type is checked against a supplied value. `timestamp`
#: and `reference` are strings on the wire and are checked as such; parsing them
#: into datetimes or ids is the writer's business, and a validator that parsed
#: would be deciding a representation this vocabulary has not fixed.
_PYTHON_TYPES: Final[Mapping[str, tuple[type, ...]]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "timestamp": (str,),
    "enum": (str,),
    "reference": (str,),
}


@dataclasses.dataclass(frozen=True)
class Violation:
    """One way a write disagrees with the profile, named so a caller can act on it."""

    code: str
    property_name: str | None
    detail: str


@dataclasses.dataclass(frozen=True)
class EntityValidationResult:
    """What the profile says about one entity write, and which profile said it.

    `profile_revision_id` is carried even when nothing is wrong. A caller
    recording a write needs to say which governance it was accepted under, and a
    revision reported only on failure would be missing from every successful
    write's provenance.
    """

    mode: str
    entity_type: str
    profile_revision_id: uuid.UUID | None
    violations: tuple[Violation, ...] = ()
    truncated: bool = False

    @property
    def enforced(self) -> bool:
        """Whether a violation here would refuse the write."""
        return self.mode == MANDATORY

    @property
    def valid(self) -> bool:
        """Whether the write may proceed.

        An advisory result is valid even carrying violations — that is what
        advisory means. A caller wanting to know whether the profile was
        satisfied asks `violations`, not this.
        """
        return not self.violations or self.mode != MANDATORY

    def messages(self) -> tuple[str, ...]:
        """The violations as strings, for a warning list or an error body."""
        return tuple(f"{violation.code}: {violation.detail}" for violation in self.violations)


class EntityValidator:
    """Resolves a tenant's governing profile and checks one entity write against it.

    Holds no cached profile. A binding can be activated or rolled back between
    two writes, and a validator serving a revision the tenant is no longer bound
    to would enforce governance nobody chose — the same reason `SchemaService`
    re-reads its registry at write time rather than caching it.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate(
        self,
        *,
        tenant_id: uuid.UUID,
        entity_type: str,
        attributes: Mapping[str, Any],
    ) -> EntityValidationResult:
        """Check one entity's type and properties against the tenant's governing profile.

        Opens its own session. A writer that already holds one — the promotion
        path, which runs inside its caller's transaction — calls
        `validate_entity_write` directly instead, so its validation reads the same
        snapshot as the rows it is about to write rather than a later one.
        """
        async with self._session_factory() as session:
            return await validate_entity_write(
                session, tenant_id=tenant_id, entity_type=entity_type, attributes=attributes
            )


async def validate_entity_write(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    attributes: Mapping[str, Any],
) -> EntityValidationResult:
    """Check one entity write against its tenant's governing profile, on a caller's session."""
    governing = await _governing_profile(session, tenant_id)
    if governing is None:
        return EntityValidationResult(mode=UNBOUND, entity_type=entity_type, profile_revision_id=None)

    revision_id, state, document = governing
    mode = MANDATORY if state == "active" else ADVISORY

    declared = _entity_types(document)
    found = declared.get(entity_type)
    if found is None:
        return _bounded(
            mode,
            entity_type,
            revision_id,
            [
                Violation(
                    code="unknown_entity_type",
                    property_name=None,
                    detail=(
                        f"{entity_type!r} is not a type this profile declares; an entity of a type the profile "
                        "does not know carries no property rules at all, so nothing about it can be checked "
                        "later either"
                    ),
                )
            ],
        )

    return _bounded(mode, entity_type, revision_id, _property_violations(found, attributes))


async def _governing_profile(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[uuid.UUID, str, str] | None:
    """The revision, binding state and canonical document governing this tenant.

    One query rather than a binding read followed by a revision read: the two
    would be separately consistent and jointly stale, and a binding activated
    between them would resolve a document belonging to neither.
    """
    row = (
        await session.execute(
            text(
                "SELECT b.profile_revision_id, b.state, r.canonical_document"
                "  FROM profile_bindings b"
                "  JOIN profile_revisions r ON r.profile_revision_id = b.profile_revision_id"
                " WHERE b.tenant_id = :tenant AND b.state = ANY(CAST(:states AS TEXT[]))"
                " ORDER BY array_position(CAST(:states AS TEXT[]), b.state), b.effective_from DESC"
                " LIMIT 1"
            ),
            {"tenant": tenant_id, "states": list(_GOVERNING_STATES)},
        )
    ).first()

    if row is None:
        return None
    document = row[2]
    # `canonical_document` is JSONB, so the driver hands back the decoded object
    # for a JSON object and a string for a JSON string. The families were stored
    # as an object of per-family JSON strings; normalize to the text form the
    # parser below expects.
    if not isinstance(document, str):
        document = json.dumps(document)
    return row[0], row[1], document


def _entity_types(document: str) -> Mapping[str, Mapping[str, Any]]:
    """Every entity type the document declares, keyed by qualified name.

    A document that cannot be read yields no types, which surfaces as
    `unknown_entity_type` rather than as an exception from inside a write path.
    A profile nobody can parse governs nothing, and refusing every write against
    it is the safe direction: the alternative is accepting every write against it.
    """
    try:
        families = json.loads(document)
        parsed: Sequence[Mapping[str, Any]] = json.loads(families[ENTITY_FAMILY])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return {f"{entry['namespace']}:{entry['type_name']}": entry for entry in parsed if _is_named(entry)}


def _is_named(entry: Mapping[str, Any]) -> bool:
    return "namespace" in entry and "type_name" in entry


def _property_violations(declared: Mapping[str, Any], attributes: Mapping[str, Any]) -> list[Violation]:
    """Every way the supplied properties disagree with the declared type.

    Collected rather than raised one at a time: an author who fixes violations a
    round-trip apiece only ever learns the shape of the last one.
    """
    definitions = {prop["name"]: prop for prop in declared.get("properties", []) if "name" in prop}
    extension_points = set(declared.get("extension_points", ()))

    violations: list[Violation] = []

    for name in sorted(set(attributes) - set(definitions)):
        if name in extension_points:
            continue
        violations.append(
            Violation(
                code="undeclared_property",
                property_name=name,
                detail=(
                    f"{name!r} is neither declared by this type nor one of its extension points; naming the points "
                    "is the whole of a tenant's permission to add to a shared type, so an unexpected property is a "
                    "refusal rather than a silent addition"
                ),
            )
        )

    for name, definition in sorted(definitions.items()):
        if definition.get("required") and name not in attributes:
            violations.append(
                Violation(
                    code="missing_required_property",
                    property_name=name,
                    detail=f"{name!r} is required by this type and was not supplied",
                )
            )
        if name in attributes:
            violations.extend(_value_violations(name, definition, attributes[name]))

    return violations


def _value_violations(name: str, definition: Mapping[str, Any], value: object) -> list[Violation]:
    """Whether one supplied value matches what its property declares."""
    value_type = definition.get("value_type")
    expected = _PYTHON_TYPES.get(str(value_type))
    if expected is None:
        return []

    # `bool` is a subclass of `int`, so a boolean would satisfy an integer
    # property without this. The reverse is not a risk: an int is not a bool.
    if value_type == "integer" and isinstance(value, bool):
        return [
            Violation(
                code="wrong_value_type",
                property_name=name,
                detail=f"{name!r} is declared integer and was given the boolean {value!r}",
            )
        ]

    if not isinstance(value, expected):
        return [
            Violation(
                code="wrong_value_type",
                property_name=name,
                detail=(f"{name!r} is declared {value_type} and was given {type(value).__name__} {value!r}"),
            )
        ]

    enum_values = definition.get("enum_values") or ()
    if value_type == "enum" and enum_values and value not in enum_values:
        return [
            Violation(
                code="value_not_in_enum",
                property_name=name,
                detail=f"{name!r} accepts {', '.join(sorted(enum_values))}; {value!r} is not one of them",
            )
        ]

    return []


def _bounded(
    mode: str,
    entity_type: str,
    revision_id: uuid.UUID,
    violations: list[Violation],
) -> EntityValidationResult:
    """Cap the reported violations, recording that the list was cut rather than short."""
    return EntityValidationResult(
        mode=mode,
        entity_type=entity_type,
        profile_revision_id=revision_id,
        violations=tuple(violations[:MAX_REPORTED_VIOLATIONS]),
        truncated=len(violations) > MAX_REPORTED_VIOLATIONS,
    )


__all__ = [
    "ADVISORY",
    "MANDATORY",
    "MAX_REPORTED_VIOLATIONS",
    "UNBOUND",
    "EntityValidationResult",
    "EntityValidator",
    "Violation",
]
