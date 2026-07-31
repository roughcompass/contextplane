"""Reference sentence-transformers embedder.

Optional. `sentence-transformers` pulls torch — roughly 750 MB — which does not
fit the memory envelope the default deployment targets, so it is not a base
dependency. Install it with:

    pip install "registry[torch]"

Two reasons to run this provider instead of the ONNX one: it is the reference
implementation the parity test measures against, and it accepts any
sentence-transformers model directory without needing an ONNX export.

`model_path` should be a **local directory**. Passing a bare model id
(`all-MiniLM-L6-v2`) makes the library fetch from the Hugging Face Hub on
construction, which is exactly what a network-isolated deployment cannot do.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


class SentenceTransformerEmbedder:
    """Embedder backed by an in-process sentence-transformers model."""

    def __init__(self, model_path: str, model_version: str, expected_dim: int) -> None:
        # Local import so deployments on another provider neither pay the torch
        # import cost nor need it installed at all.
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised by install shape, not unit tests
            raise ImportError(
                "EMBEDDING_PROVIDER=sentence_transformers requires the optional torch extra: "
                'pip install "registry[torch]"'
            ) from exc

        self.model_version = model_version
        self._dim = expected_dim
        # Not wrapped in try/except: a load failure must propagate. Substituting
        # zero vectors here would poison the index with rows nothing can later
        # distinguish from real ones.
        self._model = SentenceTransformer(model_path)

        actual = self._model.get_sentence_embedding_dimension()
        if actual is not None and actual != expected_dim:
            raise ValueError(
                f"embedding dimension mismatch: model at {model_path} produces {actual}-d vectors "
                f"but EMBEDDING_DIM is {expected_dim}. Set EMBEDDING_DIM={actual} and migrate, "
                f"or load a {expected_dim}-d model."
            )

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        """Embed a batch. Blocking and CPU-bound — callers offload to a thread."""
        vectors: npt.NDArray[np.float32] = self._model.encode(
            texts,
            convert_to_numpy=True,
            # The model's own Normalize module still runs; this flag only
            # suppresses an additional normalisation pass on top of it.
            normalize_embeddings=False,
        ).astype(np.float32)
        return vectors


__all__ = ["SentenceTransformerEmbedder"]
