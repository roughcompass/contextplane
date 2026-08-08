"""The closed contract a sandboxed source parser is allowed to produce.

The API process never parses admitted-but-unreviewed source bytes
in-process; parsing runs in a separate OS process with no database
credential, no service token, and no network route
(`registry.arc.sandbox.parser_main`), and the only thing that crosses back
over the authenticated local channel (`registry.arc.sandbox.ipc`) is one
instance of `ParserResult` defined here. This module is pure -- it imports
no service, session, ORM, or socket type, and it never reads a database row
or a file -- for the same reason `authoring_profiles.py` is pure: the
sandboxed process and the API-side adapter both import it, and neither
should have to pull in the other's capabilities just to agree on a shape.

`ParserResult` is a discriminated union on `status`: exactly one of
`ParserSuccess` (`status="ok"`, carrying a `ParsedSourceEnvelope`) or
`ParserRefusal` (`status="refused"`, carrying one of the five closed
`ParserRefusalCode` values). Every object here forbids unknown fields.
`COMPONENTS` names every model this module defines and is what
`tests/conformance/test_arc_parser_sandbox.py` walks to keep the generated
JSON Schema pinned to `tests/conformance/snapshots/arc_parser_output_schema.json`
-- the same snapshot discipline the REST/MCP wire-contract module
(`arc_authoring.py`) already established for that surface.

Two things this module enforces that a bare field-list transcription would
miss, both because they are closedness properties, not per-field shapes:

- `ParsedSourceEnvelope.sections` must be strictly ordered by unique,
  ascending `ordinal`, and every `section_id` and `source_anchor` must be
  unique across the list -- a parser that emitted two sections claiming the
  same anchor would let a later citation bind to whichever one a reader
  happened to trust, which defeats the point of an anchor.
- The envelope's own serialized size is capped at 1 MiB
  (`MAX_ENVELOPE_BYTES`), checked against `canonical_envelope_bytes()`, not
  against any single field's length. A parser bounded only per-field could
  still assemble 999 maximal sections into an envelope no single ceiling
  above would catch.

`canonical_envelope_bytes()` is a deterministic serialization used only to
measure that ceiling -- sorted keys, compact separators, UTF-8. It is
*not* a signing/hashing canonicalization profile: `ParsedSourceEnvelope`
never enters the `S -> R -> A` approval digest chain
`registry.arc.schemas.authoring_profiles` owns, so it carries none of that
module's NFC/NUL/set-ordering discipline. Conflating the two would wrongly
imply this envelope participates in a chain it does not.

`verify_source_binding()` is the API-side half of the contract: even a
schema-valid `ParsedSourceEnvelope` is untrusted until its declared
`source_evidence_id` and `source_content_digest` are compared against the
admission row the caller actually asked to have parsed. Nothing about the
wire protocol proves the sandbox parsed the content the caller thinks it
did -- a reused socket path, a stale process, or a caller-side bug could
each produce a schema-valid envelope for the wrong source -- so this check
runs before any caller may use a returned envelope for anything.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from registry.exceptions import RegistryError

#: `arc_parsed_source_envelope_v1`'s own closed profile literal -- the one
#: value `ParsedSourceEnvelope.profile` may hold.
ENVELOPE_PROFILE: Literal["arc_parsed_source_envelope_v1"] = "arc_parsed_source_envelope_v1"

#: 64 lowercase hex characters -- sha256, matching every other digest field
#: in this codebase's wire contracts.
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

MAX_SECTIONS = 1000
MAX_WARNINGS = 1000
MAX_TITLE_LENGTH = 500
MAX_HEADING_LENGTH = 500
MAX_TEXT_LENGTH = 65536
MAX_ENVELOPE_BYTES = 1024 * 1024


class _ClosedModel(BaseModel):
    """Every component in this module forbids unknown fields. Stated once
    here rather than repeated as a `model_config` line on each class below,
    the same reasoning `api/schemas/arc_authoring_shared.py::_ClosedModel`
    already uses for the REST/MCP wire contract.
    """

    model_config = ConfigDict(extra="forbid")


class ParsedSourceWarning(_ClosedModel):
    """A non-fatal note about the parse: content was truncated to a bound,
    an element the parser does not understand was dropped, or a string was
    normalized. `source_anchor` is present when the warning applies to one
    specific section rather than the document as a whole.
    """

    code: Literal["truncated", "unsupported_element", "normalization_applied"]
    source_anchor: str | None = None


class ParsedSourceSection(_ClosedModel):
    """One addressable span of the parsed document. `excerpt_digest` is the
    sha256 of `text` exactly as returned -- the value a later citation binds
    to, computed by the parser, not asserted by a caller.
    """

    section_id: str
    source_anchor: str
    ordinal: int = Field(ge=0)
    heading: str | None = Field(default=None, max_length=MAX_HEADING_LENGTH)
    text: str = Field(max_length=MAX_TEXT_LENGTH)
    excerpt_digest: str = Field(pattern=_DIGEST_PATTERN)


class ParsedSourceEnvelope(_ClosedModel):
    """The one shape a successful parse produces. `sections` must be
    strictly ordered by unique, ascending `ordinal`; `section_id` and
    `source_anchor` must each be unique across the list; at most
    `MAX_SECTIONS` sections and `MAX_WARNINGS` warnings are permitted; and
    the envelope's own canonical serialization must not exceed
    `MAX_ENVELOPE_BYTES` -- see the module docstring for why each of these
    is checked at the envelope level rather than only per field.
    """

    profile: Literal["arc_parsed_source_envelope_v1"]
    source_evidence_id: uuid.UUID
    source_content_digest: str = Field(pattern=_DIGEST_PATTERN)
    media_type: str
    parser_id: str
    parser_version: str
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    sections: list[ParsedSourceSection]
    warnings: list[ParsedSourceWarning]

    @model_validator(mode="after")
    def _check_closed_contract(self) -> ParsedSourceEnvelope:
        if len(self.sections) > MAX_SECTIONS:
            raise ValueError(f"envelope carries {len(self.sections)} sections, exceeding the {MAX_SECTIONS} ceiling")
        if len(self.warnings) > MAX_WARNINGS:
            raise ValueError(f"envelope carries {len(self.warnings)} warnings, exceeding the {MAX_WARNINGS} ceiling")

        ordinals = [section.ordinal for section in self.sections]
        if ordinals != sorted(ordinals):
            raise ValueError("sections must be ordered by ascending ordinal")
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("section ordinals must be unique")

        section_ids = [section.section_id for section in self.sections]
        if len(set(section_ids)) != len(section_ids):
            raise ValueError("section_id values must be unique across the envelope")

        anchors = [section.source_anchor for section in self.sections]
        if len(set(anchors)) != len(anchors):
            raise ValueError("source_anchor values must be unique across the envelope")

        size = len(canonical_envelope_bytes(self))
        if size > MAX_ENVELOPE_BYTES:
            raise ValueError(f"canonical envelope is {size} bytes, exceeding the {MAX_ENVELOPE_BYTES} byte ceiling")
        return self


def canonical_envelope_bytes(envelope: ParsedSourceEnvelope) -> bytes:
    """Deterministic UTF-8 JSON serialization used only to measure
    `envelope` against `MAX_ENVELOPE_BYTES`. See the module docstring for
    why this is not a signing/hashing canonicalization profile.
    """
    payload = envelope.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


#: The five closed reasons a sandboxed parse may refuse outright rather
#: than produce an envelope. A refusal carries only this code -- no source
#: bytes, no parser diagnostic text -- so a refusal can never leak the
#: content it was refusing to interpret.
ParserRefusalCode = Literal[
    "unsupported_media_type",
    "malformed_source",
    "source_too_complex",
    "output_limit_exceeded",
    "deadline_exceeded",
]


class ParserSuccess(_ClosedModel):
    """A completed parse."""

    status: Literal["ok"]
    envelope: ParsedSourceEnvelope


class ParserRefusal(_ClosedModel):
    """A bounded refusal to parse. Carries no source bytes or parser
    diagnostic text -- only the closed code naming why.
    """

    status: Literal["refused"]
    refusal_code: ParserRefusalCode


ParserResult = Annotated[ParserSuccess | ParserRefusal, Field(discriminator="status")]

#: Parses a raw dict (as received off the wire) into whichever branch it
#: actually is, or raises `pydantic.ValidationError` if it is neither --
#: the sandbox's own output is validated exactly as strictly as any other
#: caller's input would be.
PARSER_RESULT_ADAPTER: TypeAdapter[ParserSuccess | ParserRefusal] = TypeAdapter(ParserResult)


def parse_parser_result(data: dict[str, Any]) -> ParserSuccess | ParserRefusal:
    """Validate `data` (a raw dict off the wire) as a `ParserResult`."""
    return PARSER_RESULT_ADAPTER.validate_python(data)


#: Every model this module defines, by name -- what
#: `test_arc_parser_sandbox.py` walks to keep the generated JSON Schema
#: pinned to the checked-in snapshot. `ParserResult` is a plain type alias,
#: not a `BaseModel` subclass, so its schema is generated separately via
#: `PARSER_RESULT_ADAPTER.json_schema()` and is not one of these entries.
COMPONENTS: dict[str, type[BaseModel]] = {
    "ParsedSourceWarning": ParsedSourceWarning,
    "ParsedSourceSection": ParsedSourceSection,
    "ParsedSourceEnvelope": ParsedSourceEnvelope,
    "ParserSuccess": ParserSuccess,
    "ParserRefusal": ParserRefusal,
}


def component_schemas() -> dict[str, dict[str, Any]]:
    """Every generated JSON Schema this module owns, by name -- `COMPONENTS`
    plus the `ParserResult` union itself. The single source both the
    snapshot-generation step and the conformance test call, so there is
    exactly one place that decides what belongs in the snapshot.
    """
    schemas = {name: model.model_json_schema() for name, model in COMPONENTS.items()}
    schemas["ParserResult"] = PARSER_RESULT_ADAPTER.json_schema()
    return schemas


class ParserBindingError(RegistryError):
    """A sandboxed parse returned a schema-valid envelope bound to the
    wrong source. See the module docstring's `verify_source_binding()`
    paragraph for why this check exists independently of the wire
    protocol's own framing/authentication.
    """


def verify_source_binding(
    envelope: ParsedSourceEnvelope,
    *,
    source_evidence_id: uuid.UUID,
    source_content_digest: str,
) -> None:
    """Raise `ParserBindingError` unless `envelope` is bound to exactly the
    admitted source the caller asked to have parsed. Call this before using
    any field of a returned `ParsedSourceEnvelope` for anything.
    """
    if envelope.source_evidence_id != source_evidence_id:
        raise ParserBindingError(
            f"envelope source_evidence_id {envelope.source_evidence_id} does not match "
            f"the admitted source {source_evidence_id}"
        )
    if envelope.source_content_digest != source_content_digest:
        raise ParserBindingError("envelope source_content_digest does not match the admitted source's content digest")
