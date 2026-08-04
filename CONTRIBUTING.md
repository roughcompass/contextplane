# Contributing to registry

## Development setup

```bash
cd registry
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,devstack]"
make dev-up          # Postgres, mock IdP, mock entitlements, observability, API
make dev-token       # seed a dev tenant and mock-IdP credentials
```

No container runtime is required. `docker compose up -d` is an equally
supported alternative on the same ports. Both paths, and where the local
Postgres comes from, are covered in
[`docs/07-contributing/01-local-dev.md`](docs/07-contributing/01-local-dev.md).

## Code style

- Python 3.12+, formatted with `ruff format`, linted with `ruff check`.
- `mypy --strict` over `registry/` and `scripts/`.
- Tests: `pytest tests/unit` for fast feedback. `tests/integration` and
  `tests/conformance` need a real Postgres; `REGISTRY_TEST_PG` chooses
  where it comes from and defaults to whatever the machine has.
- `make all` runs every gate a PR must pass.

## Commit messages

Tasks ship one-commit-per-task. The message starts with the task ID, e.g.:

```
CAP-PN-TNN: short description of what changed

<body explaining why>
```
