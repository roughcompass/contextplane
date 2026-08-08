"""ARC sandboxed subprocesses: separate service identities that touch
hostile, admitted-but-unreviewed source bytes so the API process never has
to.

`parser_main.py` is the first process to live here; a future drafter
sandbox (model-backed proposal drafting) reuses the same `ipc.py` transport
unchanged rather than inventing a second wire protocol. Nothing under this
package imports `contextplane.storage`, `contextplane.arc.service`,
`contextplane.wiring`, or `contextplane.config` -- see
`tests/conformance/test_arc_parser_sandbox.py`'s import-graph check, which
fails the build the day that stops being true.
"""

from __future__ import annotations
