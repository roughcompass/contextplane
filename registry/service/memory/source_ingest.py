"""Claims from documents, work items, and incidents, through the one write path.

Three connectors, one route in. Each parses its own source format and produces
candidate claims; none of them writes anything. Writing goes through the claim service
after the governance service has admitted the batch, which is what makes the two
controls from the previous requirement real rather than advisory.

**Nothing writes without a declared authority tier, and the declaration is not what
sets the tier.** Those are two separate things and it took reading the write path to see
it. Authority is derived from provenance the caller cannot forge: a `document_revision`
or a `work_item` earns the weakest derivation tier, and only a registered connector over
a deterministic source type earns extraction. So "a runbook page is not equivalent to an
owner's OpenAPI sync" is already true of the claim, without this module passing anything.

What the declaration does is gate the write and record the decision. A source that never
declared cannot write at all, and what an operator chose is auditable. Letting the
declaration *set* the tier would have been worse than useless -- it would have made the
one number a caller must not control into a number a caller supplies.

**Provenance is the page and its revision, not just the page.** A runbook says
different things in different revisions. Provenance that named only the page would
point at whatever it says today, which is not the evidence the claim was drawn from --
and the whole purpose of a citation is that somebody can go and check.

**Incidents are historical facts.** Their claims land in a category whose half-life is
effectively no decay, because a service having failed last March is not less true in
April. Every other category describes current state and should fade.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import uuid
from typing import Any, Final

from registry.service.memory.claims import Evidence

# Which evidence kind each source produces. Separate kinds rather than one generic
# `connector_run` so "which of these claims came from something that broke" is
# answerable, which is the first question anybody investigating a service asks.
EVIDENCE_DOCUMENT: Final[str] = "document_revision"
EVIDENCE_WORK_ITEM: Final[str] = "work_item"
EVIDENCE_INCIDENT: Final[str] = "incident"


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A claim a connector proposes. Carries no authority field at all.

    Not "carries none yet" -- there is nowhere to put one. Authority is derived from
    the evidence at write time, so a candidate cannot arrive pre-weighted even by
    accident.
    """

    subject_reference: str
    predicate: str
    value: Any
    evidence: tuple[Evidence, ...]
    asserted_valid_from: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class IngestResult:
    admitted: bool
    written: int
    refused_reason: str | None = None


# --- document and runbook connector -------------------------------------------

# Deliberately narrow patterns. A runbook is prose, and a parser that guessed at
# meaning would manufacture claims nobody wrote -- which is worse than missing them,
# because a wrong claim carries the same citation as a right one.
_RUNBOOK_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("runbook_url", re.compile(r"runbook:\s*(?P<value>https?://\S+)", re.IGNORECASE)),
    ("owned_by_team", re.compile(r"owner(?:ed by)?:\s*(?P<value>[^\n]+)", re.IGNORECASE)),
    (
        "escalation_contact",
        re.compile(r"escalat(?:e|ion)(?:\s+to)?:\s*(?P<value>[^\n]+)", re.IGNORECASE),
    ),
    (
        "recovery_time_objective_seconds",
        re.compile(r"\bRTO:\s*(?P<value>\d+)\s*(?:s|sec|seconds)?\b", re.IGNORECASE),
    ),
)


def parse_document(
    *,
    subject_reference: str,
    page_id: str,
    revision: str,
    body: str,
) -> tuple[Candidate, ...]:
    """Candidates from one revision of one page.

    The revision is part of every citation. A runbook says different things in
    different revisions, so provenance naming only the page would point at whatever
    it says now rather than at the text the claim came from.
    """
    ref = f"{page_id}@{revision}"
    found: list[Candidate] = []
    for predicate, pattern in _RUNBOOK_PATTERNS:
        for match in pattern.finditer(body):
            raw = match.group("value").strip()
            if not raw:
                continue
            value: Any = int(raw) if predicate.endswith("_seconds") else raw
            found.append(
                Candidate(
                    subject_reference=subject_reference,
                    predicate=predicate,
                    value=value,
                    evidence=(
                        Evidence(
                            kind=EVIDENCE_DOCUMENT,
                            ref=ref,
                            excerpt=match.group(0).strip(),
                        ),
                    ),
                )
            )
    return tuple(found)


# --- work-item connector ------------------------------------------------------


def parse_work_item(
    *,
    subject_reference: str,
    item_key: str,
    url: str,
    summary: str,
) -> tuple[Candidate, ...]:
    """One claim per work item: that this capability has in-flight change against it.

    Deliberately not an attempt to read the item's content. A ticket summary is a
    human note, and inferring capability properties from it would be guessing with a
    citation attached.
    """
    return (
        Candidate(
            subject_reference=subject_reference,
            predicate="work_item_url",
            value=url,
            evidence=(Evidence(kind=EVIDENCE_WORK_ITEM, ref=item_key, excerpt=summary),),
        ),
    )


# --- incident connector -------------------------------------------------------


def parse_incident(
    *,
    subject_reference: str,
    incident_id: str,
    report_url: str,
    occurred_at: datetime.datetime,
    summary: str,
) -> tuple[Candidate, ...]:
    """Two claims per incident: that it happened, and where to read about it.

    `asserted_valid_from` is when the incident began rather than when the connector
    ran. A historical fact holds from when it happened, and recording it as valid
    from ingest time would make an old incident look like a new one.
    """
    evidence = (Evidence(kind=EVIDENCE_INCIDENT, ref=incident_id, excerpt=summary),)
    # A `Z` suffix rather than `+00:00`. The write path refuses an offset rather than
    # converting it, because converting loses which zone was meant -- so a connector
    # emitting the offset form would have every incident claim rejected.
    stamp = occurred_at.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
    return (
        Candidate(
            subject_reference=subject_reference,
            predicate="incident_occurred_at",
            value=stamp,
            evidence=evidence,
            asserted_valid_from=occurred_at,
        ),
        Candidate(
            subject_reference=subject_reference,
            predicate="incident_report_url",
            value=report_url,
            evidence=evidence,
            asserted_valid_from=occurred_at,
        ),
    )


# --- the governed write -------------------------------------------------------


class SourceIngestService:
    """Admit a batch, then write it. Never one without the other."""

    def __init__(
        self,
        *,
        claims: Any,
        governance: Any,
    ) -> None:
        self._claims = claims
        self._governance = governance

    async def ingest(
        self,
        ctx: Any,
        *,
        source_id: uuid.UUID,
        candidates: tuple[Candidate, ...],
    ) -> IngestResult:
        """Write a connector's candidates, if the source is allowed to.

        The whole batch is admitted or none of it is. A partial write would leave the
        store holding an arbitrary prefix of a document, which is harder to reason
        about than not having ingested it -- a curator cannot tell a page that says
        three things from a page that says six and was cut off.

        Authority is not passed. It is derived from the evidence each candidate
        cites, which is provenance the connector cannot forge. The registration is
        checked because writing without one is refused, not because it supplies the
        tier.
        """
        if not candidates:
            return IngestResult(admitted=True, written=0)

        admission = await self._governance.admit(source_id, count=len(candidates))
        if not admission.permitted:
            return IngestResult(admitted=False, written=0, refused_reason=admission.reason)

        written = 0
        for candidate in candidates:
            await self._claims.stage_claim(
                ctx,
                subject_reference=candidate.subject_reference,
                predicate=candidate.predicate,
                value=candidate.value,
                evidence=candidate.evidence,
                asserted_valid_from=candidate.asserted_valid_from,
            )
            written += 1
        return IngestResult(admitted=True, written=written)
