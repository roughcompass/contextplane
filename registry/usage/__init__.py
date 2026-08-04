"""The usage tier: who used which surface, queryable per tenant.

Deliberately separate from :mod:`registry.metrics`, which is the operational
tier. The two answer different questions and have incompatible shapes — the
operational tier must carry no identity at all, because a Prometheus label whose
values grow with adoption turns one metric into unbounded time series. Identity
is the whole point here, so it lives in a table with a retention window instead.

This package measures. It never decides: no authorization, entitlement, or audit
path may read it, and a conformance gate enforces that over module imports.
"""
