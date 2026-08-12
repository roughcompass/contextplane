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
