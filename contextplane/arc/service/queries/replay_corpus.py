"""Parametrized SQL for `arc_observation_replay_corpora` -- split from
`queries/observation.py` along the same service-ownership boundary
`replay_corpus.py` itself observes; see that module's own docstring.

Every function takes an already-open `AsyncSession` and controls no
transaction boundary of its own, matching every other queries module here.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class ReplayCorpusRow:
    corpus_id: uuid.UUID
    generator_version: str
    generator_input_digest: str
    canonical_corpus_digest: str
    fixture_class_count: int
    owning_scope: str
    target_tenant_id: uuid.UUID | None
    approving_authority_issuer: str
    approving_authority_subject: str
    approved_at: datetime.datetime
    expires_at: datetime.datetime


_CORPUS_COLS = (
    "corpus_id, generator_version, generator_input_digest, canonical_corpus_digest, fixture_class_count, "
    "owning_scope, target_tenant_id, approving_authority_issuer, approving_authority_subject, approved_at, "
    "expires_at"
)


def _corpus_row(row: Row[Any]) -> ReplayCorpusRow:
    return ReplayCorpusRow(
        corpus_id=row.corpus_id,
        generator_version=row.generator_version,
        generator_input_digest=row.generator_input_digest,
        canonical_corpus_digest=row.canonical_corpus_digest,
        fixture_class_count=row.fixture_class_count,
        owning_scope=row.owning_scope,
        target_tenant_id=row.target_tenant_id,
        approving_authority_issuer=row.approving_authority_issuer,
        approving_authority_subject=row.approving_authority_subject,
        approved_at=row.approved_at,
        expires_at=row.expires_at,
    )


async def insert_replay_corpus(
    session: AsyncSession,
    *,
    corpus_id: uuid.UUID,
    generator_version: str,
    generator_input_digest: str,
    canonical_corpus_digest: str,
    fixture_class_count: int,
    owning_scope: str,
    target_tenant_id: uuid.UUID | None,
    approving_authority_issuer: str,
    approving_authority_subject: str,
    approved_at: datetime.datetime,
    expires_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            f"INSERT INTO arc_observation_replay_corpora ({_CORPUS_COLS}) VALUES "  # noqa: S608 - module constant
            "(:corpus_id, :generator_version, :generator_input_digest, :canonical_corpus_digest, "
            " :fixture_class_count, :owning_scope, :target_tenant_id, :approving_authority_issuer, "
            " :approving_authority_subject, :approved_at, :expires_at)"
        ),
        {
            "corpus_id": corpus_id,
            "generator_version": generator_version,
            "generator_input_digest": generator_input_digest,
            "canonical_corpus_digest": canonical_corpus_digest,
            "fixture_class_count": fixture_class_count,
            "owning_scope": owning_scope,
            "target_tenant_id": target_tenant_id,
            "approving_authority_issuer": approving_authority_issuer,
            "approving_authority_subject": approving_authority_subject,
            "approved_at": approved_at,
            "expires_at": expires_at,
        },
    )


async def load_replay_corpus_by_digest(session: AsyncSession, canonical_corpus_digest: str) -> ReplayCorpusRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_CORPUS_COLS} FROM arc_observation_replay_corpora "  # noqa: S608 - module constant
                "WHERE canonical_corpus_digest = :digest"
            ),
            {"digest": canonical_corpus_digest},
        )
    ).one_or_none()
    return None if row is None else _corpus_row(row)


async def load_current_replay_corpus(
    session: AsyncSession, *, owning_scope: str, target_tenant_id: uuid.UUID | None, now: datetime.datetime
) -> ReplayCorpusRow | None:
    """The newest unexpired approved corpus for this exact scope, if any."""
    if owning_scope == "global":
        clause: str = "owning_scope = 'global'"
        params: dict[str, Any] = {}
    else:
        clause = "owning_scope = 'tenant' AND target_tenant_id = :tid"
        params = {"tid": target_tenant_id}
    row = (
        await session.execute(
            text(
                # _CORPUS_COLS is a module constant; `clause` is one of the two literal
                # strings assigned above -- neither is caller input.
                f"SELECT {_CORPUS_COLS} FROM arc_observation_replay_corpora "  # noqa: S608
                f"WHERE {clause} AND expires_at > :now ORDER BY approved_at DESC LIMIT 1"
            ),
            {**params, "now": now},
        )
    ).one_or_none()
    return None if row is None else _corpus_row(row)


__all__ = [
    "ReplayCorpusRow",
    "insert_replay_corpus",
    "load_current_replay_corpus",
    "load_replay_corpus_by_digest",
]
