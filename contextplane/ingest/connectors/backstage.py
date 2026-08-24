"""Backstage software-catalog connector.

E12-T1. Reads entities from a Backstage instance's catalog API and turns each
one into a `ParsedFact` with `category='software_catalog_entity'`.

`source.config` must contain:
    ``base_url``  -- the Backstage backend origin, e.g. ``https://backstage.acme.io``
Optional:
    ``kinds``      -- list of entity kinds to include, e.g. ``["Component", "API"]``
    ``page_size``  -- entities per request (default 100)

`source.credentials_ref` names an env var holding a Backstage static token,
sent as ``Authorization: Bearer``. Backstage instances behind an auth proxy
need none, in which case leave it unset.

## Three decisions the API's own documentation forces

**The artifact id is the entity ref, not the uid.** The catalog assigns `uid`
and the descriptor format says plainly that it *"can change over time"* and
"shouldn't be used as an external reference". `kind:namespace/name` is the
reference Backstage itself uses everywhere, and it is what survives a
re-ingestion that mints a new uid. Using the uid would produce a new entity on
this side every time the other side re-registered the same component.

**`metadata.etag` is the content revision.** `DiscoveredArtifact` keeps that
field for exactly this: *"`source_url` names a place, and a place backed by a
mutable ref points at different bytes over time; a blob id changes if and only
if the content does."* Backstage's etag is catalog-generated and moves with the
entity's content, so a sync can tell a re-listed entity from a changed one
without fetching it.

**`discover` lists and `fetch` re-reads.** The list endpoint returns whole
entities, so embedding them the way `release_notes.py` does would save a
request. It is not worth it here: an entity is unbounded in size, the list is
paged, and `source_url` would stop naming a place. Re-reading by ref also means
`fetch` sees whatever the catalog holds *now*, which is the honest thing for a
mutable source — and `content_revision` is how a caller notices it moved.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from contextplane.ingest.connector import (
    Connector,
    DiscoveredArtifact,
    ParsedFact,
    resolve_credential,
)

if TYPE_CHECKING:
    from contextplane.storage.models import SyncSource

#: Fixed forever: `entity_id` derivation has to be reproducible across runs, or
#: every sync writes about a different subject than the last one did.
_BACKSTAGE_NS: Final = uuid.UUID("6f3c1d2e-8b47-5a91-9d0e-2c7a4b8f1e63")

_CATEGORY: Final = "software_catalog_entity"

#: The paged query endpoint. `/entities` also exists and is the older,
#: offset-paged form; the cursor endpoint is the one the docs recommend and is
#: the only one that cannot skip or repeat a row when the catalog changes
#: mid-page.
_BY_QUERY: Final = "/api/catalog/entities/by-query"

#: Read back by the reference that survives re-ingestion. See the module
#: docstring on why this is not the uid.
_BY_NAME: Final = "/api/catalog/entities/by-name"

_DEFAULT_PAGE_SIZE: Final = 100

#: Where the entity was ingested *from*, which is a more useful `source_url` for
#: a reader than the catalog's own address: it names the file somebody edits.
_LOCATION_ANNOTATION: Final = "backstage.io/managed-by-location"


class BackstageConnector(Connector):
    """Backstage catalog entities as observed context."""

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Every entity the catalog will show us, cursor-paged.

        Kinds are filtered server-side rather than here: a tenant syncing only
        components should not pay to transfer every user and group, and
        Backstage's own filter syntax is the one its operators already know.
        """
        config: dict[str, Any] = source.config
        base_url: str = str(config["base_url"]).rstrip("/")
        page_size = int(config.get("page_size", _DEFAULT_PAGE_SIZE))
        kinds: list[str] = list(config.get("kinds") or [])

        params: dict[str, Any] = {"limit": page_size}
        if kinds:
            # One `filter` per kind is Backstage's OR form; a single filter with
            # several kinds would mean AND, which selects nothing.
            params["filter"] = [f"kind={kind}" for kind in kinds]

        artifacts: list[DiscoveredArtifact] = []
        async with httpx.AsyncClient() as client:
            cursor: str | None = None
            while True:
                page_params = dict(params)
                if cursor:
                    page_params["cursor"] = cursor
                response = await client.get(
                    f"{base_url}{_BY_QUERY}",
                    headers=_headers(_token(source.credentials_ref)),
                    params=page_params,
                )
                response.raise_for_status()
                page: dict[str, Any] = response.json()
                for entity in page.get("items", []):
                    artifacts.append(_artifact_for(base_url, entity))
                cursor = (page.get("pageInfo") or {}).get("nextCursor")
                if not cursor:
                    break
        return artifacts

    async def fetch(self, artifact: DiscoveredArtifact, source: SyncSource) -> bytes:
        """The entity as the catalog holds it now, by reference."""
        base_url: str = str(source.config["base_url"]).rstrip("/")
        kind, namespace, name = _split_ref(artifact.artifact_id)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}{_BY_NAME}/{kind}/{namespace}/{name}",
                headers=_headers(_token(source.credentials_ref)),
            )
            response.raise_for_status()
        return response.content

    def parse(self, artifact: DiscoveredArtifact, raw: bytes) -> list[ParsedFact]:
        """One fact per entity, summarising what the catalog asserts about it.

        The body is prose rather than the raw descriptor. A claim's value is
        read by people and by extraction; a YAML blob reproduced verbatim
        carries the same information in a form neither can use, and it would
        also carry the catalog's bookkeeping fields as though they were
        assertions about the component.
        """
        entity: dict[str, Any] = json.loads(raw.decode("utf-8"))
        metadata: dict[str, Any] = entity.get("metadata") or {}
        spec: dict[str, Any] = entity.get("spec") or {}

        kind = str(entity.get("kind", "Component"))
        namespace = str(metadata.get("namespace") or "default")
        name = str(metadata.get("name", ""))
        ref = f"{kind.lower()}:{namespace}/{name}"

        lines = [f"# {metadata.get('title') or name}", "", f"{kind} `{ref}` in the Backstage catalog."]
        if description := metadata.get("description"):
            lines += ["", str(description)]

        # Only the spec fields that say something about ownership or shape. A
        # dump of every key would put the catalog's own plumbing into a claim.
        for field in ("type", "lifecycle", "owner", "system", "domain"):
            if value := spec.get(field):
                lines.append(f"- {field}: {value}")
        if tags := metadata.get("tags"):
            lines.append(f"- tags: {', '.join(str(tag) for tag in tags)}")
        if links := metadata.get("links"):
            for link in links:
                if isinstance(link, dict) and link.get("url"):
                    lines.append(f"- link: {link.get('title') or link['url']} <{link['url']}>")

        return [
            ParsedFact(
                entity_id=uuid.uuid5(_BACKSTAGE_NS, ref),
                category=_CATEGORY,
                body="\n".join(lines),
                # A catalog entity carries no assertion about when it became
                # true, only about when the catalog last saw it. Left absent
                # rather than filled with the sync's own clock: a server-set
                # `valid_from` is indistinguishable afterwards from one the
                # source stated, which is the property E12-T2 exists to keep.
                valid_from=_ingested_at(metadata),
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    async def validate(self, credentials_ref: str | None) -> None:
        """One entity's worth of catalog, to prove the token is accepted."""
        if credentials_ref is None:
            return
        resolve_credential(credentials_ref)


def _token(credentials_ref: str | None) -> str | None:
    return resolve_credential(credentials_ref) if credentials_ref else None


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _artifact_for(base_url: str, entity: dict[str, Any]) -> DiscoveredArtifact:
    metadata: dict[str, Any] = entity.get("metadata") or {}
    kind = str(entity.get("kind", "Component")).lower()
    namespace = str(metadata.get("namespace") or "default")
    name = str(metadata.get("name", ""))
    annotations: dict[str, Any] = metadata.get("annotations") or {}
    return DiscoveredArtifact(
        artifact_id=f"{kind}:{namespace}/{name}",
        # The descriptor's own location where the catalog knows it, because that
        # is the file a reader would open; the catalog's API address otherwise.
        source_url=str(annotations.get(_LOCATION_ANNOTATION) or f"{base_url}{_BY_NAME}/{kind}/{namespace}/{name}"),
        artifact_type=str(entity.get("kind", "Component")),
        content_revision=_etag(metadata),
    )


def _etag(metadata: dict[str, Any]) -> str | None:
    etag = metadata.get("etag")
    return str(etag) if etag else None


def _split_ref(ref: str) -> tuple[str, str, str]:
    """`kind:namespace/name` back into its three parts.

    Raises rather than guessing at a malformed ref: an artifact id this
    connector did not mint means something upstream is handing us references
    from somewhere else, and defaulting the missing part would attach the
    resulting claims to the wrong entity silently.
    """
    kind, _, rest = ref.partition(":")
    namespace, _, name = rest.partition("/")
    if not (kind and namespace and name):
        msg = f"not a Backstage entity reference: {ref!r}"
        raise ValueError(msg)
    return kind, namespace, name


def _ingested_at(metadata: dict[str, Any]) -> datetime | None:
    """The catalog's own timestamp when it publishes one, and nothing otherwise.

    Some Backstage deployments annotate the ingestion time; most do not. Absent
    is the honest answer for an entity whose descriptor states no validity, and
    the alternative -- this process's clock -- would look afterwards exactly
    like a time the source had asserted.
    """
    annotations: dict[str, Any] = metadata.get("annotations") or {}
    stamped = annotations.get("backstage.io/created-at") or annotations.get("contextplane.io/observed-at")
    if not stamped:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamped).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
