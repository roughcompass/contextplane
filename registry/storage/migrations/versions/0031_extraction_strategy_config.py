"""Per-tenant strategy configuration, and the namespace a claim landed in.

Two things, in one migration because they are two halves of the same decision:
which strategies run for a tenant, and where their output goes.

**What a tenant may override, and what it may not.** Enablement, confidence
floor, prompt, and model are per-tenant columns. The output schema, the permitted
predicate set, and the namespace template are not here at all -- they live in code
and cannot be overridden. An override changes how well claims are found; it must
never change what they are allowed to mean. A tenant that could widen its own
predicate set would be redefining the shared vocabulary from inside a
configuration field, which is precisely what a deployment-wide ontology exists to
prevent.

**A missing row means defaults, not disabled.** Absence is the common case, and a
deployment that had to insert a row per tenant per strategy before extraction
worked would look broken on every new tenant. So the resolved configuration is
"the code's defaults, overlaid with whatever row exists".

**Prompt overrides are stored, not merged.** A partial override -- a fragment
appended to a base prompt -- would mean the effective instruction depends on a
base the tenant cannot see and that changes under them on deploy. The whole
prompt or nothing.

**`namespace` on the claim, not on the outbox row.** Namespaces group and scope
retrieval, so the value has to travel with the thing being retrieved. It is
nullable because claims from connectors and curators have no strategy and
therefore no namespace -- and a synthetic one would imply a grouping nobody
chose.

**Namespaces are not an access-control primitive.** Visibility is enforced on the
claim and at the read chokepoint. A namespace that looked like a permission would
be one a caller could opt out of by supplying a different string.
"""

from __future__ import annotations

from alembic import op

revision = "0031_extraction_strategy_config"
down_revision = "0030_extraction_outbox"
branch_labels = None
depends_on = None


_CONFIG = """
CREATE TABLE lmm_strategy_config (
    config_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    strategy_id      TEXT NOT NULL,

    is_enabled       BOOLEAN NOT NULL DEFAULT TRUE,

    -- Below this, a candidate is not staged. Zero disables the floor, which is
    -- the honest default while confidence is uncalibrated: a floor applied to an
    -- uncalibrated number filters by noise rather than by quality.
    confidence_floor NUMERIC(4, 3) NOT NULL DEFAULT 0.000,

    -- The whole prompt or nothing. A fragment merged into a base the tenant
    -- cannot see would make the effective instruction change under them on
    -- deploy.
    prompt_override  TEXT,
    model_override   TEXT,

    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_lmm_strategy_config UNIQUE (tenant_id, strategy_id),
    CONSTRAINT ck_lmm_strategy_floor CHECK (
        confidence_floor >= 0 AND confidence_floor <= 1
    ),
    -- An empty override is not an override. Storing '' would silently give the
    -- model no instructions at all, which is the worst available outcome:
    -- extraction keeps running and produces nonsense.
    CONSTRAINT ck_lmm_strategy_prompt CHECK (
        prompt_override IS NULL OR char_length(trim(prompt_override)) > 0
    ),
    CONSTRAINT ck_lmm_strategy_model CHECK (
        model_override IS NULL OR char_length(trim(model_override)) > 0
    )
)
"""

_CONFIG_INDEXES = [
    "CREATE INDEX ix_lmm_strategy_config_tenant ON lmm_strategy_config (tenant_id)",
]

# Nullable: a claim from a connector or a curator has no strategy and therefore
# no namespace. A synthetic value would imply a grouping nobody chose.
_CLAIM_NAMESPACE = """
ALTER TABLE lmm_claims
    ADD COLUMN namespace TEXT,
    ADD COLUMN strategy_id TEXT,
    ADD CONSTRAINT ck_lmm_claims_namespace CHECK (
        (namespace IS NULL) = (strategy_id IS NULL)
    )
"""

_CLAIM_INDEXES = [
    # Retrieval within a namespace is the lookup namespaces exist for.
    "CREATE INDEX ix_lmm_claims_namespace ON lmm_claims (namespace) " "WHERE namespace IS NOT NULL",
    # Per-strategy conformance and volume, read from the store rather than only
    # from a counter that resets when the process does.
    "CREATE INDEX ix_lmm_claims_strategy ON lmm_claims (strategy_id, created_at) " "WHERE strategy_id IS NOT NULL",
]


def upgrade() -> None:
    op.execute(_CONFIG)
    for statement in _CONFIG_INDEXES:
        op.execute(statement)
    op.execute(_CLAIM_NAMESPACE)
    for statement in _CLAIM_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_strategy")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_namespace")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_namespace, "
        "  DROP COLUMN IF EXISTS strategy_id, "
        "  DROP COLUMN IF EXISTS namespace"
    )
    op.execute("DROP TABLE IF EXISTS lmm_strategy_config")
