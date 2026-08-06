"""The model-backed drafter stays disabled unless the committed decision
artifact actually earned it -- proven, not assumed.

`registry.wiring.services._assert_drafter_decision_permits_serving` is the
one place `ARC_DRAFTER_MODEL_ENABLED` gets to matter. This suite proves four
things about it:

1. With the flag absent from the environment entirely (the real deployment
   default -- not merely set to a falsy string), the guard is a true no-op:
   it never reads the decision artifact and never touches the configured
   model-artifact path. No drafting path exists yet in this codebase at all
   (`registry/arc/service/drafter.py` and `registry/arc/sandbox/drafter_main.py`
   do not exist until a later task), so there is no provider call this guard
   could gate today -- but the guard itself must not attempt one either.
2. Flipping the flag true against a `human_only` decision refuses to start.
3. Flipping the flag true against an `accepted` decision whose model artifact
   digest was tampered also refuses to start -- an operator cannot swap the
   model file and keep serving under a stale digest.
4. The guard is not vacuously "always refuse": a correctly configured
   enabled path (accepted, every gate passed, matching digest) does not
   raise.

Each of 2-4 is a mutation-style proof: the fixture is a decision the guard
must evaluate for real, not the committed repo artifact, so the test result
depends on the guard's own logic rather than on which verdict happens to be
checked in today.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from registry.config import Settings
from registry.wiring import services

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCTION_ROOT = _REPO_ROOT / "registry"


def _base_decision(**overrides: Any) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "decision_version": 1,
        "model_artifact_digest": None,
        "tokenizer_digest": None,
        "prompt_profile_version": None,
        "resource_envelope": {"memory_mib": 512, "cpu_count": 1, "timeout_seconds": 30},
        "license_terms_reference": None,
        "evaluation_manifest_version": 1,
        "outcome": "human_only",
        "gate_results": [{"gate_id": "source_identity_preservation", "passed": False, "detail": "not evaluated"}],
    }
    decision.update(overrides)
    return decision


def _settings(*, enabled: bool, artifact_path: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/unused",
        arc_drafter_model_enabled=enabled,
        arc_drafter_model_artifact_path=artifact_path,
    )


# ---------------------------------------------------------------------------
# 1. Flag absent (default False): true no-op, no read attempted at all.
# ---------------------------------------------------------------------------


def test_disabled_by_default_never_reads_the_decision_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings constructed with no ARC_DRAFTER_MODEL_ENABLED keyword at all
    -- the same shape a deployment with the variable entirely absent from
    its environment produces -- must never call the loader. Proven by
    replacing the loader with one that fails the test if it is ever called,
    rather than by asserting a return value the loader itself might not
    have been reached to produce."""

    def _fail_if_called(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("load_drafter_model_decision was called while the drafter flag is disabled")

    monkeypatch.setattr(services, "load_drafter_model_decision", _fail_if_called)

    settings = Settings(database_url="postgresql+asyncpg://unused/unused")
    assert settings.arc_drafter_model_enabled is False  # the real default, not a test fixture's assumption

    assert services._assert_drafter_decision_permits_serving(settings) is None


def test_disabled_never_reads_the_configured_artifact_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even when an artifact path IS configured (an operator staged a model
    but never flipped the flag), the guard must not touch the filesystem at
    that path while disabled. A garbage path proves it: if the guard read
    it, resolving its digest would raise on the missing file."""
    garbage_path = str(tmp_path / "does-not-exist" / "model.bin")
    settings = _settings(enabled=False, artifact_path=garbage_path)

    def _fail_if_called(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("load_drafter_model_decision was called while the drafter flag is disabled")

    monkeypatch.setattr(services, "load_drafter_model_decision", _fail_if_called)

    assert services._assert_drafter_decision_permits_serving(settings) is None


# ---------------------------------------------------------------------------
# 2. Enabled + human_only decision -> refuses.
# ---------------------------------------------------------------------------


def test_enabled_against_human_only_decision_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _base_decision(outcome="human_only")
    monkeypatch.setattr(services, "load_drafter_model_decision", lambda *a, **k: decision)

    settings = _settings(enabled=True, artifact_path=None)

    with pytest.raises(RuntimeError, match="not 'accepted'"):
        services._assert_drafter_decision_permits_serving(settings)


# ---------------------------------------------------------------------------
# 3. Enabled + accepted decision + tampered digest -> refuses.
# ---------------------------------------------------------------------------


def test_enabled_against_accepted_decision_with_tampered_digest_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"the-real-model-bytes")
    real_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    tampered_digest = hashlib.sha256(b"a-different-file-entirely").hexdigest()
    assert tampered_digest != real_digest

    decision = _base_decision(
        outcome="accepted",
        model_artifact_digest=tampered_digest,
        gate_results=[{"gate_id": "source_identity_preservation", "passed": True, "detail": "evaluated"}],
    )
    monkeypatch.setattr(services, "load_drafter_model_decision", lambda *a, **k: decision)

    settings = _settings(enabled=True, artifact_path=str(artifact))

    with pytest.raises(RuntimeError, match="hashes to"):
        services._assert_drafter_decision_permits_serving(settings)


def test_enabled_with_a_failing_gate_refuses_even_if_outcome_says_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-contradictory artifact (accepted outcome, one gate recorded as
    failed) must still refuse -- the flag can never be more permissive than
    the least favorable thing the artifact itself records."""
    decision = _base_decision(
        outcome="accepted",
        model_artifact_digest="0" * 64,
        gate_results=[
            {"gate_id": "source_identity_preservation", "passed": True, "detail": "evaluated"},
            {"gate_id": "prompt_injection_containment", "passed": False, "detail": "evaluated, failed"},
        ],
    )
    monkeypatch.setattr(services, "load_drafter_model_decision", lambda *a, **k: decision)

    settings = _settings(enabled=True, artifact_path=None)

    with pytest.raises(RuntimeError, match="failing evaluation gate"):
        services._assert_drafter_decision_permits_serving(settings)


def test_enabled_with_no_artifact_path_configured_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _base_decision(
        outcome="accepted",
        model_artifact_digest="0" * 64,
        gate_results=[{"gate_id": "source_identity_preservation", "passed": True, "detail": "evaluated"}],
    )
    monkeypatch.setattr(services, "load_drafter_model_decision", lambda *a, **k: decision)

    settings = _settings(enabled=True, artifact_path=None)

    with pytest.raises(RuntimeError, match="does not name a file"):
        services._assert_drafter_decision_permits_serving(settings)


# ---------------------------------------------------------------------------
# 4. Not vacuous: a correctly earned enabled path does not raise.
# ---------------------------------------------------------------------------


def test_enabled_against_a_genuinely_accepted_matching_decision_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"the-real-model-bytes")
    real_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    decision = _base_decision(
        outcome="accepted",
        model_artifact_digest=real_digest,
        gate_results=[{"gate_id": "source_identity_preservation", "passed": True, "detail": "evaluated"}],
    )
    monkeypatch.setattr(services, "load_drafter_model_decision", lambda *a, **k: decision)

    settings = _settings(enabled=True, artifact_path=str(artifact))

    assert services._assert_drafter_decision_permits_serving(settings) is None


# ---------------------------------------------------------------------------
# 5. The committed artifact today: proves the disabled default is what
#    actually ships, against the real file on disk (no monkeypatch).
# ---------------------------------------------------------------------------


def test_the_committed_decision_artifact_keeps_the_flag_disabled_by_default() -> None:
    decision = services.load_drafter_model_decision()
    settings = Settings(database_url="postgresql+asyncpg://unused/unused")
    if decision["outcome"] != "accepted":
        # The committed artifact has not earned serving. If a future commit
        # flips it to 'accepted' this branch stops applying -- that is the
        # point: this test is pinned to today's honest verdict, not to a
        # verdict this task is asserting forever.
        assert settings.arc_drafter_model_enabled is False


# ---------------------------------------------------------------------------
# 6. No other reader: the setting is consulted nowhere except the guard.
# ---------------------------------------------------------------------------


def test_the_artifact_path_setting_has_exactly_one_production_reader() -> None:
    """`arc_drafter_model_artifact_path` must not leak into some other code
    path that could read (and thus attempt to use) the configured model file
    without going through the startup guard's four-condition check. A grep
    across the whole shipped tree, not just the module this task edited --
    a reader added anywhere else in `registry/registry/` would defeat the
    guarantee just as surely as one added inside `services.py` outside the
    guard function."""
    hits: list[str] = []
    for path in _PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "arc_drafter_model_artifact_path" in text:
            hits.append(str(path.relative_to(_REPO_ROOT)))
    assert hits == [
        "registry/config.py",
        "registry/wiring/services.py",
    ], f"arc_drafter_model_artifact_path is read outside the Settings field and its one guard: {hits}"


def test_no_drafter_service_or_sandbox_exists_yet() -> None:
    """This task's own non-goal, checked rather than assumed: no drafting
    route, service, or sandboxed process exists in this codebase yet, so
    there is no live path a model call could travel down regardless of how
    this flag is set. A later task adds them behind their own
    `arc_drafter_model_disabled` route-level refusal."""
    assert not (_PRODUCTION_ROOT / "arc" / "service" / "drafter.py").exists()
    assert not (_PRODUCTION_ROOT / "arc" / "sandbox" / "drafter_main.py").exists()
