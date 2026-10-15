# Phase 0: Repository foundation and CLAUDE.md

**Goal:** a repository Claude Code can work in productively from day one.

**Done when:** CI is green on the skeleton, `make test` and `make lint` work, and `CLAUDE.md` is reviewed.

## Starting point

The repository holds only `spec/` (spec, implementation plan, `schema.sql`). There is no git remote (B-002), so "CI green" means the CI workflow is written and `make ci` (the same steps) passes locally.

## Tasks

### 1. Layout

```
CLAUDE.md
Makefile
.env.example
.gitignore
.pre-commit-config.yaml
.github/workflows/ci.yml
docs/
  plans/phase_00_foundation.md
  progress.md
  runbooks/            (empty for now, .gitkeep)
backend/
  pyproject.toml, uv.lock
  app/
    __init__.py
    __main__.py        python -m app <command>
    cli.py             subcommands: api, worker, maintenance, migrate, seed
    settings.py        typed settings from environment (pydantic-settings)
    api/
      __init__.py
      main.py          create_app() factory, /healthz
  migrations/
    001_initial_schema.sql   copy of spec/schema.sql (runner arrives in phase 3)
  tests/
    conftest.py
    test_health.py
    test_cli.py
    test_settings.py
web/
  package.json, pnpm-lock.yaml
  vite.config.ts, tsconfig*.json, eslint.config.js, .prettierrc
  src/main.tsx, src/App.tsx, src/App.test.tsx
```

`spec/` stays where it is (D-001).

### 2. Backend tooling

- Python 3.12, `uv` for dependency management with a committed `uv.lock`.
- Runtime dependencies for now: `fastapi`, `uvicorn[standard]`, `pydantic-settings`. Later phases add their own.
- Dev dependencies: `pytest`, `pytest-asyncio`, `httpx`, `ruff`, `mypy`, `pre-commit`.
- `ruff` for lint and format (line length 110, rule sets E, F, I, B, UP, S (bandit subset), ASYNC, SIM, RUF).
- `mypy` in strict mode on `app/` (tests checked less strictly).
- `pytest` with `asyncio_mode = auto`.

### 3. Backend skeleton

- `app/settings.py`: one `Settings` class; every value comes from the environment (Render rule 2). Phase 0 fields: `env` (`local`/`test`/`staging`/`production`), `port` (from `$PORT`, default 8000), `log_level`. Cached `get_settings()`.
- `app/cli.py`: `argparse` subcommands. `api` runs uvicorn on `0.0.0.0:$PORT` (Render rule 1). `worker`, `maintenance`, `migrate`, `seed` exist as placeholders that exit with a clear "arrives in phase N" message and non zero status, so nobody mistakes them for working.
- `app/api/main.py`: `create_app()` returns a FastAPI app with `GET /healthz` returning `{"status": "ok"}`.

### 4. Web tooling and skeleton

- Vite + React + TypeScript, `pnpm` via corepack (version pinned in `packageManager`).
- ESLint (flat config, typescript-eslint, react-hooks, react-refresh), Prettier.
- Vitest + Testing Library + jsdom for unit tests.
- Scripts: `dev`, `build`, `lint`, `format`, `format:check`, `typecheck`, `test`.
- Placeholder `App` rendering the product name, with one test.

### 5. Makefile

Targets: `help`, `install`, `lint`, `fmt`, `typecheck`, `test`, `test-backend`, `test-web`, `ci`, `hooks`. Placeholders for `dev`, `down`, `reset`, `migrate`, `seed` that point to the phase that implements them (phase 1 and 3).

### 6. CI (`.github/workflows/ci.yml`)

Two jobs on every push and pull request:
- **backend**: set up uv and Python 3.12, `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy`, `pytest`. A Postgres 16 service container is declared now so phase 3 can use it without reworking the workflow.
- **web**: set up pnpm and Node 22, `pnpm install --frozen-lockfile`, lint, format check, typecheck, test, build.

The workflow is validated with `actionlint` locally if it can be installed; otherwise reviewed by hand.

### 7. Commit hooks

`.pre-commit-config.yaml` with local hooks: ruff check (fix), ruff format, and prettier + eslint on staged web files. `make hooks` installs them.

### 8. `.env.example`

Documents every variable used so far, grouped by component, with a comment per variable. Grows every phase.

### 9. `CLAUDE.md`

Short and factual, linking to `spec/` for details:
- what the product is (a few lines);
- architecture summary: components, one image many commands, Render layout;
- the 10 invariants from plan section 4, verbatim;
- commands;
- code conventions: module layout, naming, typing, errors, logging, how to add a migration / tool adapter / playbook (the last two marked as arriving in phases 7 and 8);
- testing rules: real Postgres for anything touching the database, no mocks of row level security, isolation tests mandatory for new tenant tables;
- per phase workflow (plan section 1, adjusted per D-001 to D-005);
- gotchas section.

### 10. Records

- `docs/progress.md` with a Phase 0 entry.
- Decisions in `spec/decisions_and_blockers.md`.

## Tests

| Test | Checks |
|---|---|
| `test_health.py` | `/healthz` returns 200 and `{"status": "ok"}`; unknown routes return 404 |
| `test_settings.py` | `PORT` env is honored; defaults; invalid `env` value rejected |
| `test_cli.py` | `python -m app --help` lists all subcommands; placeholder subcommands exit non zero with a message; `api` builds the uvicorn config bound to `0.0.0.0:$PORT` (checked without starting a server) |
| `App.test.tsx` | the console placeholder renders |
| Live check | start `python -m app api` with `PORT=18081`, curl `/healthz`, send SIGTERM, confirm clean exit |
| `make ci` | runs every CI step locally and passes |

## Risks

- **pnpm via corepack** may need network access to fetch pnpm; fall back to `npm i -g pnpm` if corepack fails.
- **Strict mypy with FastAPI**: decorators are typed well enough; keep `disallow_untyped_decorators` on and fix as needed.
