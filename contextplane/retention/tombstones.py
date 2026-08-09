"""Proving a record was erased without becoming an oracle for what it held.

A tombstone says: this record existed, it occupied this position, and it was
erased on this date under this policy version. It must say that much, because the
chain is append-only and successors point at the erased revision — absence is
detectable by the hole regardless, so claiming non-existence would be dishonest
rather than private.

**The proof is a tenant-keyed HMAC, never a bare content digest.** Erased content
is routinely guessable and low-entropy — a task goal, a source system's name, an
item key naming a document. A bare hash of it lets anyone who can guess the
content confirm the guess, and equal digests reveal equality across two erased
records that were never meant to be comparable. Keying the digest to a secret the
reader does not have removes both. The raw content digest stays internal to chain
verification and never appears on a tombstone or in a disclosure.

**No key material, no tombstone.** Key material is not operator-configurable on
any deployment that exists today, so the resolver ships holding none and every
mint refuses. That refusal is deliberate and is not a stub: writing a proof under
an empty or improvised key would produce a tombstone that verifies against
nothing while looking exactly like one that does, and an erasure whose proof is
worthless is worse than an erasure that visibly did not happen. A deployment that
needs tombstoning erasure needs the operator-configured key material every other
keyed surface in this codebase is already waiting on.

**Destroying the tenant's salt is an erasure of its own.** At tenant offboarding
the salt goes, and with it the ability to derive any proof for that tenant. The
tombstones stay readable as structure — the record existed, at this position, and
was erased — while their keyed metadata stops being derivable by anybody,
including this system. `disclose` says so explicitly rather than emitting a value
it can no longer stand behind.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import uuid
from collections.abc import Mapping
from typing import Protocol

from contextplane.exceptions import RegistryError
from contextplane.retention import policies

#: Domain separation for the per-tenant salt derivation. Two keyed purposes that
#: share a root secret must not produce the same bytes for the same input, or a
#: value minted for one becomes a valid value for the other.
_SALT_LABEL = b"contextplane/retention/tenant-salt/v1"

#: Domain separation for the proof itself, so a proof can never be replayed as a
#: salt derivation or as an erased-key marker.
_PROOF_LABEL = b"contextplane/retention/tombstone-proof/v1"

#: And for the minimized item-key marker.
_ITEM_KEY_LABEL = b"contextplane/retention/erased-item-key/v1"

#: How much of the keyed digest a minimized item key discloses. Twelve hex
#: characters: enough that two different keys are near-certain to differ (so a
#: receipt's lines stay distinguishable from each other after minimization),
#: short enough that the marker is obviously not the key it replaced.
_ERASED_PREFIX_LENGTH = 12

#: The prefix every minimized item key carries, so a reader can tell a minimized
#: value from a real one without knowing what the real one looked like.
ERASED_KEY_PREFIX = "erased:"


class TenantSaltUnavailable(RegistryError):
    """Raised when a tenant's salt cannot be derived, so nothing may be keyed with it.

    Two causes, deliberately one exception: the deployment has no key material at
    all, or this tenant's salt was destroyed at offboarding. Both mean the same
    thing to a caller — there is no key, so produce no keyed value — and a caller
    that could tell them apart would be tempted to treat one as recoverable.
    """


class TenantSaltResolver(Protocol):
    """Where a tenant's pseudonymization salt comes from.

    A protocol rather than a class so the storage-backed resolver a later change
    introduces can replace the shipped one without anything above it changing:
    every caller already asks this question and already handles the refusal.
    """

    def salt_for(self, tenant_id: uuid.UUID) -> bytes:
        """This tenant's salt, or a refusal when there is none to derive."""
        ...


class KeyedTenantSalt:
    """Derives each tenant's salt from operator-configured root key material.

    Derived rather than stored per tenant: a stored salt is a second secret to
    protect, back up and destroy, and the destruction is the part that has to be
    reliable. One root secret plus a domain-separated derivation gives every
    tenant an independent salt with one thing to hold — and destroying a single
    tenant's salt is naming it in `destroyed`, which is what offboarding does.

    Constructed with no key material on every deployment today. That is the
    honest state and not a placeholder: see the module docstring for why an
    improvised key is worse than a refusal.
    """

    def __init__(
        self,
        secrets: Mapping[str, bytes],
        *,
        active_key_id: str | None,
        destroyed: frozenset[uuid.UUID] = frozenset(),
    ) -> None:
        self._secrets = dict(secrets)
        self._active_key_id = active_key_id
        self._destroyed = destroyed

    def salt_for(self, tenant_id: uuid.UUID) -> bytes:
        """Derive this tenant's salt, refusing when there is no key or it was destroyed."""
        if tenant_id in self._destroyed:
            msg = "this tenant's pseudonymization salt was destroyed at offboarding; no keyed value can be derived"
            raise TenantSaltUnavailable(msg)
        if self._active_key_id is None:
            msg = "no active retention key is configured; refusing to derive an unkeyed tenant salt"
            raise TenantSaltUnavailable(msg)
        root = self._secrets.get(self._active_key_id)
        if not root:
            msg = f"retention key {self._active_key_id!r} is active but holds no material"
            raise TenantSaltUnavailable(msg)
        return hmac.new(root, _SALT_LABEL + tenant_id.bytes, hashlib.sha256).digest()


def mint_proof(
    salt: bytes,
    *,
    record_class: str,
    subject_id: uuid.UUID,
    content_digest: str,
    effective_at: datetime.datetime,
    policy_version: str = policies.POLICY_VERSION,
) -> str:
    """The tombstone's keyed proof: what was erased, bound to who and when.

    `content_digest` is the record's internally-held digest and goes *into* the
    HMAC, never onto the tombstone. That is the whole trick: the proof commits to
    the erased content, so a holder of the salt can confirm a tombstone describes
    the record it claims to, while a reader without it learns nothing about the
    content — not even whether two tombstones cover identical content, because
    the record class, id and instant all vary the input.
    """
    message = "|".join(
        (
            policy_version,
            record_class,
            str(subject_id),
            content_digest,
            effective_at.isoformat(),
        )
    ).encode("utf-8")
    return hmac.new(salt, _PROOF_LABEL + message, hashlib.sha256).hexdigest()


def erased_item_key(salt: bytes, item_key: str) -> str:
    """The marker a minimized item key is replaced with.

    Deterministic in the original key, so re-running a minimization writes the
    same value and the retry stays idempotent; keyed, so the determinism is not
    also a lookup table — an attacker holding a candidate item key cannot check it
    against the marker without the tenant's salt.
    """
    digest = hmac.new(salt, _ITEM_KEY_LABEL + item_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ERASED_KEY_PREFIX}{digest[:_ERASED_PREFIX_LENGTH]}"


def is_erased_key(item_key: str) -> bool:
    """Whether this value has already been minimized.

    What makes minimization idempotent: a second pass recognises its own output
    and leaves it alone, instead of keying the marker again and producing a
    different marker on every run.
    """
    return item_key.startswith(ERASED_KEY_PREFIX)


@dataclasses.dataclass(frozen=True)
class Disclosure:
    """What a verifier may say about an erased record.

    Deliberately not the tombstone row. The row holds the reason an operator
    recorded and the authority that asked, and neither is disclosable — the
    reason is often about a person. What ships is the structural facts plus the
    keyed proof, and only while the key still exists.
    """

    record_class: str
    subject_id: uuid.UUID
    erased_at: datetime.datetime
    policy_version: str
    #: None once the tenant's salt is destroyed: the proof is still stored, but
    #: nothing can re-derive it, so publishing it would assert a check that can
    #: no longer be performed.
    proof_hmac: str | None
    #: The policy sentence for this class, so the reader sees the rule that was
    #: applied rather than having to infer it from what is missing.
    verifier_disclosure: str


def disclose(
    *,
    record_class: str,
    subject_id: uuid.UUID,
    erased_at: datetime.datetime,
    policy_version: str,
    proof_hmac: str,
    salt_available: bool,
) -> Disclosure:
    """Build the post-erasure statement: structure and proof, never content.

    Nothing here is derived from the erased content's size, shape or subject
    identity, because each of those is a channel: a length reveals whether a note
    was one word or a paragraph, and a subject identity beyond the derived id
    re-identifies the person the erasure was performed for.
    """
    return Disclosure(
        record_class=record_class,
        subject_id=subject_id,
        erased_at=erased_at,
        policy_version=policy_version,
        proof_hmac=proof_hmac if salt_available else None,
        verifier_disclosure=policies.disposition(record_class).verifier_disclosure,
    )


__all__ = [
    "ERASED_KEY_PREFIX",
    "Disclosure",
    "KeyedTenantSalt",
    "TenantSaltResolver",
    "TenantSaltUnavailable",
    "disclose",
    "erased_item_key",
    "is_erased_key",
    "mint_proof",
]
