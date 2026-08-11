"""Adapters: turning what a source sends into the one envelope every producer shares.

An adapter's whole job is translation. It reads a source's own shape and returns an
`ExternalSignalEnvelopeV1`; it does not store, does not scan, and does not decide
what an observation is worth. Everything after translation happens at the single
ingest chokepoint, so a second adapter cannot arrive with a second set of rules.

**No adapter scans, and no adapter has a way around the floor.** Admission runs in
`signals/admission.py`, reached only through `SignalIngestService.ingest`. An
adapter that scanned its own input would be a second implementation of a security
control, and the one that drifts is the one nobody re-reads; an adapter that
wrote to the ledger directly would bypass the floor entirely. Both are structural
rather than stylistic: the adapters here return a value and have no session.

**Three things an adapter must never fill in**, for the reasons `ingest.py`
records: the ingestion time, the authority, and the content digest. An adapter
translating a source's own claim of authority into the envelope would be laundering
it — the authority comes from what the source was *registered* with, not from what
its payload says about itself.

**Two adapters ship, and the second exists to prove the first was not special.**
The direct adapter carries what a human or an agent reports about work they were
part of. The GitHub Actions adapter carries what a CI system observed about a run.
They produce the same envelope and reach storage the same way, which is the claim
this pair is here to make good: one contract, whatever the producer.

**The outcome translation is a third module and not a third source.** Delivery
outcomes arrive under a seat that is already registered and already carries its
own authority; what they need is not somewhere new to come from but a place to
be refused when they cannot be joined to anything. `control_plane.py` returns
the same envelope from the same registered identity, and adds the two refusals
the envelope itself cannot make: an outcome citing no external work, and one
citing a kind outside the closed set. Both would otherwise store cleanly and
then be permanently invisible to the receipt they belong to, which is the
failure that reads as "no outcome yet" rather than as an error.
"""

from __future__ import annotations

from contextplane.signals.adapters.control_plane import (
    OUTCOME_CONCLUSIONS,
    OUTCOME_OBJECTS,
    OutcomeRejected,
    checked_references,
    control_plane_outcome_envelope,
    outcome_payload,
)
from contextplane.signals.adapters.direct import DIRECT_SOURCE_SYSTEM, direct_envelope
from contextplane.signals.adapters.github_actions import (
    GITHUB_ACTIONS_SOURCE_SYSTEM,
    GithubDeliveryRejected,
    github_workflow_run_envelope,
    projected_payload,
)

__all__ = [
    "DIRECT_SOURCE_SYSTEM",
    "GITHUB_ACTIONS_SOURCE_SYSTEM",
    "OUTCOME_CONCLUSIONS",
    "OUTCOME_OBJECTS",
    "GithubDeliveryRejected",
    "OutcomeRejected",
    "checked_references",
    "control_plane_outcome_envelope",
    "direct_envelope",
    "github_workflow_run_envelope",
    "outcome_payload",
    "projected_payload",
]
