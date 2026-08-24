"""ServiceNow CMDB connector.

E12-T1. Reads configuration items from a ServiceNow instance's Table API and
turns each one into a `ParsedFact` with `category='configuration_item'`.

`source.config` must contain:
    ``instance_url``  -- e.g. ``https://acme.service-now.com``
Optional:
    ``table``      -- CMDB table to read (default ``cmdb_ci_service``)
    ``query``      -- a ServiceNow encoded query, passed as ``sysparm_query``
    ``page_size``  -- records per request (default 200)

`source.credentials_ref` names an env var holding ``username:password`` for
HTTP Basic, which is what the Table API documents.

## What this connector refuses to guess

**The table is configuration, not a default with a shrug.** `cmdb_ci_service`
is the default because a *service* is the CI a software catalog has an opinion
about; `cmdb_ci_server` and friends describe infrastructure this system does not
model. An operator pointing at a different table is making a deliberate choice
and the config records it.

**Reference fields are resolved to display values.** A raw reference field is a
`sys_id` and a link, which reads afterwards as an opaque identifier attached to
a claim nobody can check. `sysparm_display_value=all` returns both, and `parse`
uses the display value and keeps the id.

**No `content_revision`.** ServiceNow is not content-addressed: `sys_updated_on`
is a timestamp, and a timestamp answers "when did somebody touch this", not
"are these the same bytes". `DiscoveredArtifact` makes the field optional for
precisely this case, and filling it with a clock would make a
content-comparison downstream silently wrong rather than absent.

**Offset paging is what the API offers, and it can skip.** `sysparm_offset`
over a table somebody is editing can miss a row or repeat one. Recorded here
rather than hidden: a CMDB sync is a periodic full read, so a row missed on one
pass is picked up on the next, and the alternative -- a keyset over `sys_id` --
would impose an ordering the API does not guarantee is stable either.
"""

from __future__ import annotations

import base64
import json
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
_CMDB_NS: Final = uuid.UUID("2d9e6a71-4c38-5f02-8b6d-91af3e75c0d4")

_CATEGORY: Final = "configuration_item"

_TABLE_API: Final = "/api/now/table"

#: A service, because that is the configuration item a software catalog has an
#: opinion about. See the module docstring.
_DEFAULT_TABLE: Final = "cmdb_ci_service"

_DEFAULT_PAGE_SIZE: Final = 200

#: The columns this connector reads, named rather than taking everything. A CMDB
#: table is wide and most of it is ServiceNow's own bookkeeping; asking for the
#: whole row would carry that into a claim and make the transfer proportional to
#: how much the instance has been customised.
_FIELDS: Final = (
    "sys_id",
    "name",
    "short_description",
    "sys_class_name",
    "operational_status",
    "install_status",
    "business_criticality",
    "owned_by",
    "managed_by",
    "support_group",
    "sys_updated_on",
)


class ServiceNowCmdbConnector(Connector):
    """CMDB configuration items as observed context."""

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        """Every configuration item the query selects, offset-paged."""
        config: dict[str, Any] = source.config
        instance = str(config["instance_url"]).rstrip("/")
        table = str(config.get("table") or _DEFAULT_TABLE)
        page_size = int(config.get("page_size", _DEFAULT_PAGE_SIZE))
        query = config.get("query")

        params: dict[str, Any] = {
            "sysparm_limit": page_size,
            "sysparm_fields": ",".join(_FIELDS),
            "sysparm_display_value": "all",
            # The reference links are the half of a reference field that is
            # never readable; the display value below is the half that is.
            "sysparm_exclude_reference_link": "true",
        }
        if query:
            params["sysparm_query"] = str(query)

        artifacts: list[DiscoveredArtifact] = []
        async with httpx.AsyncClient() as client:
            offset = 0
            while True:
                response = await client.get(
                    f"{instance}{_TABLE_API}/{table}",
                    headers=_headers(source.credentials_ref),
                    params={**params, "sysparm_offset": offset},
                )
                response.raise_for_status()
                records: list[dict[str, Any]] = response.json().get("result", [])
                for record in records:
                    sys_id = _plain(record.get("sys_id"))
                    if not sys_id:
                        continue
                    artifacts.append(
                        DiscoveredArtifact(
                            artifact_id=f"{table}/{sys_id}",
                            source_url=f"{instance}{_TABLE_API}/{table}/{sys_id}",
                            artifact_type=_plain(record.get("sys_class_name")) or table,
                            # Deliberately absent -- see the module docstring.
                            content_revision=None,
                        )
                    )
                if len(records) < page_size:
                    break
                offset += page_size
        return artifacts

    async def fetch(self, artifact: DiscoveredArtifact, source: SyncSource) -> bytes:
        """One record, as the instance holds it now."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                artifact.source_url,
                headers=_headers(source.credentials_ref),
                params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"},
            )
            response.raise_for_status()
        return response.content

    def parse(self, artifact: DiscoveredArtifact, raw: bytes) -> list[ParsedFact]:
        """One fact per configuration item.

        `sys_updated_on` becomes `valid_from`, and that is a claim about the
        source record rather than about the world: it is when ServiceNow last
        saw this CI change. Stating it is better than leaving it absent, because
        a CMDB's whole value is that somebody maintains it and the date is how a
        reader judges whether anybody still does.
        """
        document: dict[str, Any] = json.loads(raw.decode("utf-8"))
        record: dict[str, Any] = document.get("result") or document
        if isinstance(record, list):  # a table read rather than a single record
            record = record[0] if record else {}

        sys_id = _plain(record.get("sys_id")) or artifact.artifact_id
        name = _display(record.get("name")) or sys_id

        lines = [f"# {name}", "", f"Configuration item `{sys_id}` in the ServiceNow CMDB."]
        if description := _display(record.get("short_description")):
            lines += ["", description]
        for label, field in (
            ("class", "sys_class_name"),
            ("operational status", "operational_status"),
            ("install status", "install_status"),
            ("business criticality", "business_criticality"),
            ("owned by", "owned_by"),
            ("managed by", "managed_by"),
            ("support group", "support_group"),
        ):
            if value := _display(record.get(field)):
                lines.append(f"- {label}: {value}")

        return [
            ParsedFact(
                entity_id=uuid.uuid5(_CMDB_NS, f"servicenow::{artifact.artifact_id}"),
                category=_CATEGORY,
                body="\n".join(lines),
                valid_from=_updated_at(record),
                source_url=artifact.source_url,
                commit_sha=None,
            )
        ]

    async def validate(self, credentials_ref: str | None) -> None:
        """Confirm the credential is present and correctly shaped.

        Refuses a credential that is not `username:password` here rather than
        letting the instance return a 401 later, because the two failures need
        different people: a malformed variable is the operator who set it, and a
        rejected one is whoever owns the account.
        """
        if credentials_ref is None:
            msg = "the ServiceNow Table API has no anonymous access; set credentials_ref"
            raise CredentialError(msg)
        _basic(resolve_credential(credentials_ref))


def _headers(credentials_ref: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if credentials_ref:
        headers["Authorization"] = f"Basic {_basic(resolve_credential(credentials_ref))}"
    return headers


def _basic(credential: str) -> str:
    """`username:password` as the Basic payload, refusing anything else."""
    if ":" not in credential:
        msg = "a ServiceNow credential must be 'username:password' for HTTP Basic"
        raise CredentialError(msg)
    return base64.b64encode(credential.encode("utf-8")).decode("ascii")


def _plain(value: object) -> str:
    """A field's stored value, whichever shape `sysparm_display_value` produced.

    With `all`, every field arrives as `{"value": ..., "display_value": ...}`;
    with the default it is a bare string. Both are handled because a caller can
    reach `parse` with either, and a `KeyError` several frames down would be a
    worse answer than the value.
    """
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value) if value is not None else ""


def _display(value: object) -> str:
    """What a person should read: the display value, falling back to the id."""
    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "")
    return str(value) if value is not None else ""


def _updated_at(record: dict[str, Any]) -> datetime | None:
    stamped = _plain(record.get("sys_updated_on"))
    if not stamped:
        return None
    # ServiceNow writes `YYYY-MM-DD HH:MM:SS` in the instance's timezone, with
    # no offset. Read as UTC and said so here: guessing a local zone would
    # produce a plausible-looking timestamp that is wrong by hours, which is
    # worse than one that is wrong by a known constant.
    try:
        parsed = datetime.fromisoformat(stamped.replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
