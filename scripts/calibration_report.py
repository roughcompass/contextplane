"""Report whether confidence scores are fit to serve, and exit non-zero if not.

The accuracy requirement says a score failing its bound is not fit to serve and the
failure is reported. There is nobody to report it to yet -- nothing is deployed and
there are no users -- so inventing an alerting integration would be building for a
consumer that does not exist. A command with an exit code is the honest surface: a
person runs it, or a pipeline does, and either way the answer is unambiguous.

Two other surfaces exist alongside this and are not duplicated here: the gauges on
the metrics endpoint, which make the state visible on a dashboard, and the refusal to
activate a failing fit, which is enforcement rather than reporting.

Exits 0 when every configured strategy either has a passing mapping or is honestly
uncalibrated. Exits 1 when a fit exists and misses its bound, because that is the
one state where scores are being served under a version string that reads as
calibrated while not being.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.service.calibration import (
    MAX_CALIBRATION_ERROR,
    MIN_ADJUDICATED_FOR_MAPPING,
    STATUS_ACTIVE,
    STATUS_FAILED,
)

_EXIT_OK = 0
_EXIT_FAILING = 1
_EXIT_NO_DATABASE = 2


async def _gather(database_url: str) -> dict[str, object]:
    engine = create_async_engine(database_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            mappings = [
                dict(row._mapping)
                for row in (
                    await session.execute(
                        text(
                            "SELECT provider_id, model_id, strategy_id, version, status, "
                            "       n_adjudicated, measured_error, fitted_at "
                            "FROM lmm_calibration_mapping "
                            "ORDER BY fitted_at DESC"
                        )
                    )
                ).all()
            ]
            judged = (
                await session.execute(
                    text("SELECT count(*) FROM lmm_claim_adjudication " "WHERE verdict IN ('correct', 'incorrect')")
                )
            ).scalar_one()
            scored = (
                await session.execute(
                    text(
                        "SELECT calibration_version, count(*) AS n FROM lmm_claims "
                        "WHERE confidence IS NOT NULL "
                        "GROUP BY calibration_version"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    return {
        "mappings": mappings,
        "judged_outcomes": int(judged),
        "claims_by_calibration": {str(r.calibration_version): int(r.n) for r in scored},
    }


def _render(state: dict[str, object]) -> tuple[str, int]:
    mappings = state["mappings"]
    assert isinstance(mappings, list)
    judged = state["judged_outcomes"]
    by_version = state["claims_by_calibration"]
    assert isinstance(by_version, dict)

    lines: list[str] = ["Confidence calibration", "=" * 22, ""]

    active = [m for m in mappings if m["status"] == STATUS_ACTIVE]
    failed = [m for m in mappings if m["status"] == STATUS_FAILED]

    if not mappings:
        lines += [
            "No mapping has been fitted, so no provider self-report contributes to any",
            "score. Confidence is derived from authority, corroboration, disagreement and",
            "age alone, and every claim records `uncalibrated` rather than a version.",
            "",
            "This is a state, not a fault. It becomes fixable once there are judged",
            f"outcomes: {MIN_ADJUDICATED_FOR_MAPPING} are needed to fit a mapping that can be",
            f"checked against the {MAX_CALIBRATION_ERROR:.0%} accuracy bound at all.",
            "",
            f"Judged outcomes recorded so far: {judged}",
        ]
    else:
        lines.append("Fitted mappings:")
        for mapping in mappings:
            mark = {STATUS_ACTIVE: "active", STATUS_FAILED: "FAILING"}.get(
                str(mapping["status"]), str(mapping["status"])
            )
            lines.append(
                f"  [{mark:>10}] {mapping['provider_id']}/{mapping['model_id']}"
                f"/{mapping['strategy_id']}"
                f"  error={float(mapping['measured_error']):.3f}"
                f"  n={mapping['n_adjudicated']}"
            )
        lines += ["", f"Judged outcomes recorded: {judged}"]

    if by_version:
        lines += ["", "Claims by the mapping that scored them:"]
        for version, count in sorted(by_version.items()):
            lines.append(f"  {count:>8}  {version}")

    if failed:
        lines += [
            "",
            f"{len(failed)} fit(s) missed the {MAX_CALIBRATION_ERROR:.0%} bound and are not",
            "serving. A mapping worse than the bound is worse than none, because it carries",
            "a version string that reads as calibrated -- so it is stored, never selected,",
            "and reported here. Refit against more judged outcomes, or review whether the",
            "provider's self-reports carry any signal at all.",
        ]
        return "\n".join(lines), _EXIT_FAILING

    if active:
        lines += ["", "Every fitted mapping is within tolerance."]
    return "\n".join(lines), _EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report calibration state; exit non-zero if a fit misses its bound.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw state as JSON instead of a report.",
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is not set. This reads calibration state from the database, so "
            "there is nothing to report without one.",
            file=sys.stderr,
        )
        return _EXIT_NO_DATABASE

    state = asyncio.run(_gather(database_url))

    if args.json:
        print(json.dumps(state, indent=2, default=str))
        # Still non-zero on a failing fit, so a pipeline reading JSON does not have
        # to parse it to learn something is wrong.
        return _render(state)[1]

    report, code = _render(state)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
