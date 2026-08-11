"""The broker's refusals are the product, so each one gets a test.

A measured run is a claim about a machine over a span of time. Everything
here exists to make that claim falsifiable: the lease proves no unrelated
work entered the span, the authenticated control proves each child belongs
to the sequence, and the manifest proves which database a worker got
without carrying the credential that would let anything else connect to it.

Two orderings matter enough to be asserted directly rather than inferred:
a rejected control must be rejected **before** any provider mutation, and a
consumed control must never authenticate twice.
"""

from __future__ import annotations

import json
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from tests.helpers.pg_run_broker import (
    AdmissionError,
    BrokerManifest,
    ControlError,
    ControlPayload,
    Inventory,
    LeaseError,
    ProviderCapabilities,
    RunBroker,
    SequenceLease,
    _utc_now,
    control_ttl_expiry,
    plan_workers,
    redacted_digest,
    serialize_control,
    sign_control,
    write_control_file,
)

CONTROLLER = "controller-1"
SEQUENCE = "sequence-1"


class Recorder:
    """Executor that records statements instead of running them.

    Lets a unit test assert on *whether the provider was touched at all*,
    which is the property several of the refusal tests below actually care
    about.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, sql: str) -> list[tuple[object, ...]]:
        self.statements.append(sql)
        return []


@pytest.fixture()
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture()
def broker(recorder: Recorder) -> RunBroker:
    return RunBroker(provider="testcontainers", execute=recorder, run_id="run123")


def _payload(lease: SequenceLease, **overrides: object) -> ControlPayload:
    base: dict[str, object] = {
        "controller_id": lease.controller_id,
        "lease_id": lease.lease_id,
        "sequence_id": lease.sequence_id,
        "child_sequence_number": 1,
        "mode": "hard-gate",
        "role": "measured",
        "committed_worker_count": 4,
        "provider": "testcontainers",
        "expected_product_commit": "a" * 40,
        "host_digest": "host-digest",
        "template_fingerprint": "fingerprint",
        "collection_digest": "collection",
        "command_digest": "command",
        "nonce": "nonce-1",
        "expires_at": control_ttl_expiry(600),
    }
    base.update(overrides)
    return ControlPayload(**base)  # type: ignore[arg-type]


def _control(lease: SequenceLease, **overrides: object) -> str:
    return serialize_control(_payload(lease, **overrides), lease.secret)


# -- lease exclusivity ----------------------------------------------------


def test_a_second_controller_cannot_take_a_held_lease(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    with pytest.raises(LeaseError, match="already leased"):
        broker.open_sequence("controller-2", "sequence-2")


def test_the_lease_is_retakeable_after_finalization(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    broker.close_sequence(CONTROLLER)
    second = broker.open_sequence("controller-2", "sequence-2")
    assert second.active


def test_a_non_owner_cannot_finalize(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    with pytest.raises(LeaseError, match="is held by"):
        broker.close_sequence("controller-2")


def test_a_finalized_lease_cannot_be_finalized_again(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    lease.finalize(CONTROLLER)
    with pytest.raises(LeaseError, match="already finalized"):
        lease.finalize(CONTROLLER)


def test_an_invalidated_lease_records_its_reason(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    broker.close_sequence(CONTROLLER, reason="UTC date rolled during template validation")
    assert not lease.active
    assert lease.invalidation_reason == "UTC date rolled during template validation"


def test_closing_without_a_lease_is_an_error(broker: RunBroker) -> None:
    with pytest.raises(LeaseError, match="no lease to close"):
        broker.close_sequence(CONTROLLER)


# -- admission ------------------------------------------------------------


def test_admission_is_open_before_a_sequence(broker: RunBroker) -> None:
    assert broker.admit(None) is None
    assert not broker.admission_closed


def test_opening_a_sequence_closes_admission(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    assert broker.admission_closed
    with pytest.raises(AdmissionError, match="admission is closed"):
        broker.admit(None)


def test_admission_reopens_after_the_sequence(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    broker.close_sequence(CONTROLLER)
    assert broker.admit(None) is None


def test_uncontrolled_provisioning_is_refused_without_touching_the_provider(
    broker: RunBroker, recorder: Recorder
) -> None:
    """The ordering that keeps a rejected child from leaving a database behind."""
    broker.open_sequence(CONTROLLER, SEQUENCE)
    with pytest.raises(AdmissionError):
        broker.create_database("cp_worker_run123_w1")
    assert recorder.statements == []
    assert broker.owned_databases == ()


def test_a_valid_control_admits_provisioning(broker: RunBroker, recorder: Recorder) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    broker.create_database("cp_worker_run123_w1", control=_control(lease))
    assert recorder.statements == ['CREATE DATABASE "cp_worker_run123_w1"']


# -- control authentication ----------------------------------------------


def test_a_forged_mac_is_refused(broker: RunBroker, recorder: Recorder) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = json.loads(_control(lease))
    document["mac"] = "0" * 64
    with pytest.raises(ControlError, match="does not authenticate"):
        broker.admit(json.dumps(document))
    assert recorder.statements == []


def test_a_control_signed_with_another_secret_is_refused(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    other = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="testcontainers")
    with pytest.raises(ControlError, match="does not authenticate"):
        broker.admit(serialize_control(_payload(lease), other.secret))


def test_a_tampered_payload_field_is_refused(broker: RunBroker) -> None:
    """The MAC covers every bound field, so editing one breaks it."""
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = json.loads(_control(lease))
    document["payload"]["committed_worker_count"] = 8
    with pytest.raises(ControlError, match="does not authenticate"):
        broker.admit(json.dumps(document))


def test_a_malformed_control_is_refused(broker: RunBroker) -> None:
    broker.open_sequence(CONTROLLER, SEQUENCE)
    with pytest.raises(ControlError, match="malformed"):
        broker.admit("{not json")


def test_a_control_missing_a_field_is_refused(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = json.loads(_control(lease))
    del document["payload"]["host_digest"]
    with pytest.raises(ControlError, match="missing or has a wrong field"):
        broker.admit(json.dumps(document))


def test_an_expired_control_is_refused(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    expired = (_utc_now() - timedelta(seconds=1)).isoformat()
    with pytest.raises(ControlError, match="expired"):
        broker.admit(_control(lease, expires_at=expired))


def test_a_cross_sequence_control_is_refused(broker: RunBroker) -> None:
    """A control minted for another sequence must not admit this one."""
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    payload = _payload(lease, sequence_id="sequence-other")
    with pytest.raises(ControlError, match="not .*sequence-1"):
        broker.admit(serialize_control(payload, lease.secret))


def test_a_cross_lease_control_is_refused(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    payload = _payload(lease, lease_id="lease-other")
    with pytest.raises(ControlError, match="not"):
        broker.admit(serialize_control(payload, lease.secret))


def test_a_control_naming_another_controller_is_refused(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    payload = _payload(lease, controller_id="controller-other")
    with pytest.raises(ControlError, match="names controller"):
        broker.admit(serialize_control(payload, lease.secret))


def test_a_control_is_consumed_exactly_once(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = _control(lease)
    assert broker.admit(document) is not None
    with pytest.raises(ControlError, match="already consumed"):
        broker.admit(document)


def test_a_replayed_nonce_is_refused_even_with_a_fresh_signature(broker: RunBroker) -> None:
    """Reusing a nonce in a newly signed control is still a replay."""
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    broker.admit(_control(lease, child_sequence_number=1))
    with pytest.raises(ControlError, match="already consumed"):
        broker.admit(_control(lease, child_sequence_number=2))


def test_distinct_nonces_admit_successive_children(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    for index in range(1, 4):
        payload = broker.admit(_control(lease, nonce=f"nonce-{index}", child_sequence_number=index))
        assert payload is not None
        assert payload.child_sequence_number == index


def test_a_rejected_control_is_not_burned(broker: RunBroker) -> None:
    """An expired control must not consume the nonce a valid retry would use.

    Rejections are checked before consumption, so a failed admission for a
    reason other than replay leaves the nonce usable.
    """
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    expired = (_utc_now() - timedelta(seconds=1)).isoformat()
    with pytest.raises(ControlError, match="expired"):
        broker.admit(_control(lease, nonce="retry", expires_at=expired))
    assert broker.admit(_control(lease, nonce="retry")) is not None


def test_a_control_cannot_be_consumed_after_the_lease_closes(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    document = _control(lease)
    broker.close_sequence(CONTROLLER)
    with pytest.raises(ControlError, match="no longer active"):
        lease.verify_and_consume(document)


# -- secrets stay in memory ----------------------------------------------


def test_the_lease_repr_does_not_leak_the_secret() -> None:
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    rendered = repr(lease)
    assert lease.secret.hex() not in rendered
    assert "_secret" not in rendered


def test_lease_evidence_omits_the_secret() -> None:
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    serialized = json.dumps(lease.as_evidence())
    assert lease.secret.hex() not in serialized


def test_control_evidence_omits_the_nonce() -> None:
    """Evidence records what a control asserted, never the replayable token."""
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    evidence = _payload(lease).as_evidence()
    assert "nonce" not in evidence
    assert evidence["committed_worker_count"] == 4


def test_the_serialized_control_never_contains_the_secret() -> None:
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    assert lease.secret.hex() not in serialize_control(_payload(lease), lease.secret)


def test_signing_is_deterministic_for_one_payload() -> None:
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    payload = _payload(lease)
    assert sign_control(payload, lease.secret) == sign_control(payload, lease.secret)


def test_a_control_file_is_created_private(tmp_path: Path) -> None:
    lease = SequenceLease(controller_id=CONTROLLER, sequence_id=SEQUENCE, provider="devstack")
    path = write_control_file(tmp_path / "nested" / "control.json", _payload(lease), lease.secret)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- manifest handoff ----------------------------------------------------


def test_the_manifest_file_is_private(tmp_path: Path) -> None:
    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://u:secret@localhost:5545/cp_w1", "cp_w1")
    path = manifest.write(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_manifest_evidence_carries_no_url(tmp_path: Path) -> None:
    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://u:secret@localhost:5545/cp_w1", "cp_w1")
    serialized = json.dumps(manifest.as_evidence())
    assert "secret" not in serialized
    assert "postgresql://" not in serialized
    assert redacted_digest("postgresql://u:secret@localhost:5545/cp_w1") in serialized


def test_the_manifest_repr_carries_no_url() -> None:
    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://u:secret@localhost:5545/cp_w1", "cp_w1")
    assert "secret" not in repr(manifest)


def test_a_worker_receives_only_its_own_url() -> None:
    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://localhost/one", "cp_one")
    manifest.assign("w2", "postgresql://localhost/two", "cp_two")
    environment = manifest.worker_environment("w1")
    assert environment["CONTEXTPLANE_TEST_DATABASE_URL"] == "postgresql://localhost/one"
    assert "two" not in json.dumps(environment)


def test_an_unknown_worker_has_no_assignment() -> None:
    manifest = BrokerManifest(run_id="run123")
    with pytest.raises(Exception, match="no assignment for worker"):
        manifest.worker_environment("w9")


def test_the_manifest_digest_changes_with_the_assignments() -> None:
    one = BrokerManifest(run_id="run123")
    one.assign("w1", "postgresql://localhost/one", "cp_one")
    two = BrokerManifest(run_id="run123")
    two.assign("w1", "postgresql://localhost/other", "cp_other")
    assert one.digest() != two.digest()


def test_the_manifest_digest_is_reproducible_from_redacted_data() -> None:
    one = BrokerManifest(run_id="run123")
    one.assign("w1", "postgresql://localhost/one", "cp_one")
    two = BrokerManifest(run_id="run123")
    two.assign("w1", "postgresql://localhost/one", "cp_one")
    assert one.digest() == two.digest()


def test_deleting_the_manifest_is_idempotent(tmp_path: Path) -> None:
    manifest = BrokerManifest(run_id="run123")
    manifest.assign("w1", "postgresql://localhost/one", "cp_one")
    path = manifest.write(tmp_path)
    manifest.delete()
    assert not path.exists()
    manifest.delete()


# -- capability-driven worker planning -----------------------------------


def _capabilities(**flags: bool) -> ProviderCapabilities:
    base = {"create": True, "clone": True, "terminate": True, "drop": True}
    base.update(flags)
    return ProviderCapabilities(provider="external", **base)  # type: ignore[arg-type]


def test_full_capabilities_allow_the_requested_parallelism() -> None:
    plan = plan_workers(_capabilities(), 8)
    assert plan.workers == 8
    assert plan.parallel


@pytest.mark.parametrize("missing", ["create", "clone", "terminate", "drop"])
def test_any_missing_capability_falls_back_to_one_worker(missing: str) -> None:
    plan = plan_workers(_capabilities(**{missing: False}), 8)
    assert plan.workers == 1
    assert not plan.parallel
    assert missing in plan.reason


def test_the_fallback_states_its_reason() -> None:
    """A silent fallback would be reported as a parallel measurement."""
    plan = plan_workers(_capabilities(clone=False), 4)
    assert "rather than sharing a mutable database" in plan.reason


def test_a_single_worker_request_is_not_a_fallback() -> None:
    assert plan_workers(_capabilities(clone=False), 1).reason == "one worker requested"


def test_zero_workers_is_refused() -> None:
    with pytest.raises(Exception, match="must be >= 1"):
        plan_workers(_capabilities(), 0)


def test_incomplete_capabilities_name_what_is_missing() -> None:
    capabilities = _capabilities(clone=False, drop=False)
    assert not capabilities.complete
    assert capabilities.missing == ("clone", "drop")


# -- database lifecycle --------------------------------------------------


def test_each_consumer_kind_gets_a_distinct_database(broker: RunBroker) -> None:
    names = {
        broker.database_name("worker", "w1"),
        broker.database_name("worker", "w2"),
        broker.database_name("scratch", "w1"),
        broker.database_name("scenario", "w1"),
    }
    assert len(names) == 4


def test_database_names_fit_the_identifier_limit(broker: RunBroker) -> None:
    assert len(broker.database_name("scenario", "x" * 100)) <= 63


def test_database_names_are_sanitized(broker: RunBroker) -> None:
    assert broker.database_name("worker", "gw-1/odd name") == "cp_worker_run123_gw_1_odd_name"


def test_a_clone_terminates_nothing_and_copies_the_template(broker: RunBroker, recorder: Recorder) -> None:
    broker.clone_database("cp_worker_run123_w1", template="cp_tmpl_abc")
    assert recorder.statements == ['CREATE DATABASE "cp_worker_run123_w1" TEMPLATE "cp_tmpl_abc"']


def test_a_drop_terminates_connections_first(broker: RunBroker, recorder: Recorder) -> None:
    """A drop with a live backend attached fails, and the run leaks it."""
    broker.create_database("cp_worker_run123_w1")
    recorder.statements.clear()
    broker.drop_database("cp_worker_run123_w1")
    assert "pg_terminate_backend" in recorder.statements[0]
    assert recorder.statements[1] == 'DROP DATABASE IF EXISTS "cp_worker_run123_w1"'


def test_cleanup_drops_everything_the_broker_created(broker: RunBroker) -> None:
    broker.create_database(broker.database_name("worker", "w1"))
    broker.clone_database(broker.database_name("worker", "w2"), template="cp_tmpl_abc")
    assert broker.cleanup() == []
    assert broker.owned_databases == ()


def test_cleanup_is_idempotent(broker: RunBroker) -> None:
    broker.create_database("cp_worker_run123_w1")
    broker.cleanup()
    assert broker.cleanup() == []


def test_cleanup_continues_past_one_failure() -> None:
    """A cleanup that stopped at the first error would leak the rest."""

    def flaky(sql: str) -> list[tuple[object, ...]]:
        if "cp_worker_run123_bad" in sql and sql.startswith("DROP"):
            raise RuntimeError("cannot drop")
        return []

    broker = RunBroker(provider="devstack", execute=flaky, run_id="run123")
    broker.create_database("cp_worker_run123_bad")
    broker.create_database("cp_worker_run123_good")
    failures = broker.cleanup()
    assert len(failures) == 1
    assert "cp_worker_run123_bad" in failures[0]
    assert "cp_worker_run123_good" not in broker.owned_databases


def test_boundaries_are_instrumented(broker: RunBroker) -> None:
    broker.create_database("cp_worker_run123_w1")
    broker.drop_database("cp_worker_run123_w1")
    names = [boundary.name for boundary in broker.boundaries]
    assert "create_database" in names
    assert "drop_database" in names
    assert all(boundary.seconds >= 0 for boundary in broker.boundaries)


# -- inventory ------------------------------------------------------------


def test_inventory_reports_a_leaked_database() -> None:
    before = Inventory(databases=("postgres", "cp_tmpl_a"), sessions=1, templates=("cp_tmpl_a",))
    after = Inventory(databases=("postgres", "cp_tmpl_a", "cp_worker_leak"), sessions=1, templates=("cp_tmpl_a",))
    assert not after.matches(before)
    assert after.unexpected_against(before)["new_databases"] == ["cp_worker_leak"]


def test_inventory_reports_a_removed_template() -> None:
    before = Inventory(databases=("postgres", "cp_tmpl_a"), sessions=1, templates=("cp_tmpl_a",))
    after = Inventory(databases=("postgres",), sessions=1, templates=())
    assert not after.matches(before)
    assert after.unexpected_against(before)["removed_templates"] == ["cp_tmpl_a"]


def test_inventory_reports_leaked_sessions() -> None:
    before = Inventory(databases=("postgres",), sessions=1, templates=())
    after = Inventory(databases=("postgres",), sessions=5, templates=())
    assert not after.matches(before)
    assert after.unexpected_against(before)["session_delta"] == 4


def test_a_clean_run_matches_its_baseline() -> None:
    before = Inventory(databases=("postgres", "cp_tmpl_a"), sessions=2, templates=("cp_tmpl_a",))
    after = Inventory(databases=("postgres", "cp_tmpl_a"), sessions=2, templates=("cp_tmpl_a",))
    assert after.matches(before)


def test_fewer_sessions_than_baseline_is_still_clean() -> None:
    """Connections closing during a run is not a leak."""
    before = Inventory(databases=("postgres",), sessions=4, templates=())
    after = Inventory(databases=("postgres",), sessions=1, templates=())
    assert after.matches(before)


def test_broker_evidence_names_the_lease_without_the_secret(broker: RunBroker) -> None:
    lease = broker.open_sequence(CONTROLLER, SEQUENCE)
    serialized = json.dumps(broker.as_evidence())
    assert lease.lease_id in serialized
    assert lease.secret.hex() not in serialized
