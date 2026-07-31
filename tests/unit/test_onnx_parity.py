"""ONNX provider vs. the reference sentence-transformers implementation.

The whole case for running ONNX is that it produces the *same* vectors as the
reference, so switching to it costs nothing: no migration, no re-embed, and rows
written before and after the switch stay comparable. This test is what makes
that claim checkable rather than asserted.

Needs both the staged artifact and the optional torch extra
(`pip install "registry[torch]"`), plus `model.safetensors`, which the container
image deliberately does not carry:

    python scripts/fetch_embedding_model.py \\
        --out .devstack/models/all-MiniLM-L6-v2 --with-torch-weights

Skips when either is missing, so a default install and a torch-free CI run both
stay green. Marked slow — loading torch takes several seconds.

Measured on the pinned artifact: minimum cosine 0.9999999, maximum absolute
element difference 3.2e-07 — float32 rounding, not a behavioural gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from registry.embedding.local_onnx import OnnxEmbedder
from tests.helpers.embedding_artifact import require_artifact

pytestmark = pytest.mark.slow

# Chosen to hit the places a reimplementation actually diverges: pooling over a
# single token, truncation past max_seq_length, and padding in a mixed batch.
_CORPUS = [
    "Payments service handling card authorisation and settlement.",
    "A short one.",
    "Deprecated in favour of the v2 ledger API; migrate before Q3.",
    "x",
    " ".join(["token"] * 400),
    'Punctuation, semicolons; dashes - and "quotes".',
]

# Float32 accumulation across two runtimes will not agree bit for bit. Anything
# below this is a real difference in the computation, not arithmetic noise.
_MIN_COSINE = 0.999


@pytest.fixture(scope="module")
def reference_vectors():
    artifact = require_artifact()
    if not (artifact / "model.safetensors").is_file():
        pytest.skip("torch weights not staged; re-run fetch_embedding_model.py --with-torch-weights")
    sentence_transformers = pytest.importorskip(
        "sentence_transformers", reason='reference implementation needs pip install "registry[torch]"'
    )
    model = sentence_transformers.SentenceTransformer(str(artifact))
    return model.encode(_CORPUS, convert_to_numpy=True, normalize_embeddings=False).astype(np.float32)


@pytest.fixture(scope="module")
def onnx_vectors():
    embedder = OnnxEmbedder(
        model_path=str(require_artifact()),
        model_version="all-MiniLM-L6-v2",
        expected_dim=384,
    )
    return embedder.encode(_CORPUS)


def _cosines(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return (left * right).sum(axis=1) / (np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1))


def test_shapes_match(reference_vectors, onnx_vectors):
    assert onnx_vectors.shape == reference_vectors.shape == (len(_CORPUS), 384)


def test_vectors_match_the_reference(reference_vectors, onnx_vectors):
    cosines = _cosines(reference_vectors, onnx_vectors)
    worst = int(np.argmin(cosines))
    assert cosines.min() >= _MIN_COSINE, (
        f"lowest cosine {cosines.min():.8f} on {_CORPUS[worst][:60]!r}. "
        f"A gap here means the ONNX path stopped reproducing the reference — "
        f"check pooling and the Normalize step."
    )


def test_reference_output_is_normalized(reference_vectors):
    """Pins the assumption the ONNX provider is built on.

    `encode(normalize_embeddings=False)` reads as if vectors come out
    unnormalized; they do not, because the model's own Normalize module runs
    regardless. The ONNX provider matches that. If a future
    sentence-transformers release changes it, this fails first and explains why.
    """
    assert np.allclose(np.linalg.norm(reference_vectors, axis=1), 1.0, atol=1e-5)
