"""ONNX Runtime embedder over a locally-staged sentence-transformers artifact.

This is the default provider. It runs the same `all-MiniLM-L6-v2` weights as the
reference sentence-transformers implementation and produces the same 384-d
vectors, but needs roughly 150 MB of dependencies instead of 900 MB and does not
pull torch — which is what makes the model shippable inside the container image
for deployments with no network egress.

Nothing here reaches the network. The artifact is staged on disk ahead of time
(see `scripts/fetch_embedding_model.py`) and located via `EMBEDDING_MODEL_PATH`.

The inference stack is not a simplification of the reference model — it
reproduces all three of its modules, declared in the artifact's `modules.json`:

    Transformer  ->  Pooling (attention-mask-weighted mean)  ->  Normalize (L2)

The `Normalize` step is easy to lose and worth being explicit about. The stored
vectors have always been unit-length: the reference `encode()` call passes
`normalize_embeddings=False`, but that flag only suppresses an *additional*
normalisation — the model's own `Normalize` module runs regardless. Dropping it
here would still rank identically under cosine distance, which is scale
invariant, so no test that only checks ordering would catch the difference. The
raw vectors would simply stop being comparable with every row already stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:  # pragma: no cover - typing only
    import onnxruntime

# Paths inside the staged artifact, relative to EMBEDDING_MODEL_PATH.
_ONNX_MODEL = "onnx/model.onnx"
_TOKENIZER = "tokenizer.json"
_MODULES = "modules.json"
_POOLING_CONFIG = "1_Pooling/config.json"
_ST_CONFIG = "sentence_bert_config.json"

# Used only when the artifact omits sentence_bert_config.json. Matches the
# upstream all-MiniLM-L6-v2 value.
_DEFAULT_MAX_SEQ_LENGTH = 256

# BERT-family fallback when the tokenizer does not name a pad token.
_FALLBACK_PAD_TOKEN = "[PAD]"

_NORMALIZE_MODULE = "Normalize"
_MEAN_POOLING_KEY = "pooling_mode_mean_tokens"


class ArtifactError(RuntimeError):
    """The staged model artifact is missing, incomplete, or the wrong shape."""


class OnnxEmbedder:
    """In-process embedder backed by `onnxruntime` and a staged ONNX artifact."""

    def __init__(self, model_path: str, model_version: str, expected_dim: int) -> None:
        # Local imports: a deployment running another provider should not pay
        # the onnxruntime import, and need not have it installed.
        import onnxruntime
        from tokenizers import Tokenizer

        self.model_version = model_version
        self._root = Path(model_path)
        self._dim = expected_dim

        onnx_file = self._require(_ONNX_MODEL)
        tokenizer_file = self._require(_TOKENIZER)

        self._max_seq_length = self._read_max_seq_length()
        self._normalize = self._read_normalize_enabled()
        self._verify_pooling_and_dim(expected_dim)

        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self._tokenizer.enable_truncation(max_length=self._max_seq_length)
        pad_token = _FALLBACK_PAD_TOKEN
        pad_id = self._tokenizer.token_to_id(pad_token)
        if pad_id is None:
            pad_id, pad_token = 0, _FALLBACK_PAD_TOKEN
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)

        self._session = onnxruntime.InferenceSession(
            str(onnx_file),
            providers=["CPUExecutionProvider"],
        )
        self._input_names = [spec.name for spec in self._session.get_inputs()]
        self._output_name = self._pick_output_name(self._session)

    # -- artifact loading ----------------------------------------------------

    def _require(self, relative: str) -> Path:
        path = self._root / relative
        if not path.is_file():
            raise ArtifactError(
                f"embedding model artifact incomplete: {path} not found. "
                f"Stage it with `python scripts/fetch_embedding_model.py --out {self._root}`, "
                f"or point EMBEDDING_MODEL_PATH at a directory that already has it."
            )
        return path

    def _read_json(self, relative: str) -> Any:
        path = self._root / relative
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"embedding model artifact malformed: {path} is not valid JSON ({exc})") from exc

    def _read_max_seq_length(self) -> int:
        config = self._read_json(_ST_CONFIG)
        if isinstance(config, dict):
            raw = config.get("max_seq_length")
            if isinstance(raw, int) and raw > 0:
                return raw
        return _DEFAULT_MAX_SEQ_LENGTH

    def _read_normalize_enabled(self) -> bool:
        """True when the artifact declares a Normalize module.

        Read rather than assumed: an operator may substitute a different
        sentence-transformers export, and whether its vectors are unit-length is
        a property of that artifact, not of this class.
        """
        modules = self._read_json(_MODULES)
        if not isinstance(modules, list):
            # No manifest to consult. Every sentence-transformers model this
            # provider targets normalises, so normalising is the safer default:
            # it keeps vectors comparable with rows already stored.
            return True
        return any(isinstance(entry, dict) and _NORMALIZE_MODULE in str(entry.get("type", "")) for entry in modules)

    def _verify_pooling_and_dim(self, expected_dim: int) -> None:
        config = self._read_json(_POOLING_CONFIG)
        if not isinstance(config, dict):
            return
        if config.get(_MEAN_POOLING_KEY) is False:
            enabled = [key for key, value in config.items() if key.startswith("pooling_mode_") and value is True]
            raise ArtifactError(
                f"embedding model artifact uses unsupported pooling {enabled or ['<none>']}; "
                f"this provider implements mean pooling only"
            )
        artifact_dim = config.get("word_embedding_dimension")
        if isinstance(artifact_dim, int) and artifact_dim != expected_dim:
            raise ArtifactError(
                f"embedding dimension mismatch: artifact at {self._root} produces {artifact_dim}-d vectors "
                f"but EMBEDDING_DIM is {expected_dim}. Storing these would corrupt the index — "
                f"set EMBEDDING_DIM={artifact_dim} and migrate, or stage a {expected_dim}-d artifact."
            )

    @staticmethod
    def _pick_output_name(session: onnxruntime.InferenceSession) -> str:
        outputs = session.get_outputs()
        for spec in outputs:
            if spec.name == "last_hidden_state":
                return str(spec.name)
        if not outputs:
            raise ArtifactError("ONNX graph declares no outputs")
        return str(outputs[0].name)

    # -- inference -----------------------------------------------------------

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        """Embed a batch. Blocking and CPU-bound — callers offload to a thread."""
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        encodings = self._tokenizer.encode_batch(texts)
        attention_mask = np.array([enc.attention_mask for enc in encodings], dtype=np.int64)
        available: dict[str, npt.NDArray[np.int64]] = {
            "input_ids": np.array([enc.ids for enc in encodings], dtype=np.int64),
            "attention_mask": attention_mask,
            "token_type_ids": np.array([enc.type_ids for enc in encodings], dtype=np.int64),
        }
        try:
            feed = {name: available[name] for name in self._input_names}
        except KeyError as exc:
            raise ArtifactError(
                f"ONNX graph expects input {exc} which this provider does not supply; "
                f"expected some subset of {sorted(available)}"
            ) from exc

        token_embeddings = self._session.run([self._output_name], feed)[0]

        # Mean-pool over real tokens only: padding must not drag the mean toward
        # zero, so weight by the attention mask instead of dividing by width.
        mask = attention_mask.astype(np.float32)[:, :, None]
        summed = (token_embeddings * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = summed / counts

        if self._normalize:
            pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)

        vectors: npt.NDArray[np.float32] = pooled.astype(np.float32)
        if vectors.shape[1] != self._dim:
            raise ArtifactError(
                f"embedding dimension mismatch: model produced {vectors.shape[1]}-d vectors, expected {self._dim}"
            )
        return vectors


__all__ = ["ArtifactError", "OnnxEmbedder"]
