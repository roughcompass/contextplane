"""Built-in PII pattern: GitHub tokens.

Detection approach
------------------
GitHub's token formats are prefix-tagged by design, which is the property this
detector relies on. Since 2021 every issued token carries a fixed prefix naming
what it is, followed by a base62 body:

- ``ghp_`` personal access token (classic)
- ``gho_`` OAuth access token
- ``ghs_`` server-to-server / GitHub App installation token
- ``ghu_`` user-to-server token
- ``github_pat_`` fine-grained personal access token

The classic four use a 36-character body; fine-grained tokens are longer and
carry an underscore-separated segment of their own. The lower bound below is
deliberately permissive rather than pinned to today's exact lengths: GitHub has
changed token length before without changing the prefix, and a detector that
stops matching after a length change fails silently in the direction that admits
the credential.

Why this detector exists
------------------------
SCM and CI systems are the sources most likely to emit tokens in payloads and
log excerpts, and the existing credential detectors do not cover them: the JWT
detector matches three-segment base64url structures only, so an opaque
prefix-tagged token passes it untouched. Registering this pattern is what makes
the class prohibited — the admission floor reads its prohibited-class set off the
shipped detector list rather than a hand-written table — so the registration in
``__init__.py``, not this file, is what closes the gap.

False-positive mitigation
--------------------------
The prefixes are distinctive enough that a match is close to conclusive, and a
word-boundary anchor on the left keeps the pattern from firing mid-identifier.
There is no right-side ``\\b`` anchor: base62 bodies end in a word character, so a
right anchor would only ever refuse a match that a following letter should not
have invalidated anyway.

A false positive here costs a refused write that the caller can rephrase. A false
negative costs a credential in storage, replicated into every derivative built
from it. The bound is set accordingly.
"""

from __future__ import annotations

import logging
import re

from contextplane.types import PiiMatchResult

_log = logging.getLogger(__name__)

# The four classic prefixes take a base62 body; `github_pat_` fine-grained tokens
# carry underscores inside theirs. Ordered alternation with the longer, more
# specific prefix first: `github_pat_` cannot be reached if a shorter alternative
# matches part of it first.
_GITHUB_TOKEN_RE = re.compile(r"(?<!\w)(github_pat_[A-Za-z0-9_]{22,}|gh[pousr]_[A-Za-z0-9]{20,})")


class _GithubTokenPattern:
    name: str = "github_token"
    category: str = "CREDENTIALS"

    def scan(self, text: str) -> list[PiiMatchResult]:
        """Return all GitHub token matches in *text*.

        Never raises; returns ``[]`` on any internal error.
        """
        try:
            results: list[PiiMatchResult] = []
            for m in _GITHUB_TOKEN_RE.finditer(text):
                results.append(
                    PiiMatchResult(
                        name=self.name,
                        offset=m.start(),
                        length=m.end() - m.start(),
                        category=self.category,
                    )
                )
            return results
        # A pattern's own bug must not break every other pattern's scan of the
        # same text (see pii_scanner.py's dispatch loop) -- but a silent []
        # here reads as "no match" when the real story is "this pattern is
        # broken", a false negative on a security control. Logged without the
        # scanned text, which is the PII this scanner exists to catch.
        except Exception:  # noqa: BLE001 - see comment above
            _log.warning("pii_pattern %s: scan failed, treating as no match", self.name, exc_info=True)
            return []


#: Module-level singleton.
pattern = _GithubTokenPattern()
