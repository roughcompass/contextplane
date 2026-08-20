"""Publishing profile revisions and tenant extensions, which may never change.

Publication is the moment a document stops being a proposal and becomes the
authority every later validator resolves against. Three properties make that
safe, and all three live in this module because they are properties of the
write path rather than of the row:

**Only compiler-accepted documents are published.** The compiler is called
before the insert, not after, and its conflicts are returned collected. A row
that reached the table without compiling would be an authority nothing could
validate against, discovered by the first write that tried.

**Nothing published is ever edited.** There is no update or delete here, and
the database carries `BEFORE UPDATE OR DELETE` triggers on both tables so the
absence is enforced rather than merely observed. A caller reaching for one gets
`PublishedDocumentIsImmutable` instead of a missing attribute, because the
answer to "how do I correct a revision?" is "publish a successor", and an error
that says so is worth more than an `AttributeError`.

**The canonical document and its digest are written together, from one
compile.** Digesting separately from canonicalizing is how two rows come to
disagree about what a digest covers.

Writes are raw SQL rather than ORM `session.add`, deliberately and not
incidentally: `scripts/check_privileged_writes.py` matches SQL text, so an ORM
write to these tables would be invisible to the gate that names this module
their only writer. The convention is what makes that gate real.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from contextplane.profile.compiler import CompiledProfile, compile_profile
from contextplane.profile.schemas.common import ProfileCompositionError
from contextplane.profile.scoring import validate_overrides

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.profile.schemas.entity import EntityTypeDefinition
    from contextplane.profile.schemas.interface import InterfaceFamilyDefinition
    from contextplane.profile.schemas.relationship import RelationshipTypeDefinition
    from contextplane.types import Clock

#: The compatibility verdicts a revision may declare. Mirrors the database
#: CHECK constraint rather than importing it: a value that passes here and
#: fails there would surface as an IntegrityError at the insert, which is a
#: worse error than a named rejection before any work is done.
COMPATIBILITY_VERDICTS: frozenset[str] = frozenset({"backward_compatible", "breaking", "deprecating"})


class ProfilePublicationError(RuntimeError):
    """A document cannot be published as asked."""


class PublishedDocumentIsImmutable(ProfilePublicationError):
    """Someone tried to change or remove something already published.

    Its own type because the remedy is specific and is not "retry": a published
    revision is corrected by publishing a successor that names it as
    predecessor, which preserves the chain readers walk backwards.
    """


class ProfileConflictError(ProfilePublicationError):
    """The document did not compile. Carries every conflict, not the first."""

    def __init__(self, conflicts: Sequence[Any]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            f"profile did not compile: {len(self.conflicts)} conflict(s): "
            + "; ".join(f"{c.code} on {c.qualified_type}" for c in self.conflicts)
        )


class DuplicatePublicationError(ProfilePublicationError):
    """This exact version or these exact bytes are already published.

    Separated from the generic error because it is the one publication failure
    that is frequently benign — a retried request, a re-run pipeline — and a
    caller that can recognise it can decide whether to treat it as success.
    """


@dataclasses.dataclass(frozen=True)
class PublishedRevision:
    """What a successful core publication produced."""

    profile_revision_id: uuid.UUID
    profile_family: str
    profile_name: str
    semantic_version: str
    document_digest: str
    compatibility: str
    predecessor_revision_id: uuid.UUID | None
    published_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class PublishedExtension:
    """What a successful tenant extension publication produced."""

    extension_revision_id: uuid.UUID
    tenant_id: uuid.UUID
    namespace: str
    target_core_revision_id: uuid.UUID
    document_digest: str
    extension_points: tuple[str, ...]
    published_at: datetime.datetime


class ProfileService:
    """The one writer of `profile_revisions` and `profile_extensions`.

    Registered as such in `scripts/check_privileged_writes.py`. A second writer
    would produce rows indistinguishable from these while having compiled
    nothing, and every validator downstream trusts that a published row
    compiled.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- Publication ------------------------------------------------------

    async def publish_revision(
        self,
        *,
        profile_family: str,
        profile_name: str,
        semantic_version: str,
        entities: Sequence[EntityTypeDefinition],
        relationships: Sequence[RelationshipTypeDefinition],
        interfaces: Sequence[InterfaceFamilyDefinition],
        compatibility: str,
        published_by: str,
        predecessor_revision_id: uuid.UUID | None = None,
        migration_plan_ref: str | None = None,
    ) -> PublishedRevision:
        """Compile, then publish. Never the other way around.

        `compatibility` is the publisher's declared verdict about this revision
        against its predecessor. It is validated against the same closed set the
        database constrains, here rather than at the insert, so a bad value is a
        named rejection instead of an IntegrityError naming a constraint.
        """
        if compatibility not in COMPATIBILITY_VERDICTS:
            msg = (
                f"unknown compatibility verdict {compatibility!r}; "
                f"expected one of {', '.join(sorted(COMPATIBILITY_VERDICTS))}"
            )
            raise ProfilePublicationError(msg)

        compiled = self._compile(entities=entities, relationships=relationships, interfaces=interfaces)

        revision_id = uuid.uuid4()
        now = self._clock.now()
        async with self._session_factory() as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO profile_revisions ("
                        "  profile_revision_id, profile_family, profile_name, semantic_version,"
                        "  canonical_document, document_digest, compatibility,"
                        "  predecessor_revision_id, migration_plan_ref, published_by, published_at"
                        ") VALUES (:rid, :family, :name, :version,"
                        "          CAST(:document AS JSONB), :digest, :compatibility,"
                        "          :predecessor, :migration_ref, :published_by, :now)"
                    ),
                    {
                        "rid": revision_id,
                        "family": profile_family,
                        "name": profile_name,
                        "version": semantic_version,
                        "document": compiled.document,
                        "digest": compiled.output_digest,
                        "compatibility": compatibility,
                        "predecessor": predecessor_revision_id,
                        "migration_ref": migration_plan_ref,
                        "published_by": published_by,
                        "now": now,
                    },
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise _publication_conflict(error, family=profile_family, name=profile_name) from error

        return PublishedRevision(
            profile_revision_id=revision_id,
            profile_family=profile_family,
            profile_name=profile_name,
            semantic_version=semantic_version,
            document_digest=compiled.output_digest,
            compatibility=compatibility,
            predecessor_revision_id=predecessor_revision_id,
            published_at=now,
        )

    async def publish_extension(
        self,
        *,
        tenant_id: uuid.UUID,
        namespace: str,
        target_core_revision_id: uuid.UUID,
        entities: Sequence[EntityTypeDefinition],
        relationships: Sequence[RelationshipTypeDefinition],
        interfaces: Sequence[InterfaceFamilyDefinition],
        published_by: str,
        # Scoring-magnitude overrides, validated to the same bar the committed
        # registry holds its own entries to. Optional because most extensions
        # extend the entity model and say nothing about scoring; an empty map
        # and an absent argument mean the same thing, which is "this tenant
        # scores on the core defaults".
        scoring_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> PublishedExtension:
        """Publish a tenant's extension of a core revision it must already have.

        The target is resolved before compiling rather than trusted from the
        request: an extension naming a revision that does not exist is a
        dangling authority, and the foreign key would catch it at the insert
        with a message about a constraint instead of about the target.

        Extension definitions are compiled *together with* nothing else here —
        the extension's own families must stand on their own before they are
        ever composed with core, so that a conflict inside the extension is
        reported as the extension's, not as a core incompatibility.
        """
        async with self._session_factory() as session:
            target = await session.execute(
                text("SELECT 1 FROM profile_revisions WHERE profile_revision_id = :rid"),
                {"rid": target_core_revision_id},
            )
            if target.first() is None:
                msg = (
                    f"target core revision {target_core_revision_id} does not exist; "
                    "an extension with no published target cannot be checked against anything"
                )
                raise ProfilePublicationError(msg)

        # Validated before compiling and before the insert, so a refused
        # override never reaches a row. A tenant who published a bad weighting
        # and had it rejected at activation would have a document in the table
        # that nothing can use and nothing will clean up.
        magnitudes = validate_overrides(scoring_overrides or {})

        compiled = self._compile(entities=entities, relationships=relationships, interfaces=interfaces)
        extension_points = tuple(sorted({definition.qualified for definition in entities}))

        # `compiled.document` is the canonical JSON *text* the output digest was
        # taken over, so the overrides are merged into a parsed copy rather than
        # appended to a string. The digest deliberately still covers the compiled
        # families only: it answers "did the type model change", and a reweighting
        # is not a change to the type model.
        document = json.loads(compiled.document)
        if magnitudes:
            # Under `magnitudes`, the key the resolver reads. Written only when
            # there is something to write: an empty key would make "publishes no
            # override" and "publishes an empty override" indistinguishable.
            document["magnitudes"] = magnitudes

        extension_id = uuid.uuid4()
        now = self._clock.now()
        async with self._session_factory() as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO profile_extensions ("
                        "  extension_revision_id, tenant_id, namespace, target_core_revision_id,"
                        "  canonical_document, document_digest, extension_points,"
                        "  compatibility_result, published_by, published_at"
                        ") VALUES (:eid, :tenant, :namespace, :target,"
                        "          CAST(:document AS JSONB), :digest, CAST(:points AS JSONB),"
                        "          :result, :published_by, :now)"
                    ),
                    {
                        "eid": extension_id,
                        "tenant": tenant_id,
                        "namespace": namespace,
                        "target": target_core_revision_id,
                        "document": json.dumps(document, sort_keys=True),
                        "digest": compiled.output_digest,
                        "points": _json_array(extension_points),
                        "result": "compatible",
                        "published_by": published_by,
                        "now": now,
                    },
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                msg = (
                    f"extension in namespace {namespace!r} for tenant {tenant_id} "
                    "conflicts with one already published"
                )
                raise DuplicatePublicationError(msg) from error

        return PublishedExtension(
            extension_revision_id=extension_id,
            tenant_id=tenant_id,
            namespace=namespace,
            target_core_revision_id=target_core_revision_id,
            document_digest=compiled.output_digest,
            extension_points=extension_points,
            published_at=now,
        )

    # -- The operations this table deliberately does not have -------------

    async def update_revision(self, *_args: object, **_kwargs: object) -> None:
        """Always refuses. Present so the refusal is discoverable.

        Without it a caller gets `AttributeError: 'ProfileService' object has no
        attribute 'update_revision'`, which reads as an incomplete service
        rather than as a decision. The database refuses this too; both exist
        because one is the rule and the other is its enforcement.
        """
        raise PublishedDocumentIsImmutable(_IMMUTABLE_GUIDANCE)

    async def delete_revision(self, *_args: object, **_kwargs: object) -> None:
        """Always refuses. See `update_revision`."""
        raise PublishedDocumentIsImmutable(_IMMUTABLE_GUIDANCE)

    # -- Reads ------------------------------------------------------------

    async def get_revision(self, revision_id: uuid.UUID) -> PublishedRevision | None:
        """A published revision's identity and chain position, or None.

        The canonical document itself is deliberately not returned here: it is
        large, it is immutable, and a caller that needs it is compiling rather
        than describing.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT profile_revision_id, profile_family, profile_name, semantic_version,"
                    "       document_digest, compatibility, predecessor_revision_id, published_at"
                    "  FROM profile_revisions WHERE profile_revision_id = :rid"
                ),
                {"rid": revision_id},
            )
            row = result.first()
        if row is None:
            return None
        return PublishedRevision(
            profile_revision_id=row[0],
            profile_family=row[1],
            profile_name=row[2],
            semantic_version=row[3],
            document_digest=row[4],
            compatibility=row[5],
            predecessor_revision_id=row[6],
            published_at=row[7],
        )

    # -- Internals --------------------------------------------------------

    def _compile(
        self,
        *,
        entities: Sequence[EntityTypeDefinition],
        relationships: Sequence[RelationshipTypeDefinition],
        interfaces: Sequence[InterfaceFamilyDefinition],
    ) -> CompiledProfile:
        try:
            return compile_profile(entities=entities, relationships=relationships, interfaces=interfaces)
        except ProfileCompositionError as error:
            raise ProfileConflictError(error.conflicts) from error


_IMMUTABLE_GUIDANCE = (
    "published profile documents are immutable. Publish a successor revision naming this one as "
    "its predecessor; the chain readers walk backwards is what makes an old digest still resolvable."
)


def _json_array(values: Sequence[str]) -> str:
    return json.dumps(list(values))


def _publication_conflict(error: IntegrityError, *, family: str, name: str) -> ProfilePublicationError:
    """Name which uniqueness rule refused, because they mean different things.

    A version collision says somebody reused a version number. A digest
    collision says these exact bytes are already published under a different
    version, which is usually a pipeline republishing unchanged input — a
    different problem with a different fix.
    """
    detail = str(error.orig) if error.orig is not None else str(error)
    if "uq_profile_revisions_digest" in detail:
        return DuplicatePublicationError(
            f"these exact bytes are already published under {family}/{name}; "
            "a second row would give one document two revision ids"
        )
    if "uq_profile_revisions_version" in detail:
        return DuplicatePublicationError(f"a revision of {family}/{name} with that semantic version already exists")
    return ProfilePublicationError(f"publication refused by the database: {detail}")


__all__ = [
    "COMPATIBILITY_VERDICTS",
    "DuplicatePublicationError",
    "ProfileConflictError",
    "ProfilePublicationError",
    "ProfileService",
    "PublishedDocumentIsImmutable",
    "PublishedExtension",
    "PublishedRevision",
]
