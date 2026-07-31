"""Unit tests for the remote embedding provider.

Uses ``httpx.MockTransport`` rather than respx, matching
``tests/unit/test_entitlement_client.py`` — respx does not reliably intercept
clients constructed inside fixtures in this environment.

The parsing tests carry most of the weight. A remote endpoint that returns the
wrong count, the wrong width, or reordered results would otherwise misalign
vectors against the text chunks they were computed for, and every affected row
would look perfectly valid in the database.
"""

from __future__ import annotations

import httpx
import numpy as np
import pytest

from registry.embedding.remote_http import (
    EmbeddingAuthError,
    EmbeddingMalformedError,
    EmbeddingServiceError,
    HttpEmbedder,
)

_ENDPOINT = "https://llm-gateway.test.local/v1/embeddings"


def _embedder(handler, *, dim: int = 4, max_retries: int = 2) -> HttpEmbedder:
    return HttpEmbedder(
        endpoint=_ENDPOINT,
        model_version="test-model",
        expected_dim=dim,
        connect_timeout_ms=100,
        read_timeout_ms=200,
        max_retries=max_retries,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(vectors: list[list[float]]) -> httpx.Response:
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    return httpx.Response(200, json={"data": data})


class TestHappyPath:
    def test_returns_vectors_in_input_order(self):
        embedder = _embedder(lambda _r: _ok([[1.0, 0, 0, 0], [0, 1.0, 0, 0]]))
        vectors = embedder.encode(["a", "b"])
        assert vectors.shape == (2, 4)
        assert vectors.dtype == np.float32
        assert np.array_equal(vectors[0], np.array([1, 0, 0, 0], dtype=np.float32))

    def test_sends_texts_and_model_id(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return _ok([[1.0, 0, 0, 0]])

        _embedder(handler).encode(["hello"])
        assert seen == {"input": ["hello"], "model": "test-model"}

    def test_empty_batch_makes_no_request(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made for an empty batch")

        assert _embedder(handler).encode([]).shape == (0, 4)

    def test_out_of_order_results_are_realigned(self):
        """Entries carry their own index; order on the wire is not guaranteed.

        Trusting arrival order would silently pair each vector with the wrong
        chunk of text.
        """

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0, 1.0, 0, 0]},
                        {"index": 0, "embedding": [1.0, 0, 0, 0]},
                    ]
                },
            )

        vectors = _embedder(handler).encode(["first", "second"])
        assert np.array_equal(vectors[0], np.array([1, 0, 0, 0], dtype=np.float32))
        assert np.array_equal(vectors[1], np.array([0, 1, 0, 0], dtype=np.float32))


class TestFailureModes:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures_are_not_retried(self, status):
        calls = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(status)

        with pytest.raises(EmbeddingAuthError):
            _embedder(handler).encode(["x"])
        assert len(calls) == 1, "a credential rejection cannot be fixed by retrying"

    def test_server_errors_are_retried_then_raise(self):
        calls = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503)

        with pytest.raises(EmbeddingServiceError):
            _embedder(handler, max_retries=2).encode(["x"])
        assert len(calls) == 3

    def test_a_retry_can_succeed(self):
        calls = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503)
            return _ok([[1.0, 0, 0, 0]])

        assert _embedder(handler).encode(["x"]).shape == (1, 4)

    def test_transport_error_is_reported_as_unavailable(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(EmbeddingServiceError, match="unavailable"):
            _embedder(handler, max_retries=0).encode(["x"])

    def test_service_errors_are_marked_retriable(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(EmbeddingServiceError) as excinfo:
            _embedder(handler, max_retries=0).encode(["x"])
        assert excinfo.value.is_retriable is True


class TestMalformedResponses:
    def _expect_malformed(self, payload: object, count: int = 1):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        with pytest.raises(EmbeddingMalformedError):
            _embedder(handler).encode(["x"] * count)

    def test_missing_data_array(self):
        self._expect_malformed({"result": []})

    def test_wrong_number_of_vectors(self):
        """Returning fewer vectors than texts must fail loudly.

        Accepting a short result would pair chunks with the wrong embeddings
        from that point on, and every row written would look valid.
        """
        self._expect_malformed({"data": [{"index": 0, "embedding": [1.0, 0, 0, 0]}]}, count=2)

    def test_wrong_dimension(self):
        self._expect_malformed({"data": [{"index": 0, "embedding": [1.0, 0]}]})

    def test_non_numeric_embedding(self):
        self._expect_malformed({"data": [{"index": 0, "embedding": ["a", "b", "c", "d"]}]})

    def test_body_is_not_json(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>gateway</html>")

        with pytest.raises(EmbeddingMalformedError):
            _embedder(handler).encode(["x"])
