"""An adapter that refuses to import, the way a real dependency does.

Valid Python throughout -- a module-level `raise`, never a syntax error. A file
that would not parse could not be linted or size-checked with the rest of this
directory, and fixing a gate to accommodate a fixture is backwards.

It is also the more faithful failure. A third-party adapter almost never fails
because somebody shipped unparseable code; it fails because its own import-time
setup found something missing, which is exactly this.
"""

from __future__ import annotations

_MISSING = "libacme"

msg = (
    f"{_MISSING} is required by this adapter and is not installed. "
    "This message exists to be surfaced by the host application: an adapter that "
    "fails to import is fatal when it is the selected provider, because falling "
    "back to no extraction would look identical to a working deployment with "
    "nothing to extract."
)
raise ImportError(msg)
