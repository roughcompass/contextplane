"""Built-in PII pattern modules.

Each sub-module exposes a module-level singleton named ``pattern`` that
implements the ``Scanner`` Protocol defined in ``contextplane.security.pii_scanner``.
Singletons are importable directly:

    from contextplane.security.pii_patterns.email import pattern as email_pattern

The dispatcher (``pii_scanner.py``) collects all singletons from
this package at startup.
"""

from contextplane.security.pii_patterns.aws_access_key import pattern as aws_access_key
from contextplane.security.pii_patterns.aws_secret_key import pattern as aws_secret_key
from contextplane.security.pii_patterns.credit_card import pattern as credit_card
from contextplane.security.pii_patterns.email import pattern as email
from contextplane.security.pii_patterns.github_token import pattern as github_token
from contextplane.security.pii_patterns.jwt_token import pattern as jwt_token
from contextplane.security.pii_patterns.phone import pattern as phone
from contextplane.security.pii_patterns.ssn import pattern as ssn

__all__ = [
    "aws_access_key",
    "aws_secret_key",
    "credit_card",
    "email",
    "github_token",
    "jwt_token",
    "phone",
    "ssn",
]

#: Ordered list of all built-in pattern singletons.  Used by the dispatcher.
BUILT_IN_PATTERNS = [
    email,
    phone,
    ssn,
    aws_access_key,
    aws_secret_key,
    jwt_token,
    github_token,
    credit_card,
]
