"""Pinned HTTP status + error-envelope code for every exception the REST API translates.

This is a regression gate for the exception-hierarchy rebase: every row here is
read directly off ``contextplane/api/errors.py::map_catalog_error`` and the
router-local ``except`` arms that translate a domain exception into an
``HTTPException`` without going through that shared helper. The table must
pass unchanged before and after the rebase — a row's status/code/message
changing means the rebase altered an observable API contract, which is the
one thing it must not do (the sole deliberate exception is the
``contextplane.arc.types`` vocabulary-error rename, called out below).

Four surfaces are covered:
1. ``map_catalog_error`` itself — the one helper most routers call.
2. ``contextplane.api.routers.workspaces``'s ``_ws_exc_to_http`` — special-cased
   ahead of ``map_catalog_error`` for the workspace-perceivability trio and
   ``WorkspacePiiBlocked``'s structured body.
3. ``contextplane.api.routers.memory``'s ``_translate`` — a second, narrower
   translator that maps ``ValidationError`` to 400 (not 422) and lets
   anything else propagate unchanged; pinned exactly as it stands today.
4. A handful of standalone, inline router translators for the roots this
   task rebases: cursor decoding, usage-range validation, and the ARC
   manifest vocabulary check.
"""

from __future__ import annotations

import datetime
import types
import uuid

import pytest
from fastapi import HTTPException

from contextplane.api.errors import coerce_to_envelope, map_catalog_error
from contextplane.api.routers.admin_usage import _window
from contextplane.api.routers.arc import AttestationBody, ManifestBody, ResolveContextRequest, resolve_context
from contextplane.api.routers.capabilities import patch_capability
from contextplane.api.routers.memory import _translate
from contextplane.api.routers.workspaces import _cursor_exc_to_http, _ws_exc_to_http
from contextplane.api.schemas.catalog import UpdateEntityRequest
from contextplane.arc.types import ArcVocabularyError
from contextplane.exceptions import (
    CatalogError,
    ConflictError,
    LifecycleError,
    NotFoundError,
    TenantIsolationError,
    ValidationError,
    VocabularyError,
)
from contextplane.pagination import InvalidCursorError
from contextplane.service.catalog.progression import ProgressionError
from contextplane.service.workspace.core import WorkspaceNotFound, WorkspaceOperationDenied
from contextplane.service.workspace.entries import WorkspacePiiBlocked
from contextplane.types import TenantContext
from contextplane.usage import reads

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _envelope(http_exc: HTTPException) -> dict:
    return coerce_to_envelope(http_exc.status_code, http_exc.detail)


# ---------------------------------------------------------------------------
# Table 1 — map_catalog_error(): the shared translator, called from every
# router that does `except CatalogError as exc: raise map_catalog_error(exc)`.
# ---------------------------------------------------------------------------

_MAP_CATALOG_ERROR_TABLE = [
    pytest.param(NotFoundError("widget missing"), 404, "not_found", "widget missing", id="NotFoundError"),
    # TenantIsolationError's message is deliberately NOT str(exc) — leaking
    # anything here would confirm cross-tenant existence to the caller.
    pytest.param(TenantIsolationError("cross-tenant peek"), 404, "not_found", "not found", id="TenantIsolationError"),
    pytest.param(ConflictError("dup key"), 409, "conflict", "dup key", id="ConflictError"),
    pytest.param(ValidationError("bad shape"), 422, "validation_error", "bad shape", id="ValidationError"),
    pytest.param(VocabularyError("bad vocab"), 422, "validation_error", "bad vocab", id="VocabularyError"),
    pytest.param(LifecycleError("bad transition"), 422, "validation_error", "bad transition", id="LifecycleError"),
    pytest.param(PermissionError("nope"), 403, "forbidden", "nope", id="PermissionError"),
    # A bare CatalogError (no subclass) falls to the generic 400 branch.
    pytest.param(CatalogError("generic catalog"), 400, "bad_request", "generic catalog", id="CatalogError-bare"),
    # A wholly unrelated exception routed through the helper still gets a
    # 400 — the caller chose to funnel it here rather than let it propagate.
    pytest.param(RuntimeError("totally unrelated"), 400, "bad_request", "totally unrelated", id="non-CatalogError"),
]


@pytest.mark.parametrize("exc, expected_status, expected_code, expected_message", _MAP_CATALOG_ERROR_TABLE)
def test_map_catalog_error_contract(exc, expected_status, expected_code, expected_message) -> None:
    http_exc = map_catalog_error(exc)
    assert http_exc.status_code == expected_status
    envelope = _envelope(http_exc)
    assert envelope == {"errors": [{"path": None, "code": expected_code, "message": expected_message}]}


# ---------------------------------------------------------------------------
# Table 2 — workspaces._ws_exc_to_http: the workspace trio + WorkspacePiiBlocked.
# ---------------------------------------------------------------------------


def test_ws_exc_to_http_workspace_not_found() -> None:
    http_exc = _ws_exc_to_http(WorkspaceNotFound("Workspace 123 not found."))
    assert http_exc.status_code == 404
    assert _envelope(http_exc) == {
        "errors": [{"path": None, "code": "not_found", "message": "Workspace 123 not found."}]
    }


def test_ws_exc_to_http_workspace_operation_denied() -> None:
    http_exc = _ws_exc_to_http(WorkspaceOperationDenied("Only admins may delete tenant-owned workspaces."))
    assert http_exc.status_code == 403
    assert _envelope(http_exc) == {
        "errors": [{"path": None, "code": "forbidden", "message": "Only admins may delete tenant-owned workspaces."}]
    }


def test_ws_exc_to_http_pii_blocked_exact_body() -> None:
    """WorkspacePiiBlocked's HTTPException.detail is a structured dict, not a
    plain string — API clients and the MCP tool adapter parse this shape
    directly. It intentionally has no ``message`` key, so it is NOT run
    through ``coerce_to_envelope`` here: asserting on the raw ``detail`` is
    the contract this router deliberately bypasses the generic envelope for.
    """
    exc = WorkspacePiiBlocked(field="workspace_entry.body", categories=["email", "ssn"])
    http_exc = _ws_exc_to_http(exc)
    assert http_exc.status_code == 422
    assert http_exc.detail == {
        "code": "pii_detected",
        "field": "workspace_entry.body",
        "categories": ["email", "ssn"],
    }


@pytest.mark.parametrize(
    "exc, expected_status, expected_code, expected_message",
    [
        pytest.param(NotFoundError("entry gone"), 404, "not_found", "entry gone", id="NotFoundError-delegated"),
        pytest.param(
            ValidationError("invalid kind"), 422, "validation_error", "invalid kind", id="ValidationError-delegated"
        ),
        pytest.param(PermissionError("no"), 403, "forbidden", "no", id="PermissionError-delegated"),
    ],
)
def test_ws_exc_to_http_delegates_rest_to_map_catalog_error(
    exc, expected_status, expected_code, expected_message
) -> None:
    http_exc = _ws_exc_to_http(exc)
    assert http_exc.status_code == expected_status
    assert _envelope(http_exc) == {"errors": [{"path": None, "code": expected_code, "message": expected_message}]}


# ---------------------------------------------------------------------------
# Table 3 — memory.py's _translate: a second, narrower translator.
# Pinned exactly as-is: ValidationError maps to 400 here, not 422 — a
# pre-existing drift from map_catalog_error's table, not something this
# task changes.
# ---------------------------------------------------------------------------


def test_memory_translate_not_found() -> None:
    http_exc = _translate(NotFoundError("session event 9 not found"))
    assert isinstance(http_exc, HTTPException)
    assert http_exc.status_code == 404
    assert _envelope(http_exc) == {"errors": [{"path": None, "code": "not_found", "message": "not found"}]}


def test_memory_translate_validation_error_maps_to_400() -> None:
    http_exc = _translate(ValidationError("bad body"))
    assert isinstance(http_exc, HTTPException)
    assert http_exc.status_code == 400
    assert _envelope(http_exc) == {"errors": [{"path": None, "code": "validation_error", "message": "bad body"}]}


def test_memory_translate_passes_through_unmapped_exceptions() -> None:
    """Anything that is neither NotFoundError nor ValidationError comes back
    unchanged — the router re-raises it and it falls to the global 500
    handler."""
    exc = RuntimeError("boom")
    assert _translate(exc) is exc


# ---------------------------------------------------------------------------
# Table 4 — standalone inline translators for the rebased roots.
# ---------------------------------------------------------------------------


def test_invalid_cursor_error_maps_to_422_invalid_cursor() -> None:
    """workspaces._cursor_exc_to_http is the same shape every list endpoint's
    cursor handling uses (graph.py, artifacts.py, retrieval.py, admin_audit.py).
    The message is a fixed string, not str(exc) — the cursor's raw contents
    never reach the client."""
    http_exc = _cursor_exc_to_http(InvalidCursorError("garbage"))
    assert http_exc.status_code == 422
    assert _envelope(http_exc) == {
        "errors": [
            {
                "path": None,
                "code": "invalid_cursor",
                "message": "The cursor value is invalid or has been tampered with.",
            }
        ]
    }


def test_invalid_range_error_maps_to_422() -> None:
    """admin_usage.py's _window: end-before-start raises InvalidRangeError,
    mapped to a plain 422 HTTPException (not build_error — the default
    status-derived code applies once this reaches the global handler)."""
    with pytest.raises(HTTPException) as exc_info:
        _window(start=datetime.date(2026, 1, 10), end=datetime.date(2026, 1, 1))
    assert exc_info.value.status_code == 422
    assert (
        exc_info.value.detail
        == f"window ends before it starts: {datetime.date(2026, 1, 10)} to {datetime.date(2026, 1, 1)}"
    )


def test_range_too_wide_error_maps_to_422() -> None:
    start = datetime.date(2020, 1, 1)
    end = start + datetime.timedelta(days=reads.MAX_RANGE_DAYS + 10)
    with pytest.raises(HTTPException) as exc_info:
        _window(start=start, end=end)
    assert exc_info.value.status_code == 422
    days = (end - start).days + 1
    assert exc_info.value.detail == f"window of {days} days exceeds the {reads.MAX_RANGE_DAYS}-day maximum"


def test_progression_error_maps_to_422_progression_rejected() -> None:
    """capabilities.py's patch_capability: ProgressionError gets a richer body
    than map_catalog_error would produce (a structured reason), which is why
    it has its own except arm ahead of `except CatalogError`."""
    entity_id = uuid.uuid4()
    service = types.SimpleNamespace(
        resolve_entity_handle=_async_return(types.SimpleNamespace(entity_id=entity_id)),
        get_full_capability=_async_return(
            types.SimpleNamespace(entity=types.SimpleNamespace(entity_id=entity_id, created_at=_NOW), facts=[])
        ),
        update_entity=_async_raise(ProgressionError("tier_not_resolvable")),
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(catalog=service)), headers={})
    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])
    body = UpdateEntityRequest(updates={"stage_progression": "ga"})

    with pytest.raises(HTTPException) as exc_info:
        import asyncio

        asyncio.run(patch_capability(str(entity_id), body, request, ctx))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {"code": "progression_rejected", "reason": "tier_not_resolvable"}


def test_arc_vocabulary_error_maps_to_400_invalid_manifest() -> None:
    """arc.py's resolve_context: an unknown task_kind raises the
    contextplane.arc.types vocabulary error (renamed to ArcVocabularyError by
    this task), mapped to 400 with code "invalid_manifest" so the caller
    is told their manifest was rejected rather than getting a bare 403.
    """
    request = types.SimpleNamespace(
        state=types.SimpleNamespace(oidc_claims={"iss": "https://issuer.example.test"}),
        headers={"x-arc-host-id": "host-1"},
    )
    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["consumer"])
    body = ResolveContextRequest(
        manifest=ManifestBody(
            session_id="session-1",
            task_kind="not_a_real_task_kind",
            environment="prod",
            data_sensitivity="none",
            repository_identity="repo://example",
        ),
        attestation=AttestationBody(
            profile="p1",
            signer_key_id="key-1",
            attestation_id="att-1",
            issued_at=_NOW,
            expires_at=_NOW + datetime.timedelta(hours=1),
            payload={},
            signature="deadbeef",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        import asyncio

        asyncio.run(resolve_context(request, body, ctx))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == [
        {
            "path": None,
            "code": "invalid_manifest",
            "message": (
                "unknown task kind 'not_a_real_task_kind'; the vocabulary is closed "
                "so a host cannot name a lower-risk value to escape an obligation"
            ),
        }
    ]


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _async_raise(exc: BaseException):
    async def _inner(*_args, **_kwargs):
        raise exc

    return _inner


# ---------------------------------------------------------------------------
# contextplane.arc.types.VocabularyError was renamed to ArcVocabularyError by this
# task (it collided by name with contextplane.exceptions.VocabularyError, a
# different exception under a different base). This class-identity check —
# and the `from contextplane.arc.types import ArcVocabularyError` line above — is
# the one deliberate diff this file carries across the rebase.
# ---------------------------------------------------------------------------


def test_arc_types_vocabulary_error_is_the_class_the_router_catches() -> None:
    from contextplane.arc.types import parse_task_kind

    with pytest.raises(ArcVocabularyError):
        parse_task_kind("not_a_real_task_kind")
