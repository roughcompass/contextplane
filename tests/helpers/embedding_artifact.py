"""Locate a staged embedding artifact for tests that need real inference.

Most tests use `StubEmbedder` and never touch a model. The few that exercise the
ONNX provider need the real ~90 MB artifact, which is not in the repo and is not
staged on a plain checkout — so they skip rather than fail when it is absent.

Two places are checked: `EMBEDDING_MODEL_PATH` if set (the container image sets
it), then the conventional local dev location. Stage it with:

    python scripts/fetch_embedding_model.py --out .devstack/models/all-MiniLM-L6-v2
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEV_ARTIFACT = _REPO_ROOT / ".devstack" / "models" / "all-MiniLM-L6-v2"

#: The ONNX graph — the one file whose presence means the artifact is real
#: rather than a directory holding only the small config files.
_SENTINEL = Path("onnx") / "model.onnx"


def find_artifact() -> Path | None:
    """Return a staged artifact directory, or None when none is available."""
    candidates = []
    configured = os.environ.get("EMBEDDING_MODEL_PATH")
    if configured:
        candidates.append(Path(configured))
    candidates.append(_DEV_ARTIFACT)

    for candidate in candidates:
        if (candidate / _SENTINEL).is_file():
            return candidate
    return None


def require_artifact() -> Path:
    """Return a staged artifact directory, or skip the test."""
    artifact = find_artifact()
    if artifact is None:
        pytest.skip(
            "no embedding artifact staged; run "
            "`python scripts/fetch_embedding_model.py --out .devstack/models/all-MiniLM-L6-v2`"
        )
    return artifact
