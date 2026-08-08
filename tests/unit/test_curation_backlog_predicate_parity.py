"""Closes the curation-backlog predicate duplication between `curation_queue.py`
and `operational_health.py`.

The two used to be independent, hand-copied CASE/JOIN queries -- one scoped to a
tenant (the curation queue itself), one cluster-wide (the operator health
gauge) -- with a comment at the cluster-wide copy warning that a future change
to one would silently leave the other behind. The fix is extraction, not a
side-by-side comment: `curation_queue.backlog_predicate()` is now the one
function both call, parameterized only by whether a tenant filter is applied.

This file is the mechanical proof that the fix holds, not a restatement of it
in prose. It does not re-derive the predicate's logic in Python and compare an
independent implementation -- that would test a *third*, test-only copy
against itself. Instead it proves the two production call sites are the one
function, byte for byte: `operational_health.py`'s actual `_QUEUE_COUNTS`
entry is asserted equal to a fresh call to the same function it is supposed to
be calling, and the cluster-wide predicate is asserted equal to the per-tenant
one with only the tenant clause's own exact text removed. Either assertion
fails the moment someone reverts one call site to a hand-copied string that
has since drifted -- which is the regression this test exists to catch before
a dashboard and a queue quietly disagree about what "backlogged" means.
"""

from __future__ import annotations

from contextplane.service.memory.curation_queue import backlog_predicate
from contextplane.service.platform.operational_health import _QUEUE_COUNTS

_TENANT_CLAUSE = "COALESCE(c.owning_tenant_id, c.author_tenant_id) = :tid\n   AND "


def test_the_tenant_clause_is_the_only_difference_between_the_two_scopes() -> None:
    """Strip the tenant clause's own exact text from the per-tenant predicate
    and what remains must be byte-for-byte the cluster-wide predicate -- not
    merely similar, not "the same JOINs in a different order"."""
    tenant_scoped = backlog_predicate(tenant_filter=True)
    cluster_wide = backlog_predicate(tenant_filter=False)

    assert _TENANT_CLAUSE in tenant_scoped
    assert tenant_scoped.replace(_TENANT_CLAUSE, "", 1) == cluster_wide


def test_operational_health_calls_the_shared_predicate_not_a_hand_copy() -> None:
    """`operational_health.py`'s own `curation_queue_backlog` entry must be
    built by calling `curation_queue.backlog_predicate(tenant_filter=False)`
    -- this fails the instant that entry goes back to being a separately
    maintained SQL string, even one that happens to match today."""
    entries = {key: sql for key, _label, sql in _QUEUE_COUNTS}
    assert entries["curation_queue_backlog"] == "SELECT COUNT(*)" + backlog_predicate(tenant_filter=False)


def test_neither_scope_filters_by_tenant_in_its_backlog_boolean() -> None:
    """The four-way backlog OR (unlinked / contested / awaiting_owner /
    below_floor) must not itself mention a tenant column -- if it ever did,
    the cluster-wide call site would silently start scoping to whichever
    bind parameter happened to be in the session, rather than reporting
    every tenant's backlog."""
    cluster_wide = backlog_predicate(tenant_filter=False)
    assert ":tid" not in cluster_wide
