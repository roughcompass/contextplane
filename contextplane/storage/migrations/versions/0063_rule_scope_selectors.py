"""A rule below `global` must name what it is scoped to -- in SQL, not only in Python.

`scope` buys precedence. `_SCOPE_ORDER` ranks a domain rule above an entity one
and an entity rule above an intent one, while an empty selector array means "no
constraint on this dimension". A rule claiming a narrow scope and selecting
nothing therefore takes the precedence without the narrowing and applies to
every manifest -- at a *higher* rank than the rules that did narrow.

`arc_applicability_rules` enforced this for two of the four narrow scopes:
`ck_arc_rules_tenant_scope_target` requires a target tenant, and
`ck_arc_rules_entity_scope_target` requires an entity. Domain and intent were
unconstrained, and both were reachable -- a domain-scoped rule with an empty
`domain_ids` was constructible in Python and insertable here, and it matched
everything at rank two.

**The other half of this authority model already requires the correspondence.**
`ck_arc_exceptions_scope_selectors` (0061) makes an approved exception name a
domain for domain scope, an entity for entity scope, and an intent kind *and* an
action class for intent scope. An exception is a narrowing of a rule; there was
no reading under which the exception may be pinned down and the rule it narrows
may not. This revision brings the rules table up to what the exceptions table
has always demanded.

**Intent scope needs both selectors, matching the exceptions table.** An intent
kind alone leaves the action class unconstrained, and the action class is the
half that decides whether an obligation is owed for the thing actually being
done.

`global` is exempt, here as in `ApplicabilityRule.__post_init__`: matching
everything is what it means.

No backfill and no data migration. Every row that could violate this would be a
rule matching everything from a narrow scope, and the CHECK is added `NOT VALID`
-free -- if any deployment held such a row the migration would fail loudly,
which is the correct outcome for a row that silently widened authority.
"""

from __future__ import annotations

from alembic import op

revision = "0063_rule_scope_selectors"
down_revision: str | None = "0062_autonomy_envelope_bindings"
branch_labels: str | None = None
depends_on: str | None = None

_DOMAIN_SCOPE_TARGET = """
ALTER TABLE arc_applicability_rules
    ADD CONSTRAINT ck_arc_rules_domain_scope_target CHECK (
        scope <> 'domain'
        OR (domain_ids IS NOT NULL AND array_length(domain_ids, 1) >= 1)
    )
"""

_INTENT_SCOPE_TARGET = """
ALTER TABLE arc_applicability_rules
    ADD CONSTRAINT ck_arc_rules_intent_scope_target CHECK (
        scope <> 'intent'
        OR (
            intent_kinds IS NOT NULL AND array_length(intent_kinds, 1) >= 1
            AND action_classes IS NOT NULL AND array_length(action_classes, 1) >= 1
        )
    )
"""


def upgrade() -> None:
    op.execute(_DOMAIN_SCOPE_TARGET)
    op.execute(_INTENT_SCOPE_TARGET)


def downgrade() -> None:
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_intent_scope_target")
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT ck_arc_rules_domain_scope_target")
