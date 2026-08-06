"""Unit tests for the sandboxed parser's own pure logic
(`registry/arc/sandbox/parser_main.py`).

`tests/conformance/test_arc_parser_sandbox.py` owns everything that needs a
real, separate OS process to mean anything: `apply_resource_limits` (which
sets a real `RLIMIT_CPU` on whatever process calls it -- calling it here
would apply that ceiling to the shared pytest worker running every other
test in this session) and `install_network_guard` (which replaces
`socket.socket`/`socket.getaddrinfo` process-wide for as long as the
interpreter lives -- calling it here would break any other test in this
session that opens a real socket). Neither is safe to call in-process, so
neither is exercised here; this file's job is everything that *is* safe:
the pure markdown-splitting logic (`parse_markdown`/`_split_sections`), the
resource-limit *decision* functions in isolation from the syscalls that
would make them dangerous (`_guarded_socket_ctor`, `_deny_getaddrinfo`,
`_current_rss_bytes`, `ResourceLimitReport`), and `start_memory_watchdog`
exercised with a ceiling high enough that it can never actually fire.
"""

from __future__ import annotations

import hashlib
import socket
import threading
import uuid

import pytest

from registry.arc.sandbox import parser_main
from registry.arc.schemas import parser_output as po

# ---------------------------------------------------------------------------
# parse_markdown / _split_sections: pure, no I/O.
# ---------------------------------------------------------------------------

_TYPICAL_MARKDOWN = (
    b"Leading prose before any heading.\n\n"
    b"# Title\n\n"
    b"Body text under the title.\n\n"
    b"## Subsection\n\n"
    b"Body text under the subsection.\n"
)


def test_parse_markdown_typical_document_succeeds() -> None:
    result = parser_main.parse_markdown(_TYPICAL_MARKDOWN, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert result.envelope.title == "Title"
    assert [s.ordinal for s in result.envelope.sections] == [0, 1, 2]
    assert result.envelope.sections[0].heading is None  # the leading, anchorless section
    assert result.envelope.warnings == []


def test_parse_markdown_unsupported_media_type_refused() -> None:
    result = parser_main.parse_markdown(b"# hi", media_type="application/pdf", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "unsupported_media_type"


def test_parse_markdown_empty_source_refused_as_malformed() -> None:
    result = parser_main.parse_markdown(b"   \n \n", media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_parse_markdown_non_utf8_source_refused_as_malformed() -> None:
    result = parser_main.parse_markdown(
        b"\xff\xfe\x00\x01", media_type="text/markdown", source_evidence_id=uuid.uuid4()
    )
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "malformed_source"


def test_parse_markdown_too_many_headings_refused_as_too_complex() -> None:
    content = "\n".join(f"# heading {i}" for i in range(po.MAX_SECTIONS + 5)).encode("utf-8")
    result = parser_main.parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserRefusal)
    assert result.refusal_code == "source_too_complex"


def test_parse_markdown_exactly_max_sections_succeeds() -> None:
    content = "\n".join(f"# heading {i}" for i in range(po.MAX_SECTIONS)).encode("utf-8")
    result = parser_main.parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert len(result.envelope.sections) == po.MAX_SECTIONS


def test_parse_markdown_truncates_long_text_with_a_warning() -> None:
    body = "x" * (po.MAX_TEXT_LENGTH + 100)
    content = f"# Heading\n{body}".encode()
    result = parser_main.parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert len(result.envelope.sections[0].text) == po.MAX_TEXT_LENGTH
    assert any(w.code == "truncated" for w in result.envelope.warnings)


def test_parse_markdown_truncates_long_heading_with_a_warning() -> None:
    heading = "H" * 600
    content = f"# {heading}\nbody".encode()
    result = parser_main.parse_markdown(content, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    assert result.envelope.sections[0].heading is not None
    assert len(result.envelope.sections[0].heading) == 500
    assert any(w.code == "truncated" for w in result.envelope.warnings)


def test_parse_markdown_excerpt_digest_matches_the_section_text() -> None:
    result = parser_main.parse_markdown(_TYPICAL_MARKDOWN, media_type="text/markdown", source_evidence_id=uuid.uuid4())
    assert isinstance(result, po.ParserSuccess)
    for section in result.envelope.sections:
        assert section.excerpt_digest == hashlib.sha256(section.text.encode("utf-8")).hexdigest()


def test_parse_markdown_document_with_only_a_heading_has_no_leading_section() -> None:
    result = parser_main.parse_markdown(
        b"# Only Heading\n", media_type="text/markdown", source_evidence_id=uuid.uuid4()
    )
    assert isinstance(result, po.ParserSuccess)
    assert len(result.envelope.sections) == 1
    assert result.envelope.sections[0].heading == "Only Heading"


# ---------------------------------------------------------------------------
# The network guard's *decision* functions, called directly rather than
# through `install_network_guard` -- exercises the exact same logic without
# ever monkeypatching the `socket` module for the rest of this process.
# ---------------------------------------------------------------------------


def test_guarded_socket_ctor_refuses_af_inet() -> None:
    with pytest.raises(PermissionError):
        parser_main._guarded_socket_ctor(socket.AF_INET, socket.SOCK_STREAM)


def test_guarded_socket_ctor_allows_af_unix() -> None:
    sock = parser_main._guarded_socket_ctor(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert sock.family == socket.AF_UNIX
    finally:
        sock.close()


def test_deny_getaddrinfo_always_refuses() -> None:
    with pytest.raises(PermissionError):
        parser_main._deny_getaddrinfo("example.com", 80)


# ---------------------------------------------------------------------------
# Resource-limit reporting and the RSS watchdog, exercised without ever
# calling `resource.setrlimit` on this process.
# ---------------------------------------------------------------------------


def test_resource_limit_report_is_a_plain_frozen_record() -> None:
    report = parser_main.ResourceLimitReport(
        cpu_seconds=30, memory_bytes_requested=512 * 1024 * 1024, memory_limit_applied=False, memory_limit_note="n/a"
    )
    assert report.cpu_seconds == 30
    assert report.memory_limit_applied is False


def test_current_rss_bytes_is_a_positive_int() -> None:
    # A real call -- this process genuinely has some resident memory --
    # but reading `getrusage` changes nothing about this process's limits.
    assert parser_main._current_rss_bytes() > 0


def test_memory_watchdog_does_not_fire_below_an_unreachable_ceiling() -> None:
    # `2**62` bytes is never reachable by this (or any real) process, so
    # this exercises the watchdog's thread lifecycle (start, poll, stop)
    # with zero risk of it ever calling `os._exit` on the test process.
    stop = parser_main.start_memory_watchdog(2**62, poll_interval=0.01)
    try:
        assert isinstance(stop, threading.Event)
    finally:
        stop.set()
