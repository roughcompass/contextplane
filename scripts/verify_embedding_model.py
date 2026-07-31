"""Smoke-check a staged embedding artifact by actually running inference on it.

Complements the checksum verification in ``fetch_embedding_model.py``. Checksums
prove the bytes arrived intact; this proves they compute the vectors the rest of
the system assumes. An artifact can be byte-perfect and still be the wrong
export — a model without its pooling config, an ONNX graph whose output tensor
is not the hidden state, a different revision with the same filenames.

Runs in the image build, so a bad artifact fails the build rather than becoming
a search-quality mystery in production:

    python scripts/verify_embedding_model.py --model-path /opt/models/all-MiniLM-L6-v2

Four properties, each mapped to a way the artifact can be wrong:

* width matches the manifest        — wrong model entirely
* vectors are unit length           — the Normalize module was dropped
* related text scores far above
  unrelated text                    — broken pooling, or the wrong output tensor
* the same input twice is identical — non-deterministic session configuration

Deliberately not a parity test. Reproducing reference vectors needs torch, which
is not installed in the image; ``tests/unit/test_onnx_parity.py`` covers that.

Exit line: ``embedding artifact ok: <model_id> (<dim>-d)``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.embedding.local_onnx import OnnxEmbedder  # noqa: E402

_MANIFEST = _REPO_ROOT / "registry" / "embedding" / "model_manifest.json"

# A near-paraphrase pair and an unrelated one. The measured values on the
# reference artifact are ~0.88 and ~0.11; the thresholds sit well clear of both
# so ordinary revision drift does not trip them, while a model that has stopped
# encoding meaning at all — uniform, random, or all-zero output — cannot pass.
_RELATED = ("payment processing service", "a service that processes payments")
_UNRELATED = ("payment processing service", "weather forecast API")
_MIN_RELATED_COSINE = 0.70
_MAX_UNRELATED_COSINE = 0.40


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return 0.0 if denominator == 0.0 else float(left @ right) / denominator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-check a staged embedding artifact.")
    parser.add_argument("--model-path", required=True, help="Directory holding the staged artifact")
    args = parser.parse_args(argv)

    with _MANIFEST.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    model_id = str(manifest["model_id"])
    expected_dim = int(manifest["dimension"])

    embedder = OnnxEmbedder(
        model_path=args.model_path,
        model_version=model_id,
        expected_dim=expected_dim,
    )

    texts = [_RELATED[0], _RELATED[1], _UNRELATED[1]]
    vectors = embedder.encode(texts)

    failures: list[str] = []

    if vectors.shape != (len(texts), expected_dim):
        failures.append(f"expected shape {(len(texts), expected_dim)}, got {vectors.shape}")

    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-4):
        failures.append(f"vectors are not unit length (norms {norms.round(4).tolist()}) — Normalize module missing?")

    related = _cosine(vectors[0], vectors[1])
    unrelated = _cosine(vectors[0], vectors[2])
    if related < _MIN_RELATED_COSINE:
        failures.append(f"related texts scored {related:.4f}, expected >= {_MIN_RELATED_COSINE}")
    if unrelated > _MAX_UNRELATED_COSINE:
        failures.append(f"unrelated texts scored {unrelated:.4f}, expected <= {_MAX_UNRELATED_COSINE}")

    repeated = embedder.encode([_RELATED[0]])
    if not np.array_equal(repeated[0], vectors[0]):
        failures.append("encoding the same text twice produced different vectors")

    if failures:
        print(f"embedding artifact at {args.model_path} failed verification:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"  related={related:.4f} unrelated={unrelated:.4f} norms=1.0")
    print(f"embedding artifact ok: {model_id} ({expected_dim}-d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
