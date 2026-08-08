"""Re-export the committed ``openapi.json``.

Runs offline — no DB I/O — so it works in CI / dev without a real
``DATABASE_URL``. The committed ``openapi.json`` is the contract surface
for downstream consumers; ``tests/conformance/test_openapi_drift.py``
fails the build on any PR that changes a router model without re-running
this script.

Usage::

    python scripts/export_openapi.py            # rewrite openapi.json
    python scripts/export_openapi.py --stdout   # print, leaving the file alone

To regenerate a typed Python client from the export::

    pip install openapi-python-client
    openapi-python-client generate --path openapi.json --output-path <dir> --overwrite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the repo root is importable when invoked as a subprocess from
# arbitrary cwd. Without this, `from registry.X import Y` raises
# ModuleNotFoundError.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The committed spec describes the default REST surface. Routers register
# their routes at import time from this variable, so it has to be pinned
# *before* the imports below rather than passed in as a setting: the
# POST-alias modes add ~30 extra paths, and inheriting an ambient value
# would make the committed contract depend on whoever's shell ran the
# export.
EXPORT_HTTP_METHODS_MODE = "rest"
# Not a Settings bypass -- this writes into the process environment for the
# import-time route registration in contextplane/api/middleware/http_methods.py
# to read; it never reads configuration itself, so there is nothing here for
# Settings to own.
os.environ["REGISTRY_HTTP_METHODS_MODE"] = EXPORT_HTTP_METHODS_MODE

from contextplane.config import Settings  # noqa: E402
from contextplane.main import create_app  # noqa: E402

_OUT = Path(__file__).parent.parent / "openapi.json"


def render() -> str:
    """Return the serialised spec exactly as it is committed."""
    # OpenAPI rendering runs offline (no DB I/O), so this is never a real
    # connection -- a literal placeholder, not an env read: no code path here
    # ever opens it, so there is nothing for DATABASE_URL to override.
    placeholder_db_url = "postgresql+asyncpg://postgres:password@localhost:5432/cap_test"
    settings = Settings(
        database_url=placeholder_db_url,
        pgbouncer_url=placeholder_db_url,
        scheduler_jobstore_url=placeholder_db_url,
        # The schema does not depend on which embedder is configured, and
        # this script has to run anywhere — a CI lint job, a laptop with no
        # model staged. The real providers load a model artifact from disk
        # at construction, so asking for one here would make regenerating
        # the spec require infrastructure it does not use.
        embedding_provider="stub",
    )
    return json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-export the committed openapi.json.")
    parser.add_argument(
        "--out",
        type=Path,
        default=_OUT,
        help=(
            "where to write the spec (default: the committed openapi.json). "
            "The drift test points this at a temp file. Not stdout — importing "
            "the app configures structured logging onto stdout, which would "
            "interleave log lines with the JSON."
        ),
    )
    args = parser.parse_args()

    args.out.write_text(render())
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
