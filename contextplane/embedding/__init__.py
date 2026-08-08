"""Embedding providers.

`build_embedder()` is the single construction point for the `Embedder` that gets
injected into `RetrievalService` and the embedding drain.

Provider modules are imported inside the branch that needs them, so a deployment
never pays the import cost — or the install cost — of a provider it does not use.
The ONNX runtime stays unimported when running the stub, and `sentence-transformers`
(which pulls torch, roughly 750 MB) is an optional install that only the
`sentence_transformers` provider requires.

**A provider that cannot load raises.** There is deliberately no fallback to
zero-vector embeddings. A silently-degraded embedder writes unusable vectors into
the same index as real ones, and afterwards nothing downstream can tell them
apart — the failure surfaces weeks later as unexplained bad search results. A
process that refuses to start is recoverable; a corrupted index is not. Operators
who genuinely want zero vectors ask for them with `EMBEDDING_PROVIDER=stub`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from contextplane.config import Settings
    from contextplane.types import Embedder

#: Every accepted value of `EMBEDDING_PROVIDER`. Kept here so config validation
#: and the operator-facing error message cannot drift apart.
PROVIDERS: tuple[str, ...] = ("onnx", "sentence_transformers", "http", "stub")


def build_embedder(settings: Settings) -> Embedder:
    """Construct the configured embedder, or raise.

    Raises:
        ValueError: `EMBEDDING_PROVIDER` is not one of `PROVIDERS`, or the
            selected provider is missing required configuration.
        Exception: whatever the provider raises when its model artifact is
            missing, unreadable, or the wrong shape. Deliberately not caught.
    """
    provider = settings.embedding_provider

    if provider == "stub":
        from contextplane.embedding.stub import StubEmbedder  # noqa: PLC0415 - see module docstring

        return StubEmbedder(dim=settings.embedding_dim)

    if provider == "onnx":
        from contextplane.embedding.local_onnx import OnnxEmbedder  # noqa: PLC0415 - see module docstring

        return OnnxEmbedder(
            model_path=settings.embedding_model_path,
            model_version=settings.embedding_model,
            expected_dim=settings.embedding_dim,
        )

    if provider == "sentence_transformers":
        from contextplane.embedding.local_torch import SentenceTransformerEmbedder  # noqa: PLC0415 - optional torch

        return SentenceTransformerEmbedder(
            model_path=settings.embedding_model_path,
            model_version=settings.embedding_model,
            expected_dim=settings.embedding_dim,
        )

    if provider == "http":
        from contextplane.embedding.remote_http import HttpEmbedder  # noqa: PLC0415 - see module docstring

        if not settings.embedding_http_endpoint:
            raise ValueError("EMBEDDING_PROVIDER=http requires EMBEDDING_HTTP_ENDPOINT to be set")
        return HttpEmbedder(
            endpoint=settings.embedding_http_endpoint,
            model_version=settings.embedding_model,
            expected_dim=settings.embedding_dim,
            connect_timeout_ms=settings.embedding_http_connect_timeout_ms,
            read_timeout_ms=settings.embedding_http_read_timeout_ms,
            max_retries=settings.embedding_http_max_retries,
        )

    raise ValueError(f"unknown EMBEDDING_PROVIDER {provider!r}; expected one of {', '.join(PROVIDERS)}")


__all__ = ["PROVIDERS", "build_embedder"]
