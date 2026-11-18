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

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.12+ | Installs Python 3.12 and all dependencies |
| [Docker](https://docs.docker.com/get-docker/) | with Compose v2 | Docker Desktop on Windows/macOS; must be running |
| git | any | |
| [Node.js](https://nodejs.org/) | 26 LTS | Only for the owner's UI in `web/` |

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

Install, start Postgres, create the schema (the Alembic migrations, then LangGraph's
checkpoint tables in the `langgraph` schema) and load the 2027 seed data.
These commands are the same in PowerShell and in a Linux/macOS shell:

```sh
uv sync
docker compose up -d --wait
uv run --env-file .env python -m anemos.db.migrate
uv run --env-file .env python -m anemos.db.seed
```

`--wait` returns once Postgres is healthy. The seed prints
`Seeded: room_types 4, rooms 14, seasons 3, room_rates 12, season_periods 5, min_stay_rules 7`
and can be run again safely.

## Run the owner's UI locally

In `.env`, set `ANEMOS_OWNER_PASSWORD` to a password of your own (at least 12
characters); the API refuses to start without it. Then, after the setup above, fill a
demo inbox. It uses no model, no API key and costs nothing: a stand-in answers from the
labelled demo emails. Running it again stores nothing twice.

```sh
uv run --env-file .env python -m anemos.demo_inbox
```

Start the API in one terminal (it listens on 127.0.0.1:8000 only):

```sh
uv run --env-file .env python -m anemos.api
```

Start the UI in a second terminal, then open <http://127.0.0.1:5173> and log in with
your password:

```sh
cd web
npm ci
npm run dev
```

`npm ci` installs exactly the locked packages and runs no install scripts; it is only
needed the first time and after `web/package-lock.json` changes. The UI's dev server
forwards `/api` to the API, so the browser sees one site (cookie and CSRF checks work
as they will in production). If the login is accepted but the UI says the browser did
not send the cookie back, set `ANEMOS_COOKIE_SECURE=false` in `.env` and restart the
API; this is for local development only.

UI checks (from `web/`): `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.

Model calls (from week 4) are capped by `ANEMOS_LLM_BUDGET_USD` in `.env` (default
$2.00, counted in the git-ignored `var/llm_spend.jsonl`). Set a hard monthly usage limit
in the OpenAI dashboard as well; the code cap only sees calls made from this machine.

## Tests

```sh
uv run pytest -m unit                          # no database, no network
uv run --env-file .env pytest -m integration   # needs Postgres running
uv run pytest -m "unit and not slow"            # quick loop: skips the process races
```

Integration tests never touch your dev database. Each run recreates a throwaway
`anemos_test` database, migrates and seeds it, and runs every test inside a transaction
that is rolled back.

### Model spend

Every model call reserves its maximum cost in the spend ledger before it starts and
settles the real cost after. A call that crashes (process killed, machine off) leaves
its reservation open, and it keeps counting at its maximum. To see and free those:

```sh
uv run python -m anemos.llm.budget list                       # spend and open reservations
uv run python -m anemos.llm.budget release <id> --cost-usd 0  # the dashboard's cost, if any
```

Release only a reservation whose call is certainly over (no run in progress), and give
the cost the OpenAI dashboard shows for it. Releasing appends a line; the ledger is
never edited.

Evaluations (anything that calls a model) live under `eval/` and are never run by
pytest or CI. The smoke run extracts the 5 emails in `eval/sets/smoke.txt` with a real
model and records every call in `eval/results/` (needs `OPENAI_API_KEY` in `.env`; a
dirty working tree is refused unless `--allow-dirty`):

```sh
uv run --env-file .env python eval/smoke.py --model gpt-6-luna --repeat 3
```

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
web/                    the owner's UI: React + TypeScript (Vite), tests with Vitest
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
- **`npm test` fails with "Vitest failed to find the current suite" (Windows).** The
  terminal's current folder starts with a lowercase drive letter (`c:\...`, which VS Code
  sometimes uses), and Node then loads Vitest twice. Change to the same folder with an
  uppercase letter and run it again: `Set-Location C:\path\to\hotel-reservations-agent\web`.
- **`docker compose` can't connect to the Docker daemon.** Start Docker Desktop (or the
  Docker service on Linux) and retry.
