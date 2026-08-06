"""The closed contract a sandboxed drafter process is allowed to produce.

Sibling of `registry.arc.schemas.parser_output`, same reasoning: the API
process never lets a sandboxed process's raw response cross into a route
response unvalidated, so both sides of the authenticated local channel
(`registry.arc.sandbox.ipc`) agree on one closed shape defined once, here,
imported by both the sandboxed process (`registry.arc.sandbox.drafter_main`)
and the API-side caller (`registry.arc.service.drafter`). This module is
pure -- no service, session, ORM, or socket import -- for the same reason
`parser_output.py` is: neither side should have to pull in the other's
capabilities just to agree on a shape.

`DrafterResult` is a discriminated union on `status`: exactly one of
`DrafterSuccess` (`status="ok"`) or `DrafterRefusal` (`status="refused"`,
carrying one of the closed `DrafterRefusalCode` values). Every object here
forbids unknown fields.

**Why `DrafterSuccess.patch` is a plain object, not `ArtifactSemanticsPartial`.**
That wire type lives in `registry.api.schemas.arc_authoring_profiles`, which
imports pydantic models built from `registry.arc.schemas.authoring_profile_shapes`
-- neither of which this module needs, and importing the wire-contract package
from inside a module the sandboxed process itself imports would blur the same
API-process/sandboxed-process boundary `parser_output.py`'s own docstring
draws. `registry.arc.service.drafter` (the API-side adapter) is what turns
this module's `patch` dict into `ArtifactSemanticsPartial` before it reaches a
route response -- one more validation pass, not a second definition of the
same shape.

**Why every success today declines every requested field.** No accepted
model artifact exists (`registry/arc/drafter/model_decision.json` records
`outcome: human_only`), so `draft_from_envelope` in `drafter_main.py` has
nothing to found a proposed value on. That is enforced structurally, not by
convention: several sibling fields on `ArtifactSemantics.directives[]` that
sit next to a citable text field (`directive_type`, `delegable_exception`)
are classification judgments no citation can derive, and the drafter is
contractually forbidden from defaulting a judgment field. A function with no
branch that could ever populate one is safer than a function that promises
not to.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

#: 64 lowercase hex characters -- sha256, matching every other digest field
#: in this codebase's wire contracts.
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

#: Mirrors `parser_output.MAX_ENVELOPE_BYTES`'s role: the canonical
#: serialization of a `DrafterSuccess` may not exceed this, checked against
#: the same deterministic byte count `canonical_result_bytes()` produces --
#: not per field, so a response with many small citations cannot slip past a
#: bound that only ever looked at one field's length.
MAX_RESULT_BYTES = 256 * 1024
MAX_CITATIONS = 200
MAX_DECLINED_FIELD_PATHS = 200


class _ClosedModel(BaseModel):
    """Every component in this module forbids unknown fields."""

    model_config = ConfigDict(extra="forbid")


class DraftedCitation(_ClosedModel):
    """One field's binding back to the source excerpt that justifies it.

    Same four fields as the wire `Citation` component
    (`registry.api.schemas.arc_authoring_shared.Citation`) -- deliberately
    not imported from there; see the module docstring for why this module
    never reaches into the wire-contract package.
    """

    field_path: str
    source_evidence_id: uuid.UUID
    source_anchor: str
    excerpt_digest: str = Field(pattern=_DIGEST_PATTERN)


class DrafterSuccess(_ClosedModel):
    """A completed drafting attempt. `citations` binds only field paths
    present as keys somewhere in `patch`; every field path the drafter did
    not populate is named in `declined_field_paths` instead -- the two
    lists partition `target_field_paths` between them, never overlapping
    and never silently dropping a requested path from both.
    """

    status: Literal["ok"]
    patch: dict[str, Any]
    citations: list[DraftedCitation] = Field(max_length=MAX_CITATIONS)
    declined_field_paths: list[str] = Field(max_length=MAX_DECLINED_FIELD_PATHS)


#: The closed reasons a sandboxed draft attempt may refuse outright rather
#: than produce a (possibly all-declined) success. A refusal carries only
#: this code -- no source bytes, no envelope contents -- so a refusal can
#: never leak the content it was asked to draft from.
DrafterRefusalCode = Literal[
    "envelope_binding_mismatch",
    "malformed_request",
    "output_limit_exceeded",
    "deadline_exceeded",
]


class DrafterRefusal(_ClosedModel):
    """A bounded refusal to draft. Carries no source content or envelope
    fragment -- only the closed code naming why."""

    status: Literal["refused"]
    refusal_code: DrafterRefusalCode


DrafterResult = Annotated[DrafterSuccess | DrafterRefusal, Field(discriminator="status")]

DRAFTER_RESULT_ADAPTER: TypeAdapter[DrafterSuccess | DrafterRefusal] = TypeAdapter(DrafterResult)


def parse_drafter_result(data: dict[str, Any]) -> DrafterSuccess | DrafterRefusal:
    """Validate `data` (a raw dict off the wire) as a `DrafterResult`."""
    return DRAFTER_RESULT_ADAPTER.validate_python(data)


def canonical_result_bytes(result: DrafterSuccess) -> bytes:
    """Deterministic UTF-8 JSON serialization used only to measure `result`
    against `MAX_RESULT_BYTES`. Mirrors `parser_output.canonical_envelope_bytes`;
    not a signing/hashing canonicalization profile."""
    payload = result.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


#: Every model this module defines, by name -- mirrors `parser_output.COMPONENTS`.
COMPONENTS: dict[str, type[BaseModel]] = {
    "DraftedCitation": DraftedCitation,
    "DrafterSuccess": DrafterSuccess,
    "DrafterRefusal": DrafterRefusal,
}


def component_schemas() -> dict[str, dict[str, Any]]:
    schemas = {name: model.model_json_schema() for name, model in COMPONENTS.items()}
    schemas["DrafterResult"] = DRAFTER_RESULT_ADAPTER.json_schema()
    return schemas


__all__ = [
    "COMPONENTS",
    "DRAFTER_RESULT_ADAPTER",
    "MAX_CITATIONS",
    "MAX_DECLINED_FIELD_PATHS",
    "MAX_RESULT_BYTES",
    "DraftedCitation",
    "DrafterRefusal",
    "DrafterRefusalCode",
    "DrafterResult",
    "DrafterSuccess",
    "canonical_result_bytes",
    "component_schemas",
    "parse_drafter_result",
]
