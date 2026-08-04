"""Drop `episodes`, `provenance`, and the `episodes_new` shadow: never written.

Both tables shipped with the original schema as the intended landing zone for
ingested events and their source attribution. Neither ever gained a writer or
a reader — session events landed in `memory_session_events`, and claim
attribution landed in `lmm_claim_provenance`, both designed later and better.
An empty table with a plausible name is not neutral: it is where the next
engineer looks for the data that actually lives somewhere else.

The guard refuses to drop a non-empty table. Nothing is deployed and no code
path can have written a row, so the guard should never fire — it exists so
that if this assumption is ever wrong, the migration fails loudly instead of
destroying the evidence that it was wrong.
"""

from __future__ import annotations

from alembic import op

revision = "0048_drop_never_used_event_tables"
down_revision = "0047_usage_capability_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # provenance first (it references episodes); episodes_new last — the
    # partitioned shadow a cutover script was meant to rename over episodes,
    # built and never cut over, so it sits empty beside the table it shadows.
    # CASCADE takes its twelve monthly partitions with it.
    for table, drop in (
        ("provenance", "DROP TABLE provenance"),
        ("episodes", "DROP TABLE episodes"),
        ("episodes_new", "DROP TABLE IF EXISTS episodes_new CASCADE"),
    ):
        exists = op.get_bind().exec_driver_sql(
            f"SELECT to_regclass('{table}') IS NOT NULL"  # noqa: S608 - closed set
        ).scalar()
        if not exists:
            continue
        count = op.get_bind().exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()  # noqa: S608 - closed set
        if count:
            msg = f"refusing to drop {table}: it holds {count} row(s) and was believed empty"
            raise RuntimeError(msg)
        op.execute(drop)


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE episodes (
            episode_id      UUID PRIMARY KEY,
            tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
            episode_type    TEXT NOT NULL,
            source_id       TEXT NOT NULL,
            actor_id        UUID REFERENCES actors(actor_id),
            content_summary TEXT,
            ts              TIMESTAMPTZ NOT NULL,
            ingested_at     TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE provenance (
            prov_id    UUID PRIMARY KEY,
            tenant_id  UUID NOT NULL REFERENCES tenants(tenant_id),
            claim_type TEXT NOT NULL,
            claim_id   UUID NOT NULL,
            episode_id UUID NOT NULL REFERENCES episodes(episode_id),
            source_url TEXT,
            commit_sha TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
