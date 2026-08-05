"""The state-access gate is the enforcement, so it needs its own tests.

Before `Services` existed, "does this router read a service that might not
be there" was answered by scanning for `getattr(app.state, ..., None)` by
eye. This gate turns that into something that runs on every commit. Each
test below plants one synthetic violation (or one synthetic clearance) in a
scratch tree and asserts the walker notices — the mutation is the point: if
the walker matched nothing, every test here would pass for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_state_access import (
    ALLOWLIST,
    Exemption,
    check_file,
    main,
)


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the gate at a scratch tree so tests never depend on real sources."""
    monkeypatch.setattr("scripts.check_state_access._REPO_ROOT", tmp_path)
    return tmp_path


def test_the_real_tree_passes() -> None:
    """The gate's own subject. Fails the moment a new bypass lands unnoticed."""
    assert main(["--paths", "registry"]) == 0


def test_every_exemption_carries_a_reason_and_functions() -> None:
    """An exemption with no reason, or no named function, is a bypass wearing
    the gate's clothes -- see `Exemption.functions`'s own docstring on why an
    empty set is not allowed to mean 'the whole file'."""
    for exemption in ALLOWLIST:
        assert exemption.reason.strip(), f"{exemption.path} has no stated reason"
        assert exemption.functions, f"{exemption.path} names no functions"
        assert exemption.rule in {"getattr", "assign"}, f"{exemption.path} has an unknown rule {exemption.rule!r}"


# ---------------------------------------------------------------------------
# Rule (a): getattr(...) reads of app/request state
# ---------------------------------------------------------------------------


def test_getattr_on_request_app_state_is_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/rogue.py",
        (
            "from fastapi import Request\n\n\n"
            "def _service(request: Request):\n"
            '    return getattr(request.app.state, "catalog", None)\n'
        ),
    )
    violations = check_file(target, rel="registry/api/routers/rogue.py")
    assert len(violations) == 1
    assert violations[0].rule == "getattr"
    assert violations[0].function == "_service"
    assert violations[0].line == 5


def test_getattr_on_bare_app_state_is_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/mcp/rogue.py",
        ("def _settings(app):\n" '    return getattr(app.state, "settings", None)\n'),
    )
    violations = check_file(target, rel="registry/api/mcp/rogue.py")
    assert len(violations) == 1
    assert violations[0].function == "_settings"


def test_nested_getattr_reaching_for_state_is_flagged(repo_root: Path) -> None:
    """`getattr(getattr(app, "state", None), "x", None)` -- the pattern this
    module's own `_services` helper is allowlisted for -- must be caught
    when it shows up somewhere that is *not* allowlisted."""
    target = _write(
        repo_root,
        "registry/api/mcp/rogue.py",
        ("def _thing(app):\n" '    return getattr(getattr(app, "state", None), "arc_preflight", None)\n'),
    )
    violations = check_file(target, rel="registry/api/mcp/rogue.py")
    # Both the inner `getattr(app, "state", None)` and the outer getattr are
    # matches -- the inner one reaches for state via getattr instead of the
    # plain attribute, and the outer one's object is itself state-shaped.
    assert len(violations) == 2
    assert all(v.function == "_thing" for v in violations)


def test_getattr_on_the_container_itself_is_not_flagged(repo_root: Path) -> None:
    """The whole point of the container: once you have it, walking it by
    field name is not the anti-pattern -- only the read *into* raw
    `app.state` is."""
    target = _write(
        repo_root,
        "registry/api/mcp/honest.py",
        (
            "def _arc_state(app, name):\n"
            "    services = app.state.services\n"
            "    return getattr(services, name, None)\n"
        ),
    )
    assert check_file(target, rel="registry/api/mcp/honest.py") == []


def test_bare_request_state_read_is_not_flagged(repo_root: Path) -> None:
    """`request.state.oidc_claims` (set by middleware, no `.app` in the
    chain) is Starlette's per-request scratch pad, not the service
    container -- a different, legitimate mechanism this gate leaves alone."""
    target = _write(
        repo_root,
        "registry/api/routers/honest.py",
        ("def _claims(request):\n" '    return getattr(request.state, "oidc_claims", None)\n'),
    )
    assert check_file(target, rel="registry/api/routers/honest.py") == []


def test_a_module_with_no_state_access_is_not_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/unrelated.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert check_file(target, rel="registry/api/routers/unrelated.py") == []


# ---------------------------------------------------------------------------
# Rule (b): app.state.<x> = ... assignments
# ---------------------------------------------------------------------------


def test_app_state_assignment_outside_wiring_is_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/rogue.py",
        ("def wire(app):\n" "    app.state.catalog = build_catalog()\n"),
    )
    violations = check_file(target, rel="registry/api/routers/rogue.py")
    assert len(violations) == 1
    assert violations[0].rule == "assign"
    assert "catalog" in violations[0].detail


def test_request_app_state_assignment_is_also_flagged(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/rogue.py",
        ("def wire(request):\n" "    request.app.state.catalog = build_catalog()\n"),
    )
    violations = check_file(target, rel="registry/api/routers/rogue.py")
    assert len(violations) == 1
    assert violations[0].rule == "assign"


def test_bare_request_state_assignment_is_not_flagged(repo_root: Path) -> None:
    """`request.state.oidc_claims = ...` is the middleware writing to the
    per-request scratch pad, not attaching a service -- out of scope."""
    target = _write(
        repo_root,
        "registry/api/middleware/honest.py",
        ("def stash(request, claims):\n" "    request.state.oidc_claims = dict(claims)\n"),
    )
    assert check_file(target, rel="registry/api/middleware/honest.py") == []


# ---------------------------------------------------------------------------
# Exemptions: directory, allowlist, bypass marker
# ---------------------------------------------------------------------------


def test_the_wiring_directory_is_exempt_from_getattr_and_allowlisted_assigns(repo_root: Path) -> None:
    """`retrieval` is in `_WIRING_ASSIGNABLE_KEYS`, so both the getattr read
    and the assignment clear here -- unlike the blanket exemption this gate
    used to give every `registry/wiring/` file for both rules."""
    target = _write(
        repo_root,
        "registry/wiring/services.py",
        (
            "def attach(app):\n"
            '    app.state.catalog = getattr(app.state, "pii_scanner", None)\n'
            "    app.state.retrieval = build_retrieval()\n"
        ),
    )
    assert check_file(target, rel="registry/wiring/services.py") == []


def test_wiring_assign_of_a_non_allowlisted_key_is_still_flagged(repo_root: Path) -> None:
    """The tightened rule: a wiring file may only assign the named keys in
    `_WIRING_ASSIGNABLE_KEYS` -- everything else a wiring function builds is
    supposed to flow into `Services` as a return value, not a new bare
    `app.state` attribute nobody added a reader comment for."""
    target = _write(
        repo_root,
        "registry/wiring/services.py",
        ("def attach(app):\n" "    app.state.some_new_service = build_it()\n"),
    )
    violations = check_file(target, rel="registry/wiring/services.py")
    assert len(violations) == 1
    assert violations[0].rule == "assign"
    assert violations[0].key == "some_new_service"


def test_an_allowlisted_function_is_exempt(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    monkeypatch.setattr(
        "scripts.check_state_access.ALLOWLIST",
        (
            Exemption(
                path="registry/api/mcp/synthetic.py",
                rule="getattr",
                functions=frozenset({"_allowed"}),
                reason="test fixture",
            ),
        ),
    )
    target = _write(
        repo_root,
        "registry/api/mcp/synthetic.py",
        (
            "def _allowed(app):\n"
            '    return getattr(app.state, "x", None)\n\n\n'
            "def _not_allowed(app):\n"
            '    return getattr(app.state, "y", None)\n'
        ),
    )
    violations = check_file(target, rel="registry/api/mcp/synthetic.py")
    # Only the un-named function is still flagged -- the allowlist is scoped
    # per-function, not per-file, so it does not cover a sibling for free.
    assert len(violations) == 1
    assert violations[0].function == "_not_allowed"


def test_the_bypass_marker_exempts_a_single_line(repo_root: Path) -> None:
    target = _write(
        repo_root,
        "registry/api/routers/one_off.py",
        (
            "def _service(request):\n"
            '    return getattr(request.app.state, "catalog", None)  # state-access: intentional\n'
        ),
    )
    assert check_file(target, rel="registry/api/routers/one_off.py") == []


# ---------------------------------------------------------------------------
# main(): exit codes and messages
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_and_names_the_file(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(
        repo_root,
        "registry/api/routers/rogue.py",
        ("def _service(request):\n" '    return getattr(request.app.state, "catalog", None)\n'),
    )
    assert main(["--paths", "registry"]) == 1
    out = capsys.readouterr().out
    assert "registry/api/routers/rogue.py:2" in out
    assert "_service" in out


def test_a_stale_exemption_fails_rather_than_passing_silently(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An allowlist entry naming a function that no longer contains a
    violation is a standing permission nobody is using."""
    monkeypatch.setattr(
        "scripts.check_state_access.ALLOWLIST",
        (
            Exemption(
                path="registry/api/mcp/synthetic.py",
                rule="getattr",
                functions=frozenset({"_gone"}),
                reason="test fixture",
            ),
        ),
    )
    _write(
        repo_root,
        "registry/api/mcp/synthetic.py",
        "def _gone(app):\n    return app.state.settings\n",
    )
    assert main(["--paths", "registry"]) == 1
    captured = capsys.readouterr()
    assert "stale-exemption" in captured.out or "stale-exemption" in captured.err


def test_a_stale_wiring_key_fails_rather_than_passing_silently(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key in `_WIRING_ASSIGNABLE_KEYS` that no `registry/wiring/` file
    actually assigns is the keyed-exemption equivalent of a stale
    `ALLOWLIST` entry -- same principle, different mechanism."""
    monkeypatch.setattr(
        "scripts.check_state_access._WIRING_ASSIGNABLE_KEYS",
        frozenset({"catalog", "a_key_nothing_assigns"}),
    )
    _write(
        repo_root,
        "registry/wiring/services.py",
        ("def attach(app):\n" "    app.state.catalog = build_catalog()\n"),
    )
    assert main(["--paths", "registry"]) == 1
    captured = capsys.readouterr()
    assert "stale-wiring-key" in captured.out or "stale-wiring-key" in captured.err
    assert "a_key_nothing_assigns" in captured.out


def test_an_out_of_scope_path_fails_rather_than_passing_silently(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--paths", "does/not/exist"]) == 1
    assert "scope does not exist" in capsys.readouterr().err


def test_explain_flag_exits_zero_and_lists_the_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    for exemption in ALLOWLIST:
        assert exemption.path in out
