# Migrations

One curated baseline (`0001_baseline_schema.py`) covers every table, index,
constraint, and seed row the service runs on today. Every migration added
from here forward is named for the schema change it makes —
`add_workspace_index.py`, `drop_capability_annotations.py` — not for a
delivery phase. `scripts/check_migration_naming.py` (run as part of
`make test-hygiene`) rejects a new filename that contains `phase\d+` or the
retired `lmm` prefix.

## Running

`DATABASE_URL` must be exported (asyncpg DSN, e.g.
`postgresql+asyncpg://postgres:password@localhost:5432/registry`). Run these
from the repo root, where `alembic.ini` lives.

```bash
alembic upgrade head      # apply all migrations
alembic current           # print the current revision
alembic downgrade -1      # roll back one revision
alembic downgrade base    # roll back everything
```

## Authoring

* Every revision is reversible. `downgrade()` must restore the previous schema.
* DDL is written verbatim in the migration file. Indexes and PARTITION DDL live
  in the migration, not in `storage/models.py`.
* Name the file for what it does to the schema, not when it shipped —
  `scripts/check_migration_naming.py` enforces this on every new file added
  under `versions/`.
