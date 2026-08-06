"""The model-backed drafter stays disabled unless the committed decision
artifact actually earned it -- proven, not assumed.

`registry.wiring.services._assert_drafter_decision_permits_serving` is the
one place `ARC_DRAFTER_MODEL_ENABLED` gets to matter. This suite proves four
things about it:

1. With the flag absent from the environment entirely (the real deployment
   default -- not merely set to a falsy string), the guard is a true no-op:
   it never reads the decision artifact and never touches the configured
   model-artifact path.
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

A fifth thing, added once `registry/arc/service/drafter.py` and
`registry/arc/sandbox/drafter_main.py` existed to check it against: the
service built on top of this guard also refuses while the model is
disabled, and it does so before touching any other collaborator -- see
`test_the_drafting_path_now_exists_and_the_model_backed_side_of_it_still_refuses`.
"""

from __future__ import annotations

import hashlib
import uuid
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


async def test_the_drafting_path_now_exists_and_the_model_backed_side_of_it_still_refuses() -> None:
    """This file's own non-goal note once read "no drafting route, service,
    or sandboxed process exists in this codebase yet ... a later task adds
    them behind their own `arc_drafter_model_disabled` route-level
    refusal" -- true when this file was written, false now that
    `registry/arc/service/drafter.py` and `registry/arc/sandbox/
    drafter_main.py` exist.

    What this test keeps proving, in the shape that actually matters, is
    not merely that the files exist but that the property the old
    assertion stood in for -- *no drafting happens while the decision
    artifact says `human_only`* -- still holds now that a real path
    exists to violate it. `DrafterService.draft` is constructed here with
    every other collaborator deliberately unusable (`None`); the disabled
    check is documented (and proven in `tests/unit/test_arc_drafter.py`'s
    own mutation-style test) to run before any of them is ever touched, so
    a version of this test that reached past it would fail with an
    `AttributeError` on a collaborator, not merely pass by accident.

    The registered *route*'s own end-to-end refusal (a live app, a real
    409, the `arc_drafter_model_disabled` response body) is proven in
    `tests/integration/test_arc_drafting.py`, not here -- this file's own
    scope stays the config-level guard and the service built directly on
    top of it, not the HTTP layer above that.
    """
    from registry.arc.service.drafter import DrafterModelDisabled, DrafterService

    assert (_PRODUCTION_ROOT / "arc" / "service" / "drafter.py").exists()
    assert (_PRODUCTION_ROOT / "arc" / "sandbox" / "drafter_main.py").exists()

    def _refusing_decision_loader() -> dict[str, Any]:
        raise AssertionError("the decision artifact was read while the model flag is disabled")

    settings = Settings(database_url="postgresql+asyncpg://unused/unused", arc_drafter_model_enabled=False)
    service = DrafterService(
        None,  # type: ignore[arg-type]
        authorization=None,  # type: ignore[arg-type]
        source_admission=None,  # type: ignore[arg-type]
        source_status=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        settings=settings,
        decision_loader=_refusing_decision_loader,
    )

    with pytest.raises(DrafterModelDisabled):
        await service.draft(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            1,
            source_evidence_id=uuid.uuid4(),
            target_field_paths=["directives"],
        )

    assert issubclass(DrafterModelDisabled, Exception)
    assert hasattr(DrafterService, "draft")
