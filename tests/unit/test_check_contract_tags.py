"""The tag gate, against planted contracts rather than only the real one.

The committed contract passes, which is what a green run looks like and says
nothing about whether the gate would catch anything. So every rule gets a
synthetic document that breaks it and one that does not — a rule tested only by
what it rejects is indistinguishable from a rule that rejects everything, and a
gate that fails on a correct contract is switched off within a week.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_contract_tags import main, violations  # noqa: E402


def _contract(paths: dict[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.1.0", "paths": paths}


def _op(*tags: str, deprecated: bool = False) -> dict[str, Any]:
    operation: dict[str, Any] = {"tags": list(tags), "responses": {}}
    if deprecated:
        operation["deprecated"] = True
    return operation


class TestEveryOperationIsTagged:
    def test_an_untagged_operation_is_reported(self) -> None:
        """Invisible in a tag-grouped client and in no section of the docs."""
        found = violations(_contract({"/v1/things": {"get": {"responses": {}}}}))
        assert any("no tag" in line for line in found)

    def test_a_tagged_operation_is_not(self) -> None:
        assert violations(_contract({"/v1/things": {"get": _op("things")}})) == []

    def test_the_three_infrastructure_endpoints_are_exempt(self) -> None:
        """`/healthz`, `/readyz` and `/metrics` are not part of the versioned API,
        and grouping them with one would be worse than leaving them out."""
        infra = {path: {"get": {"responses": {}}} for path in ("/healthz", "/readyz", "/metrics")}
        assert violations(_contract(infra)) == []

    def test_a_fourth_unversioned_path_is_not_exempt_by_pattern(self) -> None:
        """Exempt by name rather than by prefix, so adding one is deliberate."""
        assert any("no tag" in line for line in violations(_contract({"/livez": {"get": {"responses": {}}}})))

    def test_a_non_method_key_is_not_treated_as_an_operation(self) -> None:
        """`parameters` is a sibling of the methods in a path item. Reading it as
        an operation would report an untagged one on every parameterised path."""
        document = _contract({"/v1/things/{id}": {"parameters": [{"name": "id"}], "get": _op("things")}})
        assert violations(document) == []


class TestOneDelimiter:
    def test_a_bare_space_is_reported(self) -> None:
        """Three tags used one and it expressed nothing; the colon form was
        already nineteen and is the only one that expresses hierarchy."""
        found = violations(_contract({"/v1/x": {"get": _op("memory curation")}}))
        assert any("bare space" in line for line in found)

    def test_the_colon_form_is_accepted(self) -> None:
        assert violations(_contract({"/v1/x": {"get": _op("admin: memory-curation")}})) == []

    def test_a_hyphenated_leaf_is_accepted(self) -> None:
        assert violations(_contract({"/v1/x": {"get": _op("external-ids")}})) == []

    def test_a_space_inside_a_multi_word_part_is_still_reported(self) -> None:
        """`admin: memory curation` was the real case: colon-correct at the top
        level and space-delimited underneath."""
        found = violations(_contract({"/v1/x": {"get": _op("admin: memory curation")}}))
        assert any("bare space" in line for line in found)


class TestAPathBelongsSomewhere:
    def test_methods_sharing_no_tag_are_reported(self) -> None:
        """`/v1/capabilities` read as `retrieval` and wrote as `capabilities`, so
        a reader looking for it had to guess which section it landed in."""
        document = _contract({"/v1/capabilities": {"get": _op("retrieval"), "post": _op("capabilities")}})
        found = violations(document)
        assert any("share no tag" in line for line in found)

    def test_methods_sharing_one_tag_are_accepted_even_with_an_extra(self) -> None:
        """Not one-tag-per-path. `retrieval` is a genuine cross-cutting tag, and
        an operation may carry it as well as its subdomain — what must not happen
        is a path whose methods have nothing in common."""
        document = _contract(
            {"/v1/capabilities": {"get": _op("retrieval", "capabilities"), "post": _op("capabilities")}}
        )
        assert violations(document) == []

    def test_a_single_method_path_cannot_disagree_with_itself(self) -> None:
        assert violations(_contract({"/v1/x": {"get": _op("retrieval")}})) == []

    def test_a_deprecated_operation_is_exempt_from_the_split_rule(self) -> None:
        """A deprecated alias is mid-rename and the split is what the deprecation
        is retiring. `GET /v1/entities` is the live example."""
        document = _contract({"/v1/entities": {"get": _op("external-ids", deprecated=True), "post": _op("entities")}})
        assert violations(document) == []

    def test_a_deprecated_operation_is_not_exempt_from_the_other_rules(self) -> None:
        """Only the split rule. A deprecated alias with no tag or a space in one
        is still wrong, and exempting it wholesale would make `deprecated: true`
        a way to opt out of the gate."""
        untagged = _contract({"/v1/x": {"get": {"deprecated": True, "responses": {}}}})
        spaced = _contract({"/v1/y": {"get": _op("task memory", deprecated=True)}})
        assert any("no tag" in line for line in violations(untagged))
        assert any("bare space" in line for line in violations(spaced))

    def test_two_deprecated_operations_do_not_exempt_each_other_into_silence(self) -> None:
        """With every live operation deprecated there is nothing left to compare,
        and the rule has to fall silent rather than crash on an empty set."""
        document = _contract({"/v1/x": {"get": _op("a", deprecated=True), "post": _op("b", deprecated=True)}})
        assert violations(document) == []


class TestAgainstTheCommittedContract:
    def test_it_passes(self, capsys: Any) -> None:
        """End to end through the real file. Every rule test above works on
        synthetic documents and would keep passing if the script stopped reading
        `openapi.json` at all."""
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "operation(s) inspected" in out
        assert " 0 operation(s) inspected" not in out

    def test_it_inspects_the_whole_contract(self, capsys: Any) -> None:
        """A parse that silently found a handful of operations would pass while
        governing almost nothing."""
        main([])
        reported = int(capsys.readouterr().out.split("gate: ")[1].split(" ")[0])
        assert reported > 150, f"only {reported} operations inspected; the contract has far more"
