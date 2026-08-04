"""The configured chunk window reaches the producer that chunks.

`EMBEDDING_CHUNK_TOKENS` was parsed into Settings and read by nothing while the fact
producer used a module constant, so the documented knob did nothing. This pins the wiring
at the seam where it was broken: the service, not the function it calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from registry.service.facts import FactService


def _service(chunk_tokens: int | None) -> FactService:
    return FactService(
        session_factory=MagicMock(),
        clock=MagicMock(),
        vocabulary=MagicMock(),
        entity_service=MagicMock(),
        chunk_tokens=chunk_tokens,
    )


def test_the_service_holds_the_configured_window() -> None:
    assert _service(128)._chunk_tokens == 128


def test_an_unset_window_falls_back_to_the_drain_default() -> None:
    """Absent configuration must not mean absent chunking.

    The fallback is the drain's own default rather than a second literal, so the producer
    and the consumer's recompute path agree on granularity when nobody has configured it.
    """
    from registry.service.embedding_drain import _CHUNK_TOKENS

    assert _service(None)._chunk_tokens == _CHUNK_TOKENS
