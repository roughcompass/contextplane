"""Unit tests for embedder construction.

The behaviour under test is mostly about what *does not* happen. The factory
used to swallow any load failure and hand back zero vectors, which meant a
deployment that could not reach its model booted healthy and quietly filled the
index with unusable rows. Several tests here exist purely to pin that door shut.
"""

from __future__ import annotations

import numpy as np
import pytest

from registry.config import Settings, _resolve_embedding_provider
from registry.embedding import PROVIDERS, build_embedder
from registry.embedding.stub import STUB_MODEL_VERSION, StubEmbedder


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        database_url="postgresql+asyncpg://u:p@localhost/r",
        pgbouncer_url="postgresql+asyncpg://u:p@localhost/r",
        scheduler_jobstore_url="postgresql+asyncpg://u:p@localhost/r",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestProviderDispatch:
    def test_stub_provider_returns_stub_embedder(self):
        embedder = build_embedder(_settings(embedding_provider="stub"))
        assert isinstance(embedder, StubEmbedder)

    def test_stub_honours_configured_dimension(self):
        embedder = build_embedder(_settings(embedding_provider="stub", embedding_dim=1536))
        assert embedder.encode(["a", "b"]).shape == (2, 1536)

    def test_unknown_provider_names_the_accepted_values(self):
        with pytest.raises(ValueError) as excinfo:
            build_embedder(_settings(embedding_provider="magic"))
        message = str(excinfo.value)
        assert "magic" in message
        for provider in PROVIDERS:
            assert provider in message

    def test_http_provider_requires_an_endpoint(self):
        with pytest.raises(ValueError, match="EMBEDDING_HTTP_ENDPOINT"):
            build_embedder(_settings(embedding_provider="http", embedding_http_endpoint=None))


class TestFailFast:
    """A provider that cannot load must stop the process, not degrade."""

    def test_missing_onnx_artifact_raises(self, tmp_path):
        with pytest.raises(Exception) as excinfo:
            build_embedder(_settings(embedding_provider="onnx", embedding_model_path=str(tmp_path / "absent")))
        # The message has to tell an operator how to fix it — this is the error
        # a network-isolated deployment hits first.
        assert "fetch_embedding_model" in str(excinfo.value)

    def test_missing_onnx_artifact_does_not_fall_back_to_zero_vectors(self, tmp_path):
        """The specific regression: booting healthy on a model that failed to load.

        Zero vectors are written into the same index as real ones and stamped
        with whatever model id is configured, so nothing downstream can tell
        them apart later. Raising is the only recoverable outcome.
        """
        try:
            embedder = build_embedder(
                _settings(embedding_provider="onnx", embedding_model_path=str(tmp_path / "absent"))
            )
        except Exception:
            return
        pytest.fail(f"expected a raise, got a working embedder: {type(embedder).__name__}")

    def test_stub_is_the_only_way_to_get_zero_vectors(self):
        embedder = build_embedder(_settings(embedding_provider="stub"))
        assert np.array_equal(embedder.encode(["anything"]), np.zeros((1, 384), dtype=np.float32))


class TestStubEmbedder:
    def test_model_version_marks_vectors_as_fake(self):
        """The stub's model id must never look like a real model's.

        The semantic arm filters on model_id, so this string is what keeps stub
        rows from satisfying a query issued by a real embedder.
        """
        assert StubEmbedder().model_version == STUB_MODEL_VERSION
        assert STUB_MODEL_VERSION == "stub-zero"

    def test_encode_shape_and_dtype(self):
        vectors = StubEmbedder(dim=8).encode(["a", "b", "c"])
        assert vectors.shape == (3, 8)
        assert vectors.dtype == np.float32

    def test_encode_empty_batch(self):
        assert StubEmbedder(dim=8).encode([]).shape == (0, 8)


class TestLegacyStubSpelling:
    """`EMBEDDING_MODEL=stub` predates EMBEDDING_PROVIDER and still works."""

    def test_legacy_model_stub_selects_the_stub_provider(self):
        assert _resolve_embedding_provider(None, "stub") == "stub"

    def test_explicit_provider_wins_over_the_legacy_spelling(self):
        assert _resolve_embedding_provider("http", "stub") == "http"

    def test_real_model_name_defaults_to_onnx(self):
        assert _resolve_embedding_provider(None, "all-MiniLM-L6-v2") == "onnx"

    def test_provider_is_normalised(self):
        assert _resolve_embedding_provider("  STUB  ", "all-MiniLM-L6-v2") == "stub"

    def test_legacy_spelling_warns(self, caplog):
        with caplog.at_level("WARNING"):
            _resolve_embedding_provider(None, "stub")
        assert "deprecated" in caplog.text
