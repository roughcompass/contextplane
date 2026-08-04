"""Whether two values of one predicate can both be true at once.

Pure. Takes two stored values and returns a verdict, with no query and no clock
read. That is not an optimization. A comparison that consults the database
answers "do these agree *now*", and a score derived from data that has since
changed cannot be re-derived by a reader asking why a claim scored as it did.
Anything needing resolution is resolved on the write path and stored.

**Three outcomes, not two.** The write path validates several value types loosely
by necessity -- an enum resolves against no vocabulary, an entity reference is
resolved separately -- so this regularly meets a value it cannot read. Forcing
that into a two-valued answer is wrong either way: calling it incompatible
manufactures a contested claim out of a validation gap, and calling it compatible
hides a broken value. `UNDECIDABLE` routes to a human and never lowers confidence.

**Where a rule is a judgement call it errs toward compatible.** A disagreement
marks both claims contested, and a contested claim cannot be promoted and always
needs review -- consequences no reviewer can undo when both values were in fact
true. A missed disagreement is recovered by decay and by the next person who
looks. Those costs are not close to symmetric.

**Only meaningful for a predicate declaring a single value.** A set-valued
predicate's differing values are two facts, not two answers to one question.
"""

from __future__ import annotations

import datetime
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

from registry.service.version_predicates import (
    _parse_version,  # noqa: PLC2701 - the parser this module must agree with
    _predicate_to_atomics,  # noqa: PLC2701
)

Verdict = Literal["compatible", "incompatible", "undecidable"]

COMPATIBLE: Verdict = "compatible"
INCOMPATIBLE: Verdict = "incompatible"
UNDECIDABLE: Verdict = "undecidable"

# Durations here are human-authored targets, so two sources can report the same
# intent with different rounding. Exact equality would fire on that rounding, and
# every firing costs a claim its promotability permanently.
#
# Relative rather than absolute, because one second is the whole of a two-second
# timeout and nothing at all in a thirty-day recovery objective -- a single
# absolute figure is wrong at both ends of the range this ontology spans. Two
# percent is the smallest round figure that absorbs unit rounding at every scale
# while absorbing no factor-of-two, which is what real disagreement looks like.
DURATION_RELATIVE_TOLERANCE = 0.02
DURATION_MINIMUM_TOLERANCE_SECONDS = 1

# A default port carries no meaning, so two URLs differing only by one are one URL.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# The types whose values are compared as folded text.
_TEXT_FOLDED = frozenset({"string", "enum"})


def values_compatible(
    value_type: str,
    left: Any,
    right: Any,
    *,
    left_entity_id: str | None = None,
    right_entity_id: str | None = None,
) -> Verdict:
    """Can both values hold at the same instant?"""
    if value_type == "prose":
        # A paragraph cannot be compared without a model, and a model's verdict
        # is neither reproducible nor re-derivable by an auditor. Prose claims
        # are barred from promotion anyway, so a disagreement here would cost a
        # confidence drop and reviewer attention and buy nothing.
        return UNDECIDABLE

    if value_type == "boolean":
        if not (isinstance(left, bool) and isinstance(right, bool)):
            return UNDECIDABLE
        return COMPATIBLE if left == right else INCOMPATIBLE

    if value_type in {"integer", "bytes"}:
        # Exact thresholds. One byte of difference is a request one side accepts
        # and the other refuses.
        if not (_is_int(left) and _is_int(right)):
            return UNDECIDABLE
        return COMPATIBLE if left == right else INCOMPATIBLE

    if value_type == "duration_seconds":
        if not (_is_int(left) and _is_int(right)):
            return UNDECIDABLE
        tolerance = max(
            DURATION_MINIMUM_TOLERANCE_SECONDS,
            math.floor(DURATION_RELATIVE_TOLERANCE * max(abs(left), abs(right))),
        )
        return COMPATIBLE if abs(left - right) <= tolerance else INCOMPATIBLE

    if value_type == "decimal":
        # Stored as a string so nothing is lost on the way in, which means
        # "0.999" and "0.9990" are one number written two ways. Compared
        # numerically for that reason, and with no tolerance at all: the
        # predicate using this type is an availability target, where three nines
        # against two is a tenfold difference in error budget. Any tolerance wide
        # enough to be useful would swallow exactly that.
        try:
            return COMPATIBLE if Decimal(str(left)) == Decimal(str(right)) else INCOMPATIBLE
        except (InvalidOperation, ValueError, ArithmeticError):
            return UNDECIDABLE

    if value_type == "timestamp_utc":
        # Exact. Unlike a duration, an instant is not a measurement -- it is a
        # boundary somebody chose, and two sources choosing different boundaries
        # disagree.
        lhs, rhs = _parse_instant(left), _parse_instant(right)
        if lhs is None or rhs is None:
            return UNDECIDABLE
        return COMPATIBLE if lhs == rhs else INCOMPATIBLE

    if value_type in _TEXT_FOLDED:
        if not (isinstance(left, str) and isinstance(right, str)):
            return UNDECIDABLE
        return COMPATIBLE if _fold(left) == _fold(right) else INCOMPATIBLE

    if value_type == "url":
        lhs_url, rhs_url = _normalize_url(left), _normalize_url(right)
        if lhs_url is None or rhs_url is None:
            return UNDECIDABLE
        return COMPATIBLE if lhs_url == rhs_url else INCOMPATIBLE

    if value_type == "entity_ref":
        return _compare_entity_ref(left, right, left_entity_id, right_entity_id)

    if value_type == "version_predicate":
        return _compare_version_ranges(left, right)

    return UNDECIDABLE


def _is_int(value: Any) -> bool:
    # `bool` subclasses `int`, and True under a predicate meaning seconds would
    # compare as one second.
    return isinstance(value, int) and not isinstance(value, bool)


def _fold(text: str) -> str:
    """The comparison form of a token. Never written back to the claim.

    Composition, surrounding space, internal runs of space, and case are all
    noise in the identifiers this reaches -- a team name and a rotation name.

    Case folding is safe here only because every predicate whose values are
    genuinely case-sensitive is set-valued and so never reaches this test:
    `getUser` and `getuser` really are two different operations, and
    `exposes_operation` is a set. A single-valued case-sensitive predicate would
    need its own value type rather than an exception here.
    """
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _parse_instant(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_url(value: Any) -> tuple[str, str, str, str] | None:
    """Fold only what the URL standard says carries no meaning.

    Scheme and host are case-insensitive; a default port is implied; a fragment
    names a place inside one document rather than a different document. Path case
    and query string are preserved, because those do carry meaning.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None

    scheme = parts.scheme.casefold()
    host = parts.hostname.casefold()
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    return (scheme, netloc, path, parts.query)


def _compare_entity_ref(left: Any, right: Any, left_entity_id: str | None, right_entity_id: str | None) -> Verdict:
    """Compare resolved identities, not the strings that named them.

    The same entity can be named by its identifier in one claim and by an
    external system's identifier in another. Resolution happens on the write
    path -- through the same chokepoint that resolves a claim's subject, because
    resolving an arbitrary reference outside it would answer "does this exist"
    for every entity in the deployment -- and the result is stored, so this stays
    a function of two values.
    """
    if left_entity_id is not None and right_entity_id is not None:
        return COMPATIBLE if left_entity_id == right_entity_id else INCOMPATIBLE
    if left_entity_id is None and right_entity_id is None:
        # Neither names anything the catalog holds. Identical text is the only
        # evidence available that they mean the same thing, and it is enough to
        # count as agreement but not enough to call a disagreement.
        if isinstance(left, str) and isinstance(right, str) and left.strip() == right.strip():
            return COMPATIBLE
        return UNDECIDABLE
    # One side resolved and the other did not: the unresolved reference may well
    # name the same entity under a name the catalog has not learned yet.
    return UNDECIDABLE


def _compare_version_ranges(left: Any, right: Any) -> Verdict:
    """Two ranges conflict only when no version satisfies both.

    A tighter range is not a contradiction of a looser one. ">=2.1" and ">=2.0"
    are both satisfied by 2.1, and one source simply knows more -- treating that
    as a conflict would punish a source for being precise.

    Reuses the expansion the graph's own edges are validated with, so a claim and
    an edge cannot disagree about what a range means.
    """
    if not (isinstance(left, str) and isinstance(right, str)):
        return UNDECIDABLE

    lhs = _bounds(left)
    rhs = _bounds(right)
    if lhs is None or rhs is None:
        return UNDECIDABLE

    lo, lo_inclusive = _tighter_lower(lhs, rhs)
    hi, hi_inclusive = _tighter_upper(lhs, rhs)

    if lo is not None and hi is not None:
        if lo > hi:
            return INCOMPATIBLE
        if lo == hi and not (lo_inclusive and hi_inclusive):
            return INCOMPATIBLE
    # Anything this cannot prove disjoint is reported compatible. Missing a
    # conflict is recoverable; inventing one is not.
    return COMPATIBLE


class _Bounds:
    """One range as a lower and upper bound over the semver order."""

    def __init__(self) -> None:
        self.lower: Any = None
        self.lower_inclusive = True
        self.upper: Any = None
        self.upper_inclusive = False


def _bounds(predicate: str) -> _Bounds | None:
    """Collapse a predicate's AND-list of comparisons into an interval.

    Sound only because the grammar is a pure conjunction over a total order:
    every clause narrows the same interval, so the intersection is the tightest
    lower bound against the tightest upper bound. Alternation would break that,
    which is why the expansion returning None is reported undecidable rather
    than guessed at.
    """
    atomics = _predicate_to_atomics(predicate)
    if atomics is None:
        return None

    bounds = _Bounds()
    if not atomics:
        # No constraint at all, so nothing can conflict with it.
        return bounds

    for atomic in atomics:
        operator, raw = _split_atomic(atomic)
        if operator is None:
            return None
        version = _parse_version(raw)
        if version is None:
            return None

        if operator == "==":
            _raise_lower(bounds, version, inclusive=True)
            _lower_upper(bounds, version, inclusive=True)
        elif operator == ">=":
            _raise_lower(bounds, version, inclusive=True)
        elif operator == ">":
            _raise_lower(bounds, version, inclusive=False)
        elif operator == "<=":
            _lower_upper(bounds, version, inclusive=True)
        elif operator == "<":
            _lower_upper(bounds, version, inclusive=False)
        elif operator == "!=":
            # An excluded point cannot make two ranges disjoint on its own, and
            # tracking exclusions would only ever narrow an intersection this
            # function reports as compatible anyway.
            continue
        else:
            return None
    return bounds


def _split_atomic(atomic: str) -> tuple[str | None, str]:
    for operator in (">=", "<=", "==", "!=", ">", "<"):
        if atomic.startswith(operator):
            return operator, atomic[len(operator) :]
    return None, atomic


def _raise_lower(bounds: _Bounds, version: Any, *, inclusive: bool) -> None:
    if bounds.lower is None or version > bounds.lower:
        bounds.lower, bounds.lower_inclusive = version, inclusive
    elif version == bounds.lower and not inclusive:
        bounds.lower_inclusive = False


def _lower_upper(bounds: _Bounds, version: Any, *, inclusive: bool) -> None:
    if bounds.upper is None or version < bounds.upper:
        bounds.upper, bounds.upper_inclusive = version, inclusive
    elif version == bounds.upper and not inclusive:
        bounds.upper_inclusive = False


def _tighter_lower(lhs: _Bounds, rhs: _Bounds) -> tuple[Any, bool]:
    if lhs.lower is None:
        return rhs.lower, rhs.lower_inclusive
    if rhs.lower is None:
        return lhs.lower, lhs.lower_inclusive
    if lhs.lower > rhs.lower:
        return lhs.lower, lhs.lower_inclusive
    if rhs.lower > lhs.lower:
        return rhs.lower, rhs.lower_inclusive
    return lhs.lower, lhs.lower_inclusive and rhs.lower_inclusive


def _tighter_upper(lhs: _Bounds, rhs: _Bounds) -> tuple[Any, bool]:
    if lhs.upper is None:
        return rhs.upper, rhs.upper_inclusive
    if rhs.upper is None:
        return lhs.upper, lhs.upper_inclusive
    if lhs.upper < rhs.upper:
        return lhs.upper, lhs.upper_inclusive
    if rhs.upper < lhs.upper:
        return rhs.upper, rhs.upper_inclusive
    return lhs.upper, lhs.upper_inclusive and rhs.upper_inclusive


# --- near-duplicate detection ------------------------------------------------
#
# Distinct from incompatibility, and used only for collapsing claims that say the
# same thing -- never for deciding that two claims conflict. That separation matters:
# a permissive measure is safe when the consequence is "these are one claim" and
# dangerous when the consequence is "one of these is wrong".
#
# The failure this addresses is not merely volume. Twenty sessions phrasing one team
# name slightly differently produce twenty claims that the exact comparator calls
# *incompatible* -- so they all become contested, none can be promoted, and no
# reviewer can resolve them because they all mean the same thing.

# Words that carry no identity in a name. Deliberately tiny: a longer list starts
# discarding words that distinguish real teams ("core" and "shared" are not noise).
_NOISE_TOKENS = frozenset({"the", "a", "an", "of", "team", "group", "squad"})

# What separates one token from another when folding a value into tokens. Hyphens and
# underscores included, so `platform-team` and `platform team` are one name.
_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def value_tokens(value: object) -> frozenset[str]:
    """The identity-bearing tokens of a text value.

    Case, spacing, punctuation, filler words, and word order all removed, because
    none of them distinguishes one team or rotation from another. What survives is
    what a person would read as the name.
    """
    if not isinstance(value, str):
        return frozenset()
    folded = unicodedata.normalize("NFKD", value).casefold()
    tokens = {t for t in _TOKEN_SPLIT.split(folded) if t}
    identity = tokens - _NOISE_TOKENS
    # A value made entirely of filler is its own token set rather than empty, or two
    # unrelated such values would look identical.
    return frozenset(identity or tokens)


def token_similarity(left: object, right: object) -> float:
    """How much two text values share, as a ratio of their combined tokens.

    Reported alongside every collapse so a threshold change can be evaluated against
    past decisions rather than guessed at.
    """
    a, b = value_tokens(left), value_tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_near_duplicate(value_type: str, left: object, right: object) -> tuple[bool, float]:
    """Do these two values name the same thing, and how close are they?

    **Requires the identity tokens to be equal, not merely overlapping.** A partial
    overlap is not a duplicate: "core platform" and "platform" may well be two
    different teams, and collapsing them would merge two claims into one that neither
    source made. Anything looser needs a model, and a collapse decision that needed a
    model could not be re-derived -- which would make it unreviewable.

    So this catches case, spacing, punctuation, word order, and the words that carry
    no identity in a name -- which does mean "platform" and "platform team" collapse,
    because in a field naming a team the word "team" distinguishes nothing. It does
    not catch abbreviations, synonyms, or one name genuinely containing another, and
    it is not trying to.

    Only text values. A number, an instant, or a version range is either equal or it
    is not, and the exact comparator already said which.
    """
    if value_type not in {"string", "enum"}:
        return False, 0.0
    similarity = token_similarity(left, right)
    return similarity == 1.0, similarity


def intervals_overlap(
    from_a: datetime.datetime,
    to_a: datetime.datetime | None,
    from_b: datetime.datetime,
    to_b: datetime.datetime | None,
) -> bool:
    """Do two effective intervals share any instant?

    Open-ended on the right when `to` is absent, since a claim with no end is
    asserted to hold indefinitely. Half-open: a claim ending exactly when
    another begins does not overlap it, which is what makes a clean handover a
    succession rather than a disagreement.
    """
    if to_a is not None and to_a <= from_b:
        return False
    if to_b is not None and to_b <= from_a:
        return False
    return True


__all__ = [
    "COMPATIBLE",
    "DURATION_MINIMUM_TOLERANCE_SECONDS",
    "DURATION_RELATIVE_TOLERANCE",
    "INCOMPATIBLE",
    "UNDECIDABLE",
    "Verdict",
    "intervals_overlap",
    "is_near_duplicate",
    "token_similarity",
    "value_tokens",
    "values_compatible",
]
