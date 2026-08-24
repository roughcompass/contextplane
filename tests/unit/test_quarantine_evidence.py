"""Properties of the export's statements and its role set.

E4-T7b. The behaviour is pinned in `tests/integration/test_quarantine_evidence.py`,
where there is a second tenant to be excluded from. These are the checks that do
not need a database and would otherwise only be visible by reading the SQL.
"""

from __future__ import annotations

import datetime
import uuid

from contextplane.service.memory import quarantine, quarantine_evidence

_NOW = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)


def test_every_statement_is_scoped_to_a_tenant() -> None:
    """Including the members read, which is the one that cannot be.

    `claim_quarantine_members` carries no `tenant_id`, so its statement has to
    reach the tenant through a join. A future edit that "simplifies" the join
    away would still return the right rows for every well-behaved caller and
    would serve another tenant's set to anybody who guessed a UUID.
    """
    for name, statement in (
        ("ledger", quarantine_evidence._LEDGER),
        ("members", quarantine_evidence._MEMBERS),
        ("receipts", quarantine_evidence._RECEIPTS),
    ):
        assert ":tenant" in statement, f"the {name} statement is not tenant-scoped"
        assert ":qid" in statement, f"the {name} statement is not scoped to one quarantine"

    assert "JOIN claim_quarantines" in quarantine_evidence._MEMBERS, (
        "the members read no longer joins the ledger; that join is the only thing "
        "scoping it to a tenant, because the members table has no tenant column"
    )


def test_no_statement_reads_claim_content() -> None:
    """The bundle says *which* claims were withheld, not what they said.

    Serving the withheld content back through an export would be a route around
    the withholding, which is the one thing the mechanism exists to do.
    """
    for statement in (
        quarantine_evidence._LEDGER,
        quarantine_evidence._MEMBERS,
        quarantine_evidence._RECEIPTS,
    ):
        assert "memory_claims" not in statement, (
            "an export statement now reads the claims table. The bundle says which "
            "claims were withheld, not what they said; serving the content back "
            "would be a route around the withholding."
        )


def test_the_export_roles_are_the_operator_roles_plus_the_auditor() -> None:
    """Derived rather than restated, so a role that gains the ability to
    withhold gains the ability to read back what it withheld — instead of that
    having to be remembered in a second place."""
    assert quarantine.OPERATOR_ROLES < quarantine_evidence.EVIDENCE_ROLES
    assert quarantine_evidence.EVIDENCE_ROLES - quarantine.OPERATOR_ROLES == {"auditor"}


def test_the_bundle_states_what_it_evidences() -> None:
    """A document exported for somebody outside this system travels away from
    every docstring explaining it, so the caveat is a field.

    An internal chain proves nothing against the party holding the storage.
    Here it is stronger than that: neither `claim_quarantines` nor
    `context_receipts` carries a digest column at all, so these rows sit on no
    chain and the honest statement says so.
    """
    provenance = quarantine_evidence.BUNDLE_PROVENANCE
    assert "mutable rows" in provenance
    assert "no" in provenance.lower() and "digest chain" in provenance
    assert "repudiation" not in provenance.lower()


def test_nothing_the_exported_document_carries_uses_the_word_adr_0012_forbids() -> None:
    """An export is where somebody reaches for the strongest word available, so
    the temptation is checked rather than resisted.

    Over the *served* strings rather than the module source, because the module
    docstring names the banned word in order to ban it — and a check that could
    not tell a use from a mention would force the prohibition to go unwritten.
    """
    from contextplane.api.routers import admin_quarantine

    served = [quarantine_evidence.BUNDLE_PROVENANCE]
    served += [
        str(field.description or "") for field in admin_quarantine.QuarantineEvidenceResponse.model_fields.values()
    ]
    served.append(admin_quarantine.QuarantineEvidenceResponse.__doc__ or "")
    served.append(admin_quarantine.export_quarantine_evidence.__doc__ or "")

    for text_ in served:
        flat = text_.lower().replace("-", "").replace(" ", "")
        assert "nonrepudiation" not in flat, f"the exported surface claims non-repudiation: {text_!r}"


def test_a_bundle_knows_whether_it_was_reverted() -> None:
    """`is_reverted` is derived from `reverted_at` rather than stored, so the
    two cannot disagree — and the bundle exports either way."""
    base = {
        "quarantine_id": uuid.uuid4(),
        "predicate": {"selector": "connector_run", "value": "run-42"},
        "reason": "connector emitted nonsense",
        "matched_count": 1,
        "applied_by": uuid.uuid4(),
        "applied_at": _NOW,
        "members": (uuid.uuid4(),),
        "withheld_receipts": (),
    }
    live = quarantine_evidence.EvidenceBundle(**base, reverted_by=None, reverted_at=None)
    undone = quarantine_evidence.EvidenceBundle(
        **base, reverted_by=uuid.uuid4(), reverted_at=_NOW + datetime.timedelta(days=1)
    )

    assert not live.is_reverted
    assert undone.is_reverted
    assert undone.members == live.members, "a reverted bundle still carries what it withheld"


def test_the_predicate_survives_a_driver_that_returns_json_as_text() -> None:
    """asyncpg hands back JSONB as a string unless a codec is registered, and
    the write path stores it with `json.dumps`. Normalised so the bundle's shape
    does not depend on driver configuration."""
    assert quarantine_evidence._as_predicate('{"selector": "strategy_id", "value": "extract.v1"}') == {
        "selector": "strategy_id",
        "value": "extract.v1",
    }
    assert quarantine_evidence._as_predicate({"selector": "strategy_id"}) == {"selector": "strategy_id"}
