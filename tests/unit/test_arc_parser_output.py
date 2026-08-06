"""Unit tests for the parser sandbox's closed output contract's own
mechanics (`registry/arc/schemas/parser_output.py`).

`tests/conformance/test_arc_parser_sandbox.py` owns the contract-drift half
of this module: pinning `component_schemas()` to a checked-in snapshot,
sweeping every `COMPONENTS` entry to prove it rejects an unknown field, and
the real-process/real-socket sandbox tests. None of that is repeated here.
What this file adds is mechanism-level, fast, no-DB coverage of the pure
validation logic itself: every closedness rule `ParsedSourceEnvelope`
enforces (section/warning count ceilings, ordinal ordering and uniqueness,
section-id/anchor uniqueness, the byte ceiling), per-field bounds, the
discriminated union's round trip and refusal of an unrecognized branch,
and the API-side `verify_source_binding` check.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from registry.arc.schemas import parser_output as po


def _minimal_envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_parsed_source_envelope_v1",
        "source_evidence_id": str(uuid.uuid4()),
        "source_content_digest": "0" * 64,
        "media_type": "text/markdown",
        "parser_id": "test-parser",
        "parser_version": "1",
        "title": None,
        "sections": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def _section(
    ordinal: int, *, section_id: str | None = None, source_anchor: str | None = None, text: str = "body"
) -> dict[str, Any]:
    return {
        "section_id": section_id or f"s{ordinal}",
        "source_anchor": source_anchor or f"a{ordinal}",
        "ordinal": ordinal,
        "heading": None,
        "text": text,
        "excerpt_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


# ---------------------------------------------------------------------------
# Closed models: unknown-field refusal, one spot check per model (the
# exhaustive sweep across every `COMPONENTS` entry is the conformance
# suite's job).
# ---------------------------------------------------------------------------


def test_closed_base_model_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        po.ParsedSourceWarning.model_validate({"__unit_test_unknown__": 1})
    assert "extra_forbidden" in {err["type"] for err in exc_info.value.errors()}


def test_components_registry_names_exactly_the_five_models() -> None:
    assert set(po.COMPONENTS.keys()) == {
        "ParsedSourceWarning",
        "ParsedSourceSection",
        "ParsedSourceEnvelope",
        "ParserSuccess",
        "ParserRefusal",
    }


def test_component_schemas_adds_the_parser_result_union() -> None:
    schemas = po.component_schemas()
    assert set(schemas.keys()) == set(po.COMPONENTS.keys()) | {"ParserResult"}
    assert schemas["ParserResult"]["discriminator"]["propertyName"] == "status"


# ---------------------------------------------------------------------------
# Envelope-level closedness: each rule gets a refusal that fires and a
# boundary case that succeeds.
# ---------------------------------------------------------------------------


def test_envelope_accepts_a_typical_shape() -> None:
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(sections=[_section(0), _section(1)]))
    assert [s.ordinal for s in envelope.sections] == [0, 1]


def test_envelope_refuses_one_over_max_sections() -> None:
    sections = [_section(i) for i in range(po.MAX_SECTIONS + 1)]
    with pytest.raises(ValidationError, match="sections"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_one_over_max_warnings() -> None:
    warnings = [{"code": "truncated", "source_anchor": None} for _ in range(po.MAX_WARNINGS + 1)]
    with pytest.raises(ValidationError, match="warnings"):
        po.ParsedSourceEnvelope(**_minimal_envelope(warnings=warnings))


def test_envelope_refuses_duplicate_ordinal() -> None:
    sections = [_section(0), _section(0, section_id="other", source_anchor="other")]
    with pytest.raises(ValidationError, match="unique"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_unordered_sections() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=[_section(1), _section(0)]))


def test_envelope_refuses_duplicate_section_id() -> None:
    sections = [_section(0, section_id="dup"), _section(1, section_id="dup")]
    with pytest.raises(ValidationError, match="section_id"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_refuses_duplicate_source_anchor() -> None:
    sections = [_section(0, source_anchor="dup"), _section(1, source_anchor="dup")]
    with pytest.raises(ValidationError, match="source_anchor"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_byte_ceiling_fires() -> None:
    big_text = "x" * po.MAX_TEXT_LENGTH
    sections = [_section(i, text=big_text) for i in range(20)]
    with pytest.raises(ValidationError, match="byte ceiling"):
        po.ParsedSourceEnvelope(**_minimal_envelope(sections=sections))


def test_envelope_byte_ceiling_leaves_small_content_alone() -> None:
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(sections=[_section(0, text="short")]))
    assert len(po.canonical_envelope_bytes(envelope)) < po.MAX_ENVELOPE_BYTES


def test_canonical_envelope_bytes_is_deterministic() -> None:
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(sections=[_section(0)]))
    assert po.canonical_envelope_bytes(envelope) == po.canonical_envelope_bytes(envelope)


# ---------------------------------------------------------------------------
# Per-field bounds.
# ---------------------------------------------------------------------------


def test_title_over_max_length_refused() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceEnvelope(**_minimal_envelope(title="x" * (po.MAX_TITLE_LENGTH + 1)))


def test_title_at_max_length_accepted() -> None:
    envelope = po.ParsedSourceEnvelope(**_minimal_envelope(title="x" * po.MAX_TITLE_LENGTH))
    assert envelope.title is not None
    assert len(envelope.title) == po.MAX_TITLE_LENGTH


def test_section_text_over_max_length_refused() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(
            section_id="s",
            source_anchor="a",
            ordinal=0,
            heading=None,
            text="x" * (po.MAX_TEXT_LENGTH + 1),
            excerpt_digest="0" * 64,
        )


def test_section_digest_must_be_lowercase_hex64() -> None:
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(
            section_id="s", source_anchor="a", ordinal=0, heading=None, text="body", excerpt_digest="not-a-digest"
        )
    with pytest.raises(ValidationError):
        po.ParsedSourceSection(
            section_id="s", source_anchor="a", ordinal=0, heading=None, text="body", excerpt_digest="A" * 64
        )


# ---------------------------------------------------------------------------
# Discriminated union round trip.
# ---------------------------------------------------------------------------


def test_parse_parser_result_success_round_trip() -> None:
    data = {"status": "ok", "envelope": _minimal_envelope(sections=[_section(0)])}
    result = po.parse_parser_result(data)
    assert isinstance(result, po.ParserSuccess)


def test_parse_parser_result_refusal_round_trip() -> None:
    result = po.parse_parser_result({"status": "refused", "refusal_code": "malformed_source"})
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_parse_parser_result_refuses_unknown_status() -> None:
    with pytest.raises(ValidationError):
        po.parse_parser_result({"status": "pending"})


def test_parse_parser_result_refuses_unknown_refusal_code() -> None:
    with pytest.raises(ValidationError):
        po.parse_parser_result({"status": "refused", "refusal_code": "not_a_real_code"})


# ---------------------------------------------------------------------------
# API-side binding check.
# ---------------------------------------------------------------------------


def test_verify_source_binding_accepts_matching() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    po.verify_source_binding(envelope, source_evidence_id=source_evidence_id, source_content_digest="a" * 64)
    # Reached only if the call above did not raise -- the behavior under test.
    assert envelope.source_evidence_id == source_evidence_id


def test_verify_source_binding_refuses_mismatched_source_evidence_id() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    with pytest.raises(po.ParserBindingError):
        po.verify_source_binding(envelope, source_evidence_id=uuid.uuid4(), source_content_digest="a" * 64)


def test_verify_source_binding_refuses_mismatched_digest() -> None:
    source_evidence_id = uuid.uuid4()
    envelope = po.ParsedSourceEnvelope(
        **_minimal_envelope(source_evidence_id=str(source_evidence_id), source_content_digest="a" * 64)
    )
    with pytest.raises(po.ParserBindingError):
        po.verify_source_binding(envelope, source_evidence_id=source_evidence_id, source_content_digest="b" * 64)
