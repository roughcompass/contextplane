"""One saved prompt, as the resolver's own arguments rather than a wire body.

E22-T15. A prompt in an evaluation set is a whole context request, not a query
string: a set that could only vary the query could not test a lifecycle
placement, a subject, or an instruction digest, which is most of what there is
to get wrong.

**Here rather than reusing `ContextResolveRequest`, and the reason is
direction.** `api` sits above `context` in this package's layering, so a service
here importing the wire model would be a lower layer depending on a higher one.
That rule is not a formality: it is what keeps the resolver usable by a
transport nobody has written yet.

**So this is the shape and the API model is its projection**, which is the
arrangement the envelope already uses in the other direction — `context/schemas`
owns `ContextEnvelopeV1` and `api/schemas` renders it. What stops the two
drifting is not discipline but
`tests/conformance/test_prompt_request_matches_the_contract.py`, which fails
when the wire model gains a field this one does not have. That test is the only
reason this duplication is safe, and removing it would make this a second
definition nobody reconciles.

**Validation refuses rather than repairs.** A stored prompt that cannot be
resolved fails at run time, on every run, once per prompt — which is a far worse
place to find out than at the moment somebody added it.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import TYPE_CHECKING, Any, Final

from contextplane.context.lifecycle import normalize_reference_kind
from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Mirrors the wire contract's bounds. Repeated rather than imported for the
#: layering reason in the module docstring, and held equal by the conformance
#: test named there.
MAX_ARM_LIMIT: Final[int] = 200
DEFAULT_ARM_LIMIT: Final[int] = 25

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Every key a prompt may carry. Unknown keys are refused rather than ignored: a
#: caller who misspelled `subject_entity_id` and had it dropped would get a
#: resolution that ran against a different question than the one they saved.
_KNOWN: Final[frozenset[str]] = frozenset(
    {
        "arc_receipt_id",
        "instruction_digest",
        "intent_ids",
        "lifecycle_references",
        "limit",
        "max_age_s",
        "query",
        "subject_entity_id",
        "workspace_reference",
        "workspace_term",
    }
)


@dataclasses.dataclass(frozen=True)
class PromptRequestV1:
    """A saved context request, validated."""

    query: str
    arc_receipt_id: uuid.UUID | None = None
    subject_entity_id: uuid.UUID | None = None
    intent_ids: tuple[uuid.UUID, ...] = ()
    workspace_term: str | None = None
    workspace_reference: ExternalReferenceV1 | None = None
    lifecycle_references: tuple[ExternalReferenceV1, ...] = ()
    instruction_digest: str | None = None
    limit: int = DEFAULT_ARM_LIMIT
    max_age_s: float | None = None

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> PromptRequestV1:
        """Build one from stored or submitted JSON, refusing anything unusable."""
        unknown = sorted(set(raw) - _KNOWN)
        if unknown:
            raise ValidationError(f"a prompt does not carry {unknown}; the request fields are {sorted(_KNOWN)}")

        query = str(raw.get("query") or "").strip()
        if not query:
            raise ValidationError("a prompt needs a query; a resolution of nothing answers nothing")

        limit = raw.get("limit", DEFAULT_ARM_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ARM_LIMIT:
            raise ValidationError(f"limit is a whole number from 1 to {MAX_ARM_LIMIT}, got {limit!r}")

        max_age = raw.get("max_age_s")
        if max_age is not None and (not isinstance(max_age, int | float) or max_age <= 0):
            raise ValidationError(f"max_age_s is a positive number when given, got {max_age!r}")

        digest = raw.get("instruction_digest")
        if digest is not None and not (isinstance(digest, str) and _DIGEST.match(digest)):
            raise ValidationError(f"instruction_digest is 'sha256:' and 64 lowercase hex characters, got {digest!r}")

        lifecycle = tuple(_reference(entry, "lifecycle_references") for entry in raw.get("lifecycle_references") or ())
        for reference in lifecycle:
            # The same normalizer the resolver's other callers use. A second copy
            # of the vocabulary is a second vocabulary, and two spellings that
            # store cleanly and then fail to join is what closing it prevents.
            normalize_reference_kind(reference.kind)

        workspace = raw.get("workspace_reference")
        term = raw.get("workspace_term")
        return cls(
            arc_receipt_id=_uuid(raw.get("arc_receipt_id"), "arc_receipt_id"),
            instruction_digest=digest,
            intent_ids=tuple(_uuid(value, "intent_ids") or uuid.UUID(int=0) for value in raw.get("intent_ids") or ()),
            lifecycle_references=lifecycle,
            limit=limit,
            max_age_s=float(max_age) if max_age is not None else None,
            query=query,
            subject_entity_id=_uuid(raw.get("subject_entity_id"), "subject_entity_id"),
            workspace_reference=_reference(workspace, "workspace_reference") if workspace else None,
            workspace_term=str(term) if term is not None else None,
        )

    def stored(self) -> dict[str, Any]:
        """The JSON form, with absent optionals absent rather than null.

        An omitted optional that came back as an explicit null would be a second
        spelling of the same prompt, and two prompts that differ only in how
        their absences are written would compare unequal.
        """
        body: dict[str, Any] = {"limit": self.limit, "query": self.query}
        if self.arc_receipt_id is not None:
            body["arc_receipt_id"] = str(self.arc_receipt_id)
        if self.subject_entity_id is not None:
            body["subject_entity_id"] = str(self.subject_entity_id)
        if self.intent_ids:
            body["intent_ids"] = [str(value) for value in self.intent_ids]
        if self.workspace_term is not None:
            body["workspace_term"] = self.workspace_term
        if self.workspace_reference is not None:
            body["workspace_reference"] = _reference_json(self.workspace_reference)
        if self.lifecycle_references:
            body["lifecycle_references"] = [_reference_json(ref) for ref in self.lifecycle_references]
        if self.instruction_digest is not None:
            body["instruction_digest"] = self.instruction_digest
        if self.max_age_s is not None:
            body["max_age_s"] = self.max_age_s
        return body

    def resolver_arguments(self) -> dict[str, Any]:
        """The keyword arguments `ContextResolver.resolve` takes, minus the moment.

        The clock is the caller's, because a run reads it once per prompt and a
        request object carrying its own instant would be a prompt that resolves
        against the time it was saved.
        """
        return {
            "arc_receipt_id": self.arc_receipt_id,
            "instruction_digest": self.instruction_digest,
            "intent_ids": self.intent_ids,
            "lifecycle_references": self.lifecycle_references,
            "limit": self.limit,
            "max_age_s": self.max_age_s,
            "query": self.query,
            "subject_entity_id": self.subject_entity_id,
            "workspace_reference": self.workspace_reference,
            "workspace_term": self.workspace_term,
        }


def _uuid(value: object, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} is a UUID, got {value!r}") from exc


def _reference(value: object, field: str) -> ExternalReferenceV1:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} entries are objects, got {type(value).__name__}")
    try:
        return ExternalReferenceV1(
            source_system=str(value["source_system"]),
            source_namespace=str(value["source_namespace"]),
            kind=str(value["kind"]),
            external_id=str(value["external_id"]),
            classification=value["classification"],
            external_authority=str(value["external_authority"]),
            revision=value.get("revision"),
            authorized_uri=value.get("authorized_uri"),
            observed_at=value.get("observed_at"),
        )
    except KeyError as exc:
        raise ValidationError(f"{field} entry is missing {exc.args[0]!r}") from exc


def _reference_json(reference: ExternalReferenceV1) -> dict[str, Any]:
    body: dict[str, Any] = {
        "classification": reference.classification,
        "external_authority": reference.external_authority,
        "external_id": reference.external_id,
        "kind": reference.kind,
        "source_namespace": reference.source_namespace,
        "source_system": reference.source_system,
    }
    if reference.revision is not None:
        body["revision"] = reference.revision
    if reference.authorized_uri is not None:
        body["authorized_uri"] = reference.authorized_uri
    if reference.observed_at is not None:
        body["observed_at"] = reference.observed_at.isoformat()
    return body


__all__ = ["DEFAULT_ARM_LIMIT", "MAX_ARM_LIMIT", "PromptRequestV1"]
