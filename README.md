# Reservations inbox agent

[![CI](https://github.com/GeorgeTheo7/hotel-reservations-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/GeorgeTheo7/hotel-reservations-agent/actions/workflows/ci.yml)

An agent that drafts replies to guest emails for **Hotel Anemos**, a fictional 14-room hotel
on the fictional Greek island of Psymithos. The owner approves every draft before anything is
sent or any room is held.

- **Code computes prices and availability** from the database. The model never does.
- **Tools that change data** (hold a room, send a reply) run only after the owner approves.
- **Every draft is checked in code** against tool results before the owner sees it.
- **All data is fictional.**

Design decisions are in [docs/SPEC.md](docs/SPEC.md).

**Status:** week 1 of 12 — foundation only: Postgres schema, migrations, 2027 seed data,
injected clock, CI. No agent or model calls yet.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.12+ | Installs Python 3.12 and all dependencies |
| [Docker](https://docs.docker.com/get-docker/) | with Compose v2 | Docker Desktop on Windows/macOS; must be running |
| git | any | |

You don't need to install Python yourself: uv downloads the version in `.python-version`.

## Setup

Clone the repository and enter it:

```sh
git clone https://github.com/GeorgeTheo7/hotel-reservations-agent.git
cd hotel-reservations-agent
```

Create your local `.env` from the example (it is git-ignored):

```powershell
# Windows (PowerShell)
Copy-Item .env.example .env
```

```sh
# Linux / macOS
cp .env.example .env
```

Install, start Postgres, create the schema and load the 2027 seed data.
These commands are the same in PowerShell and in a Linux/macOS shell:

```sh
uv sync
docker compose up -d --wait
uv run --env-file .env alembic upgrade head
uv run --env-file .env python -m anemos.db.seed
```

`--wait` returns once Postgres is healthy. The seed prints
`Seeded: room_types 4, rooms 14, seasons 3, room_rates 12, season_periods 5, min_stay_rules 7`
and can be run again safely.

## Tests

```sh
uv run pytest -m unit                          # no database, no network
uv run --env-file .env pytest -m integration   # needs Postgres running
```

Integration tests never touch your dev database. Each run recreates a throwaway
`anemos_test` database, migrates and seeds it, and runs every test inside a transaction
that is rolled back.

Evaluations (anything that calls a model) will live under `eval/` and are never run by
pytest or CI.

## Everyday commands

Same in PowerShell and Linux/macOS shells.

| Task | Command |
|---|---|
| Lint | `uv run ruff check` |
| Format | `uv run ruff format` |
| Type-check (strict) | `uv run mypy` |
| All CI checks, locally | the lint, `ruff format --check`, mypy, and both test commands above |
| Current migration | `uv run --env-file .env alembic current` |
| New migration | `uv run --env-file .env alembic revision -m "short description" --rev-id 0003` |
| Show a migration's SQL without running it | `uv run --env-file .env alembic upgrade head --sql` |
| Stop Postgres (keeps data) | `docker compose stop` |
| Delete the database and start over | `docker compose down -v`, then the setup commands again |
| Open a SQL shell | `docker compose exec postgres psql -U anemos -d anemos` |

Migrations are plain SQL inside `op.execute(...)`. Use the next number as `--rev-id`
and write a `downgrade()` that exactly undoes `upgrade()`; a test replays the whole chain.

## Project layout

```text
src/anemos/
  clock.py              injected clock: the only place that reads the system time
  reservations/         availability, pricing, rules, dates (from week 2)
  db/
    connection.py       psycopg connection from DATABASE_URL
    migrate.py          run migrations from code
    migrations/         Alembic: env.py and versions/0001_..., 0002_...
    seed.py             2027 season seed data
tests/
  unit/                 no database, no network
  integration/          Postgres; conftest.py builds the test database
docs/SPEC.md            design decisions and milestones
compose.yaml            local Postgres (pgvector image)
.github/workflows/      CI: lint, types, unit tests, integration tests
```

## Troubleshooting

- **Database connections hang for minutes (Windows).** Use `127.0.0.1`, not `localhost`,
  in `DATABASE_URL`. On Windows `localhost` tries IPv6 first, but compose publishes the
  port on IPv4 only. `.env.example` already uses `127.0.0.1`.
- **`DATABASE_URL is not set`.** You ran a database command without `--env-file .env`,
  or `.env` doesn't exist yet (see Setup).
- **Port 5433 is already in use.** Change `POSTGRES_PORT` and the port in `DATABASE_URL`
  in your `.env`, then `docker compose up -d --wait` again.
- **`docker compose` can't connect to the Docker daemon.** Start Docker Desktop (or the
  Docker service on Linux) and retry.
