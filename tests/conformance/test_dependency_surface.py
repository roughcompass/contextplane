"""The base install must stay free of torch.

Not a style preference. Deployments this project targets are network-isolated:
the model ships as a layer in the image, and the image has to stay small enough
and light enough to run under the memory limits those environments impose.
torch is ~750 MB — more than the entire rest of the dependency tree — and pulls
the working set past what the shipped resource limits allow.

The regression is easy to cause by accident. Any new dependency that happens to
depend on sentence-transformers, transformers, or torch reintroduces it
transitively, and nothing else would notice until an image build doubled in size
or a pod started getting OOMKilled.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parent.parent.parent / "pyproject.toml"

# Matched against the distribution name at the start of each requirement string.
_FORBIDDEN_IN_BASE = ("torch", "sentence-transformers", "transformers", "nvidia-", "triton")


def _requirement_name(requirement: str) -> str:
    """Distribution name from a PEP 508 requirement string."""
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", " "):
        requirement = requirement.split(separator)[0]
    return requirement.strip().lower()


def _load() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_base_dependencies_exclude_torch():
    names = {_requirement_name(r) for r in _load()["project"]["dependencies"]}
    offenders = sorted(n for n in names if n.startswith(_FORBIDDEN_IN_BASE))
    assert not offenders, (
        f"{offenders} in [project.dependencies]. Heavy ML runtimes belong in the "
        f"optional `torch` extra — a base install has to stay deployable where the "
        f"model ships inside the image."
    )


def test_sentence_transformers_is_available_as_an_extra():
    """Removed from the base install, not from the project.

    The reference implementation still has to be installable: the parity test
    measures against it, and operators may prefer it over the ONNX path.
    """
    extras = _load()["project"]["optional-dependencies"]
    assert "torch" in extras
    names = {_requirement_name(r) for r in extras["torch"]}
    assert "sentence-transformers" in names


def test_onnx_runtime_is_a_base_dependency():
    """The default provider must work on a plain `pip install`.

    If onnxruntime were an extra, a default install would have no working
    embedder at all and the failure would land at startup.
    """
    names = {_requirement_name(r) for r in _load()["project"]["dependencies"]}
    assert "onnxruntime" in names
    assert "tokenizers" in names
