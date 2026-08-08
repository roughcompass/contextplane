"""Zero-vector embedder. No model artifact, no inference."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

#: Marks every vector this class produces as fake. The drain stamps it into
#: `embeddings.model_id`, and the semantic arm filters on that column, so stub
#: rows can never satisfy a query issued by a real embedder. Do not change this
#: to a real model id — that is precisely how fake vectors become
#: indistinguishable from real ones.
STUB_MODEL_VERSION = "stub-zero"


class StubEmbedder:
    """Returns zero vectors of the configured width.

    For tests and smoke deployments that exercise the plumbing without needing
    retrieval recall. Search still returns rows — every distance is identical, so
    the ranking is arbitrary and the lexical arm decides the order.
    """

    model_version: str = STUB_MODEL_VERSION

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        return np.zeros((len(texts), self._dim), dtype=np.float32)


__all__ = ["STUB_MODEL_VERSION", "StubEmbedder"]
