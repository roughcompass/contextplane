# Contributing to registry

## Developer Certificate of Origin (DCO)

Every commit must carry a `Signed-off-by:` trailer certifying the [Developer Certificate of Origin v1.1](https://developercertificate.org/). Sign your commit with:

```
git commit -s -m "your message"
```

The trailer must use the same name and email as your git author identity. The DCO check runs as a GitHub Action on every pull request; commits without a valid `Signed-off-by:` block the PR.

There is no Contributor License Agreement (CLA) — DCO is the only contributor sign-off mechanism.

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
- `mypy --strict` over `registry/`, `sync/`, and `scripts/`.
- Tests: `pytest tests/unit` for fast feedback. `tests/integration` and
  `tests/conformance` need a real Postgres; `REGISTRY_TEST_PG` chooses
  where it comes from and defaults to whatever the machine has.
- `make all` runs every gate a PR must pass.

## Commit messages

Tasks ship one-commit-per-task. The message starts with the task ID, e.g.:

```
CAP-PN-TNN: short description of what changed

<body explaining why>

Signed-off-by: Your Name <you@example.com>
```
