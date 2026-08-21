"""`ExpectedImpactEnvelopeService`: validates a submitted
`arc_expected_impact_envelope_v1` object against ADR 041 §4 before it is
allowed to freeze, and computes its canonical digest.

**Every shape failure this service can raise is `arc_envelope_invalid`, not
`arc_proposal_validation_failed`.** `contextplane.arc.schemas.authoring_
profiles.validate_expected_impact_envelope_v2` already enforces the six
closed `ObservationClassPredicateV1` fields (`intent_kind`,
`requested_action_classes`, `environment`, `data_sensitivity_tier`,
`entity_ids`, `domain_ids`) via its schema's `additionalProperties:
false`-equivalent object check, rejects an empty set via that same schema's
`min_items=1` on each field, and rejects overlapping items via its own
`_check_envelope_non_overlap`. But that pure module's errors carry the
*profile* validator's own refusal code
(`ProfileValidationFailed.refusal_code == "arc_proposal_validation_failed"`),
because `authoring_profiles.py` has no way to know it was called for an
envelope specifically -- it is the one shared engine every one of the
sixteen profiles goes through. Forbidden keys, empty sets, and duplicate
array entries would silently surface under the wrong refusal code if this
service just let that exception propagate; this module exists to catch
every `AuthoringProfileError` this validator can raise and re-report it
under the one code ADR 041 §4 and the TDD's own refusal table name for an
envelope: `arc_envelope_invalid`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from contextplane.arc.schemas.authoring_profile_shapes import EXPECTED_IMPACT_ENVELOPE_PROFILE
from contextplane.arc.schemas.authoring_profiles import (
    AuthoringProfileError,
    canonicalize_expected_impact_envelope_v2,
    validate_expected_impact_envelope_v2,
)
from contextplane.exceptions import RegistryError


class EnvelopeInvalid(RegistryError):
    """The submitted envelope is not acceptable (`arc_envelope_invalid`, 422).

    Covers every ADR 041 §4 rejection: profile confusion, an unknown or
    missing predicate key (`tenant_id`, `repository_identity`, `session_id`,
    `intent_summary`, or any other key outside the six approved selector
    dimensions), an empty predicate set, a duplicate array entry, an
    unknown delta code, two items sharing a delta code and an overlapping
    predicate, an invalid `minimum_count`/`maximum_count` range, and an
    envelope naming a different proposal/version than the one it was
    submitted against.
    """


@dataclasses.dataclass(frozen=True)
class EnvelopeAssessment:
    """A validated envelope, ready to freeze: its own canonical digest, and
    the exact canonical JSON-shaped object the digest was computed over --
    object keys sorted, every set-valued array (the six predicate fields)
    sorted and deduplicated, exactly as `canonicalize_expected_impact_
    envelope_v1` produced it -- so a caller persisting `envelope["items"]`
    writes the canonical form, not whatever order the request happened to
    submit."""

    envelope_digest: str
    envelope: dict[str, Any]


class ExpectedImpactEnvelopeService:
    """Validates and canonicalizes one `arc_expected_impact_envelope_v1`
    object. Stateless: holds no session, no collaborator, nothing that
    would make two instances behave differently."""

    def validate(
        self, envelope: Mapping[str, Any], *, proposal_id: uuid.UUID, proposal_version: int
    ) -> EnvelopeAssessment:
        obj = dict(envelope)
        if obj.get("profile") != EXPECTED_IMPACT_ENVELOPE_PROFILE:
            raise EnvelopeInvalid(f"expected profile {EXPECTED_IMPACT_ENVELOPE_PROFILE!r}, got {obj.get('profile')!r}")

        try:
            validate_expected_impact_envelope_v2(obj)
        except AuthoringProfileError as exc:
            raise EnvelopeInvalid(str(exc)) from exc

        if str(obj.get("proposal_id")) != str(proposal_id) or int(obj.get("proposal_version", -1)) != int(
            proposal_version
        ):
            raise EnvelopeInvalid(
                f"envelope names proposal {obj.get('proposal_id')}/{obj.get('proposal_version')}, but this "
                f"submission is for {proposal_id}/{proposal_version}"
            )

        self._check_item_ranges(obj)

        # `canonicalize_expected_impact_envelope_v2` sorts object keys and
        # sorts/deduplicates every set-valued array; parsing its output
        # back is how this service hands the caller that canonical form
        # rather than the pre-canonical *obj* -- the two can differ in
        # array order even though neither is rejected, and a persisted row
        # must reflect the canonical order the digest actually covers.
        canonical_bytes = canonicalize_expected_impact_envelope_v2(obj)
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        canonical_envelope = json.loads(canonical_bytes)
        return EnvelopeAssessment(envelope_digest=digest, envelope=canonical_envelope)

    def _check_item_ranges(self, envelope: Mapping[str, Any]) -> None:
        """`minimum_count`/`maximum_count` boundaries a closed field-set
        schema cannot express: the pure validator only checks that both
        are numbers (nullable for the maximum), not that they form a
        sensible range."""
        for item in envelope["items"]:
            minimum = item["minimum_count"]
            maximum = item["maximum_count"]
            if minimum < 0:
                raise EnvelopeInvalid(f"item {item['item_id']!r}: minimum_count must be >= 0, got {minimum}")
            if maximum is not None and maximum < minimum:
                raise EnvelopeInvalid(
                    f"item {item['item_id']!r}: maximum_count {maximum} is below minimum_count {minimum}"
                )


__all__ = ["EnvelopeAssessment", "EnvelopeInvalid", "ExpectedImpactEnvelopeService"]
