"""External reference and per-block content contracts.

The assembler and the receipt writer both consume these shapes, so the fixtures
are the artifact: a slice can build against them without importing the module,
and a change that would break a consumer breaks a committed file first.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_INSTRUCTIONS,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
)
from contextplane.context.schemas.reference import (
    ArcContentV1,
    CanonicalContentV1,
    InstructionDeltaContentV1,
    ObservedClaimContentV1,
    WorkspaceContentV1,
    normalize_reference,
    parse_block_content,
)
from contextplane.context.schemas.trust import InvalidContextItem

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "context" / "reference"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# --- references ---------------------------------------------------------------


def test_a_minimal_reference_round_trips() -> None:
    reference = normalize_reference(_load("reference-minimal.json"))
    assert reference.kind == "issue"
    assert reference.external_id == "412"


def test_a_full_reference_carries_its_optional_fields() -> None:
    reference = normalize_reference(_load("reference-full.json"))
    assert reference.revision == "v2"
    assert reference.observed_at is not None
    assert reference.observed_at.tzinfo is not None


def test_case_differences_in_the_system_resolve_to_one_reference() -> None:
    """`GitHub` and `github` are one source. Treating them as two would split a
    subject's references in half with nothing looking wrong."""
    upper = normalize_reference(_load("reference-minimal.json"))
    lower = normalize_reference(
        {**_load("reference-minimal.json"), "source_system": "github", "source_namespace": "roughcompass/contextplane"}
    )
    assert upper.collision_key() == lower.collision_key()


def test_the_external_id_keeps_its_case_because_it_belongs_to_another_system() -> None:
    lower = normalize_reference({**_load("reference-minimal.json"), "external_id": "abc"})
    upper = normalize_reference({**_load("reference-minimal.json"), "external_id": "ABC"})
    assert lower.collision_key() != upper.collision_key()


def test_the_same_id_under_two_kinds_stays_two_references() -> None:
    issue = normalize_reference(_load("reference-minimal.json"))
    pull = normalize_reference({**_load("reference-minimal.json"), "kind": "pull_request"})
    assert issue.collision_key() != pull.collision_key()


def test_a_reference_creates_no_workflow_object() -> None:
    """The contract is what the type does *not* carry: no status, no assignee, no
    lifecycle. Importing one would mean answering for a lifecycle we do not own."""
    reference = normalize_reference(_load("reference-minimal.json"))
    fields = set(vars(reference))
    assert not fields & {"status", "state", "assignee", "workflow", "lifecycle"}


# --- negative fixtures --------------------------------------------------------


def test_an_unknown_field_on_a_reference_is_refused_by_name() -> None:
    with pytest.raises(InvalidContextItem, match=r"unknown field\(s\) \['priority'\]"):
        normalize_reference(_load("negative-reference-unknown-field.json"))


def test_a_reference_missing_part_of_its_collision_scope_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match=r"missing required field\(s\) \['kind'\]"):
        normalize_reference(_load("negative-reference-missing-kind.json"))


def test_an_unknown_field_on_block_content_is_refused_by_name() -> None:
    with pytest.raises(InvalidContextItem, match=r"unknown field\(s\) \['owner'\]"):
        parse_block_content(BLOCK_CANONICAL, _load("negative-content-unknown-field.json"))


def test_an_observed_claim_citing_nothing_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="indistinguishable from an invention"):
        parse_block_content(BLOCK_OBSERVED_CLAIMS, _load("negative-observed-claim-no-evidence.json"))


def test_an_observed_claim_with_a_structured_value_is_refused() -> None:
    """A structured value carries text no content check reads."""
    with pytest.raises(InvalidContextItem, match="is a scalar"):
        parse_block_content(BLOCK_OBSERVED_CLAIMS, _load("negative-observed-claim-structured-value.json"))


# --- per-block content --------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "fixture", "expected"),
    [
        (BLOCK_CANONICAL, "content-canonical.json", CanonicalContentV1),
        (BLOCK_ARC, "content-arc.json", ArcContentV1),
        (BLOCK_OBSERVED_CLAIMS, "content-observed-claim.json", ObservedClaimContentV1),
        (BLOCK_WORKSPACE, "content-workspace.json", WorkspaceContentV1),
        (BLOCK_INSTRUCTIONS, "content-instruction-delta.json", InstructionDeltaContentV1),
    ],
)
def test_each_block_parses_into_its_own_shape(block: str, fixture: str, expected: type) -> None:
    assert isinstance(parse_block_content(block, _load(fixture)), expected)


def test_every_block_has_a_content_fixture() -> None:
    """A block with no fixture is a shape the slices will each guess at."""
    names = {
        BLOCK_CANONICAL: "canonical",
        BLOCK_ARC: "arc",
        BLOCK_OBSERVED_CLAIMS: "observed-claim",
        BLOCK_WORKSPACE: "workspace",
        BLOCK_INSTRUCTIONS: "instruction-delta",
    }
    for block in BLOCK_NAMES:
        assert (FIXTURES / f"content-{names[block]}.json").is_file(), f"no content fixture for {block}"


def test_content_for_an_unknown_block_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="unknown block"):
        parse_block_content("sidecar", {})


def test_arc_content_normalizes_its_nested_references() -> None:
    content = parse_block_content(BLOCK_ARC, _load("content-arc.json"))
    assert isinstance(content, ArcContentV1)
    assert content.references[0].source_system == "github"


def test_workspace_content_carries_no_similarity_score() -> None:
    """Workspace recall stays lexical and reference-based. A score would present
    somebody's working note as a retrieved fact."""
    content = parse_block_content(BLOCK_WORKSPACE, _load("content-workspace.json"))
    fields = set(vars(content))
    assert not fields & {"score", "similarity", "distance", "embedding", "vector"}
