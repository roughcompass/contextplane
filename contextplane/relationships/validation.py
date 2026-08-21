"""Validate a relationship write against the profile its tenant is bound to.

The generic relationship surface validated its `subject_type` through
`EntityValidator`, which reads only the `entity` family of the canonical
document. A relationship type is declared in the `relationship` family, so it
was never found there, and every relationship write — create and update, on all
three intent routes — came back carrying `unknown_entity_type` as a violation
against a type the tenant's profile did in fact declare. A binding in
`mandatory` mode reported `valid: false` for a write it had accepted, and the
violation named the wrong family in the wrong vocabulary.

Nothing branched on the outcome, which is why it survived: the router puts
`ValidationOutcomeV1` in the response and the write proceeds regardless. It is
still a wrong answer given to every caller, and the advisory contract says
`violations` may be non-empty on a successful write, so a client displaying them
had no way to tell this artifact from a real finding.

**Property rules are checked here for the two intent routes that never reach the
write service.** `RelationshipWriteService._check_properties` validates
properties against the relationship's declared constraints, but only on the
canonical route. An observation stages a claim and a request opens a review
entry, and on both this module is the only thing that looks at what was written.

**The shared machinery lives in `contextplane.entities.validation`,** which is
one layer down, and is imported rather than copied. Its natural home is
`contextplane.profile` — it resolves a binding and parses a canonical document,
neither of which is about entities — but `scripts/check_profile_write_coverage.py`
identifies profile-consulting writers by their import of the entities module, so
moving it is a change to the coverage gate as well and is not this fix.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.entities.validation import (
    ADVISORY,
    MANDATORY,
    MAX_REPORTED_VIOLATIONS,
    UNBOUND,
    TargetRevisionClaim,
    Violation,
    declared_types,
    governing_profile,
    property_violations,
    target_revision_violations,
)
from contextplane.profile.compiler import RELATIONSHIP_FAMILY

#: Named for the family it could not find the type in. `unknown_entity_type`
#: would send a caller looking through its entity declarations for a
#: relationship that was never going to be there.
UNKNOWN_RELATIONSHIP_TYPE: Final = "unknown_relationship_type"


@dataclasses.dataclass(frozen=True)
class RelationshipValidationResult:
    """What the profile says about one relationship write, and which profile said it.

    Deliberately not `EntityValidationResult`: that carries an `entity_type`, and
    a relationship write recording its subject under that name is how the wrong
    family got consulted in the first place.
    """

    mode: str
    relationship_type: str
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
        advisory means.
        """
        return not self.violations or self.mode != MANDATORY

    def messages(self) -> tuple[str, ...]:
        """The violations as strings, for a warning list or an error body."""
        return tuple(f"{violation.code}: {violation.detail}" for violation in self.violations)


class RelationshipValidator:
    """Resolves a tenant's governing profile and checks one relationship write against it.

    Holds no cached profile, for the reason `EntityValidator` does not: a binding
    activated or rolled back between two writes would otherwise have the second
    checked against governance nobody chose.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate(
        self,
        *,
        tenant_id: uuid.UUID,
        relationship_type: str,
        properties: Mapping[str, Any],
        target_revision: TargetRevisionClaim | None = None,
    ) -> RelationshipValidationResult:
        """Check one relationship's type and properties against the governing profile."""
        async with self._session_factory() as session:
            return await validate_relationship_write(
                session,
                tenant_id=tenant_id,
                relationship_type=relationship_type,
                properties=properties,
                target_revision=target_revision,
            )


async def validate_relationship_write(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relationship_type: str,
    properties: Mapping[str, Any],
    target_revision: TargetRevisionClaim | None = None,
) -> RelationshipValidationResult:
    """Check one relationship write against its tenant's governing profile."""
    governing = await governing_profile(session, tenant_id)
    if governing is None:
        return RelationshipValidationResult(mode=UNBOUND, relationship_type=relationship_type, profile_revision_id=None)

    mode = MANDATORY if governing.state == "active" else ADVISORY
    revision_id = governing.revision_id
    stale = target_revision_violations(target_revision, governing)

    declared = declared_types(governing.document, RELATIONSHIP_FAMILY)
    found = declared.get(relationship_type)
    if found is None:
        return _bounded(
            mode,
            relationship_type,
            revision_id,
            stale
            + [
                Violation(
                    code=UNKNOWN_RELATIONSHIP_TYPE,
                    property_name=None,
                    detail=(
                        f"{relationship_type!r} is not a relationship type this profile declares; an edge of a "
                        "type the profile does not know carries no endpoint, cardinality or property rules at "
                        "all, so nothing about it can be checked later either"
                    ),
                )
            ],
        )

    return _bounded(mode, relationship_type, revision_id, stale + property_violations(found, properties))


def _bounded(
    mode: str,
    relationship_type: str,
    revision_id: uuid.UUID,
    violations: list[Violation],
) -> RelationshipValidationResult:
    """Cap the reported violations, recording that the list was cut rather than short."""
    return RelationshipValidationResult(
        mode=mode,
        relationship_type=relationship_type,
        profile_revision_id=revision_id,
        violations=tuple(violations[:MAX_REPORTED_VIOLATIONS]),
        truncated=len(violations) > MAX_REPORTED_VIOLATIONS,
    )


__all__ = [
    "UNKNOWN_RELATIONSHIP_TYPE",
    "RelationshipValidationResult",
    "RelationshipValidator",
    "validate_relationship_write",
]
