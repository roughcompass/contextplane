"""The receipt MCP tools' JSON projections, without a database.

These tools exist because a REST caller could read a receipt and an agent could
not. What they return is therefore the point: an agent reads the JSON and has no
schema to consult, so a renamed or dropped field is a silent behaviour change for
the only consumer that matters.

Testable without Postgres because the projection is a pure function of a row. The
service call itself is covered by the integration suite; **what is covered here is
the part that a passing integration test would not notice** — a field quietly
renamed on the way out, or a `None` rendered as the string `"None"`.
"""

from __future__ import annotations

import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from contextplane.api.mcp.tools.receipts import _receipt_json

_RESOLVED_AT = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.UTC)


def _row(**overrides: Any) -> SimpleNamespace:
    """A receipt row shaped like the ORM object, with only what the projection reads."""
    fields: dict[str, Any] = {
        "receipt_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "intent_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "state": "complete",
        "cacheable": True,
        "resolved_at": _RESOLVED_AT,
        "requested_by": "agent-a",
        "request_digest": "abc123",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_the_projection_carries_every_field_the_rest_response_does() -> None:
    """Field-for-field with `ReceiptResponse`.

    Named explicitly rather than compared by reflection: a reflective check would
    pass if both sides lost the same field, which is the case worth failing on.
    """
    body = _receipt_json(_row())

    assert set(body) == {
        "receipt_id",
        "intent_id",
        "state",
        "cacheable",
        "resolved_at",
        "requested_by",
        "request_digest",
    }


def test_identifiers_are_strings_not_uuid_repr() -> None:
    """A UUID rendered by `str()` is the wire form; rendered by `repr()` it is not.

    Both look plausible in a log and only one round-trips.
    """
    body = _receipt_json(_row())

    assert body["receipt_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["intent_id"] == "22222222-2222-2222-2222-222222222222"


def test_an_absent_task_id_is_null_not_the_string_none() -> None:
    """A receipt need not describe a task, and `"None"` is a truthy string.

    An agent branching on this field would treat the string as a real id, which
    is the failure that survives every test that only checks the happy path.
    """
    body = _receipt_json(_row(intent_id=None))

    assert body["intent_id"] is None
    assert json.dumps(body)  # serialisable, so the tool can actually return it


def test_the_timestamp_is_iso_8601_and_keeps_its_offset() -> None:
    """A naive timestamp is a different instant to whoever reads it.

    The offset is what makes the receipt's `resolved_at` comparable to the moment
    the caller resolved at.
    """
    body = _receipt_json(_row())

    assert body["resolved_at"] == "2026-03-04T05:06:07+00:00"
    assert datetime.datetime.fromisoformat(body["resolved_at"]).tzinfo is not None


@pytest.mark.parametrize("cacheable", [True, False])
def test_cacheable_stays_a_boolean(cacheable: bool) -> None:
    """Carried as a boolean, not a truthy string.

    A caller deciding whether to cache a degraded answer reads this field, and
    `"false"` is true.
    """
    body = _receipt_json(_row(cacheable=cacheable))

    assert body["cacheable"] is cacheable


def test_the_request_digest_survives_verbatim() -> None:
    """The digest is what makes two resolutions comparable; a reformatted digest is a different one."""
    body = _receipt_json(_row(request_digest="deadbeef"))

    assert body["request_digest"] == "deadbeef"


def test_a_missing_digest_is_carried_rather_than_invented() -> None:
    """`request_digest` is nullable on rows written before it existed.

    Substituting an empty string would make an unrecorded request look like one
    recorded with no inputs.
    """
    body = _receipt_json(_row(request_digest=None))

    assert body["request_digest"] is None
