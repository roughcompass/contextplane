"""Single-use authenticated controls, and the one lease that owns the provider.

A measured child has to prove three things before it is allowed to touch the
database or collect a single test: that this controller issued it, that it is
the child the controller thinks it is, and that it has not run before. Nothing
weaker works. A flag says only that somebody passed a flag; a file on disk says
only that a file exists; neither survives a second process picking it up.

So every child gets its own control: a small document binding everything that
makes the child unique, authenticated with HMAC-SHA-256 under a secret that
exists only in this process's memory, written as a regular mode-0600 file, and
atomically consumed exactly once before collection.

**The secret never reaches disk, and neither does the control's content.** What
evidence records is each control's digest and its non-secret bound fields —
enough to prove later that a specific child ran under a specific authorization,
useless for forging another one. A bundle carrying the secret would let anyone
holding the bundle mint controls for a sequence they never ran.

**Consumption is a filesystem-atomic operation, not a check-then-write.** The
broker creates a marker with `O_EXCL`; the create either wins or raises, with
no window between deciding and recording. A read-then-write would let two
children racing on the same control both observe "unconsumed" and both proceed,
which is precisely the replay this defends against.

Five ways a control fails, all of them before collection and before any
provider mutation, because a run that got as far as creating a database has
already changed the thing it was measuring:

- **Missing** — no path, or a path naming nothing.
- **Wrong HMAC** — content edited, or minted under a different secret.
- **Expired** — issued for a child that should have started long ago.
- **Replayed** — already consumed, by this child or another.
- **Cross-sequence** — validly minted, but for a different sequence, mode,
  candidate, provider, commit, or collection than the one now running.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: How long a control stays valid after issue. Generous enough that a slow
#: `make` startup never invalidates a legitimate child, short enough that a
#: control recovered from a previous sequence's tree is dead on arrival.
DEFAULT_TTL_SECONDS: Final = 900.0

#: Mode the control file must have, exactly. Not "at most" — a control readable
#: by the group is a control anyone in that group can replay into their own
#: child, and permissions that drifted open are worth failing on.
REQUIRED_MODE: Final = 0o600

#: The environment variable a child reads to find its control. Named here so
#: the controller, the broker, and the runner cannot disagree about it.
CONTROL_ENVIRONMENT_VARIABLE: Final = "CONTEXTPLANE_INTEGRATION_CONTROL"

#: An inherited control is one this controller did not issue for this child.
#: Refused by name so the attempt is visible rather than silently overridden.
INHERITED_CONTROL_VARIABLE: Final = "CONTEXTPLANE_INTEGRATION_CONTROL_INHERITED"

_BOUND_FIELDS: Final = (
    "controller_id",
    "lease_id",
    "sequence_id",
    "child_sequence",
    "mode",
    "role",
    "worker_count",
    "provider",
    "expected_commit",
    "host_digest",
    "schema_fingerprint",
    "collection_digest",
    "command_digest",
    "nonce",
    "expires_at",
)


class ControlError(RuntimeError):
    """A control cannot be issued, or must not be honoured."""


class ControlRejected(ControlError):
    """Authentication failed. The child must not collect or provision."""


class LeaseError(RuntimeError):
    """The exclusive provider/sequence lease could not be held."""


def new_sequence_secret() -> bytes:
    """A fresh secret per sequence, held in memory and never serialized."""
    return secrets.token_bytes(32)


def canonical_payload(bound: Mapping[str, Any]) -> str:
    """The exact bytes that get authenticated.

    Sorted keys and no incidental whitespace, so the same logical control
    always produces the same MAC. A canonicalization that varied would make a
    legitimate control fail verification on a different Python build, and the
    obvious "fix" for that is to stop verifying.
    """
    missing = [name for name in _BOUND_FIELDS if name not in bound]
    if missing:
        msg = f"control is missing bound field(s): {', '.join(missing)}"
        raise ControlError(msg)
    extra = [name for name in bound if name not in _BOUND_FIELDS]
    if extra:
        # A field nobody authenticates is a field an attacker may set freely.
        msg = f"control carries unbound field(s): {', '.join(sorted(extra))}"
        raise ControlError(msg)
    return json.dumps({name: bound[name] for name in _BOUND_FIELDS}, sort_keys=True, separators=(",", ":"))


def _mac(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def control_digest(payload: str) -> str:
    """What evidence records. A digest of the content, not the content."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Control:
    """One child's authorization, and the only thing safe to write down.

    `bound` is non-secret by construction — every field in it is an identifier,
    a count, or a digest. The secret is not a member here at all, so a Control
    cannot leak one by being serialized.
    """

    bound: Mapping[str, Any]
    payload: str
    mac: str
    path: Path

    @property
    def digest(self) -> str:
        return control_digest(self.payload)

    def as_evidence(self) -> dict[str, Any]:
        """Digest plus bound fields. Never the MAC — it authenticates future
        children, so publishing it publishes the ability to replay this one."""
        record = {name: self.bound[name] for name in _BOUND_FIELDS if name != "nonce"}
        record["control_digest"] = self.digest
        return record


def issue(
    *,
    secret: bytes,
    directory: Path,
    bound: Mapping[str, Any],
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: Callable[[], float] = time.time,
) -> Control:
    """Mint one control and write it mode-0600.

    Created with `O_EXCL` at mode 0600 rather than written and then chmod'd:
    the two-step version leaves a window in which the file exists with the
    process umask's permissions, and a window is all a reader needs.
    """
    complete = dict(bound)
    complete.setdefault("nonce", secrets.token_hex(16))
    complete.setdefault("expires_at", round(now() + ttl_seconds, 6))
    payload = canonical_payload(complete)
    mac = _mac(secret, payload)

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"control-{complete['child_sequence']:03d}-{control_digest(payload)[:12]}.json"
    document = json.dumps({"payload": payload, "mac": mac}, separators=(",", ":"))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, REQUIRED_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Control(bound=complete, payload=payload, mac=mac, path=path)


@dataclass
class Broker:
    """Authenticates and consumes controls, once each, before collection.

    Holds the sequence secret in memory for the life of the sequence. The
    consumed-marker directory is the only durable state, and it records digests
    rather than controls: a marker proves a control ran without being usable to
    reconstruct it.

    `admission_open` models the contract's "close broker admission": once the
    controller has taken the lease and started the sequence, nothing outside
    that sequence may present a control at all. It is checked before
    authentication so that a foreign control is refused on the grounds that
    nothing was being admitted, rather than on the grounds that it failed a MAC
    comparison — which would tell the presenter their MAC was the problem.
    """

    secret: bytes
    consumed_root: Path
    now: Callable[[], float] = time.time
    admission_open: bool = True
    _consumed: set[str] = field(default_factory=set, init=False)

    def close_admission(self) -> None:
        self.admission_open = False

    def authenticate(self, path: Path, *, expectations: Mapping[str, Any]) -> Control:
        """Verify, then atomically consume. Raises on every failure mode.

        Order matters and is not arbitrary: shape, then authenticity, then
        expiry, then binding, then single-use. Consuming before the binding
        check would burn a legitimate control that arrived at the wrong child;
        checking the binding before the MAC would leak which fields a forged
        control got right.
        """
        if not self.admission_open:
            msg = "broker admission is closed for this sequence; no further control may be presented"
            raise ControlRejected(msg)

        document = self._load(path)
        payload = document.get("payload")
        mac = document.get("mac")
        if not isinstance(payload, str) or not isinstance(mac, str):
            msg = f"control at {path} is malformed: expected a payload and a mac"
            raise ControlRejected(msg)

        # compare_digest, not `==`. The timing of a byte-by-byte comparison
        # leaks how much of a guessed MAC was correct.
        if not hmac.compare_digest(mac, _mac(self.secret, payload)):
            msg = f"control at {path} does not authenticate under this sequence's secret"
            raise ControlRejected(msg)

        bound = json.loads(payload)
        expires_at = float(bound["expires_at"])
        if self.now() > expires_at:
            msg = f"control {control_digest(payload)[:12]} expired at {expires_at:.0f}"
            raise ControlRejected(msg)

        mismatched = sorted(
            f"{name}={bound.get(name)!r} (expected {value!r})"
            for name, value in expectations.items()
            if bound.get(name) != value
        )
        if mismatched:
            msg = (
                f"control {control_digest(payload)[:12]} is bound to a different child: "
                + "; ".join(mismatched)
                + ". A control minted for one sequence, mode, candidate, provider, commit, or "
                "collection cannot authorize another."
            )
            raise ControlRejected(msg)

        self._consume(control_digest(payload))
        return Control(bound=bound, payload=payload, mac=mac, path=path)

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            msg = f"no control at {path}; a child must present one before it may collect"
            raise ControlRejected(msg)
        info = path.lstat()
        # lstat, so a symlink is judged as a symlink. A control reached through
        # one is a control whose real contents somebody else controls.
        if not stat.S_ISREG(info.st_mode):
            msg = f"control at {path} is not a regular file"
            raise ControlRejected(msg)
        if stat.S_IMODE(info.st_mode) != REQUIRED_MODE:
            msg = f"control at {path} has mode {stat.S_IMODE(info.st_mode):04o}, not {REQUIRED_MODE:04o}"
            raise ControlRejected(msg)
        try:
            loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            msg = f"control at {path} is unreadable: {error}"
            raise ControlRejected(msg) from error
        return loaded

    def _consume(self, digest: str) -> None:
        """Exclusive-create a marker. Winning the create *is* the consumption.

        There is deliberately no "is it consumed?" read anywhere. A read
        followed by a write has a window between them, and two children racing
        on one control would both read "no" and both proceed.
        """
        self.consumed_root.mkdir(parents=True, exist_ok=True)
        marker = self.consumed_root / f"{digest}.consumed"
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, REQUIRED_MODE)
        except FileExistsError as error:
            msg = (
                f"control {digest[:12]} was already consumed; a control authorizes exactly one "
                "child and this is a replay"
            )
            raise ControlRejected(msg) from error
        os.close(descriptor)
        self._consumed.add(digest)

    @property
    def consumed_digests(self) -> tuple[str, ...]:
        return tuple(sorted(self._consumed))


def reject_inherited_control(environ: Mapping[str, str]) -> None:
    """Refuse a control this controller did not issue for this child.

    Presence is the failure, as everywhere else in this phase. A child that
    quietly ignored an inherited control would run correctly and leave no
    record that somebody tried to authorize it from outside.
    """
    attempted = sorted(
        name for name in environ if name == INHERITED_CONTROL_VARIABLE or name.startswith(INHERITED_CONTROL_VARIABLE)
    )
    if attempted:
        msg = "refusing to run with inherited control channel(s) present: " + ", ".join(attempted)
        raise ControlRejected(msg)


@dataclass(frozen=True)
class Lease:
    """Exclusive ownership of the provider and of the sequence.

    One holder at a time, enforced by `O_EXCL` on the lease file rather than by
    convention. Two sequences sharing a provider would interleave their
    database work and each would measure the other's contention as its own
    cost.
    """

    lease_id: str
    path: Path
    acquired_at: float

    def as_evidence(self) -> dict[str, Any]:
        return {"lease_id": self.lease_id, "acquired_at": round(self.acquired_at, 6)}


def acquire_lease(root: Path, *, provider: str, now: Callable[[], float] = time.time) -> Lease:
    """Take the provider lease, or fail because somebody else holds it.

    Deliberately not a wait-with-timeout. A sequence that queued behind another
    would start measuring the moment the other released, on a machine still
    settling from it, and the contract's whole reason for an exclusive window
    is that this host's timings move with load.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{provider}.lease"
    lease_id = secrets.token_hex(12)
    acquired_at = now()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, REQUIRED_MODE)
    except FileExistsError as error:
        msg = (
            f"provider {provider!r} is already leased at {path}. One sequence owns the provider at "
            "a time; a shared provider makes each sequence measure the other's contention."
        )
        raise LeaseError(msg) from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"lease_id": lease_id, "provider": provider, "acquired_at": acquired_at}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return Lease(lease_id=lease_id, path=path, acquired_at=acquired_at)


def release_lease(lease: Lease) -> None:
    """Release after final publication, never before.

    Holding it through publication is the point: a sequence whose manifest is
    still being written has not finished, and a second sequence starting into
    that window would publish into the same tree.
    """
    lease.path.unlink(missing_ok=True)
