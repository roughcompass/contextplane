"""Whether a human or a policy decided, recorded rather than inferred.

E5-T4. A disposition already records *who may approve it*, stamped at
disposition time rather than worked out later. This is the same discipline
applied to a different question: **what kind of thing decided**.

**Not inferrable from `owner_id`, which is why it is a column.** A policy runs
as some actor, and telling a policy's actor from a person's means knowing which
service accounts are automation — a fact that lives outside this table, changes
without a migration, and is wrong for exactly the deployment that adds a new
one. A field that has to be reconstructed is a field that will be reconstructed
differently by the next reader.

**The sampling consequence is the reason this needed a decision rather than a
column.** Acceptance sampling assumes the sample was *inspected*. A policy
disposition inspected nothing, so counting it toward a reviewer's sample would
raise the measured quality of a queue nobody looked at — the failure mode where
the number keeps looking fine.

Two options, and this takes the first:

1. **Policy dispositions are excluded from the sample.** The human sample
   requirement is then unchanged by automation, so deploying a more aggressive
   policy cannot quietly shrink what a person has to look at. That property is
   the whole argument: it makes automation unable to improve the number by
   reducing the evidence behind it.
2. A separate stream with its own acceptance criteria. Refused, because those
   criteria would be a defect tolerance and a consumer's risk for automated
   disposal that nobody has measured — inventing a governance fact to make an
   automated path look governed.

`ix_curation_inspected_dispositions` exists so the exclusion is cheap: the
sampling read asks for human-decided resolutions specifically, and a partial
index over exactly that is what keeps "excluded" from meaning "scanned and
discarded".
"""

from __future__ import annotations

from alembic import op

revision = "0077_disposition_actor_kind"
down_revision: str | None = "0076_reporting_obligations"
branch_labels: str | None = None
depends_on: str | None = None

ACTOR_HUMAN = "human"
ACTOR_POLICY = "policy"

_KINDS = ", ".join(f"'{kind}'" for kind in (ACTOR_HUMAN, ACTOR_POLICY))


def upgrade() -> None:
    op.execute("ALTER TABLE curation_cases ADD COLUMN disposition_actor_kind TEXT")
    op.execute(
        f"ALTER TABLE curation_cases ADD CONSTRAINT ck_case_disposition_actor_kind "
        f"CHECK (disposition_actor_kind IS NULL OR disposition_actor_kind IN ({_KINDS}))"
    )
    # Backfilled as `human`, and that is provable rather than assumed: until this
    # migration there is no policy disposition path at all -- every disposition
    # already stored arrived through a transport, past the check that the caller
    # *is* the routed owner. Adding the constraint before the backfill would
    # refuse every row that already exists.
    op.execute(
        f"UPDATE curation_cases SET disposition_actor_kind = '{ACTOR_HUMAN}' "  # noqa: S608 - ACTOR_HUMAN is a module constant, not caller input
        f"WHERE disposition IS NOT NULL AND disposition_actor_kind IS NULL"
    )
    # Set together with the disposition or not at all. A disposition whose actor
    # kind is null is one the sampling read cannot classify, and a null that the
    # read has to guess about is the inference this column exists to remove.
    op.execute(
        "ALTER TABLE curation_cases ADD CONSTRAINT ck_case_disposition_says_who_decided "
        "CHECK ((disposition IS NULL) = (disposition_actor_kind IS NULL))"
    )
    # The sampling read's whole predicate, so excluding automation costs an index
    # scan rather than a filter over every resolved case.
    op.execute(
        f"CREATE INDEX ix_curation_inspected_dispositions "
        f"ON curation_cases (tenant_id, resolved_at) "
        f"WHERE disposition_actor_kind = '{ACTOR_HUMAN}'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_curation_inspected_dispositions")
    op.execute("ALTER TABLE curation_cases DROP CONSTRAINT ck_case_disposition_says_who_decided")
    op.execute("ALTER TABLE curation_cases DROP CONSTRAINT ck_case_disposition_actor_kind")
    op.execute("ALTER TABLE curation_cases DROP COLUMN disposition_actor_kind")
