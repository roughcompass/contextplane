"""The env-var grammar: strings an operator writes, values `Settings` holds.

Every function here takes what an environment variable literally contains and
returns the value a field is set to. They are pure and they are the deployment
contract: a change to any of them changes what an operator's existing env file
produces, silently, on the next restart. That is why they are tested as a group
and why they live apart from the model that consumes them.

The dependency runs one way -- `config` imports this module, never the reverse.
Nothing here may import `contextplane.config`, and nothing here reads the
environment: a parser that fetched its own input would be unfixable from a
test and untestable from a fixture.

`_resolve_extraction_provider` is deliberately not here. It validates the
selector against a set the provider registry will own, so it belongs with the
model that will consult that registry rather than with the pure parsers.
"""

from __future__ import annotations

import logging


def _parse_csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated env value into a stripped, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_operator_allowlist(value: str | None) -> tuple[tuple[str, str], ...]:
    """Parse `ISSUER|SUBJECT,ISSUER|SUBJECT,...` into exact identity pairs.

    ARC authorizes deployment-wide governance writes on an exact
    `(issuer, subject)` pair rather than on a role, because every role in
    this system is tenant-scoped -- an admin of any tenant would otherwise
    be able to edit policy that binds every tenant.

    A malformed entry raises rather than being skipped. A silently dropped
    entry means an operator who believes they have access and does not, or
    worse, an allowlist that looks configured and is empty; startup failing
    loudly is the only outcome an operator can act on.

    `|` rather than `:` as the delimiter because issuers are URLs and
    contain colons.
    """
    if not value:
        return ()
    pairs: list[tuple[str, str]] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "|" not in entry:
            msg = (
                f"ARC_GLOBAL_OPERATOR_ALLOWLIST entry {entry!r} is missing the '|' delimiter; "
                "expected 'https://issuer.example|subject'."
            )
            raise ValueError(msg)
        issuer, _, subject = entry.partition("|")
        issuer, subject = issuer.strip(), subject.strip()
        if not issuer or not subject:
            msg = (
                f"ARC_GLOBAL_OPERATOR_ALLOWLIST entry {entry!r} has an empty issuer or subject; "
                "both halves identify the operator and neither may be blank."
            )
            raise ValueError(msg)
        pairs.append((issuer, subject))
    return tuple(pairs)


# The RFC 7230 token charset, which is what a header name may be built from.
_HEADER_NAME_CHARS: frozenset[str] = frozenset(
    "!#$%&'*+-.^_`|~" "0123456789" "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

# Headers the transport owns. Letting an operator set these through
# EXTRACTION_EXTRA_HEADERS would not configure the endpoint, it would corrupt
# the request: a supplied Content-Length or Transfer-Encoding desynchronizes
# the framing, and a supplied Host reroutes the credential to a different
# origin than the one the base URL names. The configured auth header joins
# this set at parse time so a second, conflicting credential cannot be
# smuggled past the auth-template validation.
#
# `anthropic-version` is deliberately absent: it is a vendor API-version
# selector, and pinning it is a legitimate reason to reach for this variable.
_TRANSPORT_OWNED_HEADERS: frozenset[str] = frozenset(
    {"content-type", "host", "content-length", "transfer-encoding", "connection"}
)


def _parse_extraction_extra_headers(value: str, *, auth_header: str) -> tuple[tuple[str, str], ...]:
    """Parse `Name:value,Name:value,...` into header pairs.

    Deliberately not written in `_parse_role_mapping`'s error style. That one
    interpolates the offending fragment into the message, which is harmless for
    a role mapping and not harmless here: these pairs routinely carry
    credentials, and the message surfaces as an uncaught startup exception, so
    an echoed fragment lands a live token in the crash log and in whatever
    ships those logs onward. Every message below names the 1-based pair index
    and what was wrong with it -- enough to fix the variable, nothing readable
    by someone who only has the log.
    """
    if not value.strip():
        return ()
    owned = set(_TRANSPORT_OWNED_HEADERS)
    if auth_header.strip():
        owned.add(auth_header.strip().lower())
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, raw_pair in enumerate(value.split(","), start=1):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            msg = f"EXTRACTION_EXTRA_HEADERS pair {index} is missing the ':' delimiter; expected 'Name:value'."
            raise ValueError(msg)
        raw_name, _, raw_value = pair.partition(":")
        name, header_value = raw_name.strip(), raw_value.strip()
        if not name:
            msg = f"EXTRACTION_EXTRA_HEADERS pair {index} has an empty header name."
            raise ValueError(msg)
        if not set(name) <= _HEADER_NAME_CHARS:
            msg = (
                f"EXTRACTION_EXTRA_HEADERS pair {index} has a header name outside the "
                "permitted token characters (letters, digits, and !#$%&'*+-.^_`|~)."
            )
            raise ValueError(msg)
        lowered = name.lower()
        if lowered in owned:
            msg = (
                f"EXTRACTION_EXTRA_HEADERS pair {index} sets a header the transport itself "
                "controls, or the configured auth header; it may not be overridden here."
            )
            raise ValueError(msg)
        if lowered in seen:
            msg = f"EXTRACTION_EXTRA_HEADERS pair {index} repeats a header name an earlier pair already set."
            raise ValueError(msg)
        if not header_value:
            msg = f"EXTRACTION_EXTRA_HEADERS pair {index} has an empty header value."
            raise ValueError(msg)
        if any(not (" " <= character <= "~") for character in header_value):
            msg = (
                f"EXTRACTION_EXTRA_HEADERS pair {index} has a header value containing a "
                "character outside visible ASCII; control characters would split the request."
            )
            raise ValueError(msg)
        seen.add(lowered)
        pairs.append((name, header_value))
    return tuple(pairs)


def _resolve_extraction_base_url(raw: str) -> str:
    """Normalize the extraction endpoint, refusing userinfo.

    `https://user:secret@gateway/v1` would put a credential in a setting that
    is not a secret -- bound for a ConfigMap, an argument list, and every log
    line that reports the effective endpoint. The message cannot echo the URL
    for the same reason.
    """
    url = raw.strip()
    if not url:
        return ""
    scheme, separator, remainder = url.partition("//")
    authority = (remainder if separator else scheme).partition("/")[0]
    if "@" in authority:
        msg = (
            "EXTRACTION_BASE_URL may not carry userinfo (a 'user:password@host' prefix); "
            "supply the credential through EXTRACTION_API_KEY instead, which is held as a secret."
        )
        raise ValueError(msg)
    return url


def _resolve_extraction_auth_template(raw: str) -> str:
    """Normalize the auth-header template, requiring exactly one `{key}`.

    Zero occurrences is the failure worth catching: it means the operator
    pasted the credential itself into a setting that is not a secret, rather
    than the placeholder the credential gets substituted into. More than one
    means the credential would be written into the header twice. Substitution
    is `str.replace`, never `str.format`, so a credential containing a brace
    cannot make the template blow up or re-enter formatting.
    """
    template = raw.strip()
    if not template:
        return ""
    if template.count("{key}") != 1:
        msg = (
            "EXTRACTION_AUTH_TEMPLATE must contain the literal '{key}' exactly once; "
            "it is the placeholder the credential is substituted into, not a place to paste one."
        )
        raise ValueError(msg)
    return template


def _resolve_embedding_provider(raw_provider: str | None, model: str) -> str:
    """Pick the embedding provider, honouring the superseded spelling.

    `EMBEDDING_MODEL=stub` used to be how an operator asked for zero vectors,
    before the provider became a setting of its own. Deployments and runbooks
    still carry it, so it keeps working — but it is ambiguous (a model id doing
    double duty as an implementation switch) and is reported as deprecated.
    """
    provider = (raw_provider or "").strip().lower()
    if provider:
        return provider
    if model == "stub":
        logging.getLogger(__name__).warning(
            "EMBEDDING_MODEL=stub is deprecated and will stop selecting the stub embedder; "
            "set EMBEDDING_PROVIDER=stub instead"
        )
        return "stub"
    return "onnx"


def _parse_role_mapping(value: str | None) -> dict[str, str]:
    """Parse `EXTERNAL:internal,EXTERNAL:internal,...` into a dict.

    Pairs missing a colon raise ValueError immediately. Whitespace surrounding
    keys and values is stripped. Duplicate external keys take last-wins —
    legitimate during LDAP rename rollouts where old and new strings ship
    concurrently. Semantic validation (non-empty, internal role membership)
    happens in a model validator so direct-dict construction in tests is
    also covered.
    """
    if not value:
        return {}
    result: dict[str, str] = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"ENTITLEMENT_ROLE_MAPPING pair {pair!r} is missing the ':' delimiter; " "expected 'EXTERNAL:internal'."
            )
        external, internal = pair.split(":", maxsplit=1)
        result[external.strip()] = internal.strip()
    return result
