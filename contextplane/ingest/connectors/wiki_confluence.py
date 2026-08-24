"""Confluence Cloud wiki connector.

E12-T1. Reads pages from a Confluence Cloud site through REST API v2 and turns
each one into a `ParsedFact` with `category='wiki_page'`.

`source.config` must contain:
    ``base_url``  -- the site origin, e.g. ``https://acme.atlassian.net``
Optional:
    ``space_ids``  -- numeric space ids to restrict the read to
    ``page_size``  -- pages per request (default 100)

`source.credentials_ref` names an env var holding ``email:api_token`` for HTTP
Basic, which is what Atlassian Cloud documents for API tokens.

## Why a wiki is the connector that most needs its authority declared

`source_governance.py` states the rule this connector is the example in: *"A
Confluence page is not an owner's OpenAPI sync, and the difference decides
conflicts for the lifetime of every claim the source produces."* Nothing here
sets that tier -- registration does, before the first write, and this connector
would be wrong to imply otherwise. What it can do is not overstate what it
found, which is why the body below is the page's text and not a summary of it.

## Three decisions

**`version.number` is the content revision.** A Confluence page version
increments if and only if the page changed, which is what
`DiscoveredArtifact.content_revision` is for -- a place backed by a mutable ref
points at different bytes over time, and this is the field that says whether it
did.

**Storage format, converted to text.** `body-format=storage` returns Confluence's
XHTML-ish storage representation. Served raw it would put markup into a claim's
value, where a reader and an extractor both have to strip it; `atlas_doc_format`
would put JSON there instead. Tags are removed and entities decoded here, once,
where the decision is visible.

**`version.createdAt` is `valid_from`.** That is when this revision of the page
came into being, stated by the source rather than by this process's clock --
the property E12-T2 turns into a schema constraint. An unversioned page has no
such instant and gets none.
"""

from __future__ import annotations

import base64
import html
import json
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from contextplane.ingest.connector import (
    Connector,
    CredentialError,
    DiscoveredArtifact,
    ParsedFact,
    resolve_credential,
)

if TYPE_CHECKING:
    from contextplane.storage.models import SyncSource

#: Fixed forever, so a re-sync writes about the same subject as the last one.
_WIKI_NS: Final = uuid.UUID("8c41b7f0-3d29-5e64-a17b-6f8250c93ade")

_CATEGORY: Final = "wiki_page"

_PAGES: Final = "/wiki/api/v2/pages"

_DEFAULT_PAGE_SIZE: Final = 100

#: Storage-format markup, stripped in `parse`. Deliberately not a general HTML
#: parser: this runs on every page of a corpus, the input is Confluence's own
#: well-formed output rather than arbitrary web HTML, and a dependency that can
#: execute or fetch on parse would break `parse`'s purity contract.
_TAG: Final = re.compile(r"<[^>]+>")
_BLOCK_END: Final = re.compile(r"</(p|h[1-6]|li|tr|div|blockquote)>", re.IGNORECASE)
_BLANK_RUN: Final = re.compile(r"\n{3,}")


class ConfluenceWikiConnector(Connector):
    """Confluence pages as observed context."""

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Every current page in scope, cursor-paged.

        Only `status=current`: an archived or trashed page is content somebody
        deliberately withdrew, and syncing it would reintroduce as observed
        context exactly what a person decided should stop being read.
        """
        config: dict[str, Any] = source.config
        base_url = str(config["base_url"]).rstrip("/")
        page_size = int(config.get("page_size", _DEFAULT_PAGE_SIZE))
        space_ids = [str(space) for space in (config.get("space_ids") or [])]

        params: dict[str, Any] = {"limit": page_size, "status": "current"}
        if space_ids:
            params["space-id"] = space_ids

        artifacts: list[DiscoveredArtifact] = []
        async with httpx.AsyncClient() as client:
            url: str | None = f"{base_url}{_PAGES}"
            page_params: dict[str, Any] | None = params
            while url:
                response = await client.get(url, headers=_headers(source.credentials_ref), params=page_params)
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                for page in body.get("results", []):
                    page_id = str(page.get("id", ""))
                    if not page_id:
                        continue
                    artifacts.append(
                        DiscoveredArtifact(
                            artifact_id=page_id,
                            source_url=f"{base_url}{_PAGES}/{page_id}",
                            artifact_type="page",
                            content_revision=_version_number(page),
                        )
                    )
                # `_links.next` is a path relative to the site, and it already
                # carries the cursor -- so the params must not be sent again or
                # they would be appended a second time.
                nxt = (body.get("_links") or {}).get("next")
                url = f"{base_url}{nxt}" if nxt else None
                page_params = None
        return artifacts

    async def fetch(self, artifact: DiscoveredArtifact, source: SyncSource) -> bytes:
        """One page with its storage-format body."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                artifact.source_url,
                headers=_headers(source.credentials_ref),
                params={"body-format": "storage"},
            )
            response.raise_for_status()
        return response.content

    def parse(self, artifact: DiscoveredArtifact, raw: bytes) -> list[ParsedFact]:
        """One fact per page: its title, and its text without the markup."""
        page: dict[str, Any] = json.loads(raw.decode("utf-8"))
        page_id = str(page.get("id") or artifact.artifact_id)
        title = str(page.get("title") or f"page {page_id}")
        storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""

        text = _to_text(str(storage))
        body = f"# {title}\n\n{text}".rstrip() if text else f"# {title}"

        return [
            ParsedFact(
                entity_id=uuid.uuid5(_WIKI_NS, f"confluence::{page_id}"),
                category=_CATEGORY,
                body=body,
                valid_from=_version_created_at(page),
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    async def validate(self, credentials_ref: str | None) -> None:
        """Confirm the credential is present and correctly shaped.

        Refused here rather than at the first request for the same reason the
        CMDB connector refuses: a malformed variable is the operator who set it,
        a rejected one is whoever owns the account, and a 401 cannot tell them
        apart.
        """
        if credentials_ref is None:
            msg = "Confluence Cloud has no anonymous API access; set credentials_ref"
            raise CredentialError(msg)
        _basic(resolve_credential(credentials_ref))


def _headers(credentials_ref: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if credentials_ref:
        headers["Authorization"] = f"Basic {_basic(resolve_credential(credentials_ref))}"
    return headers


def _basic(credential: str) -> str:
    """`email:api_token` as the Basic payload, refusing anything else."""
    if ":" not in credential:
        msg = "a Confluence credential must be 'email:api_token' for HTTP Basic"
        raise CredentialError(msg)
    return base64.b64encode(credential.encode("utf-8")).decode("ascii")


def _version_number(page: dict[str, Any]) -> str | None:
    version = page.get("version")
    if isinstance(version, dict) and version.get("number") is not None:
        return str(version["number"])
    return None


def _version_created_at(page: dict[str, Any]) -> datetime | None:
    version = page.get("version")
    stamped = version.get("createdAt") if isinstance(version, dict) else None
    if not stamped:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamped).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_text(storage: str) -> str:
    """Confluence storage format as readable text.

    Block ends become newlines before tags are stripped, so a page's structure
    survives as paragraphs instead of collapsing into one line. Entities are
    decoded last, so a `&lt;p&gt;` written *as text* by an author is not
    mistaken for markup and removed.
    """
    with_breaks = _BLOCK_END.sub("\n\n", storage)
    with_breaks = re.sub(r"<br\s*/?>", "\n", with_breaks, flags=re.IGNORECASE)
    stripped = _TAG.sub("", with_breaks)
    decoded = html.unescape(stripped)
    return _BLANK_RUN.sub("\n\n", decoded).strip()
