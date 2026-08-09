"""External signals: what other systems observed, recorded as observations.

A signal is this system's record that some other system asserted something. The
source stays authoritative — nothing stored here replaces it, and a claim derived
from a signal inherits at most the authority the signal carries, never more.

The package sits above `context` and below the API layer in the module boundary
contract: signal ingestion reads context receipts and admission, while only the
API surface and the composition root construct what lives here.
"""

from __future__ import annotations

from contextplane.signals.models import ExternalSignal

__all__ = ["ExternalSignal"]
