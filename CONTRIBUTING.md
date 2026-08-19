# Contributing to Context Plane

Start here. This file is the contributor entry point; the working detail lives
under [`.develop/`](.develop/), which holds development artifacts (how the
project is built) as distinct from [`docs/`](docs/), which documents the
shipped product.

| I need to… | Go to |
|---|---|
| Get a local stack running | [`.develop/local-dev.md`](.develop/local-dev.md) |
| Understand the gates and CI wiring | [`.develop/ci.md`](.develop/ci.md) |
| Know how work is claimed, reviewed, and merged | [`.develop/delivery-process.md`](.develop/delivery-process.md) |
| Read or add an architecture decision | [`.develop/adr/`](.develop/adr/) |
| See what is planned and claimable | [`.develop/plan/`](.develop/plan/) |

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,devstack]"
make dev-up          # Postgres, mock IdP, mock entitlements, observability, API
make dev-token       # seed a dev tenant and mock-IdP credentials
```

No container runtime is required. `docker compose up -d` is an equally
supported alternative on the same ports. Both paths, and where the local
Postgres comes from, are covered in
[`.develop/local-dev.md`](.develop/local-dev.md).

## Code style

- Python 3.12+, formatted with `ruff format`, linted with `ruff check`.
- `mypy --strict` over `contextplane/` and `scripts/`.
- Tests: `pytest tests/unit` for fast feedback. `tests/integration` and
  `tests/conformance` need a real Postgres; `CONTEXTPLANE_TEST_PG` chooses
  where it comes from and defaults to whatever the machine has.
- `make all` runs every gate a PR must pass.

Repo-wide conventions every contributor and AI agent follows are in
[`CLAUDE.md`](CLAUDE.md).

## Branches, commits, and merging

Every change lands through a pull request that passes the required checks;
nobody pushes to `main`. The full lifecycle — claiming work, push cadence,
stale-claim takeover, parallelism rules — is
[`.develop/delivery-process.md`](.develop/delivery-process.md).

The **PR title** must be conventional-commit format, because it becomes the
squash commit and feeds the changelog:

```
feat(memory): add provenance-scoped quarantine
fix(arc): reject stale approval evidence
docs: relocate development artifacts to .develop
```

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `ci`,
`build`, `revert`. Individual commits on the branch are squashed, so their
messages are working notes rather than permanent history.

## Reporting

Redact tokens, credentials, tenant identifiers, and personal data before
pasting logs into issues or pull requests. Post excerpts, never full dumps.
