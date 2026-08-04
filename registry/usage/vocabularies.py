"""Closed vocabularies for the usage-event columns the database constrains.

Every column in ``usage_events`` is an identifier, a timestamp, a number, or a
term from a set fixed in this file. That is not a style preference — it is the
property that makes the table structurally incapable of holding content.

A free-text column in a high-volume table with a retention window and a
right-to-be-forgotten obligation is where someone eventually pastes a customer
email or an account id. Scanning for that after the fact is a losing game; making
it *unrepresentable* is not. So there is no text column to misuse, and a
conformance test asserts one cannot be added.

Query text is the one place the pull is strongest, and it is answered the same way
ARC answers it: a digest, a length, and a result count. That supports "how often
did a search return nothing" without storing what anyone asked for. Recording the
terms themselves was raised and deliberately deferred — it would reverse a stated
non-goal, and it belongs in an amendment rather than in a widening here.

These constants exist apart from the CHECK constraints that enforce them, so a
conformance test asserts the two describe the same sets. Two lists that must agree
and live in different files eventually disagree.
"""

from typing import Final

from registry.metrics import STATUS_CLASSES

__all__ = [
    "OUTCOMES",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "STATUS_CLASSES",
    "SURFACES",
    "SURFACE_MCP",
    "SURFACE_REST",
]


# ---------------------------------------------------------------------------
# usage_events.surface
# ---------------------------------------------------------------------------
#
# Which door the call came through. Two members, and there will not be a third
# for browser traffic: client-side interaction analytics stays with a third-party
# tool, so nothing here is ever submitted by a browser. Every row in this table
# describes a call the service itself authenticated and served.
#
# The split matters more than it looks. The vision's primary consumer is an agent
# over MCP, and agent traffic has never been distinguishable from a script's.
# Collapsing these two would make the single most interesting question about
# adoption unanswerable.

SURFACE_REST: Final[str] = "rest"
"""An authenticated HTTP call to the REST surface."""

SURFACE_MCP: Final[str] = "mcp"
"""An MCP tool invocation."""

SURFACES: Final[frozenset[str]] = frozenset({SURFACE_REST, SURFACE_MCP})


# ---------------------------------------------------------------------------
# usage_events.outcome
# ---------------------------------------------------------------------------
#
# Two members, deliberately coarser than the status class stored beside it.
# `outcome` answers "did the caller get what they asked for"; `status_class`
# carries the detail. Both are kept because they disagree in a way that matters:
# a 404 on a lookup is a *successful* request that failed to find anything, and
# collapsing that into one field would either hide a real miss or invent an error
# rate out of ordinary not-founds.

OUTCOME_OK: Final[str] = "ok"
"""The call was served. Says nothing about whether it found anything."""

OUTCOME_ERROR: Final[str] = "error"
"""The call failed — refused, malformed, or the service broke."""

OUTCOMES: Final[frozenset[str]] = frozenset({OUTCOME_OK, OUTCOME_ERROR})


# `STATUS_CLASSES` is re-exported rather than redefined. It is the same closed set
# the operational tier already publishes, and a second copy would let the two
# tiers disagree about what a 429 is called — which is exactly the kind of drift
# that makes two dashboards built from one service tell different stories.
