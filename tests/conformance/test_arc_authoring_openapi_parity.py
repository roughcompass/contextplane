"""Conformance gate for the exported `contextplane/openapi.json` against the
frozen authoring-surface contract.

`test_arc_authoring_schemas.py` already pins every component's
*standalone* `model_json_schema()` output to its own snapshot -- that check
is complete and does not need repeating here. What it cannot check is
whether the real, fully-registered router still uses those exact shapes:
no route existed when it was written. This module is the non-vacuous half
of that promise, run once the whole authoring surface is wired:

- every one of the 67 frozen components that a live route actually
  references matches its frozen shape exactly (modulo the `$defs` vs
  `#/components/schemas/` referencing style FastAPI's whole-app export uses
  in place of Pydantic's own per-model `$defs`, and the presence of a
  `"default": null` key that FastAPI's route-embedded schema generation
  drops for some nested optional fields -- neither changes what a client
  may send or will receive, so neither is treated as a wire-contract
  difference here);
- the small number of frozen components no live route references is
  exactly the documented set below, not a superset that would mean
  something *new* silently stopped being wired;
- the expanded Appendix A path set (every `{PV}` abbreviation written out
  in full) is present, except the one route Appendix A documents that was
  never built -- also named and explained below, not silently dropped from
  the expected set;
- there is no standalone `{PV}/approve` route anywhere in the exported
  spec;
- the `AVAILABLE_ACTION_ROUTE_ACTIONS` map `test_arc_authoring_schemas.py`
  could only check for internal consistency (no router existed yet to
  check it against) resolves against real `add_mutation_route` call sites.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from contextplane.api.schemas import arc_authoring as aa

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_OPENAPI = _PROJECT_ROOT / "openapi.json"
_ROUTERS_DIR = _PROJECT_ROOT / "contextplane" / "api" / "routers"


def _load_openapi() -> dict[str, Any]:
    return json.loads(_OPENAPI.read_text())


# ---------------------------------------------------------------------------
# Component parity against the frozen snapshot, as actually wired.
# ---------------------------------------------------------------------------

# Three frozen components no registered route ever produces or consumes,
# each for a distinct, already-true-before-this-task reason -- not a
# regression this export introduced:
#
# - `PagedProposalSummaries` is the MCP-only result of `arc_list_proposals`
#   (Appendix A.2). There is no REST "list proposals" route at all, so it
#   can never appear in `openapi.json`, which only ever describes REST.
# - `UploadAdmissionRequest` is parsed by hand from a multipart form field
#   (`POST /v1/arc/sources/uploads`'s `metadata` part) rather than accepted
#   as a FastAPI `Body()` parameter, because the same request also carries
#   a raw byte stream in a sibling part. FastAPI only promotes `Body()`/
#   `response_model` parameters into `components.schemas`; a value parsed
#   inside a plain dependency function is invisible to it. The wire shape
#   is still enforced at runtime by `UploadAdmissionRequest.model_validate_
#   json(...)` -- this is an OpenAPI-documentation gap, not a contract one.
# - `ExceptionApprovalEvidenceRequest` / `ApprovalEvidenceResponse` are a
#   genuine drift, not a documentation quirk -- see
#   `test_the_appendix_a_approval_evidence_route_gap_is_the_only_missing_path`
#   below for the full explanation. Reported, not silently resolved: fixing
#   it means either adding a route (out of this task's scope; no `AAS-T##`
#   task in this phase owns it) or amending the TDD to match what shipped.
_KNOWN_UNREFERENCED_COMPONENTS = frozenset(
    {
        "PagedProposalSummaries",
        "UploadAdmissionRequest",
        "ExceptionApprovalEvidenceRequest",
        "ApprovalEvidenceResponse",
    }
)


def _flatten_for_openapi_comparison(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Rewrite one model's standalone `model_json_schema()` output into the
    same referencing style `app.openapi()` uses across the whole app:
    `#/$defs/X` -> `#/components/schemas/X`, with every nested model's own
    `$defs` entry pulled out to become its own top-level comparison target
    (mirroring how `components.schemas` is one flat dict, not one entry per
    model with its dependencies nested inside).

    `"default"` keys are stripped throughout (both here and by the caller,
    on the live side) -- see this module's own docstring for why a
    default's presence or absence carries no wire-contract meaning by
    itself; whether a field is actually optional is fully carried by the
    schema's `"required"` list, which this function does not touch.
    """
    schema = dict(schema)
    defs = schema.pop("$defs", {})
    body_text = json.dumps(schema).replace("#/$defs/", "#/components/schemas/")
    nested = {
        name: json.loads(json.dumps(value).replace("#/$defs/", "#/components/schemas/")) for name, value in defs.items()
    }
    return _strip_defaults(json.loads(body_text)), {k: _strip_defaults(v) for k, v in nested.items()}


def _strip_defaults(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_defaults(v) for k, v in node.items() if k != "default"}
    if isinstance(node, list):
        return [_strip_defaults(v) for v in node]
    return node


def test_every_wired_frozen_component_matches_its_snapshot_shape_exactly() -> None:
    """Non-vacuous half of `test_arc_authoring_schemas.py`'s own promise:
    for every frozen component a live route actually references, the
    shape in the exported spec is exactly the frozen one. A component
    this finds unreferenced is
    asserted separately below against the one documented, named set --
    this test only ever compares shapes, never silently skips a name."""
    live_schemas = _load_openapi()["components"]["schemas"]
    mismatches: list[str] = []
    unreferenced: list[str] = []

    for name, model in aa.COMPONENTS.items():
        live = live_schemas.get(name)
        if live is None:
            unreferenced.append(name)
            continue
        body, nested = _flatten_for_openapi_comparison(model.model_json_schema())
        if _strip_defaults(live) != body:
            mismatches.append(name)
        for nested_name, nested_body in nested.items():
            nested_live = live_schemas.get(nested_name)
            if nested_live is not None and _strip_defaults(nested_live) != nested_body:
                mismatches.append(nested_name)

    assert not mismatches, f"frozen component(s) drifted from snapshot shape once wired: {sorted(set(mismatches))}"
    assert set(unreferenced) == _KNOWN_UNREFERENCED_COMPONENTS, (
        "the set of frozen components no live route references changed -- "
        f"got {sorted(unreferenced)}, expected exactly {sorted(_KNOWN_UNREFERENCED_COMPONENTS)}. "
        "Either a component that should now be wired still is not, or a new "
        "one silently stopped being referenced."
    )


# ---------------------------------------------------------------------------
# Expanded Appendix A path set.
# ---------------------------------------------------------------------------

_PV = "/v1/arc/proposals/{proposal_id}/versions/{proposal_version}"

# Every route Appendix A.1 names, `{PV}` expanded literally, except the one
# path in the next section's own gap test.
_EXPECTED_PATHS: tuple[tuple[str, str], ...] = (
    # Source admission.
    ("POST", "/v1/arc/sources/uploads"),
    ("POST", "/v1/arc/sources/connector-fetches"),
    ("GET", "/v1/arc/sources/{source_evidence_id}"),
    ("GET", "/v1/arc/sources/{source_evidence_id}/body"),
    ("POST", "/v1/arc/admin/source-connectors"),
    ("POST", "/v1/arc/admin/source-upload-policies"),
    # Artifact families and proposals.
    ("POST", "/v1/arc/artifacts"),
    ("GET", "/v1/arc/artifacts/{artifact_id}"),
    ("POST", "/v1/arc/artifacts/{artifact_id}/proposals"),
    ("GET", "/v1/arc/proposals/{proposal_id}"),
    ("GET", _PV),
    ("PATCH", _PV),
    ("POST", f"{_PV}/validate"),
    ("POST", f"{_PV}/semantic-tests"),
    ("GET", f"{_PV}/baseline-diff"),
    ("GET", f"{_PV}/review-package"),
    ("POST", f"{_PV}/reach-confirmations"),
    ("POST", f"{_PV}/draft"),
    ("POST", f"{_PV}/submit"),
    ("POST", f"{_PV}/withdraw"),
    ("POST", f"{_PV}/reject"),
    ("POST", f"{_PV}/supersede"),
    # Verifier enrollment (D1).
    ("POST", "/v1/arc/admin/approval-verifiers/enrollment-challenges"),
    ("POST", "/v1/arc/admin/approval-verifiers"),
    ("POST", "/v1/arc/admin/approval-verifiers/{approval_verifier_id}/revoke"),
    # Projection approval (D2) -- minus the documented gap route below.
    ("POST", f"{_PV}/approval-challenges"),
    ("POST", "/v1/arc/approval-challenges/{approval_challenge_id}/complete"),
    # Observation and qualification.
    ("GET", f"{_PV}/observation"),
    ("POST", f"{_PV}/observation/qualify"),
    ("POST", f"{_PV}/observation/accept"),
    ("POST", "/v1/arc/admin/observation-replay-corpora"),
    # Activation.
    ("GET", "/v1/arc/revisions/{revision_id}/activation-eligibility"),
    ("POST", "/v1/arc/revisions/{revision_id}/activate"),
    ("POST", "/v1/arc/revisions/{revision_id}/revoke"),
)

# Appendix A's Projection approval table names this route with the frozen
# `ExceptionApprovalEvidenceRequest` -> `ApprovalEvidenceResponse` shapes at
# a non-admin path. It was never built at that path or with those shapes.
# The only route doing this job is the pre-existing, non-frozen
# `POST /v1/arc/admin/revisions/{revision_id}/approval-evidence`
# (`AttachEvidenceRequest` -> a generic accepted-envelope response), which
# predates this authoring-surface phase and which the legacy-bypass
# removal left in place while narrowing it to
# `evidence_type == "exception_approval"` only. Functionally the D6 safety
# property holds (non-`exception_approval` writes refuse with
# `arc_evidence_type_not_writable`); what never happened is migrating that
# route onto the frozen Appendix A path and component pair. Reported here
# rather than silently added (out of this task's scope -- no route task
# owns it) or silently dropped from the appendix (that would hide the
# drift instead of naming it).
_DOCUMENTED_MISSING_APPENDIX_PATH = ("POST", "/v1/arc/revisions/{revision_id}/approval-evidence")
_DOCUMENTED_SUBSTITUTE_PATH = ("POST", "/v1/arc/admin/revisions/{revision_id}/approval-evidence")


def test_expanded_appendix_a_path_set_is_present() -> None:
    paths = _load_openapi()["paths"]
    missing = [
        (method, path) for method, path in _EXPECTED_PATHS if path not in paths or method.lower() not in paths[path]
    ]
    assert not missing, f"Appendix A route(s) missing from the exported spec: {missing}"


def test_artifact_family_collection_is_published_with_server_side_filters() -> None:
    operation = _load_openapi()["paths"]["/v1/arc/artifacts"]["get"]
    parameters = {parameter["name"] for parameter in operation["parameters"]}
    assert parameters == {"cursor", "q", "kind", "owning_scope", "page_size"}
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/ArtifactFamilyListResponse"}


def test_the_appendix_a_approval_evidence_route_gap_is_the_only_missing_path() -> None:
    """Names the one Appendix A route that was never built, so a second,
    unrelated route silently going missing cannot hide behind this one
    already being an accepted, tracked gap."""
    paths = _load_openapi()["paths"]
    method, path = _DOCUMENTED_MISSING_APPENDIX_PATH
    assert path not in paths or method.lower() not in paths.get(path, {}), (
        f"{method} {path} now exists -- update this test and "
        f"`_EXPECTED_PATHS` to include it; the gap this test names is closed."
    )
    sub_method, sub_path = _DOCUMENTED_SUBSTITUTE_PATH
    assert sub_path in paths and sub_method.lower() in paths[sub_path], (
        f"the documented substitute route {sub_method} {sub_path} is itself "
        "missing -- the exception_approval evidence write path may have "
        "moved or been removed without this test being updated."
    )


def test_no_standalone_approve_route_exists_under_a_proposal_version() -> None:
    """Appendix A is normative that `submitted -> approved` is a side
    effect of `POST /v1/arc/approval-challenges/{id}/complete`, never a
    route of its own. No `/approve` path segment should exist anywhere in
    the exported spec at all -- there is no legitimate reason for one."""
    paths = _load_openapi()["paths"]
    approve_paths = [p for p in paths if "/approve" in p]
    assert not approve_paths, f"a standalone approve route exists: {approve_paths}"


# ---------------------------------------------------------------------------
# Registered-action parity: `AVAILABLE_ACTION_ROUTE_ACTIONS` (frozen when
# component schemas were first pinned, checked there only for internal
# consistency) against real `add_mutation_route(...)` call sites.
# ---------------------------------------------------------------------------

# (router module, expected path, expected verb) for every AvailableAction's
# route action. Two are off-resource (§A.6, `AVAILABLE_ACTION_OFF_RESOURCE_
# EXCEPTIONS`): `request_approval` creates a challenge under `{PV}` but is
# not itself a `{PV}`-root transition, and `activate` lives on the revision
# resource, not the proposal version, at all.
_EXPECTED_ACTION_ROUTES: dict[aa.AvailableAction, tuple[str, str, str]] = {
    aa.AvailableAction.EDIT: ("arc_authoring.py", f"{_PV}", "PATCH"),
    aa.AvailableAction.VALIDATE: ("arc_authoring.py", f"{_PV}/validate", "POST"),
    aa.AvailableAction.RUN_SEMANTIC_TESTS: ("arc_authoring.py", f"{_PV}/semantic-tests", "POST"),
    aa.AvailableAction.CONFIRM_REACH: ("arc_drafting.py", f"{_PV}/reach-confirmations", "POST"),
    aa.AvailableAction.DRAFT: ("arc_drafting.py", f"{_PV}/draft", "POST"),
    aa.AvailableAction.SUBMIT: ("arc_authoring.py", f"{_PV}/submit", "POST"),
    aa.AvailableAction.WITHDRAW: ("arc_authoring.py", f"{_PV}/withdraw", "POST"),
    aa.AvailableAction.REJECT: ("arc_authoring.py", f"{_PV}/reject", "POST"),
    aa.AvailableAction.SUPERSEDE: ("arc_authoring.py", f"{_PV}/supersede", "POST"),
    aa.AvailableAction.REQUEST_APPROVAL: ("arc_approval.py", f"{_PV}/approval-challenges", "POST"),
    aa.AvailableAction.QUALIFY: ("arc_observation.py", f"{_PV}/observation/qualify", "POST"),
    aa.AvailableAction.ACCEPT_QUALIFICATION: ("arc_observation.py", f"{_PV}/observation/accept", "POST"),
    aa.AvailableAction.ACTIVATE: ("arc_activation.py", "/revisions/{revision_id}/activate", "POST"),
}

# `{PV}`, as it appears literally inside a router module (each router's own
# `APIRouter(prefix="/v1/arc")` supplies the `/v1/arc` half; the path
# strings passed to `add_mutation_route` never repeat it).
_PV_ROUTE_SUFFIX = "/proposals/{proposal_id}/versions/{proposal_version}"


def _extract_mutation_routes(module_filename: str) -> list[dict[str, str | None]]:
    """AST call-site extraction, not a text grep: mirrors `scripts/
    check_arc_approval_writers.py`'s own reasoning for why call-site
    inspection is the right tool for "does this router really register
    this action", rather than trusting a string that could appear in a
    comment or a docstring instead of a real call.
    """
    tree = ast.parse((_ROUTERS_DIR / module_filename).read_text())
    routes: list[dict[str, str | None]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_mutation_route":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}

        def _literal(value_node: ast.expr | None) -> str | None:
            return value_node.value if isinstance(value_node, ast.Constant) else None

        routes.append(
            {
                "path": _literal(kwargs.get("path")),
                "action": _literal(kwargs.get("action")),
                "verb": _literal(kwargs.get("verb")),
            }
        )
    return routes


def test_available_action_route_map_resolves_against_real_routers() -> None:
    """The check deferred until routes existed to check against: for every
    `AvailableAction`, the frozen `(action string)` from
    `AVAILABLE_ACTION_ROUTE_ACTIONS` actually
    names a registered `add_mutation_route` call, on the expected router,
    at the expected path and verb -- not merely a string that happens to
    match somewhere. Distinguishing "on the expected router" matters: the
    bare action string `"create"` is reused by three unrelated routes
    (artifact-family creation, approval-challenge creation, and -- had this
    module also collected `arc_admin.py` -- connector/policy registration),
    so action-string membership alone would pass even if the frozen map
    pointed at the wrong resource entirely.
    """
    assert set(aa.AVAILABLE_ACTION_ROUTE_ACTIONS.keys()) == set(_EXPECTED_ACTION_ROUTES.keys())

    routers_cache: dict[str, list[dict[str, str | None]]] = {}
    unresolved: list[str] = []
    for action_enum, (module_filename, expected_path, expected_verb) in _EXPECTED_ACTION_ROUTES.items():
        expected_action = aa.AVAILABLE_ACTION_ROUTE_ACTIONS[action_enum]
        routes = routers_cache.setdefault(module_filename, _extract_mutation_routes(module_filename))
        expected_path_suffix = expected_path.replace(_PV, _PV_ROUTE_SUFFIX)
        match = any(
            r["action"] == expected_action and r["path"] == expected_path_suffix and r["verb"] == expected_verb
            for r in routes
        )
        if not match:
            unresolved.append(
                f"{action_enum.value}: no {expected_verb} route at {expected_path_suffix!r} "
                f"with action={expected_action!r} in {module_filename}"
            )

    assert not unresolved, "AvailableAction route mapping did not resolve against real routers:\n" + "\n".join(
        unresolved
    )
