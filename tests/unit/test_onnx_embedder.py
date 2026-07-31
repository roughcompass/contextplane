"""Unit tests for the ONNX embedding provider.

Split in two. The artifact-validation tests build broken directories on the fly
and need no model, so they always run — they cover the failure messages a
network-isolated deployment is most likely to see. The inference tests need the
real ~90 MB artifact and skip without it.
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from registry.embedding.local_onnx import ArtifactError, OnnxEmbedder
from tests.helpers.embedding_artifact import require_artifact


def _build_embedder(path: str, dim: int = 384) -> OnnxEmbedder:
    return OnnxEmbedder(model_path=path, model_version="all-MiniLM-L6-v2", expected_dim=dim)


class TestArtifactValidation:
    def test_empty_directory_names_the_missing_file(self, tmp_path):
        with pytest.raises(ArtifactError) as excinfo:
            _build_embedder(str(tmp_path))
        assert "onnx/model.onnx" in str(excinfo.value)

    def test_error_tells_the_operator_how_to_stage_it(self, tmp_path):
        with pytest.raises(ArtifactError) as excinfo:
            _build_embedder(str(tmp_path))
        message = str(excinfo.value)
        assert "fetch_embedding_model.py" in message
        assert "EMBEDDING_MODEL_PATH" in message

    def test_missing_tokenizer_is_reported(self, tmp_path):
        (tmp_path / "onnx").mkdir()
        (tmp_path / "onnx" / "model.onnx").write_bytes(b"not really a model")
        with pytest.raises(ArtifactError, match="tokenizer.json"):
            _build_embedder(str(tmp_path))

    def test_malformed_json_is_reported_as_such(self, tmp_path):
        (tmp_path / "onnx").mkdir()
        (tmp_path / "onnx" / "model.onnx").write_bytes(b"x")
        (tmp_path / "tokenizer.json").write_text("{}")
        (tmp_path / "sentence_bert_config.json").write_text("{ not json")
        with pytest.raises(ArtifactError, match="not valid JSON"):
            _build_embedder(str(tmp_path))


class TestArtifactDimensionGuard:
    """A width disagreement must surface at construction, not at insert time."""

    @pytest.fixture
    def staged(self, tmp_path):
        artifact = require_artifact()
        target = tmp_path / "model"
        shutil.copytree(artifact, target)
        return target

    def test_dimension_mismatch_refuses_to_construct(self, staged):
        with pytest.raises(ArtifactError) as excinfo:
            _build_embedder(str(staged), dim=1536)
        message = str(excinfo.value)
        assert "384" in message and "1536" in message

    def test_unsupported_pooling_is_rejected(self, staged):
        pooling = staged / "1_Pooling" / "config.json"
        config = json.loads(pooling.read_text())
        config["pooling_mode_mean_tokens"] = False
        config["pooling_mode_cls_token"] = True
        pooling.write_text(json.dumps(config))

        with pytest.raises(ArtifactError, match="pooling"):
            _build_embedder(str(staged))


class TestInference:
    @pytest.fixture(scope="class")
    def embedder(self):
        return _build_embedder(str(require_artifact()))

    def test_encode_shape_and_dtype(self, embedder):
        vectors = embedder.encode(["one", "two", "three"])
        assert vectors.shape == (3, 384)
        assert vectors.dtype == np.float32

    def test_encode_empty_batch(self, embedder):
        assert embedder.encode([]).shape == (0, 384)

    def test_vectors_are_unit_length(self, embedder):
        """Guards the Normalize module.

        Dropping it would leave cosine ranking unchanged — cosine is scale
        invariant — so no ordering assertion would catch it. Only the raw
        magnitude shows the difference, and it is the difference between
        vectors that are comparable with already-stored rows and ones that
        are not.
        """
        norms = np.linalg.norm(embedder.encode(["alpha", "a much longer sentence here"]), axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)

    def test_related_text_scores_above_unrelated(self, embedder):
        vectors = embedder.encode(
            ["payment processing service", "a service that processes payments", "weather forecast API"]
        )
        assert float(vectors[0] @ vectors[1]) > float(vectors[0] @ vectors[2])

    def test_encoding_is_deterministic(self, embedder):
        assert np.array_equal(embedder.encode(["stable"]), embedder.encode(["stable"]))

    def test_batching_matches_single_encoding(self, embedder):
        """Padding must not leak into the pooled result.

        Mean pooling weights by the attention mask precisely so a short text
        batched alongside a long one is unaffected by the padding it receives.
        """
        texts = ["short", "a considerably longer piece of text than the other one in this batch"]
        batched = embedder.encode(texts)
        for index, text in enumerate(texts):
            assert np.allclose(batched[index], embedder.encode([text])[0], atol=1e-5)

    def test_text_beyond_max_sequence_length_is_truncated_not_rejected(self, embedder):
        vectors = embedder.encode([" ".join(["word"] * 5000)])
        assert vectors.shape == (1, 384)
        assert np.isfinite(vectors).all()
