"""What produced a resolution that no request can express.

E22-T15. A comparison across a configuration change is meaningless if neither
side records which configuration produced it, so a run pins both halves of what
produced it. The request half is already in each resolution's receipt. This is
the other half.

**What goes in, and the rule for adding to it.** A fact belongs here when it can
change what a resolution returns *and* a caller cannot set it per request. The
recall branch qualifies: it decides whether the semantic workspace scan runs at
all, it is read from a committed artifact, and no request can ask for it.
`limit` does not: a caller sets it, so it is in the request and the receipt has
it.

The failure this shape is chosen against is a fingerprint that changes when
nothing about retrieval did. A digest over the whole `Settings` object would
change on a log-level edit and declare every prior run incomparable, which is
the same as having no fingerprint — a comparison surface that always says "these
are not comparable" is one nobody consults.

**Absence is a value.** A deployment with no embedder is a different deployment
from one that has an embedder and a branch that forbids using it, and both are
different from one that uses it. The three are distinct fingerprints, because
the three return different workspace blocks.

**Adding a field changes every fingerprint, and that is correct.** A run taken
before the product knew a fact was relevant genuinely is not comparable to one
taken after — the earlier run cannot say what that fact was. The alternative,
versioning the fingerprint so old runs keep comparing equal, would assert
equality about a dimension nobody measured.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from contextplane.context.semantic_workspace import RecallDecision

#: Bumped when a field is added or its meaning changes, so a fingerprint from a
#: different generation is visibly different rather than accidentally equal.
#: Included in the digest for the same reason: two generations that happened to
#: agree on every field would otherwise compare equal while measuring different
#: things.
FINGERPRINT_GENERATION: Final = 1


def resolver_fingerprint(
    *,
    decision: RecallDecision,
    embedder_available: bool,
    arm_limit: int,
    item_cap: int,
    arm_timeout_s: float,
) -> str:
    """The deployment half of what produced a resolution.

    Every argument is passed rather than read from a global, so the value is a
    function of what the caller says the deployment is. A version that reached
    into module state would produce a fingerprint no test could vary, and an
    untestable fingerprint is one nobody finds out is wrong.
    """
    facts = {
        "arm_limit": arm_limit,
        "arm_timeout_s": arm_timeout_s,
        "branch": decision.branch,
        "embedder_available": embedder_available,
        "generation": FINGERPRINT_GENERATION,
        "item_cap": item_cap,
        "lexical_approved": decision.lexical_approved,
        "semantic_approved": decision.semantic_approved,
        "similarity_floor": decision.similarity_floor,
    }
    # `sort_keys` so the digest is a function of the values and not of the order
    # a dict literal happened to be written in; separators so a formatter that
    # changed whitespace could not change the digest.
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


__all__ = ["FINGERPRINT_GENERATION", "resolver_fingerprint"]
