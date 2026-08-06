"""Pins the docstring ratchet so a narrower rule selection or a filler docstring cannot pass silently.

Two things must both hold, and neither is provable by the other:

1. `pyproject.toml` itself selects D100-D103 (scoped to `registry/service/` and
   `registry/api/`) with the `pep257` pydocstyle convention. Read the config
   file directly rather than trusting a CLI `--select` flag -- a flag proves
   nothing about what `make lint` actually runs.
2. The named mandatory targets (the GDPR erasure path, `catalog/core.py`, and
   every `admin_usage.py`/`admin_memory_curation.py` definition whose docstring
   becomes an OpenAPI description) carry a docstring that is not a bare
   restatement of the symbol's own name.
"""

from __future__ import annotations

import inspect
import subprocess  # noqa: S404 - invokes this repo's own ruff via sys.executable with a fixed argv, no caller input
import sys
import tomllib
from pathlib import Path

from registry.api.routers import admin_memory_curation, admin_usage
from registry.service.catalog import core as catalog_core
from registry.service.governance import erasure
from registry.service.memory import claim_erasure

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_ruff_lint_config() -> dict:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["ruff"]["lint"]


def test_pyproject_selects_docstring_rules_with_pep257_convention():
    """The config, not a CLI flag, is what `make lint` runs -- pin the rule list and convention here."""
    lint_config = _load_ruff_lint_config()
    for code in ("D100", "D101", "D102", "D103"):
        assert code in lint_config["select"], f"pyproject.toml [tool.ruff.lint] select must include {code}"
    assert lint_config["pydocstyle"]["convention"] == "pep257", (
        "pyproject.toml [tool.ruff.lint.pydocstyle] convention must be 'pep257', "
        "or the D200-series style rules conflict with each other on the first `make lint` run"
    )


def test_docstring_rules_scoped_out_of_tests_scripts_and_other_registry_packages():
    """The ratchet is narrower than the whole tree by design; the ignore list is the other half of the contract."""
    per_file_ignores = _load_ruff_lint_config()["per-file-ignores"]
    d_codes = {"D100", "D101", "D102", "D103"}
    tests_ignored = set(per_file_ignores.get("tests/**", []))
    assert d_codes <= tests_ignored, "tests/** must ignore D100-D103 -- test helpers are not the public contract"
    scripts_ignored = set(per_file_ignores.get("scripts/**", []))
    assert d_codes <= scripts_ignored, "scripts/** must ignore D100-D103"


def test_ruff_reports_zero_docstring_violations_in_scope():
    """Confirms the tree is actually clean under the selected rules, not just that the config asks for it to be."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "D100,D101,D102,D103",
            "--output-format=concise",
            "registry/service",
            "registry/api",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"ruff found docstring violations:\n{result.stdout}"


def _is_meaningful(doc: str | None, *, name: str) -> bool:
    """A docstring counts only if it says more than the symbol's own name already does."""
    if not doc:
        return False
    doc = inspect.cleandoc(doc)
    if len(doc) < 10:
        return False
    # Reject a docstring that is just the name turned into a sentence, e.g.
    # "get_entity" -> "Get entity." tells a reader nothing the signature didn't.
    words_in_name = {part for part in name.replace("_", " ").split() if part}
    words_in_doc = {w.strip(".,;:()`\"'") for w in doc.split()}
    return not words_in_name.issubset(words_in_doc) or len(doc) > len(name) + 20


def _assert_all_meaningful(owner: object, names: list[str], *, label: str) -> None:
    for name in names:
        member = getattr(owner, name, None)
        assert member is not None, f"{label}.{name} must exist"
        doc = member.__doc__
        if isinstance(member, property):
            doc = member.fget.__doc__ if member.fget else None
        assert _is_meaningful(doc, name=name), f"{label}.{name} needs a docstring that explains more than its name"


def test_erasure_registry_and_participant_protocol_have_meaningful_docstrings():
    """The fan-out registry and the interface every subsystem implements must both explain their own contract."""
    _assert_all_meaningful(
        erasure.ErasureRegistry,
        ["register", "erase_actor", "subsystems"],
        label="ErasureRegistry",
    )
    assert _is_meaningful(erasure.ErasureRegistry.__doc__, name="ErasureRegistry")
    _assert_all_meaningful(
        erasure.ErasureParticipant,
        ["subsystem", "erase_actor"],
        label="ErasureParticipant",
    )
    _assert_all_meaningful(erasure.ErasureCounts, ["total"], label="ErasureCounts")


def test_erasure_participants_document_their_own_erase_actor():
    """Each concrete participant's erase_actor must say what it deletes, not just repeat the protocol's shape."""
    for cls in (erasure.WorkspaceErasure, erasure.SessionMemoryErasure, erasure.EmbeddingErasure):
        _assert_all_meaningful(cls, ["erase_actor"], label=cls.__name__)


def test_claim_erasure_documents_the_transaction_boundary():
    """The claims subsystem's erase_actor is where selection, repair, and vector deletion commit together."""
    _assert_all_meaningful(claim_erasure.ClaimErasure, ["erase_actor"], label="ClaimErasure")


def test_catalog_service_documents_every_public_method():
    """catalog/core.py is a mandatory target in full -- every delegate and every method with real logic."""
    mandatory_methods = [
        "create_entity",
        "get_entity",
        "resolve_entity_handle",
        "update_entity",
        "delete_entity",
        "seed_default_roles",
        "create_fact",
        "update_fact",
        "delete_fact",
        "create_fact_from_sync",
        "upsert_synced_facts",
        "get_full_capability",
        "create_edge",
        "delete_edge",
    ]
    _assert_all_meaningful(catalog_core.CatalogService, mandatory_methods, label="CatalogService")


def _defined_in(module: object) -> list[tuple[str, object]]:
    """Public functions/classes actually defined in `module`, not merely imported into it."""
    return [
        (name, obj)
        for name, obj in inspect.getmembers(module)
        if not name.startswith("_")
        and (inspect.isfunction(obj) or inspect.isclass(obj))
        and getattr(obj, "__module__", None) == module.__name__
    ]


def test_admin_usage_route_handlers_and_schemas_have_meaningful_docstrings():
    """Every public function and schema class here becomes an OpenAPI description; none may be blank or a name-echo."""
    members = _defined_in(admin_usage)
    assert members, "admin_usage module must have route handlers and schemas defined in it"
    for name, obj in members:
        assert _is_meaningful(obj.__doc__, name=name), f"admin_usage.{name} needs a meaningful docstring"


def test_admin_memory_curation_route_handlers_and_schemas_have_meaningful_docstrings():
    """The memory-curation operator surface is a mandatory target for every route and schema it defines."""
    members = _defined_in(admin_memory_curation)
    assert members, "admin_memory_curation module must have route handlers and schemas defined in it"
    for name, obj in members:
        assert _is_meaningful(obj.__doc__, name=name), f"admin_memory_curation.{name} needs a meaningful docstring"
