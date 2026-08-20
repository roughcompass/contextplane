"""Assert a governed relationship, or refuse it — in one transaction, under one lock.

Row-local rules the database can state, it states: the temporal exclusion forbids
two assertions of one type over one ordered pair in force at once, and the check
constraints hold the readiness and scope vocabularies. What no constraint can
express is an *aggregate* rule — "at most three of these per source" is a fact
about rows this one is not, and a `SELECT count(*)` followed by an `INSERT` is
two statements with a gap in the middle. Two transactions each counting three
under a maximum of four both insert, and the fourth and fifth land together.

So every aggregate decision here happens while holding
`relationship_aggregate_lock_key(binding, relationship_type, cardinality_scope)`,
a transaction-scoped advisory lock the schema defines precisely so that every
writer derives the same key from the same three values. The lock is taken before
the count and released by the commit, which means the number the maximum check
read is still true when the row lands. Unlocked count-then-write is not a
performance choice here; it is the bug.

**The row, its provenance, its audit entry and its outbox entry commit together.**
A relationship the closure cache never hears about is a graph whose derived
answers silently diverge from it, and an assertion with no provenance is not
governed — nothing can say which profile revision validated it or who produced
it. All four are written on the caller's session and share its fate.

**Entity rows are not read here.** The `entities` table belongs to the visibility
chokepoint, which sits above this package in the import contract — so endpoint
resolution is a port this module declares and the composition root fills, the same
way `visibility` is handed into the catalog area. The chokepoint keeps owning
every entity read; this module states what it needs to know about an endpoint and
refuses to guess.

**Cross-organization writes deny by default.** The policy seam below is asked, and
an implementation that is not wired yet denies rather than allows — a grant system
that has not arrived cannot be assumed permissive.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.relationships import queries, readiness, rows
from contextplane.relationships.definitions import RelationshipConstraints
from contextplane.relationships.refusals import (
    CROSS_ORG_DENIED,
    DUPLICATE_REFUSED,
    ENDPOINT_MISSING,
    ENDPOINT_TYPE_MISMATCH,
    INVERSE_NOT_WRITABLE,
    MAXIMUM_EXCEEDED,
    NO_ACTIVE_BINDING,
    RELATIONSHIP_ASSERTED,
    RELATIONSHIP_SUPERSEDED,
    SYMMETRY_ENDPOINTS_DIFFER,
    UNDECLARED_PROPERTY,
    UNKNOWN_TYPE,
    RelationshipWriteRefused,
)


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """What the write path needs to know about one end of a relationship.

    Deliberately small. The entity's name, attributes and visibility are the
    resolver's business; this module needs the type to check against the
    definition and the owning tenant to check the boundary, and asking for more
    would make the port harder to satisfy without making the check stronger.
    """

    entity_id: uuid.UUID
    entity_type: str
    tenant_id: uuid.UUID


class EndpointResolver(Protocol):
    """Resolves an entity id to its type and owner, through the visibility chokepoint.

    Declared here and implemented above so that the chokepoint keeps owning every
    `entities` read while this package stays below `contextplane.service` in the
    import contract. Returning `None` means "not visible to this caller", which is
    the same answer as "does not exist" on purpose: a resolver that distinguished
    them would let a caller probe for rows in another tenant.
    """

    async def resolve(self, session: AsyncSession, *, tenant_id: uuid.UUID, entity_id: uuid.UUID) -> Endpoint | None:
        """The endpoint, or `None` when it does not exist or is not visible."""
        ...


class CrossOrgPolicy(Protocol):
    """Decides whether an edge may cross an organization boundary.

    A separate port from the resolver because the two answer to different
    authorities: one reports what exists, the other whether a grant permits it.
    """

    async def permits(
        self,
        session: AsyncSession,
        *,
        source: Endpoint,
        destination: Endpoint,
        constraints: RelationshipConstraints,
    ) -> bool:
        """Whether a grant permits this edge to cross an organization boundary."""
        ...


class DenyCrossOrg:
    """The default policy: refuse every crossing.

    Not a placeholder that lets things through until the real one arrives. An
    omitted policy is a denial — the definition table makes `cross_org_policy`
    NOT NULL for the same reason — so the deployment without a grant system
    refuses the write rather than permitting one nobody approved.
    """

    async def permits(
        self,
        session: AsyncSession,
        *,
        source: Endpoint,
        destination: Endpoint,
        constraints: RelationshipConstraints,
    ) -> bool:
        """Always `False` — an ungranted crossing is refused, never assumed."""
        return False


@dataclasses.dataclass(frozen=True)
class AssertedRelationship:
    """What a successful assertion produced, including what a reversal would need."""

    relationship_id: uuid.UUID
    definition_id: uuid.UUID
    provenance_id: uuid.UUID
    profile_binding_id: uuid.UUID
    readiness_state: str
    effective_from: datetime.datetime


class RelationshipWriteService:
    """The one path that writes a governed relationship assertion.

    Takes a session per call rather than a factory: the assertion, its provenance,
    its audit row and its outbox row have to commit with whatever else the caller
    is doing — a promotion, an import, a request — and a service that opened its
    own transaction could not offer that.
    """

    def __init__(
        self,
        *,
        endpoints: EndpointResolver,
        cross_org: CrossOrgPolicy | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._cross_org = cross_org if cross_org is not None else DenyCrossOrg()

    async def assert_relationship(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        relationship_type: str,
        source_entity_id: uuid.UUID,
        destination_entity_id: uuid.UUID,
        properties: Mapping[str, Any] | None = None,
        now: datetime.datetime,
        effective_from: datetime.datetime | None = None,
        produced_by: str = "contextplane",
        source_system: str = "contextplane",
        source_namespace: str = "internal",
    ) -> AssertedRelationship:
        """Validate and write one governed assertion, or raise `RelationshipWriteRefused`.

        The order is not arbitrary. Everything that can be decided without the
        lock is decided first, so a request that was never going to succeed does
        not queue behind one that might; the lock is then taken once, and the two
        aggregate questions — is this a duplicate, does it exceed the maximum —
        are both answered inside it, along with the readiness count that depends
        on the same window.
        """
        started_at = effective_from if effective_from is not None else now
        supplied = dict(properties or {})

        binding = await self._active_binding(session, tenant_id)
        constraints, definition_id = await self._definition(session, binding, relationship_type)

        source = await self._endpoint(session, tenant_id, source_entity_id, role="source")
        destination = await self._endpoint(session, tenant_id, destination_entity_id, role="destination")

        self._check_endpoint_types(constraints, source, destination)
        self._check_symmetry(constraints, source, destination)
        self._check_properties(constraints, supplied)
        await self._check_cross_org(session, constraints, source, destination)

        # Everything above is row-local. From here the answers depend on rows this
        # write is about to join, so the hold starts and does not end until commit.
        await self._lock_scope(session, binding, constraints)

        return await self._write_under_lock(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            constraints=constraints,
            definition_id=definition_id,
            binding=binding,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            properties=supplied,
            started_at=started_at,
            now=now,
            produced_by=produced_by,
            source_system=source_system,
            source_namespace=source_namespace,
            action=RELATIONSHIP_ASSERTED,
        )

    async def supersede_relationship(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        relationship_id: uuid.UUID,
        relationship_type: str,
        source_entity_id: uuid.UUID,
        destination_entity_id: uuid.UUID,
        properties: Mapping[str, Any] | None = None,
        now: datetime.datetime,
        effective_from: datetime.datetime | None = None,
        produced_by: str = "contextplane",
        source_system: str = "contextplane",
        source_namespace: str = "internal",
    ) -> AssertedRelationship:
        """End the named assertion and start its replacement at the same instant.

        **The row that ends is the one named, not one inferred from the body.** An
        update that resolved its target by re-deriving `(type, source,
        destination)` would be an update whose subject the caller could not see:
        two edges differing only in properties are the same triple, and the write
        would silently pick one.

        Which is why the body may not move the edge. A supersession whose type or
        endpoints differ from the row it names is refused rather than applied:
        those three are the assertion's identity, and changing them is retiring
        one edge and asserting another — two acts, with two audit rows, not one
        amendment.

        The predecessor is closed *before* the aggregate checks run, and at the
        same instant its replacement starts. `count_in_force` treats the interval
        as half-open, so a row ending exactly at `at` is already not counted:
        the duplicate check, the inverse check and the maximum all see the world
        the replacement is joining rather than the one it is leaving, with no
        special case needed for the row being replaced.
        """
        started_at = effective_from if effective_from is not None else now
        supplied = dict(properties or {})

        binding = await self._active_binding(session, tenant_id)
        constraints, definition_id = await self._definition(session, binding, relationship_type)

        source = await self._endpoint(session, tenant_id, source_entity_id, role="source")
        destination = await self._endpoint(session, tenant_id, destination_entity_id, role="destination")

        self._check_endpoint_types(constraints, source, destination)
        self._check_symmetry(constraints, source, destination)
        self._check_properties(constraints, supplied)
        await self._check_cross_org(session, constraints, source, destination)

        await self._lock_scope(session, binding, constraints)

        await rows.close_superseded(
            session,
            tenant_id=tenant_id,
            relationship_id=relationship_id,
            constraints=constraints,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            at=started_at,
        )
        await rows.enqueue_closure(session, tenant_id=tenant_id, edge_id=relationship_id, now=now)

        return await self._write_under_lock(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            constraints=constraints,
            definition_id=definition_id,
            binding=binding,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            properties=supplied,
            started_at=started_at,
            now=now,
            produced_by=produced_by,
            source_system=source_system,
            source_namespace=source_namespace,
            action=RELATIONSHIP_SUPERSEDED,
        )

    async def _write_under_lock(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        constraints: RelationshipConstraints,
        definition_id: uuid.UUID,
        binding: rows.Binding,
        source_entity_id: uuid.UUID,
        destination_entity_id: uuid.UUID,
        properties: Mapping[str, Any],
        started_at: datetime.datetime,
        now: datetime.datetime,
        produced_by: str,
        source_system: str,
        source_namespace: str,
        action: str,
    ) -> AssertedRelationship:
        """The aggregate checks and the four rows, with the lock already held.

        Shared by the assertion and the supersession so the two cannot drift: a
        replacement that skipped the duplicate check, or wrote its edge without
        its provenance, would be a second write path with none of the first one's
        guarantees.
        """
        supplied = dict(properties)

        await self._check_duplicate(
            session,
            tenant_id=tenant_id,
            constraints=constraints,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            at=started_at,
        )
        await self._check_inverse_not_already_stored(
            session,
            tenant_id=tenant_id,
            constraints=constraints,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            at=started_at,
        )
        in_force = await readiness.count_in_force(
            session,
            tenant_id=tenant_id,
            relationship_type=constraints.relationship_type,
            cardinality_scope=constraints.cardinality_scope,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            at=started_at,
        )
        self._check_maximum(constraints, in_force)

        state = readiness.readiness_for(observed=in_force + 1, minimum=constraints.min_cardinality)

        edge_id = await rows.write_edge(
            session,
            tenant_id=tenant_id,
            relationship_type=constraints.relationship_type,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            started_at=started_at,
            now=now,
            actor_id=actor_id,
        )
        provenance_id = await rows.write_provenance(
            session,
            tenant_id=tenant_id,
            constraints=constraints,
            binding=binding,
            now=now,
            produced_by=produced_by,
            source_system=source_system,
            source_namespace=source_namespace,
        )
        await rows.write_metadata(
            session,
            edge_id=edge_id,
            tenant_id=tenant_id,
            definition_id=definition_id,
            constraints=constraints,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            properties=supplied,
            started_at=started_at,
            state=state,
            provenance_id=provenance_id,
            binding=binding,
            now=now,
        )
        await rows.enqueue_closure(session, tenant_id=tenant_id, edge_id=edge_id, now=now)
        await rows.write_audit(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            edge_id=edge_id,
            constraints=constraints,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            state=state,
            now=now,
            action=action,
        )

        return AssertedRelationship(
            relationship_id=edge_id,
            definition_id=definition_id,
            provenance_id=provenance_id,
            profile_binding_id=binding.binding_id,
            readiness_state=state,
            effective_from=started_at,
        )

    # -- resolution ---------------------------------------------------------

    async def _active_binding(self, session: AsyncSession, tenant_id: uuid.UUID) -> rows.Binding:
        """The tenant's active binding, which every governed row must name.

        Only `active` counts. A `validating` binding is a profile being measured,
        not one in force, and writing assertions against it would put rows in the
        graph governed by a revision nobody has approved.
        """
        row = (
            await session.execute(
                text(
                    "SELECT binding_id, profile_revision_id, extension_set_digest FROM profile_bindings"
                    " WHERE tenant_id = :tenant AND state = 'active'"
                    " ORDER BY effective_from DESC LIMIT 1"
                ),
                {"tenant": tenant_id},
            )
        ).first()
        if row is None:
            raise RelationshipWriteRefused(
                NO_ACTIVE_BINDING,
                "this tenant is bound to no active profile; a governed assertion has to name the revision that "
                "validated it, and there is none to name",
            )
        return rows.Binding(binding_id=row[0], profile_revision_id=row[1], extension_set_digest=row[2])

    async def _definition(
        self, session: AsyncSession, binding: rows.Binding, relationship_type: str
    ) -> tuple[RelationshipConstraints, uuid.UUID]:
        """The compiled constraint set for this type under the tenant's revision."""
        row = (
            (
                await session.execute(
                    text(
                        "SELECT definition_id, relationship_type, source_type, destination_type, direction,"
                        "       property_schema, duplicate_policy, symmetry, inverse_view_policy,"
                        "       min_cardinality, max_cardinality, cardinality_scope, authority, cross_org_policy"
                        "  FROM relationship_type_definitions"
                        " WHERE profile_revision_id = :rid AND relationship_type = :rtype"
                        " ORDER BY extension_revision_id NULLS FIRST LIMIT 1"
                    ),
                    {"rid": binding.profile_revision_id, "rtype": relationship_type},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RelationshipWriteRefused(
                UNKNOWN_TYPE,
                f"{relationship_type!r} is not a relationship type the bound profile declares; an edge of a type "
                "with no definition carries no rules, so nothing could check it now or later",
            )
        return (
            RelationshipConstraints(
                relationship_type=row["relationship_type"],
                source_type=row["source_type"],
                destination_type=row["destination_type"],
                direction=row["direction"],
                property_schema=row["property_schema"],
                duplicate_policy=row["duplicate_policy"],
                symmetry=row["symmetry"],
                inverse_view_policy=row["inverse_view_policy"],
                min_cardinality=row["min_cardinality"],
                max_cardinality=row["max_cardinality"],
                cardinality_scope=row["cardinality_scope"],
                authority=row["authority"],
                cross_org_policy=row["cross_org_policy"],
            ),
            row["definition_id"],
        )

    async def _endpoint(
        self, session: AsyncSession, tenant_id: uuid.UUID, entity_id: uuid.UUID, *, role: str
    ) -> Endpoint:
        resolved = await self._endpoints.resolve(session, tenant_id=tenant_id, entity_id=entity_id)
        if resolved is None:
            raise RelationshipWriteRefused(
                ENDPOINT_MISSING,
                f"the {role} endpoint {entity_id} does not exist or is not visible to this caller",
            )
        return resolved

    # -- row-local checks ---------------------------------------------------

    @staticmethod
    def _check_endpoint_types(constraints: RelationshipConstraints, source: Endpoint, destination: Endpoint) -> None:
        for role, endpoint, expected in (
            ("source", source, constraints.source_type),
            ("destination", destination, constraints.destination_type),
        ):
            if endpoint.entity_type != expected:
                raise RelationshipWriteRefused(
                    ENDPOINT_TYPE_MISMATCH,
                    f"the {role} endpoint is a {endpoint.entity_type!r} but "
                    f"{constraints.relationship_type!r} joins {expected!r} there",
                )

    @staticmethod
    def _check_symmetry(constraints: RelationshipConstraints, source: Endpoint, destination: Endpoint) -> None:
        """A symmetric type asserts the reverse too, which needs the same type at both ends.

        Checked here as well as in the profile schema because a definition row can
        be projected from a document published before a schema rule tightened, and
        this is the last point before the row lands.
        """
        if constraints.symmetry == "symmetric" and source.entity_type != destination.entity_type:
            raise RelationshipWriteRefused(
                SYMMETRY_ENDPOINTS_DIFFER,
                f"{constraints.relationship_type!r} is symmetric, so its reverse is a claim of the same kind; "
                f"{source.entity_type!r} and {destination.entity_type!r} are not",
            )

    @staticmethod
    def _check_properties(constraints: RelationshipConstraints, supplied: Mapping[str, Any]) -> None:
        """Every supplied property must be one the definition declares.

        The compiled `property_schema` is keyed by name, so this is a lookup rather
        than a scan whose cost grows with a schema the writer did not choose.
        """
        undeclared = sorted(set(supplied) - set(constraints.property_schema))
        if undeclared:
            raise RelationshipWriteRefused(
                UNDECLARED_PROPERTY,
                f"{constraints.relationship_type!r} declares no {', '.join(repr(n) for n in undeclared)}; a property "
                "the definition does not carry cannot be validated by anything that reads the edge later",
            )

    async def _check_cross_org(
        self,
        session: AsyncSession,
        constraints: RelationshipConstraints,
        source: Endpoint,
        destination: Endpoint,
    ) -> None:
        if source.tenant_id == destination.tenant_id:
            return
        if constraints.cross_org_policy != "allow_with_grant":
            raise RelationshipWriteRefused(
                CROSS_ORG_DENIED,
                f"{constraints.relationship_type!r} denies cross-organization edges and these endpoints are owned "
                "by different tenants",
            )
        if not await self._cross_org.permits(session, source=source, destination=destination, constraints=constraints):
            raise RelationshipWriteRefused(
                CROSS_ORG_DENIED,
                f"{constraints.relationship_type!r} permits a cross-organization edge only with a grant, and no "
                "grant permits this one",
            )

    # -- the locked section -------------------------------------------------

    async def _lock_scope(
        self, session: AsyncSession, binding: rows.Binding, constraints: RelationshipConstraints
    ) -> None:
        """Take the aggregate lock for this binding, type and scope.

        Transaction-scoped, so it is released by the commit or rollback and never
        by this code — a lock released early is a lock that was not held for the
        write it was protecting. The key comes from the schema's own function so
        every writer derives it identically; deriving it here would be a second
        implementation free to disagree.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(relationship_aggregate_lock_key(:binding, :rtype, :scope))"),
            {
                "binding": binding.binding_id,
                "rtype": constraints.relationship_type,
                "scope": constraints.cardinality_scope,
            },
        )

    @staticmethod
    async def _check_duplicate(
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        constraints: RelationshipConstraints,
        source_entity_id: uuid.UUID,
        destination_entity_id: uuid.UUID,
        at: datetime.datetime,
    ) -> None:
        if constraints.duplicate_policy != "reject":
            return
        existing = await queries.in_force_between(
            session,
            tenant_id=tenant_id,
            relationship_type=constraints.relationship_type,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
            at=at,
        )
        if existing is not None:
            raise RelationshipWriteRefused(
                DUPLICATE_REFUSED,
                f"{constraints.relationship_type!r} rejects duplicates and one is already in force over this pair",
            )

    @staticmethod
    async def _check_inverse_not_already_stored(
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        constraints: RelationshipConstraints,
        source_entity_id: uuid.UUID,
        destination_entity_id: uuid.UUID,
        at: datetime.datetime,
    ) -> None:
        """Refuse a write whose fact is already stored in the opposite direction.

        Only for a type whose inverse is a `read_only` view. That policy says the
        reverse direction is *derived* from the stored one, so asserting the mirror
        would store one fact twice — and the two copies would then be free to be
        ended, re-asserted or re-propertied independently, with nothing able to say
        which was the real edge.

        `independently_asserted` is the policy that says the opposite: the reverse
        is its own assertion with its own provenance, so both directions are meant
        to exist and this check does not run.

        Only reachable for a type whose endpoints are the same type — an
        asymmetric type between two different types is already refused by the
        endpoint check, because the mirror puts the wrong type at each end. This
        is what catches the case that check cannot see.
        """
        if constraints.inverse_view_policy != "read_only":
            return
        if constraints.direction == "undirected":
            return
        mirrored = await queries.in_force_between(
            session,
            tenant_id=tenant_id,
            relationship_type=constraints.relationship_type,
            source_entity_id=destination_entity_id,
            destination_entity_id=source_entity_id,
            at=at,
        )
        if mirrored is not None:
            raise RelationshipWriteRefused(
                INVERSE_NOT_WRITABLE,
                f"{constraints.relationship_type!r} publishes its inverse as a read-only view, and this pair is "
                "already asserted in the opposite direction; storing the mirror would hold one fact twice",
            )

    @staticmethod
    def _check_maximum(constraints: RelationshipConstraints, in_force: int) -> None:
        if constraints.max_cardinality is None:
            return
        if in_force + 1 > constraints.max_cardinality:
            raise RelationshipWriteRefused(
                MAXIMUM_EXCEEDED,
                f"{constraints.relationship_type!r} allows at most {constraints.max_cardinality} per "
                f"{constraints.cardinality_scope}, and {in_force} are already in force",
            )


__all__ = [
    "CROSS_ORG_DENIED",
    "DUPLICATE_REFUSED",
    "ENDPOINT_MISSING",
    "ENDPOINT_TYPE_MISMATCH",
    "INVERSE_NOT_WRITABLE",
    "MAXIMUM_EXCEEDED",
    "NO_ACTIVE_BINDING",
    "RELATIONSHIP_ASSERTED",
    "RELATIONSHIP_SUPERSEDED",
    "SYMMETRY_ENDPOINTS_DIFFER",
    "UNDECLARED_PROPERTY",
    "UNKNOWN_TYPE",
    "AssertedRelationship",
    "CrossOrgPolicy",
    "DenyCrossOrg",
    "Endpoint",
    "EndpointResolver",
    "RelationshipWriteRefused",
    "RelationshipWriteService",
]
