"""The one module permitted to write `assertion_provenance`.

Provenance is what makes a governed assertion checkable: which system said it, at
which upstream revision, under which authority, judged against which profile. A
caller that could rewrite that could make a claim appear supported by evidence
that never said it — and unlike a forged assertion, a forged *provenance* leaves
the assertion itself looking untouched.

So the table has exactly one writer, and `scripts/check_privileged_writes.py`
fails the build when a second one appears. That gate is the mechanism; this module
is what it points at.

**There is no update path here, and that is the design.** Provenance is immutable:
an assertion whose evidence changed is a new assertion superseding the old one,
with its own row. A `correct_provenance` function would be indistinguishable at
the row level from the forgery the single-writer rule exists to prevent, because
both produce a row whose contents no longer match what was originally observed.
`supersede` below writes the replacement and returns both ids, so the caller's
assertion row can point at the new one while the old row stays exactly as
written.

**Writes join the caller's transaction.** Provenance that committed separately
from the assertion it describes could outlive a rolled-back write — leaving
evidence for something that never happened — or be lost while the assertion
survived, leaving a governed row nothing accounts for.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.entities.provenance import AssertionProvenance

_INSERT = text(
    "INSERT INTO assertion_provenance ("
    "  provenance_id, tenant_id, source_system, source_namespace, external_record_id, external_revision,"
    "  event_time, observed_at, ingested_at, derivation_method, derivation_profile, authority,"
    "  freshness_state, expires_at, revocation_ref, revoked_at, confidence,"
    "  validating_profile_revision_id, extension_set_digest, produced_by, approved_by, created_at"
    ") VALUES ("
    "  :pid, :tenant, :system, :namespace, :record, :revision,"
    "  :event_time, :observed_at, :ingested_at, :derivation_method, :derivation_profile, :authority,"
    "  :freshness, :expires_at, :revocation_ref, :revoked_at, :confidence,"
    "  :validating_revision, :extension_digest, :produced_by, :approved_by, :created_at"
    ")"
)


async def record(
    session: AsyncSession,
    provenance: AssertionProvenance,
    *,
    created_at: object = None,
) -> uuid.UUID:
    """Write one provenance row and return its id.

    Takes an `AssertionProvenance` rather than loose parameters so the
    completeness rules cannot be bypassed by a writer that assembles its own
    dictionary — the record refuses to exist incomplete, so anything reaching here
    has already been checked.

    `created_at` defaults to `ingested_at`. The two coincide for an ordinary
    write, and a caller backfilling a historical assertion can still say when the
    row actually entered the table separately from when the fact was ingested.
    """
    provenance_id = uuid.uuid4()
    stamped = created_at if created_at is not None else provenance.ingested_at
    await session.execute(
        _INSERT,
        {
            "pid": provenance_id,
            "tenant": provenance.tenant_id,
            "system": provenance.source_system,
            "namespace": provenance.source_namespace,
            "record": provenance.external_record_id,
            "revision": provenance.external_revision,
            "event_time": provenance.event_time,
            "observed_at": provenance.observed_at,
            "ingested_at": provenance.ingested_at,
            "derivation_method": provenance.derivation_method,
            "derivation_profile": provenance.derivation_profile,
            "authority": provenance.authority,
            "freshness": provenance.freshness_state,
            "expires_at": provenance.expires_at,
            "revocation_ref": provenance.revocation_ref,
            "revoked_at": provenance.revoked_at,
            "confidence": provenance.confidence,
            "validating_revision": provenance.validating_profile_revision_id,
            "extension_digest": provenance.extension_set_digest,
            "produced_by": provenance.produced_by,
            "approved_by": provenance.approved_by,
            "created_at": stamped,
        },
    )
    return provenance_id


async def supersede(
    session: AsyncSession,
    *,
    superseded_provenance_id: uuid.UUID,
    replacement: AssertionProvenance,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Record replacement evidence without touching what it replaces.

    Returns `(superseded_id, replacement_id)` — the pair a caller needs to move
    its assertion onto the new evidence while leaving an audit trail that still
    contains the old.

    The superseded row is deliberately *not* modified, not even to mark it
    replaced. Provenance is a record of what was observed at a moment, and a
    module that could edit one row of it to reflect a later fact is a module that
    could edit any of it. Which provenance an assertion currently relies on is a
    property of the assertion, so it is recorded there.
    """
    replacement_id = await record(session, replacement)
    return superseded_provenance_id, replacement_id


def for_governed_write(
    *,
    tenant_id: uuid.UUID,
    validating_profile_revision_id: uuid.UUID,
    authority: str,
    ingested_at: object,
    produced_by: str,
    source_system: str,
    source_namespace: str,
    **optional: object,
) -> AssertionProvenance:
    """Build the provenance an internal governed write carries.

    A convenience for the common case — the platform itself asserting something
    under a profile it just validated against — and deliberately not a default:
    every field it fills is still passed, so a reader of a call site can see what
    is being claimed rather than inferring it from this function's name.
    """
    return AssertionProvenance(
        tenant_id=tenant_id,
        source_system=source_system,
        source_namespace=source_namespace,
        ingested_at=ingested_at,  # type: ignore[arg-type]
        authority=authority,
        freshness_state="fresh",
        produced_by=produced_by,
        validating_profile_revision_id=validating_profile_revision_id,
        **dataclasses.asdict(_Optional(**optional)),  # type: ignore[arg-type]
    )


@dataclasses.dataclass(frozen=True)
class _Optional:
    """The optional provenance fields, so `for_governed_write` cannot accept a typo.

    Spelled as a dataclass rather than passed through as `**kwargs` because a
    misspelled keyword would otherwise reach `AssertionProvenance` and raise there
    with a message about the wrong function — or, worse, if the name happened to
    match a field with a default, silently set nothing.
    """

    external_record_id: str | None = None
    external_revision: str | None = None
    event_time: object = None
    observed_at: object = None
    derivation_method: str | None = None
    derivation_profile: str | None = None
    expires_at: object = None
    revocation_ref: str | None = None
    revoked_at: object = None
    confidence: float | None = None
    extension_set_digest: str | None = None
    approved_by: str | None = None


__all__ = ["for_governed_write", "record", "supersede"]
