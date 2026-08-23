"""How much of a category's queue a reviewer has to look at, and why that number.

E5-T2. One governed sampling policy per tenant and claim category, with the
sample size derived from a stated defect tolerance and consumer's risk rather
than chosen.

**The key is `(tenant_id, claim_category)`, and the epic asked for
`(tenant, action class, sensitivity tier)`. Both halves of that changed, and
both for reasons the entry invited: "reuse it or justify a second axis."**

**Action class became claim category.** ARC's `ActionClass` is `merge`,
`deploy`, `production_configuration_mutation`, `secret_release`, `data_export` —
governed *actions an agent takes*. What this queue holds is *claims awaiting
adjudication*, and a claim is not an action. Reusing that vocabulary would have
keyed a review budget on a dimension no queued row has.

There is a second, independent reason it could not be reused as written: the
import-linter layer contract places `arc` above `service`, so
`service.memory` may not import `ActionClass` at all. `CLAIM_CATEGORIES` is a
closed set on this layer, is already a column on `memory_claims`, and is already
what decay is keyed on -- so it is the axis the row actually carries.

**Sensitivity tier is not here, and that is a finding rather than an omission.**
E1's handling tier is declared on *streams*: `memory_source_namespaces` is keyed
on `(tenant_id, source_system, source_namespace)`. A `memory_claims` row has a
`namespace` and no `source_system`, so it cannot reach that table -- there is no
tier to select a policy by, and inventing one inside a sampling policy would
have made up a governance fact. Giving claims a derivable tier is a real change
with its own task; a policy keyed on a column nobody can populate would have
been a policy nobody could select.

**The sample size is derived, not chosen, and the arithmetic is stored beside
it.** For a zero-acceptance plan, the chance of accepting a lot whose true defect
rate is `p` after `n` draws is `(1 - p)**n`. Requiring that to be at most the
consumer's risk `beta` gives `n >= ln(beta) / ln(1 - p)`. At a 5% tolerance and
10% consumer's risk that is 45. The columns hold all three so a reviewer can
recompute the third from the first two, which is what makes this a `derived`
magnitude under
`.develop/adr/0014-derived-magnitudes-are-a-third-status.md`'s rule rather than
a number somebody picked.
"""

from __future__ import annotations

from alembic import op

from contextplane.service.catalog.global_vocabulary import CLAIM_CATEGORIES

revision = "0075_claim_sampling_policy"
down_revision: str | None = "0074_receipt_prequarantine"
branch_labels: str | None = None
depends_on: str | None = None

#: Built from the vocabulary rather than typed beside it, the way migration 0068
#: builds its tier CHECK from `sensitivity.TIERS`. A category added there must
#: not need a migration hunt to become policyable.
_CATEGORY_LIST = ", ".join(f"'{category}'" for category in sorted(CLAIM_CATEGORIES))

_TABLE = f"""
CREATE TABLE claim_sampling_policies (
    tenant_id          UUID NOT NULL REFERENCES tenants(tenant_id),
    claim_category     TEXT NOT NULL,

    -- The two stated inputs. `defect_tolerance` is the share of wrong claims
    -- this category may carry before review is failing; `consumers_risk` is the
    -- chance of accepting a category that is worse than that. Both are
    -- decisions, and both are recorded so the third column can be checked.
    defect_tolerance   NUMERIC(5, 4) NOT NULL,
    consumers_risk     NUMERIC(5, 4) NOT NULL,

    -- Derived from the two above, stored rather than computed at read: a
    -- reviewer's budget must not change because a floating-point library did.
    -- The service recomputes and refuses a row whose stored value disagrees.
    min_sample         INTEGER NOT NULL,

    -- Who decided, and why. A sampling floor is a governance claim in exactly
    -- the way a handling tier is, and the same bar applies: a number recorded
    -- with "prod" beside it is one nobody can review.
    set_by             UUID NOT NULL REFERENCES actors(actor_id),
    set_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason             TEXT NOT NULL,

    PRIMARY KEY (tenant_id, claim_category),

    CONSTRAINT ck_csp_category CHECK (claim_category IN ({_CATEGORY_LIST})),
    -- Strictly between 0 and 1 in both cases. A tolerance of 0 demands an
    -- infinite sample and a tolerance of 1 accepts anything; a consumer's risk
    -- of 0 is the same demand from the other side. The derivation divides by
    -- `ln(1 - defect_tolerance)`, which is why 1 is excluded here rather than
    -- caught later as a division error.
    CONSTRAINT ck_csp_tolerance CHECK (defect_tolerance > 0 AND defect_tolerance < 1),
    CONSTRAINT ck_csp_risk CHECK (consumers_risk > 0 AND consumers_risk < 1),
    CONSTRAINT ck_csp_sample CHECK (min_sample >= 1),
    CONSTRAINT ck_csp_reason_len CHECK (char_length(reason) BETWEEN 20 AND 2000)
)
"""


def upgrade() -> None:
    op.execute(_TABLE)


def downgrade() -> None:
    op.execute("DROP TABLE claim_sampling_policies")
