"""Reading back what may approve: enrolled verifiers and standing exceptions.

Thirteen of fourteen `/v1/arc/admin` paths were write-only, and the one `GET`
read the *caller* rather than any governed object. So an operator could enrol a
verifier and then never find it again, and an exception — an object whose whole
definition is *documented* deviation — was invisible from the moment it was
granted.

## Per object, sharing a shape

`GovernanceObject` is a response contract, not a query. The six ARC governance
objects share intent and not schema: scope is `scope_kind` here and
`owning_scope` there, the tenant column has three spellings, and "still in force"
is a `revoked_at`, an `effective_until`, or — for source connectors — nothing at
all. A single index would have to normalise across that, and normalising
in-force for an object that cannot be revoked means inventing a state the schema
does not have.

So each object gets its own read, and they agree on what they return.

## In force is computed, never stored

Both tables carry the columns the answer is made of, and neither carries the
answer. Computing it means one place decides what "in force" means and a caller
cannot disagree by reading the columns differently — which is exactly what four
screens would otherwise do, four ways.

An exception with no `effective_until` is in force **indefinitely**, and says
so rather than being handed an invented expiry. That is a real state: an
open-ended exception is a policy change wearing a smaller word, and the surface
that shows it should make that visible rather than tidy it away.

## What this does not decrypt

`arc_approved_exceptions` stores its statement and justification encrypted at
rest, with plaintext columns as the alternative representation. This read
returns the plaintext where a deployment stores plaintext and **nothing** where
it stores ciphertext — it does not reach for a key. A list endpoint that
decrypted every row would turn "which exceptions exist" into a bulk disclosure
of why each was granted, which is a different question with a different
audience. `has_statement` says whether there is content to fetch, so a reader
can tell "no justification was given" from "you are not seeing it here".
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.types import Clock

#: What every governance object answers, whatever it is. The shared half of the
#: response, so four screens read alike without a union query pretending the
#: tables are one table.
KIND_VERIFIER: Final[str] = "approval_verifier"
KIND_EXCEPTION: Final[str] = "approved_exception"
KIND_CONNECTOR: Final[str] = "source_connector"
KIND_UPLOAD_POLICY: Final[str] = "source_upload_policy"
KIND_REPLAY_CORPUS: Final[str] = "observation_replay_corpus"

#: How long a page may be. A governance surface answers "what is in force", and
#: an operator scrolling six pages of it has already lost the answer.
MAX_PAGE: Final[int] = 200
DEFAULT_PAGE: Final[int] = 50


@dataclasses.dataclass(frozen=True)
class GovernanceObject:
    """The shared shape. `detail` carries whatever the object itself is."""

    kind: str
    object_id: str
    scope: str
    target_tenant_id: uuid.UUID | None
    #: Computed from the row's own columns, never stored. See the module docstring.
    in_force: bool
    #: `None` when nothing ends it — which is a state, not a missing value.
    in_force_until: datetime.datetime | None
    created_at: datetime.datetime
    detail: dict[str, object]


def _verifier_in_force(row: object, now: datetime.datetime) -> bool:
    """Not revoked, and inside its validity window.

    Three columns and one answer. A caller reading the columns itself would have
    to know that `valid_to` is nullable and that a null means "no end" rather
    than "expired", which is the kind of thing one reader gets wrong once.
    """
    if row.revoked_at is not None:  # type: ignore[attr-defined]
        return False
    if row.valid_from > now:  # type: ignore[attr-defined]
        return False
    valid_to = row.valid_to  # type: ignore[attr-defined]
    return valid_to is None or valid_to > now


def _exception_in_force(row: object, now: datetime.datetime) -> bool:
    if row.revoked_at is not None:  # type: ignore[attr-defined]
        return False
    if row.effective_from > now:  # type: ignore[attr-defined]
        return False
    until = row.effective_until  # type: ignore[attr-defined]
    return until is None or until > now


class GovernanceReadService:
    """Lists the ARC governance objects an operator has to be able to find."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_approval_verifiers(
        self,
        *,
        tenant_id: uuid.UUID | None,
        in_force_only: bool = False,
        page_size: int = DEFAULT_PAGE,
    ) -> list[GovernanceObject]:
        """Enrolled verifiers, newest first.

        `tenant_id` selects the tenant's own verifiers **and** the global ones,
        because a global verifier can approve for that tenant and a list that
        omitted them would answer "who may approve here" wrongly by exactly the
        set that matters most.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT approval_verifier_id, verifier_kind, allowed_evidence_types, "
                        "       scope_kind, scope_tenant_id, provider_id, valid_from, valid_to, "
                        "       revoked_at, created_at "
                        "  FROM arc_approval_verifiers "
                        " WHERE (CAST(:tid AS UUID) IS NULL "
                        "        OR scope_kind = 'global' OR scope_tenant_id = CAST(:tid AS UUID)) "
                        " ORDER BY created_at DESC, approval_verifier_id "
                        " LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": min(page_size, MAX_PAGE)},
                )
            ).all()

        found = [
            GovernanceObject(
                kind=KIND_VERIFIER,
                object_id=row.approval_verifier_id,
                scope=row.scope_kind,
                target_tenant_id=row.scope_tenant_id,
                in_force=_verifier_in_force(row, now),
                in_force_until=row.valid_to,
                created_at=row.created_at,
                detail={
                    "verifier_kind": row.verifier_kind,
                    "allowed_evidence_types": list(row.allowed_evidence_types),
                    # The provider, never the key. `public_key` is deliberately
                    # not selected: a list of who may approve does not need the
                    # material, and a surface that returned it would be one more
                    # place a key can leak from.
                    "provider_id": row.provider_id,
                    "valid_from": row.valid_from,
                    "revoked_at": row.revoked_at,
                },
            )
            for row in rows
        ]
        return [item for item in found if item.in_force] if in_force_only else found

    async def list_source_connectors(
        self,
        *,
        tenant_id: uuid.UUID | None,
        in_force_only: bool = False,
        page_size: int = DEFAULT_PAGE,
    ) -> list[GovernanceObject]:
        """What ARC may fetch, and from where.

        Global grants are included for the same reason verifiers are: a global
        connector admits material for this tenant, so a list that showed only
        the tenant's own would understate what may reach it.

        **This entry's own premise changed while it waited.** E14-T1b was written
        when a connector could not be revoked at all, and said its honest
        in-force answer was "permanent". E14-T2 gave both tables a `revoked_at`,
        so the answer is now a real one — and `in_force_until` stays null,
        because a live connector still has no expiry. Withdrawal is the only
        thing that ends it.
        """
        return await self._list_grant(
            table="arc_source_connectors",
            id_column="connector_id",
            kind=KIND_CONNECTOR,
            extra_columns="allowed_schemes, allowed_hosts, allowed_media_types, " "allowed_verifier_ids, max_bytes",
            tenant_id=tenant_id,
            in_force_only=in_force_only,
            page_size=page_size,
        )

    async def list_upload_policies(
        self,
        *,
        tenant_id: uuid.UUID | None,
        in_force_only: bool = False,
        page_size: int = DEFAULT_PAGE,
    ) -> list[GovernanceObject]:
        """The same grant, pushed rather than pulled."""
        return await self._list_grant(
            table="arc_source_upload_policies",
            id_column="policy_id",
            kind=KIND_UPLOAD_POLICY,
            extra_columns="allowed_media_types, allowed_verifier_ids, max_bytes",
            tenant_id=tenant_id,
            in_force_only=in_force_only,
            page_size=page_size,
        )

    async def _list_grant(
        self,
        *,
        table: str,
        id_column: str,
        kind: str,
        extra_columns: str,
        tenant_id: uuid.UUID | None,
        in_force_only: bool,
        page_size: int,
    ) -> list[GovernanceObject]:
        """Connectors and upload policies, which differ only in their payload.

        One method because they are the same governed thing with different
        columns — and two tables that are supposed to behave identically are
        exactly the pair that drifts when each gets its own query.
        """
        # No clock read: unlike a verifier or a corpus, these two have no time
        # component at all. A grant is live until somebody withdraws it, so
        # "in force" is a null check, and asking the clock would imply otherwise.
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {id_column} AS object_id, owning_scope, tenant_id, registered_at, "  # noqa: S608 - table and column names are module constants, not caller input
                        f"       revoked_at, revocation_reason, {extra_columns} "
                        f"  FROM {table} "
                        " WHERE (CAST(:tid AS UUID) IS NULL "
                        "        OR owning_scope = 'global' OR tenant_id = CAST(:tid AS UUID)) "
                        f" ORDER BY registered_at DESC, {id_column} "
                        " LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": min(page_size, MAX_PAGE)},
                )
            ).mappings()

        found = [
            GovernanceObject(
                kind=kind,
                object_id=row["object_id"],
                scope=row["owning_scope"],
                target_tenant_id=row["tenant_id"],
                in_force=row["revoked_at"] is None,
                # Null, and not because nobody filled it in: a live grant has no
                # expiry. Withdrawal is the only thing that ends one, which is
                # why `in_force` and `in_force_until` disagree here in a way they
                # do not for a verifier.
                in_force_until=None,
                created_at=row["registered_at"],
                detail={
                    key: (list(row[key]) if isinstance(row[key], list) else row[key])
                    for key in ("revoked_at", "revocation_reason", *extra_columns.replace(" ", "").split(","))
                },
            )
            for row in rows
        ]
        return [item for item in found if item.in_force] if in_force_only else found

    async def list_replay_corpora(
        self,
        *,
        tenant_id: uuid.UUID | None,
        in_force_only: bool = False,
        page_size: int = DEFAULT_PAGE,
    ) -> list[GovernanceObject]:
        """What observation is replayed against, and until when.

        The only one of the three with a real expiry: a corpus approval lapses on
        its own, where a connector runs until somebody withdraws it. So this is
        the one where `in_force_until` carries a date, and a reader comparing the
        three learns something true about how they differ.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT corpus_id, owning_scope, target_tenant_id, generator_version, "
                        "       canonical_corpus_digest, fixture_class_count, approved_at, expires_at "
                        "  FROM arc_observation_replay_corpora "
                        " WHERE (CAST(:tid AS UUID) IS NULL "
                        "        OR owning_scope = 'global' OR target_tenant_id = CAST(:tid AS UUID)) "
                        " ORDER BY approved_at DESC, corpus_id "
                        " LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": min(page_size, MAX_PAGE)},
                )
            ).all()

        found = [
            GovernanceObject(
                kind=KIND_REPLAY_CORPUS,
                object_id=str(row.corpus_id),
                scope=row.owning_scope,
                target_tenant_id=row.target_tenant_id,
                in_force=row.approved_at <= now < row.expires_at,
                in_force_until=row.expires_at,
                created_at=row.approved_at,
                detail={
                    "generator_version": row.generator_version,
                    # The digest *is* the corpus. A regenerated one at the same
                    # generator version is a different digest and a separate
                    # approval, which is what keeps "it behaved correctly"
                    # meaning one thing across qualifications.
                    "canonical_corpus_digest": row.canonical_corpus_digest,
                    "fixture_class_count": row.fixture_class_count,
                },
            )
            for row in rows
        ]
        return [item for item in found if item.in_force] if in_force_only else found

    async def list_approved_exceptions(
        self,
        *,
        tenant_id: uuid.UUID,
        in_force_only: bool = False,
        page_size: int = DEFAULT_PAGE,
    ) -> list[GovernanceObject]:
        """Exceptions granted for one tenant, newest first.

        Tenant-scoped without a global escape, unlike verifiers: an exception's
        `lower_scope_tenant_id` is `NOT NULL`, so every exception belongs to
        exactly one tenant and there is no cross-tenant set to include.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT exception_id, higher_scope_directive_id, lower_scope_kind, "
                        "       lower_scope_tenant_id, lower_scope_domain_id, "
                        "       lower_scope_entity_id, effective_from, effective_until, "
                        "       revoked_at, created_at, approval_evidence_id, "
                        "       exception_statement_plaintext, "
                        "       (exception_statement_ciphertext IS NOT NULL "
                        "        OR exception_statement_plaintext IS NOT NULL) AS has_statement "
                        "  FROM arc_approved_exceptions "
                        " WHERE lower_scope_tenant_id = :tid "
                        " ORDER BY created_at DESC, exception_id "
                        " LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": min(page_size, MAX_PAGE)},
                )
            ).all()

        found = [
            GovernanceObject(
                kind=KIND_EXCEPTION,
                object_id=str(row.exception_id),
                scope=row.lower_scope_kind,
                target_tenant_id=row.lower_scope_tenant_id,
                in_force=_exception_in_force(row, now),
                in_force_until=row.effective_until,
                created_at=row.created_at,
                detail={
                    "higher_scope_directive_id": str(row.higher_scope_directive_id),
                    "approval_evidence_id": str(row.approval_evidence_id),
                    "lower_scope_domain_id": row.lower_scope_domain_id,
                    # `entity_id`, not `capability_id`: migration 0049 renamed
                    # it, and the baseline's spelling is two years of history
                    # rather than the live column.
                    "lower_scope_entity_id": (str(row.lower_scope_entity_id) if row.lower_scope_entity_id else None),
                    "effective_from": row.effective_from,
                    "revoked_at": row.revoked_at,
                    # Plaintext where the deployment stores plaintext, and
                    # nothing where it stores ciphertext -- this read does not
                    # reach for a key. `has_statement` is what lets a reader tell
                    # "none was given" from "not shown here".
                    "statement": row.exception_statement_plaintext,
                    "has_statement": bool(row.has_statement),
                },
            )
            for row in rows
        ]
        return [item for item in found if item.in_force] if in_force_only else found
