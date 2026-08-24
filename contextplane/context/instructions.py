"""The instruction channel: what the caller declared, and what we say back.

E22-T14, implementing ADR 0020. Three operations and one arm.

**Declaration is a digest, submitted content is separate.** A caller sends a
digest of its instruction set on every resolve; if the service has not seen that
digest, the caller submits the content once and every later resolve carries the
digest alone. The round trip that buys is one submission per *distinct
instruction set*, not per resolve -- and an agent's base instructions change on
the order of days while it resolves many times a minute.

**An unknown digest resolves normally.** Refusing would fail a first-run resolve
for a state the service is in rather than one the caller caused. So three
dispositions are distinguished and never two:

- `NOT_DECLARED` -- the caller sent no digest;
- `DECLARED_UNKNOWN` -- a digest arrived and its content was never submitted;
- `DECLARED_KNOWN` -- the content is on hand and a delta is computable.

Collapsing the first two is what makes partial adoption invisible, which is the
failure the decision's dissent predicts. Every surface built on this signal has
to be able to tell them apart, so the service reports which one it was rather
than reporting an empty block three different ways.

**This module never stores the caller's instructions as truth.** What it holds
is the set that was in force at a resolution, which is a historical fact about a
resolution rather than a current fact about an agent. Nothing here reads that
content back to an agent to tell it what its instructions are.

**Selection is ADR 0021, and it is three scopes rather than a predicate.** A
delta corrects one declared set, or whatever a named principal declares at any
digest, or every declaring caller in the tenant. A rule engine over instruction
content would be the inference ADR 0020 rejected as unfalsifiable, one level up:
an author who wrote a predicate could not say afterwards which agents it reached.

**Every applicable delta is served, and each says its own scope.** Serving only
the narrowest was rejected — a tenant-wide correction about credential handling
and a digest-specific one about deprecation checks are not alternatives, and
suppressing either because the other exists would withhold a governed
instruction on the strength of a coincidence.

Precedence is in the payload rather than in the order. The envelope sorts every
block by receipt item id so a receipt is checkable across two resolutions, so an
ordering asserted here would be one the envelope discards. The read below still
orders narrowest-first, and that is load-bearing in one place: the joined
contradiction note reads most-specific first.

**A tenant-scoped delta reaches callers whose content was never submitted**, and
that is deliberate. ADR 0020's dissent is that a `declared_unknown` caller
receives nothing forever; this is the part of that answerable without their
content. Contradiction still cannot be computed for them, and the record says so
rather than reporting none.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import hashlib
import re
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from contextplane.context.assembler import contextual_item
from contextplane.context.schemas.envelope import BLOCK_INSTRUCTIONS
from contextplane.context.schemas.trust import TRUST_ASSERTED, TrustMetadataV1
from contextplane.exceptions import ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.schemas.envelope import ContextItemV1
    from contextplane.types import TenantContext

#: The provenance string on every served delta. A stable system identifier
#: rather than a display name, because display names get renamed and the
#: provenance goes with them.
DELTA_SOURCE: Final = "contextplane.instruction_delta"

#: `sha256:` and 64 lowercase hex characters, matching the spelling the feedback
#: area already uses. Pinned in one place and checked in the schema too, so a
#: second writer cannot introduce a bare-hex variant that never joins.
_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The schema's bound on submitted content, repeated here so an over-large
#: submission is refused with an explanation rather than by a constraint
#: violation the caller cannot read.
MAX_CONTENT_CHARS: Final = 262_144


class InvalidInstructionDeclaration(ValidationError):
    """A digest or a body the channel refuses. Raised, never repaired.

    A `ValidationError` so both transports already map it to a caller fault
    rather than a server one. A refusal type of its own would need mapping twice
    and would be mapped correctly once.
    """


class Disposition(enum.StrEnum):
    """What was known about the caller's instruction set at resolve time.

    Three members rather than two, and the surfaces are required to render all
    three. `DECLARED_UNKNOWN` reported as `NOT_DECLARED` would make an
    integration that declares look identical to one that never adopted the
    channel at all, which is the precise shape of quiet degradation the decision
    behind this exists to prevent.
    """

    NOT_DECLARED = "not_declared"
    DECLARED_UNKNOWN = "declared_unknown"
    DECLARED_KNOWN = "declared_known"


#: Said back to a caller whose instruction block is empty, per disposition.
#: Phrased as what the caller can do about it where there is something, because
#: three identical empty blocks with different causes teach a caller to ignore
#: all three.
BLOCK_NOTES: Final[dict[Disposition, str]] = {
    Disposition.NOT_DECLARED: (
        "the instructions block is empty because the request declared no instruction set; "
        "send instruction_digest to receive governed corrections to it"
    ),
    Disposition.DECLARED_UNKNOWN: (
        "the instructions block is empty because the declared instruction set was never submitted; "
        "no delta can be computed against content the service has not seen, and this is not the same "
        "as there being no corrections -- submit the content once to find out which"
    ),
    Disposition.DECLARED_KNOWN: (
        "the instructions block is empty because no governed correction applies to the declared " "instruction set"
    ),
}


def digest_of(content: str) -> str:
    """The digest of one instruction set, in the one spelling that joins."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def validated_digest(digest: str) -> str:
    """`digest` if it is well-formed, else a refusal naming the shape.

    Checked before the row is written rather than after, because a malformed
    digest stored on a declaration would be a declaration that can never match a
    submission -- indistinguishable, later, from an integration that submitted
    nothing.
    """
    if not _DIGEST_PATTERN.match(digest):
        raise InvalidInstructionDeclaration(
            f"instruction_digest must be 'sha256:' and 64 lowercase hex characters, got {digest!r}"
        )
    return digest


@dataclasses.dataclass(frozen=True)
class ServedDelta:
    """One governed correction, as it was served."""

    delta_id: uuid.UUID
    body: str
    contradicts: bool
    contradiction_note: str | None
    authored_at: datetime.datetime
    #: Which of ADR 0021's three scopes put this delta in front of this caller:
    #: their declared set, them as a principal, or their whole tenant. Carried to
    #: the agent because "everyone was told this" and "you were told this" are
    #: different weights on the same sentence.
    scope: str = "digest"


@dataclasses.dataclass(frozen=True)
class DeclarationOutcome:
    """What the channel did for one resolution.

    Carries the disposition even when nothing was served, because "no delta
    applied" and "no delta was computable" are the same empty block with
    opposite meanings, and only the record distinguishes them.
    """

    disposition: Disposition
    digest: str | None
    deltas: tuple[ServedDelta, ...] = ()

    @property
    def contradictions(self) -> tuple[ServedDelta, ...]:
        """The served deltas that contradict what the caller declared.

        Served, not withheld: the contradicting delta is usually the valuable
        one -- the whole point of the channel is to say "your instructions are
        wrong about this" -- and a channel that holds its most useful message
        until a human notices is one nobody comes to rely on. Flagged, though,
        and the flag is this.
        """
        return tuple(delta for delta in self.deltas if delta.contradicts)

    def contradiction_note(self) -> str | None:
        """One line naming everything contradicted, or `None`.

        Joined rather than reduced to a count: a resolution that says "2
        contradictions" gives an evaluator nothing to act on, and the decision
        requires the record to say *what* was contradicted.
        """
        notes = [delta.contradiction_note for delta in self.contradictions if delta.contradiction_note]
        if not notes:
            return None
        return "; ".join(notes)


_SUBMIT = text(
    """
    INSERT INTO declared_instruction_sets (tenant_id, digest, content, submitted_by, submitted_at)
    VALUES (:tenant_id, :digest, :content, :actor_id, :now)
    ON CONFLICT (tenant_id, digest) DO NOTHING
    """
)

_IS_KNOWN = text("SELECT 1 FROM declared_instruction_sets WHERE tenant_id = :tenant_id AND digest = :digest")

_CONTENT_OF = text("SELECT content FROM declared_instruction_sets WHERE tenant_id = :tenant_id AND digest = :digest")

#: The serving read. Tenant-scoped in the predicate rather than filtered after,
#: and withdrawn rows are excluded here rather than skipped by the caller: a
#: suppression a caller has to remember is one a second caller will forget.
_LIVE_DELTAS = text(
    """
    SELECT delta_id, body, contradicts, contradiction_note, authored_at, scope
      FROM instruction_deltas
     WHERE tenant_id = :tenant_id
       AND withdrawn_at IS NULL
       AND (
             (scope = 'digest'    AND target_digest = :digest)
          OR (scope = 'principal' AND target_principal = :actor_id)
          OR  scope = 'tenant'
           )
     -- Narrowest first. Not what the envelope serves in -- `ordered_items` sorts
     -- every block by receipt item id -- but what `contradiction_note()` joins
     -- in, so the record of what a resolution contradicted reads most-specific
     -- first.
     ORDER BY CASE scope WHEN 'digest' THEN 0 WHEN 'principal' THEN 1 ELSE 2 END,
              authored_at,
              delta_id
     LIMIT :limit
    """
)

_RECORD_DECLARATION = text(
    """
    INSERT INTO resolution_instruction_declarations (
        tenant_id, receipt_id, actor_id, digest, content_known,
        contradicted, contradiction_note, declared_at
    )
    VALUES (
        :tenant_id, :receipt_id, :actor_id, :digest, :content_known,
        :contradicted, :contradiction_note, :now
    )
    """
)


class InstructionChannel:
    """Submission, disposition and the delta read, for one deployment.

    Owns no transaction. Every method takes or opens the session it needs, so
    the resolver can record a declaration inside the same unit of work as its
    receipt without this class knowing that is what is happening.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def submit(self, ctx: TenantContext, *, content: str, now: datetime.datetime) -> str:
        """Store one instruction set, keyed by its digest, and return the digest.

        Idempotent by construction: the digest *is* the content, so a second
        submission of the same set is the same row. `DO NOTHING` rather than
        `DO UPDATE` -- overwriting would let a later submitter change what a
        resolution months ago is recorded as having been served against.
        """
        body = content.strip()
        if not body:
            raise InvalidInstructionDeclaration("an instruction set with no content is not a declaration")
        if len(body) > MAX_CONTENT_CHARS:
            raise InvalidInstructionDeclaration(
                f"an instruction set is limited to {MAX_CONTENT_CHARS} characters, got {len(body)}"
            )

        digest = digest_of(body)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                _SUBMIT,
                {
                    "actor_id": ctx.actor_id,
                    "content": body,
                    "digest": digest,
                    "now": now,
                    "tenant_id": ctx.tenant_id,
                },
            )
        return digest

    async def content_of(self, ctx: TenantContext, digest: str) -> str | None:
        """The submitted content for one digest, or `None` if never submitted."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CONTENT_OF, {"digest": validated_digest(digest), "tenant_id": ctx.tenant_id}
            )
            row = result.first()
        return None if row is None else str(row.content)

    async def resolve_declaration(self, ctx: TenantContext, *, digest: str | None, limit: int) -> DeclarationOutcome:
        """The disposition and the deltas for one resolve.

        Both in one call and one session, because the disposition is a statement
        about the read that produced the deltas. Computing them separately would
        admit a window in which content arrived between the two, and the
        resolution would record `declared_known` for a delta read that ran
        against nothing.
        """
        if digest is None:
            return DeclarationOutcome(disposition=Disposition.NOT_DECLARED, digest=None)

        checked = validated_digest(digest)
        async with self._session_factory() as session:
            known = (await session.execute(_IS_KNOWN, {"digest": checked, "tenant_id": ctx.tenant_id})).first()

            # The read runs whether or not the content is known, which is ADR
            # 0021's stated consequence. A `declared_unknown` caller cannot be
            # served a `digest`-scoped delta -- there is nothing to target -- but
            # a `principal` or `tenant` one reaches them, and withholding it
            # would leave exactly the callers ADR 0020's dissent is about
            # receiving nothing forever.
            rows = (
                await session.execute(
                    _LIVE_DELTAS,
                    {
                        "actor_id": ctx.actor_id,
                        "digest": checked,
                        "limit": limit,
                        "tenant_id": ctx.tenant_id,
                    },
                )
            ).all()

        return DeclarationOutcome(
            # Still `declared_unknown` when the content never arrived. Serving a
            # delta does not make the set known, and a contradiction against it
            # is still not computable -- the disposition is what tells a surface
            # to say so rather than to report no contradictions.
            disposition=Disposition.DECLARED_KNOWN if known is not None else Disposition.DECLARED_UNKNOWN,
            digest=checked,
            deltas=tuple(
                ServedDelta(
                    authored_at=row.authored_at,
                    body=str(row.body),
                    scope=str(row.scope),
                    contradiction_note=row.contradiction_note,
                    contradicts=bool(row.contradicts),
                    delta_id=row.delta_id,
                )
                for row in rows
            ),
        )

    async def record(
        self,
        ctx: TenantContext,
        *,
        outcome: DeclarationOutcome,
        receipt_id: uuid.UUID | None,
        now: datetime.datetime,
    ) -> None:
        """Write what this resolution declared. A no-op when nothing was declared.

        No row for `NOT_DECLARED` -- the absence *is* the record, and writing a
        row per undeclared resolve would put a row on every resolution in the
        product to say nothing happened.
        """
        if outcome.disposition is Disposition.NOT_DECLARED:
            return

        note = outcome.contradiction_note()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                _RECORD_DECLARATION,
                {
                    "actor_id": ctx.actor_id,
                    "content_known": outcome.disposition is Disposition.DECLARED_KNOWN,
                    "contradicted": note is not None,
                    "contradiction_note": note,
                    "digest": outcome.digest,
                    "now": now,
                    "receipt_id": receipt_id,
                    "tenant_id": ctx.tenant_id,
                },
            )


def delta_items(outcome: DeclarationOutcome) -> tuple[ContextItemV1, ...]:
    """The served deltas as block items.

    `policy` because a correction to an agent's instructions is a policy
    statement about how to act, not a fact about the world; `asserted` because a
    person authored it and stands behind it, and never `attested` -- authoring a
    delta is not an attestation path, and promoting it to one here would let a
    correction reach an agent wearing the weight of a governed artifact.

    `mutable` because a delta can be withdrawn after this read, and an agent
    caching one would outlive the correction it cached.
    """
    return tuple(
        contextual_item(
            block=BLOCK_INSTRUCTIONS,
            source=DELTA_SOURCE,
            item_key=str(delta.delta_id),
            payload=_delta_payload(delta),
            trust=TrustMetadataV1(
                assertion_kind="policy",
                attribution=None,
                authority=DELTA_SOURCE,
                classification="internal",
                freshness=delta.authored_at,
                mutability="mutable",
                source=DELTA_SOURCE,
                trust=TRUST_ASSERTED,
            ),
        )
        for delta in outcome.deltas
    )


def _delta_payload(delta: ServedDelta) -> dict[str, object]:
    """One delta as an agent receives it.

    `contradicts` is in the payload rather than only in the resolution record,
    because the agent is the party that has to weigh a correction against what
    its operator told it, and a flag only an evaluator can see days later does
    not help it do that.
    """
    return {
        "body": delta.body,
        "contradicts": delta.contradicts,
        "contradiction_note": delta.contradiction_note,
        "delta_id": str(delta.delta_id),
        "scope": delta.scope,
    }


__all__ = [
    "BLOCK_NOTES",
    "DELTA_SOURCE",
    "MAX_CONTENT_CHARS",
    "DeclarationOutcome",
    "Disposition",
    "InstructionChannel",
    "InvalidInstructionDeclaration",
    "ServedDelta",
    "delta_items",
    "digest_of",
    "validated_digest",
]
