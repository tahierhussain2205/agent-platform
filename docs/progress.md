# Progress

What each phase delivered. Decisions and blockers are in [spec/decisions_and_blockers.md](../spec/decisions_and_blockers.md).

## Phase 0: Repository foundation (done)

- Monorepo layout: `backend/` (FastAPI skeleton, single CLI entry point), `web/` (Vite + React + TypeScript), `docs/`, `.github/workflows/ci.yml`.
- Tooling: uv, ruff, mypy (strict), pytest; pnpm, ESLint, Prettier, Vitest; pre-commit hooks running the project's pinned tools.
- `make lint | fmt | typecheck | test | ci | hooks`.
- `CLAUDE.md` with architecture, invariants, commands, conventions, testing rules and workflow.
- `backend/migrations/001_initial_schema.sql` copied from `spec/schema.sql` (runner arrives in phase 3).
- Verified: `make ci` passes (14 backend tests, 1 web test, production build); `actionlint` passes on the workflow; the API binds `0.0.0.0:$PORT` and shuts down gracefully on SIGTERM; commit hooks pass on all files.
- Not verified: a GitHub Actions run (no remote, B-002).
