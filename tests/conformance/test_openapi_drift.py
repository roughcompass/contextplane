"""Committed openapi.json must match the spec the export script produces.

If a router model changes shape (new field, type change), this test fails.
The fix is to regenerate the committed spec:

    make openapi-export

The spec is generated in a subprocess rather than in-process, and that
matters. Routers register their routes at import time from
``CONTEXTPLANE_HTTP_METHODS_MODE``, and the test suite sets that variable to
``both`` so alias paths like ``/v1/capabilities/{id}:delete`` are
exercised elsewhere. Generating here in-process would therefore compare
the committed REST-only contract against a spec carrying ~30 extra alias
paths, and the test would fail for a reason that has nothing to do with
the router models it exists to guard. A subprocess gets a clean import
with the export script's own pinned mode — the same one that produced
the committed file.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - invokes this repo's own export script (sys.executable + fixed args below), no caller input
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_COMMITTED_SPEC = _PROJECT_ROOT / "openapi.json"
_EXPORT_SCRIPT = _PROJECT_ROOT / "scripts" / "export_openapi.py"


def test_committed_openapi_uses_public_product_name() -> None:
    spec = json.loads(_COMMITTED_SPEC.read_text())

    assert spec["info"]["title"] == "DE Context Plane for Agents"


def test_committed_openapi_matches_generated(tmp_path: Path) -> None:
    assert _COMMITTED_SPEC.exists(), f"{_COMMITTED_SPEC} is missing — regenerate with `make openapi-export`"

    generated_path = tmp_path / "openapi.json"
    completed = subprocess.run(
        [sys.executable, str(_EXPORT_SCRIPT), "--out", str(generated_path)],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"scripts/export_openapi.py failed ({completed.returncode}):\n" f"{completed.stdout}\n{completed.stderr}"
    )

    generated = generated_path.read_text()
    committed = _COMMITTED_SPEC.read_text()

    assert generated == committed, (
        "openapi.json drifted from the spec the export script produces.\n" "Regenerate with:\n" "  make openapi-export"
    )


def test_no_response_field_is_a_bare_score() -> None:
    """Three quantities in this system share the 0..1 scale and mean different
    things, so no field may be called just `score`.

    E15-T1 renamed the service dataclass for that reason and stopped there. The
    adapter then wrote `score=result.fused_rank_score`, undoing the rename one
    layer from the only place a reader sees it, and the acceptance that certified
    the rename grepped a single module while its goal sentence described the
    tree. This greps the contract, which is the artifact the claim is about --
    and the surface a UI author copies a name from.

    Scoped to `score` exactly. `fused_rank_score`, `salience`, `eval_score` and
    `confidence` all say which quantity they are, which is the whole ask.
    """
    spec = json.loads(_COMMITTED_SPEC.read_text(encoding="utf-8"))
    offenders = sorted(
        f"{name}.score"
        for name, schema in spec["components"]["schemas"].items()
        if isinstance(schema, dict) and "score" in (schema.get("properties") or {})
    )

    assert not offenders, (
        "a bare `score` reached the contract: "
        + ", ".join(offenders)
        + ". Name the quantity -- a reader cannot tell a fused rank from a salience from a "
        "confidence, and the wire is where the next author learns which names are acceptable."
    )
