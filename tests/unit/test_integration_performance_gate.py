"""Provenance and control-plane behaviour for the outer performance gate.

Two properties dominate this module, and both are about failing *before*
anything is measured or mutated.

Provenance decides which repository and which commit a number describes. Git
takes that answer from the environment, so a controller that inherited
``GIT_DIR`` would happily certify a measurement against a tree nobody ran.
These cases build real repositories and point real redirections at them,
because the interesting question is not whether a string was filtered but
whether Git actually answered about somewhere else.

Controls decide whether a child is allowed to run at all. Every rejection path
the contract names is exercised here — missing, wrong-HMAC, expired, replayed,
cross-sequence — and each one must fail with no side effect, because a child
that got as far as provisioning a database has already changed the thing it was
about to measure.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # noqa: S404 - these cases build real repositories, because the question is what git actually answers
import sys
import tempfile
import time
from pathlib import Path

import pytest
from integration_control import (
    BROKER_ENDPOINT_VARIABLE,
    CONTROL_ENVIRONMENT_VARIABLE,
    INHERITED_CONTROL_VARIABLE,
    REQUIRED_MODE,
    Broker,
    BrokerServer,
    BrokerUnavailable,
    ControlRejected,
    LeaseError,
    acquire_lease,
    canonical_payload,
    control_digest,
    issue,
    new_sequence_secret,
    present_control,
    reject_inherited_control,
    release_lease,
)

import scripts.run_integration_performance_gate as gate
from scripts.integration_evidence import EvidenceError, parse_time_file
from scripts.integration_provenance import (
    IGNORED_OUTPUT_PREFIX,
    DirtyTree,
    ProvenanceError,
    attempted_git_variables,
    bind_commit,
    child_environment,
    git_environment,
    host_digest,
    open_git,
    reject_inherited_git,
)
from scripts.integration_scheduler import EXTERNAL_MAX_SECONDS, TERMINATION_GRACE_SECONDS

# --------------------------------------------------------------------------
# Repository fixtures
# --------------------------------------------------------------------------


def make_repository(path: Path, *, filename: str = "tracked.txt") -> str:
    """A real repository with one real commit. Returns the commit ID.

    Real rather than mocked: every assertion below is about what Git answers,
    and a fake that answered correctly would prove only that the fake was
    written to agree with the test.
    """
    path.mkdir(parents=True, exist_ok=True)

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.email=gate@test.local",
                "-c",
                "user.name=gate",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    git("init", "-q", "-b", "main")
    (path / filename).write_text("content\n", encoding="utf-8")
    (path / ".gitignore").write_text(f"{IGNORED_OUTPUT_PREFIX}\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return git("rev-parse", "HEAD").strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "product"
    commit = make_repository(root)
    return root, commit


# --------------------------------------------------------------------------
# Inherited Git is refused at entry, by name and without values
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"])
def test_any_inherited_git_variable_is_refused(variable: str) -> None:
    with pytest.raises(ProvenanceError, match=variable):
        reject_inherited_git({variable: "/somewhere/else/.git", "PATH": "/usr/bin"})


def test_a_redirection_is_refused_even_when_it_points_at_a_clean_real_repository(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    """The redirection target being *valid* is what makes this dangerous.

    A broken ``GIT_DIR`` fails loudly on its own. One aimed at a real, clean,
    committed repository produces perfectly good answers — about the wrong
    tree. Provenance would then record a commit and a clean status that both
    describe somewhere nobody measured.
    """
    root, _ = repository
    decoy = tmp_path / "decoy"
    make_repository(decoy)

    with pytest.raises(ProvenanceError, match="GIT_DIR"):
        open_git({"GIT_DIR": str(decoy / ".git"), "PATH": os.environ.get("PATH", "")}, expected_root=root)


def test_the_attempted_names_are_reported_and_their_values_are_not() -> None:
    """A rejected attempt is still an attempt to point somewhere. Repeating
    the path in an error message carries it to every reader of the log."""
    secret_path = "/var/attacker-controlled-repository/.git"

    with pytest.raises(ProvenanceError) as raised:
        reject_inherited_git({"GIT_DIR": secret_path})

    assert "GIT_DIR" in str(raised.value)
    assert secret_path not in str(raised.value)


def test_every_attempted_variable_is_named_not_only_the_first() -> None:
    assert attempted_git_variables({"GIT_WORK_TREE": "a", "GIT_DIR": "b", "PATH": "/usr/bin"}) == (
        "GIT_DIR",
        "GIT_WORK_TREE",
    )


def test_a_measured_child_is_launched_with_no_git_variable_whatsoever() -> None:
    """No carve-out, because the runner refuses on presence and not on name.

    The assertion this replaces exempted `GIT_TERMINAL_PROMPT` from a check
    named for carrying no Git variable through, and the controller set that
    variable into every child it launched. The child refused before collection
    and the sequence voided at its first child, so the gate could not run at
    all -- while a test called "carries no git variable through" stayed green.
    """
    child = child_environment({"GIT_DIR": "/x", "GIT_AUTHOR_NAME": "y", "GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin"})

    assert not [name for name in child if name.startswith("GIT_")]
    assert child["PATH"] == "/usr/bin"


def test_the_controllers_own_git_calls_still_refuse_to_prompt() -> None:
    """The other half of the split: a prompt inside a timed sequence reads as
    a slow run, so the controller's own Git environment keeps the variable."""
    git = git_environment({"GIT_DIR": "/x", "PATH": "/usr/bin"})

    assert git["GIT_TERMINAL_PROMPT"] == "0"
    assert not [name for name in git if name.startswith("GIT_") and name != "GIT_TERMINAL_PROMPT"]
    assert git["PATH"] == "/usr/bin"


def test_the_runner_accepts_the_environment_the_controller_builds_for_it() -> None:
    """The two halves of the contract, asserted in one place.

    Both sides were individually correct: the controller had a reason to set
    `GIT_TERMINAL_PROMPT` and the runner had a reason to refuse every `GIT_*`
    on presence. Nothing ran them together, so they disagreed for as long as
    they existed and the disagreement surfaced only as a voided sequence nine
    seconds in. This calls the real qualification against the real builder, so
    a future variable added to either side cannot go unnoticed by both.
    """
    from run_integration_tests import forbidden_variables, qualify

    # A real ambient environment rather than a hand-picked dict, so a variable
    # the controller starts forwarding later is covered without anyone
    # remembering to add it here. `PYTEST_*` is dropped because this process is
    # a pytest run and a controller is not: those two names are the harness's
    # own contamination, and the runner is right to refuse them.
    ambient = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}

    built = child_environment({**ambient, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"})

    # Named separately from the call below because the two say different
    # things: this one names which channels the child would have been refused
    # for, so a regression reads as a variable rather than as an exception.
    assert forbidden_variables(built) == ()
    assert qualify(built, []) is None


# --------------------------------------------------------------------------
# The top level must be the root we were told to measure
# --------------------------------------------------------------------------


def test_the_verified_top_level_matches_the_canonical_root(repository: tuple[Path, str]) -> None:
    root, _ = repository

    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    assert context.root == root.resolve()


def test_a_subdirectory_cannot_pass_itself_off_as_the_product_root(repository: tuple[Path, str]) -> None:
    """Git answers about the enclosing repository from anywhere inside it, so
    without this comparison a nested path would qualify as a top level."""
    root, _ = repository
    nested = root / "contextplane"
    nested.mkdir()

    with pytest.raises(ProvenanceError, match="top level"):
        open_git({"PATH": os.environ.get("PATH", "")}, expected_root=nested)


# --------------------------------------------------------------------------
# Clean-tree checkpoints
# --------------------------------------------------------------------------


def test_a_clean_tree_passes_its_checkpoint(repository: tuple[Path, str]) -> None:
    root, _ = repository
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    assert context.assert_clean(checkpoint="before-sequence") == ()


def test_ignored_evidence_output_is_not_a_dirty_tree(repository: tuple[Path, str]) -> None:
    """The controller writes its own evidence under an ignored directory. A
    gate that called that dirt could never take a second measurement."""
    root, _ = repository
    evidence = root / IGNORED_OUTPUT_PREFIX / "integration-performance" / "run-1"
    evidence.mkdir(parents=True)
    (evidence / "manifest.json").write_text("{}", encoding="utf-8")
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    assert context.assert_clean(checkpoint="after-child") == ()


def test_a_tracked_modification_invalidates_the_checkpoint(repository: tuple[Path, str]) -> None:
    root, _ = repository
    (root / "tracked.txt").write_text("edited\n", encoding="utf-8")
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    with pytest.raises(DirtyTree, match="not clean"):
        context.assert_clean(checkpoint="after-child")


def test_a_non_ignored_untracked_path_invalidates_the_checkpoint(repository: tuple[Path, str]) -> None:
    root, _ = repository
    (root / "stray.py").write_text("x = 1\n", encoding="utf-8")
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    with pytest.raises(DirtyTree, match="not clean"):
        context.assert_clean(checkpoint="after-child")


def test_a_tracked_file_renamed_into_the_ignored_directory_is_still_dirt(repository: tuple[Path, str]) -> None:
    """A rename has two sides and only one of them is permitted. Excusing the
    line on its destination alone would let a tracked file be removed from the
    measured tree by moving it somewhere the checker was told to ignore."""
    root, _ = repository
    destination = root / IGNORED_OUTPUT_PREFIX.rstrip("/")
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(root), "mv", "tracked.txt", f"{IGNORED_OUTPUT_PREFIX}tracked.txt"],
        check=True,
        capture_output=True,
    )
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    with pytest.raises(DirtyTree):
        context.assert_clean(checkpoint="after-child")


# --------------------------------------------------------------------------
# Commit binding, resolved two independent ways
# --------------------------------------------------------------------------


def test_the_expected_commit_and_head_are_resolved_separately_and_agree(repository: tuple[Path, str]) -> None:
    root, commit = repository
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    binding = bind_commit(context, commit)

    assert binding.expected == commit
    assert binding.head == commit
    assert binding.agrees


def test_a_checkout_that_moved_under_the_sequence_is_refused(repository: tuple[Path, str]) -> None:
    """The failure this catches makes the two values differ while either one
    alone still looks entirely reasonable."""
    root, first_commit = repository
    (root / "tracked.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=gate@test.local",
            "-c",
            "user.name=gate",
            "commit",
            "-q",
            "-m",
            "second",
        ],
        check=True,
        capture_output=True,
    )
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    with pytest.raises(ProvenanceError, match="commit drift"):
        bind_commit(context, first_commit)


def test_a_sequence_that_names_no_commit_is_refused(repository: tuple[Path, str]) -> None:
    root, _ = repository
    context = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)

    with pytest.raises(ProvenanceError, match="expected-commit"):
        bind_commit(context, "")


def test_the_host_digest_is_stable_and_carries_no_hostname() -> None:
    assert host_digest() == host_digest()
    assert len(host_digest()) == 32


# --------------------------------------------------------------------------
# Control documents
# --------------------------------------------------------------------------


def bound_fields(**overrides: object) -> dict[str, object]:
    """A complete, valid binding. Overrides express one difference at a time."""
    complete: dict[str, object] = {
        "controller_id": "ctl-1",
        "lease_id": "lease-1",
        "sequence_id": "seq-1",
        "child_sequence": 1,
        "mode": "scale",
        "role": "measured",
        "worker_count": 4,
        "provider": "devstack",
        "expected_commit": "a" * 40,
        "host_digest": "h" * 32,
        "schema_fingerprint": "s" * 16,
        "collection_digest": "c" * 64,
        "command_digest": "d" * 64,
        "nonce": "n" * 32,
        "expires_at": 4_000_000_000.0,
    }
    complete.update(overrides)
    return complete


def expectations_from(bound: dict[str, object]) -> dict[str, object]:
    """What the broker checks: the identity of the child, not its nonce."""
    return {
        name: bound[name]
        for name in ("sequence_id", "mode", "role", "worker_count", "provider", "expected_commit", "collection_digest")
    }


def test_a_control_missing_a_bound_field_cannot_be_canonicalized() -> None:
    incomplete = bound_fields()
    del incomplete["worker_count"]

    with pytest.raises(Exception, match="worker_count"):
        canonical_payload(incomplete)


def test_a_field_nobody_authenticates_is_refused() -> None:
    """An unbound field is one an attacker may set freely — it rides along
    inside the document without being covered by the MAC."""
    with pytest.raises(Exception, match="unbound"):
        canonical_payload(bound_fields(smuggled="anything"))


def test_canonicalization_does_not_depend_on_key_order() -> None:
    forward = bound_fields()
    reversed_order = dict(reversed(list(forward.items())))

    assert canonical_payload(forward) == canonical_payload(reversed_order)


def test_an_issued_control_is_a_regular_file_readable_only_by_its_owner(tmp_path: Path) -> None:
    control = issue(secret=new_sequence_secret(), directory=tmp_path / "controls", bound=bound_fields())

    info = control.path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == REQUIRED_MODE


def test_the_sequence_secret_never_reaches_the_control_file(tmp_path: Path) -> None:
    """A bundle carrying the secret lets anyone holding it mint controls for a
    sequence they never ran."""
    secret = new_sequence_secret()

    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound_fields())

    assert secret.hex() not in control.path.read_text(encoding="utf-8")


def test_evidence_records_the_digest_and_the_bound_fields_but_not_the_mac(tmp_path: Path) -> None:
    """The MAC authenticates future children, so publishing it publishes the
    ability to replay this one."""
    control = issue(secret=new_sequence_secret(), directory=tmp_path / "controls", bound=bound_fields())

    record = control.as_evidence()

    assert record["control_digest"] == control.digest
    assert record["worker_count"] == 4
    assert "nonce" not in record
    assert control.mac not in json.dumps(record)


# --------------------------------------------------------------------------
# Every way a control fails, all of them before collection
# --------------------------------------------------------------------------


def make_broker(tmp_path: Path, secret: bytes, *, now: float = 1_000.0) -> Broker:
    return Broker(secret=secret, consumed_root=tmp_path / "consumed", now=lambda: now)


def test_a_valid_control_authenticates_once(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)

    authenticated = broker.authenticate(control.path, expectations=expectations_from(bound))

    assert authenticated.digest == control.digest
    assert broker.consumed_digests == (control.digest,)


def test_a_missing_control_is_refused(tmp_path: Path) -> None:
    broker = make_broker(tmp_path, new_sequence_secret())

    with pytest.raises(ControlRejected, match="no control"):
        broker.authenticate(tmp_path / "absent.json", expectations={})


def test_an_edited_control_no_longer_authenticates(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    document = json.loads(control.path.read_text(encoding="utf-8"))
    tampered = json.loads(document["payload"])
    tampered["worker_count"] = 1
    document["payload"] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    control.path.write_text(json.dumps(document), encoding="utf-8")
    broker = make_broker(tmp_path, secret)

    with pytest.raises(ControlRejected, match="does not authenticate"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_a_control_minted_under_a_different_secret_is_refused(tmp_path: Path) -> None:
    bound = bound_fields()
    control = issue(secret=new_sequence_secret(), directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, new_sequence_secret())

    with pytest.raises(ControlRejected, match="does not authenticate"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_an_expired_control_is_refused(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields(expires_at=500.0)
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret, now=1_000.0)

    with pytest.raises(ControlRejected, match="expired"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_a_replayed_control_is_refused_on_its_second_presentation(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)
    broker.authenticate(control.path, expectations=expectations_from(bound))

    with pytest.raises(ControlRejected, match="already consumed"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_a_second_broker_cannot_re_admit_a_consumed_control(tmp_path: Path) -> None:
    """Consumption is durable, not per-process. A replay that only had to
    outlive one broker would be a replay that works."""
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    make_broker(tmp_path, secret).authenticate(control.path, expectations=expectations_from(bound))

    with pytest.raises(ControlRejected, match="already consumed"):
        make_broker(tmp_path, secret).authenticate(control.path, expectations=expectations_from(bound))


@pytest.mark.parametrize(
    "field,foreign",
    [
        ("sequence_id", "seq-2"),
        ("mode", "hard-gate"),
        ("worker_count", 8),
        ("provider", "testcontainers"),
        ("expected_commit", "b" * 40),
        ("collection_digest", "e" * 64),
    ],
)
def test_a_control_bound_to_a_different_child_is_refused(tmp_path: Path, field: str, foreign: object) -> None:
    """Validly minted under this secret, and still not this child's control."""
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)
    expectations = expectations_from(bound) | {field: foreign}

    with pytest.raises(ControlRejected, match="different child"):
        broker.authenticate(control.path, expectations=expectations)


def test_a_cross_sequence_control_is_not_consumed_by_the_failed_attempt(tmp_path: Path) -> None:
    """A binding failure must not burn the control. It is a legitimate
    authorization that arrived at the wrong child, and the child it belongs to
    still has to be able to present it."""
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)
    with pytest.raises(ControlRejected):
        broker.authenticate(control.path, expectations=expectations_from(bound) | {"sequence_id": "seq-2"})

    authenticated = broker.authenticate(control.path, expectations=expectations_from(bound))

    assert authenticated.digest == control.digest


def test_a_control_the_group_can_read_is_refused(tmp_path: Path) -> None:
    """Anyone who can read it can replay it into their own child."""
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    control.path.chmod(0o640)
    broker = make_broker(tmp_path, secret)

    with pytest.raises(ControlRejected, match="mode"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_a_control_reached_through_a_symlink_is_refused(tmp_path: Path) -> None:
    """Its real contents are controlled by whoever owns the link target."""
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    link = tmp_path / "link.json"
    link.symlink_to(control.path)
    broker = make_broker(tmp_path, secret)

    with pytest.raises(ControlRejected, match="regular file"):
        broker.authenticate(link, expectations=expectations_from(bound))


def test_nothing_is_admitted_once_the_sequence_has_closed_admission(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)
    broker.close_admission()

    with pytest.raises(ControlRejected, match="admission is closed"):
        broker.authenticate(control.path, expectations=expectations_from(bound))


def test_an_inherited_control_channel_is_refused() -> None:
    with pytest.raises(ControlRejected, match=INHERITED_CONTROL_VARIABLE):
        reject_inherited_control({INHERITED_CONTROL_VARIABLE: "/var/somebody-elses-control.json"})


def test_the_control_variable_a_child_reads_is_the_one_the_runner_forwards() -> None:
    """Three components have to agree on this name or a legitimate child looks
    like an unauthorized one."""
    from scripts.run_integration_tests import _CHILD_ALLOWLIST

    assert CONTROL_ENVIRONMENT_VARIABLE in _CHILD_ALLOWLIST


def test_a_consumed_marker_records_a_digest_and_not_the_control(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    bound = bound_fields()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=bound)
    broker = make_broker(tmp_path, secret)
    broker.authenticate(control.path, expectations=expectations_from(bound))

    markers = list((tmp_path / "consumed").iterdir())

    assert [marker.name for marker in markers] == [f"{control_digest(control.payload)}.consumed"]
    assert markers[0].read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------
# The exclusive provider lease
# --------------------------------------------------------------------------


def test_one_sequence_owns_the_provider(tmp_path: Path) -> None:
    acquire_lease(tmp_path, provider="devstack")

    with pytest.raises(LeaseError, match="already leased"):
        acquire_lease(tmp_path, provider="devstack")


def test_a_released_lease_can_be_taken_again(tmp_path: Path) -> None:
    first = acquire_lease(tmp_path, provider="devstack")
    release_lease(first)

    second = acquire_lease(tmp_path, provider="devstack")

    assert second.lease_id != first.lease_id


def test_two_providers_are_leased_independently(tmp_path: Path) -> None:
    """Holding devstack must not block a parity sequence on testcontainers."""
    acquire_lease(tmp_path, provider="devstack")

    parity = acquire_lease(tmp_path, provider="testcontainers")

    assert parity.lease_id


# --------------------------------------------------------------------------
# The outer controller: what a caller may not change
# --------------------------------------------------------------------------


def test_the_scale_candidates_are_not_reachable_from_the_command_line() -> None:
    """A caller who could choose the candidates could choose the one that
    passes, which is the whole result the sequence exists to establish."""
    parser = gate.build_parser()

    arguments = parser.parse_args(["scale", "--evidence-root", "run/x", "--expected-commit", "a" * 40])

    assert not hasattr(arguments, "workers")
    assert gate.SCALE_CANDIDATES == (1, 2, 4, 8)


@pytest.mark.parametrize("mode", ["scale", "hard-gate"])
def test_every_mode_requires_an_expected_commit(mode: str) -> None:
    """A sequence that names no commit certifies nothing, and there is no
    sensible default to fall back on."""
    with pytest.raises(SystemExit):
        gate.build_parser().parse_args([mode, "--evidence-root", "run/x"])


def test_a_scale_sequence_is_one_warm_up_and_three_measured_runs_per_candidate() -> None:
    plans = gate.scale_plans("devstack")

    assert len(plans) == len(gate.SCALE_CANDIDATES) * (gate.MEASURED_RUNS + 1)
    assert [plan.child_sequence for plan in plans] == list(range(1, len(plans) + 1))
    for count in gate.SCALE_CANDIDATES:
        roles = [plan.role for plan in plans if plan.worker_count == count]
        assert roles == ["warm-up", "measured-1", "measured-2", "measured-3"]


def test_the_warm_up_is_not_a_measured_run() -> None:
    """The first run of anything pays for a cold page cache and a cold pool.
    Paying that inside a measured run makes the candidate look worse than the
    system it stands for."""
    plans = gate.scale_plans("devstack")

    assert [plan.measured for plan in plans[:4]] == [False, True, True, True]


def test_provider_parity_is_exactly_one_explicit_testcontainers_child() -> None:
    plans = gate.parity_plans(4)

    assert len(plans) == 1
    assert plans[0].provider == "testcontainers"
    assert plans[0].deadline_seconds == gate.PARITY_TIMEOUT_SECONDS


def test_parity_gets_an_operational_deadline_rather_than_the_performance_ceiling() -> None:
    """A parity run over 60 seconds is a complete, usable result. Applying the
    performance boundary to it would fail a run that measured what it was
    asked to measure."""
    assert gate.parity_plans(4)[0].deadline_seconds > EXTERNAL_MAX_SECONDS
    assert gate.scale_plans("devstack")[0].deadline_seconds == EXTERNAL_MAX_SECONDS


def test_the_canonical_command_carries_no_selector_or_worker_override() -> None:
    command = gate.canonical_command("devstack")

    assert command == ("env", "CONTEXTPLANE_TEST_PG=devstack", "make", "test-integration")
    assert not [part for part in command if part.startswith("-")]


def test_the_resolved_command_is_the_exact_timed_make_invocation(tmp_path: Path) -> None:
    command = gate.resolved_command("devstack", time_file=tmp_path / "t.txt", control=tmp_path / "c.json")

    assert command[:2] == ["/usr/bin/time", "-p"]
    assert command[-2:] == ["make", "test-integration"]
    assert f"{CONTROL_ENVIRONMENT_VARIABLE}={tmp_path / 'c.json'}" in command


def test_the_command_digest_does_not_vary_between_identical_children(tmp_path: Path) -> None:
    """Per-child paths differ by construction. A digest that included them
    would vary between children the digest exists to prove were identical."""
    assert gate.command_digest("devstack") == gate.command_digest("devstack")
    assert gate.command_digest("devstack") != gate.command_digest("testcontainers")


@pytest.mark.parametrize("variable", ["PYTEST", "PYTHON", "MAKEFLAGS", "MAKEFILES", "GIT_DIR"])
def test_a_forbidden_channel_at_controller_entry_is_refused(variable: str) -> None:
    with pytest.raises(gate.GateError, match=variable):
        gate.qualify_controller({variable: "true", "PATH": "/usr/bin"})


def test_the_controller_refuses_an_inherited_control() -> None:
    """Refused by name, whichever layer names it first.

    The channel appears in both the runner's forbidden set and the control
    layer's own check, and which one fires is an implementation detail. What
    must not vary is that presenting somebody else's control refuses the
    invocation rather than overriding it silently.
    """
    with pytest.raises((gate.GateError, ControlRejected), match=INHERITED_CONTROL_VARIABLE):
        gate.qualify_controller({INHERITED_CONTROL_VARIABLE: "/var/elsewhere.json"})


def test_a_clean_controller_invocation_is_not_refused() -> None:
    """The negative control, paired with its own positive.

    On its own, either half is satisfiable by a broken implementation: a
    function that refuses everything passes every case above, and one that
    refuses nothing passes this one. The pair pins both directions against the
    same environment, differing only in the tampering.
    """
    clean = {"PATH": "/usr/bin", "HOME": "/home/x"}

    assert gate.qualify_controller(clean) is None
    with pytest.raises(gate.GateError, match="PYTEST"):
        gate.qualify_controller({**clean, "PYTEST": "true"})


# --------------------------------------------------------------------------
# The committed worker default
# --------------------------------------------------------------------------


def test_the_hard_gate_reads_the_worker_count_the_repository_committed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.contextplane.integration]\nworkers = 4\n", encoding="utf-8")

    assert gate.committed_worker_count(tmp_path) == 4


def test_a_repository_with_no_committed_default_cannot_run_the_hard_gate(tmp_path: Path) -> None:
    """The hard gate is defined as a measurement of what is committed, so
    there is nothing to measure until a scale sequence has selected one."""
    (tmp_path / "pyproject.toml").write_text("[tool.other]\nx = 1\n", encoding="utf-8")

    with pytest.raises(gate.GateError, match="no committed worker default"):
        gate.committed_worker_count(tmp_path)


def test_a_nonsense_committed_default_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.contextplane.integration]\nworkers = 0\n", encoding="utf-8")

    with pytest.raises(gate.GateError, match="positive integer"):
        gate.committed_worker_count(tmp_path)


# --------------------------------------------------------------------------
# External timing, read only after the child has exited
# --------------------------------------------------------------------------


def test_external_timing_is_read_from_the_completed_time_file(tmp_path: Path) -> None:
    path = tmp_path / "external-time.txt"
    path.write_text("real 12.34\nuser 5.67\nsys 1.23\n", encoding="utf-8")

    timing = parse_time_file(path)

    assert timing.real == pytest.approx(12.34)
    assert timing.as_evidence()["external_real_seconds"] == pytest.approx(12.34)


def test_a_truncated_time_file_is_a_refusal_rather_than_a_number(tmp_path: Path) -> None:
    """The failure mode this prevents is not a missing measurement but a wrong
    one: a half-written `real` that still parses."""
    path = tmp_path / "external-time.txt"
    path.write_text("real 12.34\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="incomplete"):
        parse_time_file(path)


def test_a_missing_time_file_is_a_refusal(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError, match="no timing file"):
        parse_time_file(tmp_path / "absent.txt")


# --------------------------------------------------------------------------
# The deadline reaches the whole process group
# --------------------------------------------------------------------------


def test_a_child_that_outlives_its_deadline_takes_its_whole_tree_down(tmp_path: Path) -> None:
    """A real process group, because the bug this guards against is invisible
    in-process.

    `make` spawns pytest which spawns workers. Signalling only the process the
    controller can see leaves that tree alive past the deadline, still holding
    the database the next child is about to take — and the next child then
    measures the previous one's contention as its own cost.
    """
    script = tmp_path / "spawner.py"
    marker = tmp_path / "grandchild-alive.txt"
    # A parent that spawns a grandchild and then sleeps. Killing the parent
    # alone leaves the grandchild running and the marker growing.
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"\"import time\\nwhile True:\\n open(r'{marker}', 'a').write('x')\\n time.sleep(0.05)\"])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.0)
    assert marker.exists(), "the grandchild never started; the case would prove nothing"

    gate._terminate_group(process, grace_seconds=0.5)

    size_at_termination = marker.stat().st_size
    time.sleep(0.6)
    assert marker.stat().st_size == size_at_termination, "the grandchild outlived the deadline"


def test_terminating_an_already_dead_group_is_not_an_error() -> None:
    """A child that exited on its own between the timeout firing and the
    signal being sent is the ordinary race, not a failure to report."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait()

    gate._terminate_group(process, grace_seconds=0.1)

    # It returned rather than raising ProcessLookupError, and the child it was
    # asked about is still reaped. A controller that raised here would turn an
    # ordinary race into a void sequence.
    assert process.poll() == 0


def test_the_termination_grace_never_exceeds_five_hundred_milliseconds() -> None:
    assert TERMINATION_GRACE_SECONDS == 0.5


# --- presenting a control across a real process boundary ----------------------
#
# The child cannot hold the sequence secret, so it cannot check its own
# control's MAC. These cases run a real broker on a real Unix socket and assert
# that the authenticated reply is the only thing that lets a child proceed, and
# that the worker count reaches it through that reply rather than through argv.


def _bound(**overrides: object) -> dict[str, object]:
    bound = {
        "controller_id": "controller-1",
        "lease_id": "lease-1",
        "sequence_id": "sequence-1",
        "child_sequence": 1,
        "mode": "scale",
        "role": "measured",
        "worker_count": 4,
        "provider": "devstack",
        "expected_commit": "a" * 40,
        "host_digest": "host",
        "schema_fingerprint": "schema",
        "collection_digest": "collection",
        "command_digest": "command",
    }
    bound.update(overrides)
    return bound


def _serving(tmp_path: Path, secret: bytes, expectations: dict[str, object]) -> BrokerServer:
    broker = Broker(secret=secret, consumed_root=tmp_path / "consumed")
    # Short socket path: a Unix socket path is length-limited well below what a
    # pytest tmp_path can reach, and the failure is an unrelated OSError.
    endpoint = Path(tempfile.mkdtemp()) / "broker.sock"
    return BrokerServer(broker=broker, path=endpoint, expectations=expectations)


def test_a_child_learns_its_worker_count_from_the_authenticated_reply(tmp_path: Path) -> None:
    """The count cannot come from argv: the canonical command is identical
    across candidates, so the control is the only channel that differs."""
    secret = new_sequence_secret()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=_bound())
    with _serving(tmp_path, secret, {"sequence_id": "sequence-1", "mode": "scale"}) as server:
        returned = present_control(server.path, control.path)
    assert returned["worker_count"] == 4
    assert "mac" not in returned


class _Captured(Exception):
    """Stops `execute_sequence` at the one call this case is about."""


def test_the_controller_hands_each_child_a_broker_to_authenticate_against(
    tmp_path: Path, repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the controller actually builds, not what a test can build by hand.

    Every other control-plane case here calls `present_control` directly, so
    all of them passed while the controller stood up no socket at all: it
    minted a control, put its path in the child's environment, and left
    `CONTEXTPLANE_INTEGRATION_BROKER` unset. Two sealed sequences voided at
    child 1 before a test ran, and no unit test could have caught it, because
    none of them ever asked what `execute_sequence` puts in a child's
    environment.

    So this intercepts the real `run_child` and asserts on the environment the
    real `execute_sequence` assembled. Asserting that a hand-built environment
    is accepted would pass identically against the broken controller.
    """
    root, commit = repository
    git = open_git({"PATH": os.environ.get("PATH", "")}, expected_root=root)
    sequence = gate.SequenceRun(
        mode="scale", provider="devstack", evidence_root=tmp_path / "evidence", expected_commit=commit, git=git
    )
    plan = gate.ChildPlan(
        child_sequence=1, mode="scale", role="warm-up", worker_count=4, provider="devstack", deadline_seconds=60.0
    )

    seen: dict[str, str] = {}

    def _capture(_plan: object, **kwargs: object) -> None:
        seen.update(dict(kwargs["environment"]))  # type: ignore[arg-type]
        raise _Captured

    monkeypatch.setattr(gate, "run_child", _capture)

    secret = new_sequence_secret()
    broker = Broker(secret=secret, consumed_root=tmp_path / "consumed")
    endpoint = Path(tempfile.mkdtemp()) / "broker.sock"
    lease = acquire_lease(tmp_path / "leases", provider="devstack")
    try:
        with BrokerServer(broker=broker, path=endpoint) as server, pytest.raises(_Captured):
            gate.execute_sequence(
                sequence,
                [plan],
                lease=lease,
                secret=secret,
                environment={"PATH": os.environ.get("PATH", "")},
                product_root=root,
                server=server,
            )
    finally:
        release_lease(lease)

    assert seen[BROKER_ENDPOINT_VARIABLE] == str(endpoint)


def test_a_child_given_both_halves_by_the_controller_is_authorized(tmp_path: Path) -> None:
    """The other half: what the runner does with both variables once it has them."""
    from run_integration_tests import authorize

    secret = new_sequence_secret()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=_bound())

    with _serving(tmp_path, secret, {"sequence_id": "sequence-1", "mode": "scale"}) as server:
        environment = {
            CONTROL_ENVIRONMENT_VARIABLE: str(control.path),
            BROKER_ENDPOINT_VARIABLE: str(server.path),
        }
        authorized = authorize(environment)

    assert authorized is not None
    assert authorized["worker_count"] == 4


def test_a_child_given_only_a_control_refuses_rather_than_running_unauthorized(tmp_path: Path) -> None:
    """The discriminator, and it is the exact failure both voided sequences hit.

    Without it the case above could be satisfied by a runner that ignored the
    endpoint entirely. A half-configured child is refused rather than
    downgraded to an unauthenticated run, because the run that proceeds without
    an answer is the one whose evidence means nothing.
    """
    from run_integration_tests import authorize

    control = issue(secret=new_sequence_secret(), directory=tmp_path / "controls", bound=_bound())

    with pytest.raises(ControlRejected, match="is not authorization"):
        authorize({CONTROL_ENVIRONMENT_VARIABLE: str(control.path)})


def test_the_broker_refuses_a_control_minted_under_a_different_secret(tmp_path: Path) -> None:
    forged = issue(secret=new_sequence_secret(), directory=tmp_path / "controls", bound=_bound())
    with _serving(tmp_path, new_sequence_secret(), {}) as server, pytest.raises(ControlRejected, match="authenticate"):
        present_control(server.path, forged.path)


def test_a_control_is_consumed_once_and_a_replay_is_refused(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=_bound())
    with _serving(tmp_path, secret, {}) as server:
        present_control(server.path, control.path)
        with pytest.raises(ControlRejected, match="already consumed"):
            present_control(server.path, control.path)


def test_a_control_bound_to_another_sequence_is_refused(tmp_path: Path) -> None:
    secret = new_sequence_secret()
    control = issue(secret=secret, directory=tmp_path / "controls", bound=_bound(sequence_id="somebody-elses"))
    with _serving(tmp_path, secret, {"sequence_id": "sequence-1"}) as server:
        with pytest.raises(ControlRejected, match="different child"):
            present_control(server.path, control.path)


def test_an_unreachable_broker_is_a_refusal_rather_than_a_shrug(tmp_path: Path) -> None:
    """A sequence must not run unauthorized because the broker died."""
    with pytest.raises(BrokerUnavailable, match="unreachable"):
        present_control(tmp_path / "nothing.sock", tmp_path / "control.json")
