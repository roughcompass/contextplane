"""Prompt sets, runs over them, and verdicts that outlive the page.

E22-T15. Context Lab resolves one prompt and forgets it. It states its own
boundary plainly — *the resolver retrieves context only; it does not call a
language model, generate an answer, or invent an evaluation score* — and that
boundary holds here unchanged. A run resolves and records; a verdict is a person
saying what they thought. Nothing in this module produces a number about
quality.

## What this adds that Context Lab cannot do

A **set** of prompts rather than one at a time. A **run**, which resolves the
whole set and keeps the receipts. **Comparison**, because two runs of one set are
two rows a reader can put side by side. And **persistence**, so a judgement can
be revisited after a change instead of being lost with the tab — which is what
makes *"what changed for this prompt set after I adjusted that policy?"*
answerable at all.

## Three rules, each because its opposite is the ordinary way this goes wrong

**An errored prompt stays in the run.** A resolution that raised produces an item
with no receipt and a failure, not an absent row. Dropping it is how a number
improves without anything improving, and it is indistinguishable from the system
having got better at the prompts it did not crash on. The research harness next
door states the same rule for the same reason.

**A run pins the deployment that produced it.** Two runs with different
fingerprints are not comparable, and this module says so rather than letting a
surface diff them — a policy change reported as a quality change is the specific
wrong answer this whole loop exists to avoid producing.

**A prompt is resolved through the same resolver every caller uses.** Not a copy
of it, and not with checks relaxed for evaluation. An evaluation that ran against
a laxer path would be measuring something the product does not serve, which is
the most expensive kind of wrong answer available here.

## The guard is here and not in a router

Every service has two transports; a check on a route is a check the MCP tool does
not have. Tenant scoping is in the predicates below, and the write paths take the
caller's identity rather than accepting one.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from contextplane.context.evaluation.expectations import ExpectationsV1
from contextplane.context.evaluation.prompt_request import PromptRequestV1
from contextplane.exceptions import NotFoundError, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.resolve import ContextResolver
    from contextplane.types import Clock, TenantContext

#: The most prompts one set may hold. A bound rather than a target: a run
#: resolves every prompt, so an unbounded set is one caller deciding how much
#: work every other request queues behind.
MAX_PROMPTS: Final[int] = 100

#: The most rows any list read returns.
MAX_PAGE_SIZE: Final[int] = 100

#: What a reviewer may say. Closed, and three rather than a scale: the boundary
#: this inherits is that nothing here invents a number, and a five-point scale is
#: a number wearing words.
VERDICTS: Final[tuple[str, ...]] = ("right", "wrong", "unusable")


@dataclasses.dataclass(frozen=True)
class PromptSet:
    """A named collection of prompts, and how many it holds."""

    set_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime.datetime
    retired_at: datetime.datetime | None
    prompt_count: int


@dataclasses.dataclass(frozen=True)
class Prompt:
    """One request in a set, where it sits, and what it is checking."""

    prompt_id: uuid.UUID
    position: int
    request: dict[str, Any]
    intent_note: str | None
    #: What this prompt asserts about a run, declared before the run. `None` is a
    #: real state -- an evaluator exploring has not yet decided what good looks
    #: like -- and is different from an object full of permissive thresholds,
    #: which would be checks that always pass.
    expectations: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class RunItem:
    """One prompt's resolution within a run.

    `receipt_id` and `failure` are exclusive and the schema enforces it. A reader
    branching on which is present is branching on whether the resolution
    happened, which is the distinction that matters.
    """

    item_id: uuid.UUID
    prompt_id: uuid.UUID
    position: int
    receipt_id: uuid.UUID | None
    envelope_state: str | None
    failure: str | None
    duration_ms: int
    verdicts: tuple[Verdict, ...] = ()


@dataclasses.dataclass(frozen=True)
class Verdict:
    """One reviewer's judgement of one prompt's resolution."""

    verdict: str
    note: str | None
    recorded_by: uuid.UUID
    recorded_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class Run:
    """One execution of a set, and what it was executed under."""

    run_id: uuid.UUID
    set_id: uuid.UUID
    resolver_fingerprint: str
    prompt_count: int
    started_at: datetime.datetime
    finished_at: datetime.datetime | None
    items: tuple[RunItem, ...] = ()

    def comparable_to(self, other: Run) -> bool:
        """Whether two runs measure the same thing.

        Same set and same deployment. Different fingerprints mean the
        configuration moved between them, so a difference in results is not
        evidence about retrieval quality — it is evidence the configuration
        changed, which the reader already knew.
        """
        return self.set_id == other.set_id and self.resolver_fingerprint == other.resolver_fingerprint


class EvaluationRunService:
    """Prompt sets, runs, and verdicts, for one deployment."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        resolver: ContextResolver,
        clock: Clock,
        fingerprint: str,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver
        self._clock = clock
        # Computed once by the composition root, because it describes the
        # deployment and not the request. Recomputing per run would let a value
        # that is supposed to be constant across a process vary within one.
        self._fingerprint = fingerprint

    # -- prompt sets -------------------------------------------------------

    async def create_set(self, ctx: TenantContext, *, name: str, description: str | None = None) -> PromptSet:
        """A named set, empty. Prompts are added afterwards."""
        label = name.strip()
        if not (1 <= len(label) <= 200):
            raise ValidationError("a prompt set's name is 1 to 200 characters")

        now = self._clock.now()
        set_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO evaluation_prompt_sets "
                    "(set_id, tenant_id, name, description, created_by, created_at) "
                    "VALUES (:sid, :tid, :name, :description, :actor, :now)"
                ),
                {
                    "actor": ctx.actor_id,
                    "description": (description or "").strip() or None,
                    "name": label,
                    "now": now,
                    "sid": set_id,
                    "tid": ctx.tenant_id,
                },
            )
        return PromptSet(
            created_at=now,
            description=(description or "").strip() or None,
            name=label,
            prompt_count=0,
            retired_at=None,
            set_id=set_id,
        )

    async def add_prompt(
        self,
        ctx: TenantContext,
        *,
        set_id: uuid.UUID,
        request: dict[str, Any],
        intent_note: str | None = None,
        expectations: dict[str, Any] | None = None,
    ) -> Prompt:
        """Append one prompt to a set, with what it is checking.

        `request` is validated before it is stored, so the JSON column is not a
        place unvalidated shapes accumulate. A set whose prompts cannot be
        resolved is one that fails at run time, per prompt, on every run — which
        is a much worse place to discover it.

        `expectations` is validated for the same reason and declared here rather
        than after a run, on `scenarios.py`'s argument: *a scenario whose required
        facts were written after seeing what the system returned would be
        satisfied by whatever the system returned*. Writing them at the prompt is
        what makes that true by construction — there is nowhere to put one later.
        """
        validated = PromptRequestV1.of(request)
        declared = ExpectationsV1.of(expectations) if expectations is not None else None

        async with self._session_factory() as session, session.begin():
            await self._require_live_set(session, ctx, set_id)
            count = (
                await session.execute(
                    text("SELECT count(*) FROM evaluation_prompts WHERE set_id = :sid AND tenant_id = :tid"),
                    {"sid": set_id, "tid": ctx.tenant_id},
                )
            ).scalar_one()
            if count >= MAX_PROMPTS:
                raise ValidationError(f"a prompt set holds at most {MAX_PROMPTS} prompts, and this one has {count}")

            prompt_id = uuid.uuid4()
            note = (intent_note or "").strip() or None
            stored = validated.stored()
            stored_expectations = declared.stored() if declared is not None else None
            await session.execute(
                text(
                    "INSERT INTO evaluation_prompts "
                    "(prompt_id, set_id, tenant_id, position, request, intent_note, expectations, added_at) "
                    "VALUES (:pid, :sid, :tid, :position, CAST(:request AS JSONB), :note, "
                    "        CAST(:expectations AS JSONB), :now)"
                ),
                {
                    "expectations": None if stored_expectations is None else _json(stored_expectations),
                    "note": note,
                    "now": self._clock.now(),
                    "pid": prompt_id,
                    "position": count,
                    "request": _json(stored),
                    "sid": set_id,
                    "tid": ctx.tenant_id,
                },
            )
        return Prompt(
            expectations=stored_expectations,
            intent_note=note,
            position=int(count),
            prompt_id=prompt_id,
            request=stored,
        )

    async def list_sets(self, ctx: TenantContext, *, page_size: int = 50) -> tuple[PromptSet, ...]:
        """This tenant's sets, newest first, with how many prompts each holds."""
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValidationError(f"page_size is 1 to {MAX_PAGE_SIZE}")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.set_id, s.name, s.description, s.created_at, s.retired_at, "
                        "       (SELECT count(*) FROM evaluation_prompts p WHERE p.set_id = s.set_id) AS prompt_count "
                        "  FROM evaluation_prompt_sets s "
                        " WHERE s.tenant_id = :tid "
                        " ORDER BY s.created_at DESC "
                        " LIMIT :limit"
                    ),
                    {"limit": page_size, "tid": ctx.tenant_id},
                )
            ).mappings()
        return tuple(
            PromptSet(
                created_at=row["created_at"],
                description=row["description"],
                name=row["name"],
                prompt_count=int(row["prompt_count"]),
                retired_at=row["retired_at"],
                set_id=row["set_id"],
            )
            for row in rows
        )

    # -- running -----------------------------------------------------------

    async def start_run(self, ctx: TenantContext, *, set_id: uuid.UUID) -> Run:
        """Resolve every prompt in a set, once, and keep what came back.

        Sequential rather than concurrent, and that is a measurement decision
        rather than a simplification: `duration_ms` is recorded per item, and
        twenty concurrent resolutions contend for the same pool, so each one's
        duration would be a fact about the batch size rather than about the
        prompt. A run of a hundred prompts is slow; a run whose timings mean
        nothing is worse.

        Every prompt is attempted. A prompt that raises produces an item saying
        so and the run continues, because stopping would leave the later prompts
        unmeasured and the run would report on a subset chosen by whichever
        prompt happened to fail first.
        """
        prompts = await self.prompts_in(ctx, set_id=set_id)
        started = self._clock.now()
        run_id = uuid.uuid4()

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO evaluation_runs "
                    "(run_id, set_id, tenant_id, resolver_fingerprint, prompt_count, started_by, started_at) "
                    "VALUES (:rid, :sid, :tid, :fingerprint, :count, :actor, :now)"
                ),
                {
                    "actor": ctx.actor_id,
                    "count": len(prompts),
                    "fingerprint": self._fingerprint,
                    "now": started,
                    "rid": run_id,
                    "sid": set_id,
                    "tid": ctx.tenant_id,
                },
            )

        items: list[RunItem] = []
        for prompt in prompts:
            items.append(await self._resolve_one(ctx, run_id=run_id, prompt=prompt))

        finished = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE evaluation_runs SET finished_at = :now WHERE run_id = :rid AND tenant_id = :tid"),
                {"now": finished, "rid": run_id, "tid": ctx.tenant_id},
            )

        return Run(
            finished_at=finished,
            items=tuple(items),
            prompt_count=len(prompts),
            resolver_fingerprint=self._fingerprint,
            run_id=run_id,
            set_id=set_id,
            started_at=started,
        )

    async def _resolve_one(self, ctx: TenantContext, *, run_id: uuid.UUID, prompt: Prompt) -> RunItem:
        """One prompt, through the resolver every other caller uses."""
        item_id = uuid.uuid4()
        began = time.monotonic()
        receipt_id: uuid.UUID | None = None
        envelope_state: str | None = None
        failure: str | None = None

        try:
            body = PromptRequestV1.of(prompt.request)
            resolved = await self._resolver.resolve(ctx, moment=self._clock.now(), **body.resolver_arguments())
            receipt_id = resolved.receipt_id
            envelope_state = resolved.envelope.state
        except Exception as exc:  # noqa: BLE001 - a failed prompt is a recorded outcome
            # Recorded, never re-raised. The rule this whole module is built
            # around: a system error is a failure, not an exclusion, and a run
            # that stopped at the first one would report on a subset chosen by
            # whichever prompt happened to break.
            failure = f"{type(exc).__name__}: {exc}"[:2000]

        duration_ms = max(0, round((time.monotonic() - began) * 1000))
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO evaluation_run_items "
                    "(item_id, run_id, tenant_id, prompt_id, receipt_id, envelope_state, failure, duration_ms) "
                    "VALUES (:iid, :rid, :tid, :pid, :receipt, :state, :failure, :duration)"
                ),
                {
                    "duration": duration_ms,
                    "failure": failure,
                    "iid": item_id,
                    "pid": prompt.prompt_id,
                    "receipt": receipt_id,
                    "rid": run_id,
                    "state": envelope_state,
                    "tid": ctx.tenant_id,
                },
            )
        return RunItem(
            duration_ms=duration_ms,
            envelope_state=envelope_state,
            failure=failure,
            item_id=item_id,
            position=prompt.position,
            prompt_id=prompt.prompt_id,
            receipt_id=receipt_id,
        )

    # -- reading -----------------------------------------------------------

    async def prompts_in(self, ctx: TenantContext, *, set_id: uuid.UUID) -> tuple[Prompt, ...]:
        """One set's prompts, in the order a run resolves them."""
        async with self._session_factory() as session:
            await self._require_set(session, ctx, set_id)
            rows = (
                await session.execute(
                    text(
                        "SELECT prompt_id, position, request, intent_note, expectations "
                        "  FROM evaluation_prompts "
                        " WHERE set_id = :sid AND tenant_id = :tid "
                        " ORDER BY position"
                    ),
                    {"sid": set_id, "tid": ctx.tenant_id},
                )
            ).mappings()
        return tuple(
            Prompt(
                expectations=None if row["expectations"] is None else dict(row["expectations"]),
                intent_note=row["intent_note"],
                position=int(row["position"]),
                prompt_id=row["prompt_id"],
                request=dict(row["request"]),
            )
            for row in rows
        )

    async def runs_of(self, ctx: TenantContext, *, set_id: uuid.UUID, page_size: int = 20) -> tuple[Run, ...]:
        """This set's runs, newest first, without their items.

        Headers only. A comparison starts by choosing two runs, and loading every
        item of every run to render that choice would read the whole history to
        answer a question about two rows of it.
        """
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValidationError(f"page_size is 1 to {MAX_PAGE_SIZE}")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT run_id, set_id, resolver_fingerprint, prompt_count, started_at, finished_at "
                        "  FROM evaluation_runs "
                        " WHERE tenant_id = :tid AND set_id = :sid "
                        " ORDER BY started_at DESC "
                        " LIMIT :limit"
                    ),
                    {"limit": page_size, "sid": set_id, "tid": ctx.tenant_id},
                )
            ).mappings()
        return tuple(
            Run(
                finished_at=row["finished_at"],
                prompt_count=int(row["prompt_count"]),
                resolver_fingerprint=row["resolver_fingerprint"],
                run_id=row["run_id"],
                set_id=row["set_id"],
                started_at=row["started_at"],
            )
            for row in rows
        )

    async def run(self, ctx: TenantContext, run_id: uuid.UUID) -> Run:
        """One run with its items and every verdict on them."""
        async with self._session_factory() as session:
            header = (
                (
                    await session.execute(
                        text(
                            "SELECT run_id, set_id, resolver_fingerprint, prompt_count, started_at, finished_at "
                            "  FROM evaluation_runs WHERE run_id = :rid AND tenant_id = :tid"
                        ),
                        {"rid": run_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .first()
            )
            if header is None:
                raise NotFoundError(f"evaluation run {run_id} not found")

            item_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT i.item_id, i.prompt_id, p.position, i.receipt_id, i.envelope_state, "
                            "       i.failure, i.duration_ms "
                            "  FROM evaluation_run_items i "
                            "  JOIN evaluation_prompts p ON p.prompt_id = i.prompt_id "
                            " WHERE i.run_id = :rid AND i.tenant_id = :tid "
                            " ORDER BY p.position"
                        ),
                        {"rid": run_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )

            verdict_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT v.item_id, v.verdict, v.note, v.recorded_by, v.recorded_at "
                            "  FROM evaluation_verdicts v "
                            "  JOIN evaluation_run_items i ON i.item_id = v.item_id "
                            " WHERE i.run_id = :rid AND v.tenant_id = :tid "
                            " ORDER BY v.recorded_at"
                        ),
                        {"rid": run_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )

        by_item: dict[uuid.UUID, list[Verdict]] = {}
        for row in verdict_rows:
            by_item.setdefault(row["item_id"], []).append(
                Verdict(
                    note=row["note"],
                    recorded_at=row["recorded_at"],
                    recorded_by=row["recorded_by"],
                    verdict=row["verdict"],
                )
            )

        return Run(
            finished_at=header["finished_at"],
            items=tuple(
                RunItem(
                    duration_ms=int(row["duration_ms"]),
                    envelope_state=row["envelope_state"],
                    failure=row["failure"],
                    item_id=row["item_id"],
                    position=int(row["position"]),
                    prompt_id=row["prompt_id"],
                    receipt_id=row["receipt_id"],
                    verdicts=tuple(by_item.get(row["item_id"], ())),
                )
                for row in item_rows
            ),
            prompt_count=int(header["prompt_count"]),
            resolver_fingerprint=header["resolver_fingerprint"],
            run_id=header["run_id"],
            set_id=header["set_id"],
            started_at=header["started_at"],
        )

    # -- judging -----------------------------------------------------------

    async def record_verdict(
        self, ctx: TenantContext, *, item_id: uuid.UUID, verdict: str, note: str | None = None
    ) -> Verdict:
        """One reviewer's judgement of one prompt's resolution.

        Replaces that reviewer's earlier verdict on the same item rather than
        adding a second. A reviewer who changed their mind has one opinion, and
        two rows would leave a reader counting revisions as agreement.

        Attributed to the caller, never to an actor the caller names. A verdict
        somebody could file under another person's name is not evidence about
        anything.
        """
        if verdict not in VERDICTS:
            raise ValidationError(f"verdict is one of {list(VERDICTS)}, got {verdict!r}")
        text_note = (note or "").strip() or None
        if verdict != "right" and text_note is None:
            raise ValidationError(
                f"a {verdict!r} verdict says why: a judgement with no reason is one the next reader "
                "has to reach again from scratch"
            )

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            found = (
                await session.execute(
                    text("SELECT 1 FROM evaluation_run_items WHERE item_id = :iid AND tenant_id = :tid"),
                    {"iid": item_id, "tid": ctx.tenant_id},
                )
            ).first()
            if found is None:
                raise NotFoundError(f"evaluation run item {item_id} not found")

            await session.execute(
                text(
                    "INSERT INTO evaluation_verdicts "
                    "(item_id, tenant_id, verdict, note, recorded_by, recorded_at) "
                    "VALUES (:iid, :tid, :verdict, :note, :actor, :now) "
                    "ON CONFLICT (item_id, recorded_by) "
                    "DO UPDATE SET verdict = EXCLUDED.verdict, note = EXCLUDED.note, "
                    "              recorded_at = EXCLUDED.recorded_at"
                ),
                {
                    "actor": ctx.actor_id,
                    "iid": item_id,
                    "note": text_note,
                    "now": now,
                    "tid": ctx.tenant_id,
                    "verdict": verdict,
                },
            )
        return Verdict(note=text_note, recorded_at=now, recorded_by=ctx.actor_id, verdict=verdict)

    # -- helpers -----------------------------------------------------------

    async def _require_set(self, session: AsyncSession, ctx: TenantContext, set_id: uuid.UUID) -> None:
        found = (
            await session.execute(
                text("SELECT 1 FROM evaluation_prompt_sets WHERE set_id = :sid AND tenant_id = :tid"),
                {"sid": set_id, "tid": ctx.tenant_id},
            )
        ).first()
        if found is None:
            raise NotFoundError(f"prompt set {set_id} not found")

    async def _require_live_set(self, session: AsyncSession, ctx: TenantContext, set_id: uuid.UUID) -> None:
        """A retired set is readable and not writable.

        Its past runs stay legible, which is why it is retired rather than
        deleted; adding a prompt to it would change what a name means after the
        runs that used that name were taken.
        """
        row = (
            await session.execute(
                text("SELECT retired_at FROM evaluation_prompt_sets WHERE set_id = :sid AND tenant_id = :tid"),
                {"sid": set_id, "tid": ctx.tenant_id},
            )
        ).first()
        if row is None:
            raise NotFoundError(f"prompt set {set_id} not found")
        if row.retired_at is not None:
            raise ValidationError(
                f"prompt set {set_id} was retired; its runs stay readable, but changing it now would "
                "change what those runs were about"
            )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_PROMPTS",
    "VERDICTS",
    "EvaluationRunService",
    "Prompt",
    "PromptSet",
    "Run",
    "RunItem",
    "Verdict",
]
