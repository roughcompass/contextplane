"""Context-bundle assembly and budget enforcement.

The bundle is what the agent actually reads. Two rules govern it, and they pull in
opposite directions:

1. **Mandatory directives are never silently dropped.** If the complete mandatory
   set does not fit the budget, the answer is `blocked_budget_exceeded` — not a
   shorter bundle. A truncated obligation list that still says `ready` is the
   worst possible output: the agent believes it knows what it must do.
2. **CAP facts are informational and never change the status.** They are budgeted
   separately, and when they do not fit they are omitted with a receipt reason
   while the mandatory-derived status stands.

Rule 2 was contradicted by two parts of its own specification, so it is stated
plainly here in the code that implements it: an optional CAP-fact failure is
recorded as an omission and is **not** a degradation. The status a caller sees is
derived from the mandatory set alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from registry.arc.schemas.canonical import bundle_content_bytes
from registry.arc.service.selection import ScopedDirective, SelectionResult
from registry.arc.types import ResolutionStatus

# CAP facts get their own allowance so they can never crowd out an obligation.
CAP_FACTS_BUDGET_BYTES = 4 * 1024

BLOCKED_BUDGET_EXCEEDED = "blocked_budget_exceeded"
OMITTED_CAP_FACTS_OVER_BUDGET = "cap_facts_over_budget"
OMITTED_CAP_FACTS_UNAVAILABLE = "cap_facts_unavailable"


@dataclass(frozen=True)
class CapFact:
    """Deterministic informational projection of a capability.

    Ordered by the caller's manifest order and then by capability id, so the same
    request yields the same bundle — semantic search rank must not reach this.
    """

    capability_id: str
    owner: str
    lifecycle: str
    version: str
    interface_reference: str | None = None

    def as_content(self) -> dict[str, str | None]:
        return {
            "capability_id": self.capability_id,
            "owner": self.owner,
            "lifecycle": self.lifecycle,
            "version": self.version,
            "interface_reference": self.interface_reference,
        }


@dataclass(frozen=True)
class ContextBundle:
    """What `resolve_context` returns, plus the accounting a receipt records."""

    status: ResolutionStatus
    directives: tuple[dict[str, object], ...]
    cap_facts: tuple[dict[str, str | None], ...]
    rendered_content_bytes: int
    budget_limit_bytes: int
    blocked_reasons: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()
    omission_reasons: tuple[str, ...] = ()
    offending_artifact_ids: tuple[str, ...] = field(default=())

    @property
    def is_ready(self) -> bool:
        return self.status is ResolutionStatus.READY


def _directive_content(scoped: ScopedDirective) -> dict[str, object]:
    """The startup-layer projection of one directive.

    Deliberately minimal: identity, citation, and the machine-comparable
    constraint. Full source text is reachable only through JIT detail, redacted by
    audience — putting it here would push every bundle over budget and hand every
    matched actor content some of them may not read.
    """
    directive = scoped.directive
    constraint = directive.constraint
    return {
        "directive_id": str(directive.directive_id),
        "revision_id": str(directive.revision_id),
        "directive_type": str(directive.directive_type),
        "scope": str(scoped.scope),
        "source_anchor": directive.source_anchor,
        "constraint": (
            None
            if constraint is None
            else {
                "modality": str(constraint.modality),
                "operator": str(constraint.operator),
                "values": sorted(constraint.values),
            }
        ),
    }


def assemble(
    selection: SelectionResult,
    *,
    budget_limit_bytes: int,
    cap_facts: tuple[CapFact, ...] = (),
    cap_facts_available: bool = True,
) -> ContextBundle:
    """Render a bundle and enforce the budget.

    Order of operations matters. The mandatory set is measured first and on its
    own, so the decision to block can never depend on how large the optional
    content happened to be. Only then are CAP facts considered, against their own
    allowance.
    """
    if budget_limit_bytes <= 0:
        msg = f"budget_limit_bytes must be positive, got {budget_limit_bytes}"
        raise ValueError(msg)

    mandatory = tuple(_directive_content(s) for s in selection.mandatory)
    optional = tuple(_directive_content(s) for s in selection.optional)

    # Measured without CAP facts: a mandatory set that does not fit must block
    # regardless of what else was on offer.
    mandatory_bytes = bundle_content_bytes({"status": str(selection.status), "directives": list(mandatory)})

    if selection.status is ResolutionStatus.BLOCKED:
        # Already blocked upstream. Report it with its own reasons rather than
        # re-deciding, and still record the byte count for the receipt.
        return ContextBundle(
            status=ResolutionStatus.BLOCKED,
            directives=mandatory,
            cap_facts=(),
            rendered_content_bytes=mandatory_bytes,
            budget_limit_bytes=budget_limit_bytes,
            blocked_reasons=selection.blocked_reasons,
            offending_artifact_ids=tuple(sorted({str(f.left.revision_id) for f in selection.conflicts})),
        )

    if mandatory_bytes > budget_limit_bytes:
        # Name the offenders so an operator can act, without leaking content: ids
        # and anchors only, which the caller was already entitled to see.
        return ContextBundle(
            status=ResolutionStatus.BLOCKED,
            directives=(),
            cap_facts=(),
            rendered_content_bytes=mandatory_bytes,
            budget_limit_bytes=budget_limit_bytes,
            blocked_reasons=(BLOCKED_BUDGET_EXCEEDED,),
            offending_artifact_ids=tuple(sorted(str(s.directive.revision_id) for s in selection.mandatory)),
        )

    omissions: list[str] = []
    included_facts: tuple[dict[str, str | None], ...] = ()

    if not cap_facts_available:
        omissions.append(OMITTED_CAP_FACTS_UNAVAILABLE)
    elif cap_facts:
        rendered = tuple(f.as_content() for f in cap_facts)
        if bundle_content_bytes({"cap_facts": list(rendered)}) > CAP_FACTS_BUDGET_BYTES:
            omissions.append(OMITTED_CAP_FACTS_OVER_BUDGET)
        else:
            included_facts = rendered

    content_bytes = bundle_content_bytes(
        {
            "status": str(selection.status),
            "directives": list(mandatory + optional),
            "cap_facts": list(included_facts),
        }
    )

    # CAP-fact omission is recorded and does not touch the status. Two parts of
    # the specification disagreed about this; the rule that CAP facts never change
    # the resolution status is the authoritative one.
    return ContextBundle(
        status=selection.status,
        directives=mandatory + optional,
        cap_facts=included_facts,
        rendered_content_bytes=content_bytes,
        budget_limit_bytes=budget_limit_bytes,
        degraded_reasons=selection.degraded_reasons,
        omission_reasons=tuple(sorted(omissions)),
    )
