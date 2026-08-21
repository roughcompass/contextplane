"""Gate: a magnitude a feature demands validation for may not be grandfathered.

`contextplane/ranking.py` already refuses a registry entry with no validation
status, so most of what this checks is checked twice. That is deliberate and it
is the point of the file: the loader's refusals protect the *running service*,
and this protects the *artifact* — including on a change that relaxes the loader.
A gate that only asked the loader whether the loader was happy would agree with
it by construction, and would keep passing through the one commit that matters.

So this reads the JSON directly. It never calls the accessors it is checking.

The rule this exists for is the machine-readable half of a safety ordering E3
and E5 both depend on:

    an entry with `requires_validated: true` must have `status: validated`

Registration says a number is owned and has a written reason. Validation says
somebody checked it predicts, and names who, against what data, and with what
result. `grandfathered` says neither, honestly. A feature whose activation is
gated on validation must not be able to turn on against a number nobody checked.

The loader now enforces that rule too, and refuses the whole registry so the
process does not start. This still runs: it fails in review rather than at boot,
and it survives a change that relaxes the loader.

The one rule that is only here, because it is about the artifact rather than the
run:

    an entry with `requires_validated: true` must have `coupling: consumed`

A `pinned` magnitude is one a test asserts agreement with rather than one the
code reads, so nothing on a serving path calls its accessor. Gating it is
protection in appearance only, and the loader cannot notice — the entry is
well-formed, it just never governs anything.

Everything else here is a shape rule that keeps the last one meaningful. A
`validated` status with no evidence behind it is a word, and the word is what a
later reader would trust; a `grandfathered` status with no reason is an
exemption nobody has to justify.

Anti-vacuity: an empty registry already fails the loader, and this fails rather
than passes if it inspects zero entries — the two are different failures and
both are worth having, because a gate that scans nothing and prints a tick is
the failure mode every check in this directory is written against.

Run locally:

    python3 scripts/check_governed_magnitudes.py
    python3 scripts/check_governed_magnitudes.py --explain
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checklib import repo_root, require_nonempty, run_guard

#: Read as a path rather than imported from `contextplane.ranking`, so this gate
#: still inspects the artifact if the loader stops pointing at it.
_REGISTRY = repo_root() / "contextplane" / "ranking_registry.json"

_STATUSES = ("validated", "grandfathered")

#: What a `validated` status has to be able to show. Four fields rather than a
#: free-text blob because "who validated this" and "against what data" are the
#: two questions a reviewer asks first, and a blob makes both optional.
_EVIDENCE_FIELDS = ("validated_by", "validated_on", "method", "result")


def _violations(entry: dict[str, Any]) -> list[str]:
    """Everything wrong with one entry, so one run reports them all."""
    model_id = entry.get("model_id", "<unnamed>")
    found: list[str] = []

    validation = entry.get("validation")
    if not isinstance(validation, dict):
        return [f"{model_id}: carries no `validation` block; registration and validation are different claims"]

    status = validation.get("status")
    if status not in _STATUSES:
        found.append(f"{model_id}: validation.status is {status!r}, expected one of {list(_STATUSES)}")

    requires = entry.get("requires_validated")
    if not isinstance(requires, bool):
        found.append(
            f"{model_id}: `requires_validated` is {requires!r}, expected a boolean; "
            "an absent field would read as 'no' for a magnitude whose consumer demands 'yes'"
        )

    if status == "grandfathered" and not str(validation.get("reason") or "").strip():
        found.append(
            f"{model_id}: grandfathered without a reason; an exemption nobody has to justify "
            "is one nobody will revisit"
        )

    if status == "validated":
        missing = [field for field in _EVIDENCE_FIELDS if not str(validation.get(field) or "").strip()]
        if missing:
            found.append(
                f"{model_id}: validated but missing {missing}; a status without its evidence is a word, "
                "and the word is what a later reader would trust"
            )

    if requires is True and status != "validated":
        found.append(
            f"{model_id}: `requires_validated` is true but the status is {status!r}. "
            "A feature whose activation is gated on validation cannot turn on against a number "
            "nobody checked — either validate it and record the evidence, or clear the flag."
        )

    coupling = entry.get("coupling")
    if requires is True and coupling != "consumed":
        # A `pinned` entry is one a test asserts agreement with rather than one
        # the code reads — the authority ladder, whose order the governance
        # kernel keeps its own copy of. Nothing on a serving path calls the
        # accessor for it, so the loader's refusal never reaches production for
        # that magnitude and the flag protects nothing while looking like it
        # does. This epic's whole thesis is that a gate believed exhaustive and
        # quietly defeated is worse than none; the same applies one entry at a
        # time.
        found.append(
            f"{model_id}: `requires_validated` is true but coupling is {coupling!r}, not 'consumed'. "
            "Only a consumed magnitude is read on a serving path, so gating a pinned one is protection "
            "in appearance only — either make the consumer read it, or clear the flag."
        )

    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--explain", action="store_true", help="print each entry's status and coupling")
    # Explicit argv so a caller -- notably the unit test -- is not handed
    # whatever pytest was invoked with.
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    document = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    entries = document.get("magnitudes")
    if not isinstance(entries, list):
        print(f"{_REGISTRY.name}: `magnitudes` is missing or not a list", file=sys.stderr)
        return 1

    require_nonempty(
        entries,
        f"the magnitude population in {_REGISTRY.name}",
        hint="A registry governing nothing is a defect rather than a state; the loader refuses it too.",
    )

    violations: list[str] = []
    for entry in entries:
        violations.extend(_violations(entry))

    validated = sum(1 for e in entries if (e.get("validation") or {}).get("status") == "validated")
    gated = sum(1 for e in entries if e.get("requires_validated") is True)
    print(
        f"governed-magnitudes gate: {len(entries)} entr(ies) inspected, "
        f"{validated} validated, {gated} requiring validation"
    )

    if args.explain:
        for entry in entries:
            validation = entry.get("validation") or {}
            print(
                f"  {entry.get('model_id', '<unnamed>'):<34} "
                f"status={validation.get('status', '<none>'):<14} "
                f"coupling={entry.get('coupling', '<none>'):<9} "
                f"requires_validated={entry.get('requires_validated')}"
            )

    if not violations:
        return 0

    for line in violations:
        print(line, file=sys.stderr)
    print(
        f"\nEdit {_REGISTRY.relative_to(repo_root())}. Registration says a number is owned; "
        "validation says somebody checked it predicts. See the file's own `_comment`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
