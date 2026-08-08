"""Embedder backed by a remote OpenAI-compatible embeddings endpoint.

For deployments that already run an approved embedding service — an internal
model-serving gateway, Azure OpenAI, Bedrock behind a proxy — and would rather
not ship model weights in the image at all.

**Not the air-gapped path.** This provider needs egress by definition. It exists
because the provider seam makes it nearly free, and because a deployment with an
internal endpoint should not have to pay for in-process inference.

Wire format is the OpenAI embeddings shape, which internal gateways, vLLM, and
text-embeddings-inference all speak:

    POST  {"input": ["text", ...], "model": "<model_version>"}
    200   {"data": [{"index": 0, "embedding": [0.1, ...]}, ...]}

`encode()` is synchronous, matching the `Embedder` protocol, and uses a blocking
client. Callers run it off the event loop via `asyncio.to_thread`, so blocking
here costs a worker thread rather than the whole loop.

Vectors are returned as received. If the endpoint does not L2-normalise and the
stored corpus was built by a provider that does, cosine ranking still agrees —
cosine distance is scale invariant — but raw distances shift. Point a new
endpoint at a fresh `EMBEDDING_MODEL` id so the semantic arm's `model_id` filter
keeps the two corpora apart.
"""

from __future__ import annotations

import random
import time

import httpx
import numpy as np
import numpy.typing as npt
from prometheus_client import Counter, Histogram

from contextplane.exceptions import RegistryError

# ---------------------------------------------------------------------------
# Exceptions


class EmbeddingClientError(RegistryError):
    """Base class for every error raised by the remote embedder."""


class EmbeddingAuthError(EmbeddingClientError):
    """Endpoint rejected the request (HTTP 401 or 403).

    A credential problem, not a transient one. Retrying cannot fix it.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"embedding endpoint rejected the request (HTTP {status_code})")
        self.status_code = status_code


class EmbeddingServiceError(EmbeddingClientError):
    """Endpoint is unavailable: 5xx, 429 after retries, timeout, or network failure.

    ``is_retriable`` marks the class of failure the drain may re-attempt later.
    A separate field rather than an ``isinstance`` check keeps the contract
    explicit at call sites.
    """

    is_retriable: bool = True

    def __init__(self, reason: str) -> None:
        super().__init__(f"embedding endpoint unavailable: {reason}")
        self.reason = reason


class EmbeddingMalformedError(EmbeddingClientError):
    """Endpoint returned 200 but the body was not the expected shape.

    Treated as a hard failure rather than an empty result: silently returning
    fewer vectors than texts would misalign chunks against their embeddings.
    """


# ---------------------------------------------------------------------------
# Telemetry — counter labeled by outcome class plus a duration histogram.

_CALLS_TOTAL = Counter(
    "registry_embedding_calls_total",
    "Remote embedding endpoint HTTP calls, labeled by outcome status class.",
    ["status_class"],
)

_CALL_DURATION = Histogram(
    "registry_embedding_call_duration_seconds",
    "End-to-end latency of remote embedding calls (including retries).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

_STATUS_2XX = "2xx"
_STATUS_AUTH = "4xx_auth"
_STATUS_5XX = "5xx_retriable"
_STATUS_MALFORMED = "malformed"


def _backoff_seconds() -> float:
    """Jittered backoff between 50 ms and 150 ms."""
    return random.uniform(0.050, 0.150)  # noqa: S311 - retry-jitter timing, not a token/id/secret; non-cryptographic use is correct here


class HttpEmbedder:
    """Embedder that calls a remote OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        endpoint: str,
        model_version: str,
        expected_dim: int,
        *,
        connect_timeout_ms: int,
        read_timeout_ms: int,
        max_retries: int,
        client: httpx.Client | None = None,
    ) -> None:
        self.model_version = model_version
        self._endpoint = endpoint
        self._dim = expected_dim
        self._connect_timeout = connect_timeout_ms / 1000.0
        self._read_timeout = read_timeout_ms / 1000.0
        self._max_retries = max_retries
        self._client = client or httpx.Client()

    def close(self) -> None:
        self._client.close()

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        """Embed a batch. Blocking on network I/O — callers offload to a thread."""
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        started = time.monotonic()
        try:
            payload = self._request_with_retries(texts)
        finally:
            _CALL_DURATION.observe(time.monotonic() - started)
        return self._parse(payload, expected_rows=len(texts))

    # -- transport -----------------------------------------------------------

    def _request_with_retries(self, texts: list[str]) -> object:
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        body = {"input": texts, "model": self.model_version}

        last_reason = "no attempt made"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self._endpoint, json=body, timeout=timeout)
            except httpx.HTTPError as exc:
                last_reason = repr(exc)
            else:
                status = response.status_code
                if status in (401, 403):
                    _CALLS_TOTAL.labels(status_class=_STATUS_AUTH).inc()
                    raise EmbeddingAuthError(status)
                if status < 400:
                    _CALLS_TOTAL.labels(status_class=_STATUS_2XX).inc()
                    try:
                        return response.json()
                    except ValueError as exc:
                        _CALLS_TOTAL.labels(status_class=_STATUS_MALFORMED).inc()
                        raise EmbeddingMalformedError(f"response body is not JSON: {exc}") from exc
                # 429 and 5xx are worth another attempt; other 4xx are not, but
                # retrying a handful of times is cheaper than special-casing
                # every gateway's idea of a client error.
                last_reason = f"HTTP {status}"

            if attempt < self._max_retries:
                time.sleep(_backoff_seconds())

        _CALLS_TOTAL.labels(status_class=_STATUS_5XX).inc()
        raise EmbeddingServiceError(f"{last_reason} after {self._max_retries + 1} attempt(s)")

    # -- response parsing ----------------------------------------------------

    def _parse(self, payload: object, expected_rows: int) -> npt.NDArray[np.float32]:
        if not isinstance(payload, dict):
            raise EmbeddingMalformedError(f"expected a JSON object, got {type(payload).__name__}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise EmbeddingMalformedError("response has no 'data' array")
        if len(data) != expected_rows:
            raise EmbeddingMalformedError(f"expected {expected_rows} embeddings, got {len(data)}")

        # Order is not guaranteed by the wire format — every entry carries its
        # own index. Sorting by it keeps vectors aligned with their input chunks.
        rows: list[list[float]] = [[] for _ in range(expected_rows)]
        for position, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise EmbeddingMalformedError(f"data[{position}] is not an object")
            raw_index = entry.get("index", position)
            index = raw_index if isinstance(raw_index, int) and 0 <= raw_index < expected_rows else position
            vector = entry.get("embedding")
            if not isinstance(vector, list) or not all(isinstance(value, int | float) for value in vector):
                raise EmbeddingMalformedError(f"data[{position}].embedding is not a numeric array")
            if len(vector) != self._dim:
                raise EmbeddingMalformedError(
                    f"embedding dimension mismatch: endpoint returned {len(vector)}-d vectors "
                    f"but EMBEDDING_DIM is {self._dim}"
                )
            rows[index] = [float(value) for value in vector]

        return np.asarray(rows, dtype=np.float32)


__all__ = [
    "EmbeddingAuthError",
    "EmbeddingClientError",
    "EmbeddingMalformedError",
    "EmbeddingServiceError",
    "HttpEmbedder",
]
