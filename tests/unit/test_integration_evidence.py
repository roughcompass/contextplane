"""Write-once, atomic, and redacted — proved against a real filesystem.

These use `tmp_path` rather than a fake filesystem on purpose. Exclusive
creation, rename-into-place, and fsync are operating-system behaviours; a fake
that reimplements them would pass whether or not the real calls are correct.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.integration_evidence import (
    EvidenceError,
    EvidenceWriter,
    SecretLeak,
    assert_no_secrets,
    create_run_directory,
    read_manifest,
    run_scoped_digest,
    verify_manifest,
)


def writer_for(tmp_path, run_id: str = "run-1") -> EvidenceWriter:
    return EvidenceWriter(create_run_directory(tmp_path, run_id))


# --------------------------------------------------------------------------
# Write-once
# --------------------------------------------------------------------------


def test_a_run_id_cannot_be_reused(tmp_path) -> None:
    """Reuse is how a stale artifact gets presented as a fresh measurement."""
    create_run_directory(tmp_path, "run-1")

    with pytest.raises(EvidenceError, match="already has evidence"):
        create_run_directory(tmp_path, "run-1")


def test_a_reused_run_id_does_not_destroy_the_original(tmp_path) -> None:
    first = writer_for(tmp_path)
    first.write_json("inner.json", {"nodes": 30})
    first.finalize(exit_status=0, summary={"ok": True})

    with pytest.raises(EvidenceError):
        create_run_directory(tmp_path, "run-1")

    assert read_manifest(tmp_path / "run-1")["summary"] == {"ok": True}


@pytest.mark.parametrize("run_id", ["", "../escape", "/absolute", ".hidden", "a/b"])
def test_unusable_run_ids_are_refused(tmp_path, run_id: str) -> None:
    with pytest.raises(EvidenceError, match="unusable run ID|already has evidence"):
        create_run_directory(tmp_path, run_id)


def test_nothing_can_be_added_after_finalization(tmp_path) -> None:
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 1})
    writer.finalize(exit_status=0, summary={})

    with pytest.raises(EvidenceError, match="is finalized"):
        writer.write_json("late.json", {"sneaked": True})


def test_a_run_finalizes_exactly_once(tmp_path) -> None:
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 1})
    writer.finalize(exit_status=0, summary={})

    with pytest.raises(EvidenceError, match="already finalized"):
        writer.finalize(exit_status=0, summary={})


def test_the_manifest_cannot_be_written_as_a_raw_file(tmp_path) -> None:
    writer = writer_for(tmp_path)

    with pytest.raises(EvidenceError, match="written by finalize"):
        writer.write_json("manifest.json", {"forged": True})


# --------------------------------------------------------------------------
# Finalization ordering and integrity
# --------------------------------------------------------------------------


def test_an_unfinalized_run_has_no_manifest_to_read(tmp_path) -> None:
    """Absence of a manifest means "did not finish", which is not the same
    fact as a run that finished badly."""
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 1})

    with pytest.raises(EvidenceError, match="did not finalize"):
        read_manifest(writer.path)


def test_manifest_checksums_every_raw_file(tmp_path) -> None:
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 30})
    writer.write_jsonl("events.jsonl", [{"seq": 1}, {"seq": 2}])

    manifest = writer.finalize(exit_status=0, summary={"role": "measured"})

    assert set(manifest["checksums"]) == {"inner.json", "events.jsonl"}
    assert verify_manifest(writer.path) == {}


def test_a_tampered_raw_file_is_caught_by_reverification(tmp_path) -> None:
    """The verifier re-checksums rather than trusting the recorded value — a
    manifest that vouches for itself proves only that one hand wrote both."""
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 30})
    writer.finalize(exit_status=0, summary={})

    (writer.path / "inner.json").write_text('{"nodes": 1}\n', encoding="utf-8")

    mismatches = verify_manifest(writer.path)
    assert "inner.json" in mismatches
    assert "checksum" in mismatches["inner.json"]


def test_a_deleted_raw_file_is_caught_by_reverification(tmp_path) -> None:
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 30})
    writer.finalize(exit_status=0, summary={})

    (writer.path / "inner.json").unlink()

    assert "missing file" in verify_manifest(writer.path)["inner.json"]


def test_adopted_external_timing_file_is_checksummed_too(tmp_path) -> None:
    """`/usr/bin/time` writes its own file; the manifest must still bind it."""
    writer = writer_for(tmp_path)
    timing = tmp_path / "run.time"
    timing.write_text("real 52.10\nuser 30.00\nsys 5.00\n", encoding="utf-8")
    writer.adopt("timing", timing)

    manifest = writer.finalize(exit_status=0, summary={})

    assert "timing" in manifest["checksums"]


def test_adopting_a_missing_file_fails_rather_than_recording_nothing(tmp_path) -> None:
    writer = writer_for(tmp_path)

    with pytest.raises(EvidenceError, match="cannot adopt missing file"):
        writer.adopt("timing", tmp_path / "never-written.time")


def test_jsonl_records_are_one_document_per_line(tmp_path) -> None:
    writer = writer_for(tmp_path)
    path = writer.write_jsonl("events.jsonl", [{"seq": 1}, {"seq": 2}, {"seq": 3}])

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_a_database_url_cannot_be_serialized(tmp_path) -> None:
    writer = writer_for(tmp_path)

    with pytest.raises(SecretLeak, match="looks like a credential"):
        writer.write_json("inner.json", {"server": "postgresql://user:pw@localhost:5432/db"})


def test_a_forbidden_key_is_refused_whatever_it_holds(tmp_path) -> None:
    """A key named `database_url` holding "redacted" is still a schema that
    invites the next writer to put a URL in it."""
    writer = writer_for(tmp_path)

    with pytest.raises(SecretLeak, match="may never be serialized"):
        writer.write_json("inner.json", {"database_url": "redacted"})


def test_a_nested_secret_is_found(tmp_path) -> None:
    writer = writer_for(tmp_path)

    with pytest.raises(SecretLeak, match=r"\$\.workers\[1\]\.dsn"):
        writer.write_json("inner.json", {"workers": [{"id": "w0"}, {"dsn": "x"}]})


def test_a_secret_in_the_summary_is_found_at_finalization(tmp_path) -> None:
    writer = writer_for(tmp_path)
    writer.write_json("inner.json", {"nodes": 1})

    with pytest.raises(SecretLeak):
        writer.finalize(exit_status=0, summary={"token": "abc"})


def test_a_password_assignment_in_free_text_is_caught() -> None:
    with pytest.raises(SecretLeak):
        assert_no_secrets({"log": "connecting with password=hunter2"})


def test_redacted_identities_still_compare_within_one_run() -> None:
    """The digest has to be useful for the comparison evidence actually makes:
    two records naming the same database inside one run."""
    first = run_scoped_digest("run-1", "cp_test_w0")
    again = run_scoped_digest("run-1", "cp_test_w0")
    other = run_scoped_digest("run-1", "cp_test_w1")

    assert first == again
    assert first != other


def test_the_same_database_digests_differently_in_two_runs() -> None:
    """Cross-run correlation of raw identities is the leak a digest prevents."""
    assert run_scoped_digest("run-1", "cp_test_w0") != run_scoped_digest("run-2", "cp_test_w0")


def test_a_digest_does_not_contain_the_identity_it_stands_for() -> None:
    assert "cp_test_w0" not in run_scoped_digest("run-1", "cp_test_w0")


def test_ordinary_evidence_passes_the_scan() -> None:
    assert_no_secrets(
        {
            "run_id": "run-1",
            "provider": "devstack",
            "server_digest": run_scoped_digest("run-1", "cp_test"),
            "intervals": [{"phase": "execution", "duration_seconds": 41.2}],
        }
    )


# ---------------------------------------------------------------------------
# The verifier refuses what it cannot independently re-derive
# ---------------------------------------------------------------------------
#
# These exercise the judging half rather than the recording half. The property
# under test throughout is that no field an attacker could write is believed:
# changing a bound record, a commit, a tree, or removing a gate must prevent a
# target-met record, and each is checked by mutating exactly one thing away from
# a known-good baseline so a pass cannot come from the fixture being broken in
# some other way.

import sys as _sys  # noqa: E402 - the verifier tests below need `scripts/` on the path first
from pathlib import Path as _Path  # noqa: E402 - same

_SCRIPTS = _Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS))

import verify_integration_evidence as vie  # noqa: E402 - resolved via the sys.path line above


def _sealed(tmp_path, document: dict, name: str = "seq-manifest.json"):
    """Write a manifest with a correct sidecar, the way the controller does."""
    path = tmp_path / name
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    path.with_suffix(".sha256").write_text(f"{vie.sha256_text(payload)}  {path.name}\n", encoding="utf-8")
    return path


def _runs(count: int = 3, *, worker_count: int = 4, provider: str = "devstack", real: float = 41.0):
    return [
        {
            "run_id": f"run-{index}",
            "child_sequence": index,
            "mode": "hard-gate",
            "role": "warmup" if index == 1 else "measured",
            "worker_count": worker_count,
            "provider": provider,
            "exit_status": 0,
            "timed_out": False,
            "control_digest": f"{index:064x}",
            "command": ["/usr/bin/time", "-p", "make", "test-integration"],
            "checksums": {"time": "a" * 64},
            "external_real_seconds": real,
            "inner_summary": {"collected": 10, "reported": 10},
        }
        for index in range(1, count + 1)
    ]


def test_a_manifest_whose_bytes_changed_after_sealing_is_refused(tmp_path) -> None:
    """The sidecar is recomputed from bytes, not read out of the document."""
    path = _sealed(tmp_path, {"runs": _runs()})
    path.write_text(path.read_text(encoding="utf-8").replace("41.0", "31.0"), encoding="utf-8")
    with pytest.raises(vie.VerificationFailure, match="sidecar checksum"):
        vie.load_sealed(path)


def test_a_manifest_with_no_sidecar_is_refused(tmp_path) -> None:
    path = tmp_path / "unsealed-manifest.json"
    path.write_text(json.dumps({"runs": []}), encoding="utf-8")
    with pytest.raises(vie.VerificationFailure, match="no checksum sidecar"):
        vie.load_sealed(path)


def test_an_empty_run_list_is_refused_rather_than_passing_vacuously() -> None:
    """A sequence with no children satisfies every check that iterates children."""
    with pytest.raises(vie.VerificationFailure, match="lists no runs"):
        vie.check_sequence_integrity({"runs": []}, require_outer_controller=False)


def test_a_replayed_child_sequence_number_is_refused() -> None:
    runs = _runs()
    runs[2]["child_sequence"] = 2
    with pytest.raises(vie.VerificationFailure, match="duplicate child sequence"):
        vie.check_sequence_integrity(
            {"runs": runs, "run_ids": [r["run_id"] for r in runs]}, require_outer_controller=False
        )


def test_a_missing_child_leaves_a_gap_that_is_refused() -> None:
    runs = _runs()
    del runs[1]
    with pytest.raises(vie.VerificationFailure, match="not contiguous"):
        vie.check_sequence_integrity(
            {"runs": runs, "run_ids": [r["run_id"] for r in runs]}, require_outer_controller=False
        )


def test_run_ids_that_disagree_with_the_run_records_are_refused() -> None:
    runs = _runs()
    with pytest.raises(vie.VerificationFailure, match="do not match its run records"):
        vie.check_sequence_integrity(
            {"runs": runs, "run_ids": ["run-1", "run-2", "run-9"]}, require_outer_controller=False
        )


def test_a_sequence_with_no_controller_is_refused_when_one_is_required() -> None:
    """Runs with no single owner may have been spliced from separate sequences."""
    runs = _runs()
    with pytest.raises(vie.VerificationFailure, match="names no controller"):
        vie.check_sequence_integrity(
            {"runs": runs, "run_ids": [r["run_id"] for r in runs]}, require_outer_controller=True
        )


def test_a_reused_control_digest_is_refused() -> None:
    runs = _runs()
    runs[1]["control_digest"] = runs[0]["control_digest"]
    with pytest.raises(vie.VerificationFailure, match="share a control digest"):
        vie.check_authenticated_controls({"consumed_control_digests": []}, runs)


def test_controls_consumed_must_match_the_controls_presented() -> None:
    """A child that collected without its control consumed was never authorized."""
    runs = _runs()
    with pytest.raises(vie.VerificationFailure, match="does not match the controls"):
        vie.check_authenticated_controls({"consumed_control_digests": [runs[0]["control_digest"]]}, runs)


def test_serialized_control_material_is_refused() -> None:
    runs = _runs()
    runs[0]["sequence_secret"] = "s3cret"
    with pytest.raises(vie.VerificationFailure, match="serialized control material"):
        vie.check_authenticated_controls({"consumed_control_digests": [r["control_digest"] for r in runs]}, runs)


def test_inner_reported_external_timing_is_refused() -> None:
    """A child cannot time its own spawn, so it may not report external time."""
    run = _runs(1)[0]
    del run["external_real_seconds"]
    run["inner_summary"]["external_real_seconds"] = 40.0
    with pytest.raises(vie.VerificationFailure, match="from inside the child"):
        vie.external_real(run)


def test_timing_with_no_checksummed_time_file_is_refused() -> None:
    run = _runs(1)[0]
    run["checksums"] = {}
    with pytest.raises(vie.VerificationFailure, match="no checksummed external timing"):
        vie.external_real(run)


def test_a_run_at_exactly_the_external_ceiling_is_refused() -> None:
    """`/usr/bin/time -p` rounds to two decimals, so equality already exceeded it."""
    runs = _runs(1, real=vie.EXTERNAL_MAX_SECONDS)
    with pytest.raises(vie.VerificationFailure, match="at or over"):
        vie.check_external_budget(runs)


def test_a_borrowed_phase_budget_is_refused() -> None:
    """An underrun in one phase does not pay for an overrun in another."""
    runs = _runs(1)
    runs[0]["inner_summary"]["provisioning_seconds"] = vie.PROVISIONING_MAX_SECONDS + 0.1
    runs[0]["inner_summary"]["internal_total_seconds"] = 10.0
    with pytest.raises(vie.VerificationFailure, match="non-borrowable"):
        vie.check_phase_deadlines(runs)


def test_a_non_canonical_command_is_refused() -> None:
    """A selector means the measured set was not the committed set."""
    runs = _runs()
    runs[1]["command"] = ["make", "test-integration", "-k", "smoke"]
    with pytest.raises(vie.VerificationFailure, match="non-canonical command"):
        vie.check_canonical_commands(
            {"canonical_command": ["make", "test-integration"], "provider": "devstack"},
            runs,
            provider="devstack",
        )


def test_a_lifecycle_binding_naming_no_present_record_is_refused(tmp_path) -> None:
    """Without the checksum bound, any lifecycle result could be paired after the fact."""
    (tmp_path / "lifecycle-run.json").write_text("{}", encoding="utf-8")
    with pytest.raises(vie.VerificationFailure, match="matches the bound checksum"):
        vie.check_lifecycle_binding({"lifecycle_checksum": "b" * 64}, tmp_path)


def test_an_unbound_lifecycle_result_is_refused(tmp_path) -> None:
    with pytest.raises(vie.VerificationFailure, match="does not bind the lifecycle"):
        vie.check_lifecycle_binding({}, tmp_path)
