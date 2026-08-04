"""ARC — make child/parent tenant agreement structural.

`arc_directives.tenant_id` and `arc_applicability_rules.tenant_id` are copies of
their revision's tenant. Nothing enforced that: both columns are nullable and
reference `tenants` only, so a child row naming a different tenant than its
parent was storable. Agreement held solely because `ArtifactService` derives each
child's tenant from its parent inside the INSERT, and no UPDATE mutates it.

That matters more than it looks. The corpus query that assembles candidates for a
context resolution filters on the *revision's* tenant, and the applicability
matcher checks the requesting tenant only for `tenant`-scoped rules — a
`domain`-, `capability`-, or `task`-scoped rule carries no tenant check anywhere
downstream. So one predicate over one column was the entire boundary between one
tenant's governance and another's resolution, backed by a service-layer
convention rather than by the schema.

A composite foreign key makes the parent the source of truth: a child may only
name the tenant its revision names.

**What this does not catch, stated plainly.** The default `MATCH SIMPLE`
semantics satisfy a composite foreign key whenever any referencing column is
NULL. So a child carrying `tenant_id IS NULL` under a tenant-owned revision is
still storable. `MATCH FULL` would reject it, but `MATCH FULL` also requires all
referencing columns to be NULL together — and `revision_id` is NOT NULL, so it
would forbid global children entirely, which is a legitimate and common shape.

That residue is harmless for the boundary this protects: visibility is decided
from the revision's tenant, not the child's, so a NULL child under a tenant
parent cannot widen anything. What the constraint closes is the case that could
— a child asserting a *different*, concrete tenant.
"""

from __future__ import annotations

from alembic import op

revision = "0024_arc_child_tenant_agreement"
down_revision = "0023_arc_phase1"
branch_labels = None
depends_on = None


# Refused before altering anything, so an operator gets the counts and the query
# to inspect rather than a bare constraint-violation error naming one row. A
# deployment whose data disagrees needs to know how much disagrees and why
# before it decides what to do about it.
_PREFLIGHT = """
DO $$
DECLARE
    bad_directives INTEGER;
    bad_rules      INTEGER;
BEGIN
    SELECT count(*) INTO bad_directives
      FROM arc_directives d
      JOIN arc_revisions r ON r.revision_id = d.revision_id
     WHERE d.tenant_id IS NOT NULL
       AND d.tenant_id IS DISTINCT FROM r.tenant_id;

    SELECT count(*) INTO bad_rules
      FROM arc_applicability_rules ar
      JOIN arc_revisions r ON r.revision_id = ar.revision_id
     WHERE ar.tenant_id IS NOT NULL
       AND ar.tenant_id IS DISTINCT FROM r.tenant_id;

    IF bad_directives > 0 OR bad_rules > 0 THEN
        RAISE EXCEPTION
            'refusing to add child/parent tenant agreement: % directive(s) and % rule(s) name a '
            'different tenant than their revision. Each is a row where a tenant boundary was '
            'already crossed. Inspect them before deciding: SELECT d.revision_id, d.directive_id, '
            'd.tenant_id, r.tenant_id FROM arc_directives d JOIN arc_revisions r USING (revision_id) '
            'WHERE d.tenant_id IS NOT NULL AND d.tenant_id IS DISTINCT FROM r.tenant_id;',
            bad_directives, bad_rules;
    END IF;
END
$$
"""

# The composite foreign key needs a matching unique constraint to reference.
# `revision_id` is already the primary key, so this adds no new uniqueness --
# it exists only to give the reference a target.
_UNIQUE_TARGET = "ALTER TABLE arc_revisions ADD CONSTRAINT uq_arc_revisions_id_tenant UNIQUE (revision_id, tenant_id)"

# Deferred to commit, matching the idiom this schema already uses for its cyclic
# references. Checked per-statement, moving an artifact between global and
# tenant scope would impose an order on the caller -- children before the parent
# in one direction, after it in the other -- and a transaction that got it
# backwards would fail even though its end state is consistent. What matters is
# that no *committed* row disagrees, which is exactly what deferral checks.
_CHILD_FKS = [
    "ALTER TABLE arc_directives ADD CONSTRAINT fk_arc_directives_revision_tenant "
    "FOREIGN KEY (revision_id, tenant_id) REFERENCES arc_revisions (revision_id, tenant_id) "
    "DEFERRABLE INITIALLY DEFERRED",
    "ALTER TABLE arc_applicability_rules ADD CONSTRAINT fk_arc_rules_revision_tenant "
    "FOREIGN KEY (revision_id, tenant_id) REFERENCES arc_revisions (revision_id, tenant_id) "
    "DEFERRABLE INITIALLY DEFERRED",
]


def upgrade() -> None:
    op.execute(_PREFLIGHT)
    op.execute(_UNIQUE_TARGET)
    for statement in _CHILD_FKS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE arc_applicability_rules DROP CONSTRAINT fk_arc_rules_revision_tenant")
    op.execute("ALTER TABLE arc_directives DROP CONSTRAINT fk_arc_directives_revision_tenant")
    op.execute("ALTER TABLE arc_revisions DROP CONSTRAINT uq_arc_revisions_id_tenant")
