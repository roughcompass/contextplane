"""The refusals, and the properties of the statements behind them.

E11-T3. Everything here runs before a transaction opens, which is the design:
the log is of reads, not of attempts. The reads themselves are pinned in
`tests/integration/test_audit_drilldown.py`, where there is a database to
observe the record in.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.exceptions import ValidationError
from contextplane.service.memory import audit_drilldown
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)
_START = _NOW - datetime.timedelta(days=30)
_WHY = "Reviewing a complaint that this actor's claims were not being adjudicated."
_CTX = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["auditor"])


def _service() -> audit_drilldown.AuditDrilldownService:
    """No session factory. Every assertion below refuses before one is used, and
    passing `None` is how that is held to: a refusal that reached the database
    would raise an `AttributeError` here rather than pass quietly."""
    return audit_drilldown.AuditDrilldownService(None, clock=None)  # type: ignore[arg-type]


async def _read(**overrides: object) -> None:
    kwargs: dict[str, object] = {
        "subject_actor_id": uuid.uuid4(),
        "metric": audit_drilldown.METRIC_CLAIMS_AUTHORED,
        "window_start": _START,
        "window_end": _NOW,
        "justification": _WHY,
    }
    kwargs.update(overrides)
    await _service().read_actor_metric(_CTX, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_unknown_metric_is_refused() -> None:
    """The set is closed. An open one would make the drill-down a query language
    over one actor, which is the surveillance surface with extra steps."""
    with pytest.raises(ValidationError, match="unknown drill-down metric"):
        await _read(metric="everything_they_have_ever_done")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["audit", "checking", "   " + "x" * 19])
async def test_a_reason_that_is_not_a_sentence_is_refused(reason: str) -> None:
    """Twenty characters will not make a bad reason good, but it stops "audit"
    and "checking" from being reasons at all. Whitespace is stripped first, so
    padding is not a way past the floor."""
    with pytest.raises(ValidationError, match="willing to have read back"):
        await _read(justification=reason)


@pytest.mark.asyncio
async def test_a_reason_longer_than_the_column_is_refused_here_first() -> None:
    """So a caller gets a sentence naming the field rather than a constraint
    violation surfacing as a 500."""
    with pytest.raises(ValidationError, match="willing to have read back"):
        await _read(justification="x" * (audit_drilldown.MAX_JUSTIFICATION + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("end", [_START, _START - datetime.timedelta(days=1)])
async def test_a_window_that_ends_before_it_starts_is_refused(end: datetime.datetime) -> None:
    with pytest.raises(ValidationError, match="window_end must be after window_start"):
        await _read(window_end=end)


def test_the_service_floor_matches_the_database_check() -> None:
    """Two copies, held together here.

    The floor is in the CHECK as well as the service so a second writer cannot
    decide otherwise; that only holds while the numbers agree, and a service
    floor *below* the constraint would turn a refusal into a 500.
    """
    migration = (
        audit_drilldown.__file__.rsplit("/contextplane/", 1)[0]
        + "/contextplane/storage/migrations/versions/0081_audit_justified_reads.py"
    )
    with open(migration, encoding="utf-8") as handle:
        source = handle.read()
    assert (
        f"BETWEEN {audit_drilldown.MIN_JUSTIFICATION} AND {audit_drilldown.MAX_JUSTIFICATION}" in source
    ), "the service's justification bounds no longer match ck_justified_read_reason"


def test_every_served_metric_has_a_statement_and_every_statement_is_served() -> None:
    """A metric advertised but not computed is a broken endpoint; one computed
    but unreachable is dead code the next reader trusts."""
    assert set(audit_drilldown.DRILLDOWN_METRICS) == set(audit_drilldown._COUNTS)


def test_every_statement_scopes_to_one_tenant_and_one_actor() -> None:
    """The whole point of the surface is that it is narrow. A statement that
    could express a wider read is one a later edit widens by deleting a clause."""
    for metric, statement in audit_drilldown._COUNTS.items():
        assert ":tenant" in statement, f"{metric} does not scope to a tenant"
        assert ":subject" in statement, f"{metric} does not scope to one actor"
        assert ":start" in statement and ":end" in statement, f"{metric} is unbounded in time"
        assert "GROUP BY" not in statement, (
            f"{metric} groups. Every drill-down answers one question about one actor; "
            "a grouped statement is a ranking waiting for a caller to page through it."
        )
        assert statement.strip().upper().startswith("SELECT COUNT("), (
            f"{metric} returns something other than a count. A row-returning drill-down "
            "is a per-actor export, which is not what the justification was written for."
        )
