"""Normalized external references, and what each block's items may contain.

Two things live here, and they share a rule: **an unknown field is refused, not
ignored.** A schema that silently drops what it does not recognise turns a
producer's typo into missing data, and the producer gets no signal — the item
just arrives thinner than it was sent. Every constructor here takes a mapping and
rejects extras by name.

References name things the registry does not own and create nothing here. A
reference is a way to point at an issue, a document or a service in another
system; importing it would mean this registry starts answering for a lifecycle it
does not control.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Literal

from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
)
from contextplane.context.schemas.trust import ExternalReferenceV1, InvalidContextItem


def _reject_unknown(kind: str, payload: dict[str, Any], allowed: frozenset[str]) -> None:
    """Refuse a payload carrying fields this schema does not define.

    Named individually because "unknown field" without the name sends the
    producer looking through their whole payload for a typo this function already
    found.
    """
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise InvalidContextItem(
            f"{kind} received unknown field(s) {unknown}; a schema that ignores what it does not "
            "recognise turns a producer's typo into missing data with no signal back"
        )


def _require(kind: str, payload: dict[str, Any], required: tuple[str, ...]) -> None:
    missing = sorted(field for field in required if field not in payload)
    if missing:
        raise InvalidContextItem(f"{kind} is missing required field(s) {missing}")


# --- normalized external references -------------------------------------------

# Case is not identity for a system or a kind: `GitHub` and `github` are one
# source, and treating them as two would split a subject's references in half
# without anything looking wrong. The opaque id keeps its case, because it
# belongs to the other system and may well be case-sensitive there.
_CASE_INSENSITIVE_FIELDS = ("source_system", "source_namespace", "kind")

_REFERENCE_FIELDS: frozenset[str] = frozenset(
    {
        "source_system",
        "source_namespace",
        "kind",
        "external_id",
        "classification",
        "external_authority",
        "revision",
        "authorized_uri",
        "observed_at",
    }
)

_REFERENCE_REQUIRED = (
    "source_system",
    "source_namespace",
    "kind",
    "external_id",
    "classification",
    "external_authority",
)


def normalize_reference(payload: dict[str, Any]) -> ExternalReferenceV1:
    """Build a reference from a producer's mapping, normalized and closed.

    Normalization happens before construction so two spellings of one reference
    produce one collision key rather than two rows that never meet.
    """
    _reject_unknown("external reference", payload, _REFERENCE_FIELDS)
    _require("external reference", payload, _REFERENCE_REQUIRED)

    values = dict(payload)
    for field in _CASE_INSENSITIVE_FIELDS:
        raw = values[field]
        if not isinstance(raw, str):
            raise InvalidContextItem(f"external reference {field} must be a string, got {type(raw).__name__}")
        values[field] = raw.strip().lower()

    external_id = values["external_id"]
    if not isinstance(external_id, str):
        raise InvalidContextItem(f"external reference external_id must be a string, got {type(external_id).__name__}")
    # Trimmed but not lowercased: the id is the other system's, and folding its
    # case would merge two things that system considers distinct.
    values["external_id"] = external_id.strip()

    observed_at = values.get("observed_at")
    if isinstance(observed_at, str):
        try:
            values["observed_at"] = datetime.datetime.fromisoformat(observed_at)
        except ValueError as exc:
            raise InvalidContextItem(f"external reference observed_at is not an ISO-8601 timestamp: {exc}") from exc

    return ExternalReferenceV1(**values)


# --- per-block item content ---------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CanonicalContentV1:
    """The registry's own answer about a subject."""

    entity_id: str
    entity_kind: str
    display_name: str
    attributes: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class ArcContentV1:
    """An attested governance artifact, as context rather than as authority."""

    artifact_id: str
    artifact_kind: str
    version: str
    summary: str
    references: tuple[ExternalReferenceV1, ...] = ()


@dataclasses.dataclass(frozen=True)
class ObservedClaimContentV1:
    """One claim somebody or something asserted about a subject.

    `evidence_event_ids` is what makes it checkable. A claim citing nothing is
    indistinguishable from an invention, which is why the field is required here
    rather than optional with an empty default.
    """

    claim_id: str
    predicate: str
    value: str | int | float | bool | None
    evidence_event_ids: tuple[str, ...]
    excerpt: str | None = None


@dataclasses.dataclass(frozen=True)
class WorkspaceContentV1:
    """An operator-authored note, decision or question.

    Recall stays lexical and reference-based: this content carries no vector and
    no similarity score, because a workspace entry is somebody's working note and
    ranking it by embedding distance would present it as a retrieved fact.
    """

    entry_id: str
    entry_kind: str
    title: str
    body_md: str
    references: tuple[ExternalReferenceV1, ...] = ()


BlockContent = Literal["canonical", "arc", "observed_claims", "workspace"]

_CONTENT_FIELDS: dict[str, frozenset[str]] = {
    BLOCK_CANONICAL: frozenset({"entity_id", "entity_kind", "display_name", "attributes"}),
    BLOCK_ARC: frozenset({"artifact_id", "artifact_kind", "version", "summary", "references"}),
    BLOCK_OBSERVED_CLAIMS: frozenset({"claim_id", "predicate", "value", "evidence_event_ids", "excerpt"}),
    BLOCK_WORKSPACE: frozenset({"entry_id", "entry_kind", "title", "body_md", "references"}),
}

_CONTENT_REQUIRED: dict[str, tuple[str, ...]] = {
    BLOCK_CANONICAL: ("entity_id", "entity_kind", "display_name", "attributes"),
    BLOCK_ARC: ("artifact_id", "artifact_kind", "version", "summary"),
    BLOCK_OBSERVED_CLAIMS: ("claim_id", "predicate", "value", "evidence_event_ids"),
    BLOCK_WORKSPACE: ("entry_id", "entry_kind", "title", "body_md"),
}


def parse_block_content(
    block: str, payload: dict[str, Any]
) -> CanonicalContentV1 | ArcContentV1 | ObservedClaimContentV1 | WorkspaceContentV1:
    """Build the content object a given block's items carry.

    One entry point rather than four constructors, so the block-to-shape mapping
    lives in one place. An assembler that picked the shape itself would be free to
    pick the wrong one, and a workspace note shaped as a canonical entity is a
    working note wearing the registry's authority.
    """
    if block not in _CONTENT_FIELDS:
        raise InvalidContextItem(f"unknown block {block!r}; the four blocks are {sorted(_CONTENT_FIELDS)}")

    _reject_unknown(f"{block} content", payload, _CONTENT_FIELDS[block])
    _require(f"{block} content", payload, _CONTENT_REQUIRED[block])

    values = dict(payload)
    if "references" in values:
        raw_refs = values["references"]
        if not isinstance(raw_refs, list):
            raise InvalidContextItem(f"{block} content references must be a list")
        values["references"] = tuple(normalize_reference(ref) for ref in raw_refs)

    if block == BLOCK_CANONICAL:
        return CanonicalContentV1(**values)
    if block == BLOCK_ARC:
        return ArcContentV1(**values)
    if block == BLOCK_WORKSPACE:
        return WorkspaceContentV1(**values)

    event_ids = values.get("evidence_event_ids")
    if not isinstance(event_ids, list | tuple) or not event_ids:
        raise InvalidContextItem(
            "an observed claim must cite at least one evidence event; a claim citing nothing is "
            "indistinguishable from an invention"
        )
    values["evidence_event_ids"] = tuple(event_ids)
    value = values.get("value")
    if not isinstance(value, str | int | float | bool | None):
        raise InvalidContextItem(
            f"an observed claim value is a scalar, got {type(value).__name__}; a structured value carries "
            "text no content check reads"
        )
    return ObservedClaimContentV1(**values)


__all__ = [
    "ArcContentV1",
    "BlockContent",
    "CanonicalContentV1",
    "ObservedClaimContentV1",
    "WorkspaceContentV1",
    "normalize_reference",
    "parse_block_content",
]
