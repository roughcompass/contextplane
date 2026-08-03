"""Lint gate: every environment variable is documented in both places, or neither.

Two files describe the deployment surface, for two audiences. `.env.example` is
what an operator copies; `docs/05-reference/03-configuration.md` is what they read
to understand what they copied. A variable in one and not the other is a variable
somebody will either set without knowing what it does, or look for and not find.

This drift is invisible without a gate, which is the whole reason for one. The
extraction variables shipped in `.env.example` and were missing from the reference
for a full release cycle; nothing failed, nothing warned, and it was found only
because somebody happened to grep.

**Both directions are checked.** A documented variable the example does not offer
is the more embarrassing half: the reference promises a knob that no operator
copying the example would know exists.

**Not a check that the code reads them.** A separate gate owns the
"everything goes through Settings" rule. This one is only about the two documents
agreeing, because they are the pair that drifts.

Run locally:
    python registry/scripts/check_env_documented.py
    python registry/scripts/check_env_documented.py --explain
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_EXAMPLE = _REPO_ROOT / "registry" / ".env.example"
_REFERENCE = _REPO_ROOT / "registry" / "docs" / "05-reference" / "03-configuration.md"

# `NAME=` at the start of a line, or commented-out as `# NAME=`. The commented
# form is how the example offers a variable without setting it -- an API key, say
# -- and those still need documenting.
_ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)

# In the reference, variables appear as `BACKTICK NAME BACKTICK` in a table cell.
# Matching backticked all-caps tokens rather than parsing the tables keeps this
# robust to a variable documented in prose instead of a row.
_BACKTICKED = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")

# Names that look like variables in one file but are not the deployment surface.
#
# `DATABASE_URL` and friends are in the required-variables table and the example,
# so they are not here -- these are only the false positives.
_NOT_VARIABLES: frozenset[str] = frozenset(
    {
        # Log-level names, documented as values of LOG_LEVEL.
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
        # Entitlement-grammar values, documented as example environments and
        # discriminators rather than as variables an operator sets.
        "DEV",
        "PRD",
        "NPD",
        "REGISTRY",
        "GRAPHREGISTRY",
        "PASS",
        "FAIL",
        "TRUE",
        "FALSE",
        "NULL",
        # SQL and HTTP tokens that appear backticked in prose.
        "GET",
        "POST",
        "PATCH",
        "PUT",
        "DELETE",
        "SELECT",
        "INSERT",
        "UPDATE",
        "JSON",
        "JSONB",
        "UUID",
        "TEXT",
        "BOOLEAN",
        "INTEGER",
        "NUMERIC",
        "TIMESTAMPTZ",
        "CHECK",
        "UNIQUE",
        "CASCADE",
        "SKIP",
        "LOCKED",
        # Protocol and format names.
        "OIDC",
        "JWT",
        "RS256",
        "MCP",
        "OTLP",
        "REST",
        "HTTP",
        "HTTPS",
        "URL",
        "URI",
        "API",
        "SLO",
        "PII",
        "RBAC",
        "ARC",
        "DR",
        "TTL",
        "LRU",
        "FTS",
        "HNSW",
    }
)


def _names(text: str, pattern: re.Pattern[str]) -> set[str]:
    return {m for m in pattern.findall(text) if m not in _NOT_VARIABLES}


def compare() -> tuple[set[str], set[str]]:
    """Return (in the example but undocumented, documented but not offered)."""
    example = _names(_ENV_EXAMPLE.read_text(encoding="utf-8"), _ASSIGNMENT)
    reference = _names(_REFERENCE.read_text(encoding="utf-8"), _BACKTICKED)
    return example - reference, reference - example


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify .env.example and the configuration reference agree.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Describe what the gate checks and why, then exit 0.",
    )
    args = parser.parse_args(argv)

    if args.explain:
        print(__doc__)
        return 0

    for path in (_ENV_EXAMPLE, _REFERENCE):
        if not path.is_file():
            # Printed whole rather than relative to the repo root: a path that is
            # missing may also be outside the root, and relativizing it would
            # raise inside the error handler -- turning a clear "file moved"
            # message into a traceback.
            print(f"missing: {path}", file=sys.stderr)
            return 1

    undocumented, unoffered = compare()

    if not undocumented and not unoffered:
        print("env-documented gate: .env.example and the configuration reference agree")
        return 0

    if undocumented:
        print(
            f"{len(undocumented)} variable(s) in .env.example are absent from "
            f"docs/05-reference/03-configuration.md:",
            file=sys.stderr,
        )
        for name in sorted(undocumented):
            print(f"  {name}", file=sys.stderr)
        print(
            "\nAn operator copying the example would set these without knowing what "
            "they do. Add a table row under the matching section.",
            file=sys.stderr,
        )

    if unoffered:
        print(
            f"\n{len(unoffered)} variable(s) are documented but absent from "
            f".env.example:",
            file=sys.stderr,
        )
        for name in sorted(unoffered):
            print(f"  {name}", file=sys.stderr)
        print(
            "\nThe reference promises a knob no operator copying the example would "
            "know exists. Add it to .env.example, or add it to _NOT_VARIABLES if it "
            "is not really a variable.",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
