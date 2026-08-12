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
from pathlib import Path

import pytest

from scripts.integration_control import (
    CONTROL_ENVIRONMENT_VARIABLE,
    INHERITED_CONTROL_VARIABLE,
    REQUIRED_MODE,
    Broker,
    ControlRejected,
    LeaseError,
    acquire_lease,
    canonical_payload,
    control_digest,
    issue,
    new_sequence_secret,
    reject_inherited_control,
    release_lease,
)
from scripts.integration_provenance import (
    IGNORED_OUTPUT_PREFIX,
    DirtyTree,
    ProvenanceError,
    attempted_git_variables,
    bind_commit,
    host_digest,
    open_git,
    reject_inherited_git,
    sanitized_environment,
)

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


def test_the_sanitized_environment_carries_no_git_variable_through() -> None:
    sanitized = sanitized_environment({"GIT_DIR": "/x", "GIT_AUTHOR_NAME": "y", "PATH": "/usr/bin"})

    assert not [name for name in sanitized if name.startswith("GIT_") and name != "GIT_TERMINAL_PROMPT"]
    assert sanitized["GIT_TERMINAL_PROMPT"] == "0"
    assert sanitized["PATH"] == "/usr/bin"


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
