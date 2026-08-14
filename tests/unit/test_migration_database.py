"""The two behaviours of the migration-database lifecycle that fail silently.

Both are about destruction. A reversibility test is handed a database and told
to downgrade it, so the lifecycle's job is to guarantee that database is
disposable and that it is gone afterwards. Neither guarantee announces itself
when it breaks: pointing the lifecycle at a database another consumer owns
looks exactly like correct use from the call site, and a clone that is never
dropped is invisible until a later run inherits it.

So they are proven here, against a fake executor, where the adversarial inputs
can actually be supplied — a real server cannot be asked to hand over a live
worker database just to watch the refusal work.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - the helper drives alembic's CLI; these tests assert what it hands that subprocess

import pytest

from tests.helpers.migration_database import (
    AlembicFailed,
    MigrationDatabases,
    WorkerDatabaseRefused,
    assert_alembic_ok,
    database_of,
    plain_url,
    reject_protected_database,
    url_for,
)

BASE_URL = "postgresql+asyncpg://postgres:password@localhost:5432/cp_session"
TEMPLATE = "cp_tmpl_abc123"


class _Executor:
    """Records statements and can be told to fail on a chosen one."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.statements: list[str] = []
        self._fail_on = fail_on

    def __call__(self, sql: str) -> list[tuple[object, ...]]:
        self.statements.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            raise RuntimeError(f"executor refused: {sql}")
        return []


def _runner(returncode: int = 0) -> object:
    def run(args: tuple[str, ...], database_url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=returncode, stdout="", stderr="boom")

    return run


def _databases(executor: _Executor, **kwargs: object) -> MigrationDatabases:
    return MigrationDatabases(
        execute=executor,
        base_url=BASE_URL,
        template=TEMPLATE,
        run_id="fixedrun",
        runner=_runner(),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


# --- a database another consumer owns is refused -------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "cp_worker_abc12345_gw0",
        "cp_scenario_abc12345_embedding",
        "cp_run_abc12345_main",
    ],
)
def test_a_database_another_consumer_owns_is_refused(name: str) -> None:
    """Refused by kind, so a consumer nobody has invented yet is still caught."""
    with pytest.raises(WorkerDatabaseRefused, match="owned by another consumer"):
        reject_protected_database(name)


def test_an_empty_database_name_is_refused() -> None:
    """A missing name would otherwise reach `DROP DATABASE IF EXISTS ""`."""
    with pytest.raises(WorkerDatabaseRefused):
        reject_protected_database("")


def test_this_modules_own_scratch_names_are_accepted() -> None:
    """The refusal has to admit the thing it exists to create.

    Asserted as an accepted-set rather than as two bare calls: a refusal broad
    enough to reject its own scratch databases would fail every reversibility
    node at once, and a test that only proved "does not raise" would read as
    passing if the function stopped being called at all.
    """
    candidates = ("cp_migr_abc12345_signal", TEMPLATE)
    accepted = []
    for name in candidates:
        try:
            reject_protected_database(name)
        except WorkerDatabaseRefused:
            continue
        accepted.append(name)
    assert accepted == list(candidates)


def test_dropping_a_protected_database_is_refused_before_any_statement_runs() -> None:
    """The refusal precedes the terminate, not only the drop.

    A terminate against a live worker database is already damage, even if the
    drop that follows it is refused.
    """
    executor = _Executor()
    databases = _databases(executor)
    with pytest.raises(WorkerDatabaseRefused):
        databases.drop("cp_worker_abc12345_gw0")
    assert executor.statements == []


def test_cloning_from_a_protected_template_is_refused() -> None:
    """A template is copied wholesale, so the wrong one is a silent substitution."""
    executor = _Executor()
    databases = MigrationDatabases(
        execute=executor,
        base_url=BASE_URL,
        template="cp_worker_abc12345_gw0",
        run_id="fixedrun",
        runner=_runner(),  # type: ignore[arg-type]
    )
    with pytest.raises(WorkerDatabaseRefused):
        databases.clone("signal")
    assert executor.statements == []


# --- cleanup ------------------------------------------------------------------


def test_a_clone_is_created_from_the_template_and_recorded() -> None:
    executor = _Executor()
    databases = _databases(executor)
    database = databases.clone("signal reference binding")

    assert database.name == "cp_migr_fixedrun_signal_reference_binding"
    assert databases.created == [database.name]
    assert executor.statements == [f'CREATE DATABASE "{database.name}" TEMPLATE "{TEMPLATE}"']
    # The clone's URL names the clone, not the session database it came from.
    assert database_of(database.url) == database.name
    assert database_of(BASE_URL) == "cp_session"


def test_cleanup_drops_every_clone_and_empties_the_ledger() -> None:
    executor = _Executor()
    databases = _databases(executor)
    first = databases.clone("first")
    second = databases.clone("second")

    assert databases.cleanup() == []
    assert databases.created == []
    dropped = [sql for sql in executor.statements if sql.startswith("DROP DATABASE")]
    assert dropped == [
        f'DROP DATABASE IF EXISTS "{first.name}"',
        f'DROP DATABASE IF EXISTS "{second.name}"',
    ]


def test_a_drop_terminates_connections_before_dropping() -> None:
    """Ordering, not merely presence: a drop with a backend attached fails."""
    executor = _Executor()
    databases = _databases(executor)
    database = databases.clone("ordering")
    executor.statements.clear()

    databases.drop(database.name)

    assert len(executor.statements) == 2
    assert executor.statements[0].startswith("SELECT pg_terminate_backend")
    assert database.name in executor.statements[0]
    assert executor.statements[1].startswith("DROP DATABASE IF EXISTS")


def test_cleanup_reports_a_failure_instead_of_stopping_at_it() -> None:
    """One undroppable database must not strand the rest.

    A cleanup that raises on the first failure leaves every later clone behind,
    and the next run inherits them — which is the failure this collects rather
    than raises to avoid.
    """
    executor = _Executor(fail_on='DROP DATABASE IF EXISTS "cp_migr_fixedrun_second"')
    databases = _databases(executor)
    databases.clone("first")
    databases.clone("second")
    databases.clone("third")

    failures = databases.cleanup()

    assert len(failures) == 1
    assert "cp_migr_fixedrun_second" in failures[0]
    # The ledger is emptied regardless, so a second cleanup does not retry
    # forever, and the first and third were still dropped.
    assert databases.created == []
    dropped = [sql for sql in executor.statements if sql.startswith("DROP DATABASE")]
    assert any("_first" in sql for sql in dropped)
    assert any("_third" in sql for sql in dropped)


def test_cleanup_is_safe_to_call_twice() -> None:
    executor = _Executor()
    databases = _databases(executor)
    databases.clone("once")
    assert databases.cleanup() == []
    before = len(executor.statements)
    assert databases.cleanup() == []
    assert len(executor.statements) == before


def test_a_clone_is_dropped_even_when_the_test_body_raises() -> None:
    executor = _Executor()
    databases = _databases(executor)

    with pytest.raises(ValueError, match="body failed"):
        with databases.head_clone("raising") as database:
            name = database.name
            raise ValueError("body failed")

    assert databases.created == []
    assert f'DROP DATABASE IF EXISTS "{name}"' in executor.statements


# --- the subprocess contract --------------------------------------------------


def test_a_failed_alembic_run_reports_its_own_stderr() -> None:
    """The reason a migration failed is in stderr, so the assertion carries it."""
    completed = subprocess.CompletedProcess(args=["alembic"], returncode=1, stdout="", stderr="constraint blew up")
    with pytest.raises(AlembicFailed, match="constraint blew up"):
        assert_alembic_ok(completed, "downgrade")


def test_a_successful_alembic_run_passes_silently() -> None:
    assert_alembic_ok(subprocess.CompletedProcess(args=["alembic"], returncode=0, stdout="", stderr=""), "upgrade")


def test_the_subprocess_receives_the_clone_url_not_the_session_url() -> None:
    """Pointing Alembic at the session database is the mistake with real cost."""
    seen: list[str] = []

    def run(args: tuple[str, ...], database_url: str) -> subprocess.CompletedProcess[str]:
        seen.append(database_url)
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    databases = MigrationDatabases(
        execute=_Executor(),
        base_url=BASE_URL,
        template=TEMPLATE,
        run_id="fixedrun",
        runner=run,  # type: ignore[arg-type]
    )
    database = databases.clone("target")
    database.downgrade("0033_receipt_evidence")

    assert seen == [database.url]
    assert database_of(seen[0]) == database.name


def test_a_driverless_url_is_what_a_subprocess_environment_gets() -> None:
    """Alembic's own configuration resolves the driver; the URL must not pin one."""
    assert plain_url(url_for(BASE_URL, "cp_migr_x")) == ("postgresql://postgres:password@localhost:5432/cp_migr_x")


def test_a_url_query_string_survives_redirection() -> None:
    """A provider that appends options must not lose them when the database changes."""
    assert url_for("postgresql://h/db?sslmode=require", "other") == "postgresql://h/other?sslmode=require"
