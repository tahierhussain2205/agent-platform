# Common commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

# uv warns when a foreign virtualenv is active; the project always uses backend/.venv.
unexport VIRTUAL_ENV
export COREPACK_ENABLE_DOWNLOAD_PROMPT := 0

UV  := cd backend && uv run
FAKES := cd fakes && uv run
WEB := pnpm --dir web
E2E := pnpm --dir e2e

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install backend and web dependencies
	cd backend && uv sync --frozen
	cd fakes && uv sync --frozen
	$(WEB) install --frozen-lockfile
	$(E2E) install --frozen-lockfile
	$(E2E) exec playwright install chromium

.PHONY: hooks
hooks: ## Install the git commit hooks
	$(UV) pre-commit install --config ../.pre-commit-config.yaml

## ---------------------------------------------------------------- quality

.PHONY: lint
lint: ## Lint and format-check backend and web
	$(UV) ruff check . ../scripts
	$(UV) ruff format --check . ../scripts
	$(FAKES) ruff check .
	$(FAKES) ruff format --check .
	$(WEB) lint
	$(WEB) format:check

.PHONY: fmt
fmt: ## Format backend and web
	$(UV) ruff check --fix . ../scripts
	$(UV) ruff format . ../scripts
	$(FAKES) ruff check --fix .
	$(FAKES) ruff format .
	$(WEB) format

.PHONY: typecheck
typecheck: ## Type check backend (mypy) and web (tsc)
	$(UV) mypy app tests
	$(FAKES) mypy fakes
	$(WEB) typecheck
	$(E2E) exec tsc --noEmit -p .

## ---------------------------------------------------------------- tests

.PHONY: test
test: test-backend test-fakes test-web ## Run all unit and integration tests

.PHONY: test-fakes
test-fakes: ## Run the SaaS stand-in tests
	$(FAKES) pytest

.PHONY: test-backend
test-backend: infra ## Run backend tests (starts Postgres and Temporal first)
	$(UV) pytest

.PHONY: test-mutations
test-mutations: infra ## Weaken each DB guarantee in turn and check the schema suite catches it
	./scripts/schema_mutations.sh

.PHONY: test-web
test-web: ## Run web unit tests
	$(WEB) test

.PHONY: build-web
build-web: ## Build the web console
	$(WEB) build

.PHONY: ci
ci: lint typecheck test test-mutations build-web ## Run every CI step locally

## ---------------------------------------------------------------- local stack

COMPOSE := docker compose
INFRA   := postgres pgbouncer valkey temporal objectstore jaeger fakes

.PHONY: dev
dev: ## Build and start the whole stack, wait until healthy
	$(COMPOSE) up -d --build --wait

.PHONY: infra
infra: ## Start only the infrastructure (run api/worker on the host with uv)
	$(COMPOSE) up -d --wait $(INFRA)

.PHONY: down
down: ## Stop the stack, keep data
	$(COMPOSE) down

.PHONY: reset
reset: ## Stop the stack, delete all local data, start again
	$(COMPOSE) down --volumes --remove-orphans
	$(MAKE) dev

.PHONY: migrate
migrate: ## Apply migrations to the local database
	$(COMPOSE) run --rm migrate

.PHONY: seed
seed: ## Load local development data
	$(COMPOSE) run --rm seed

.PHONY: maintenance
maintenance: ## Run the maintenance jobs once (what the Render cron job does daily)
	$(COMPOSE) run --rm --no-deps seed app maintenance

.PHONY: logs
logs: ## Follow logs (S=service to pick one)
	$(COMPOSE) logs -f $(S)

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) ps -a

.PHONY: smoke
smoke: ## Check the running stack end to end
	./scripts/smoke.sh

.PHONY: test-image
test-image: ## Check the backend image (non root, $$PORT, SIGTERM); needs `make dev`
	./scripts/test_image.sh

.PHONY: e2e
e2e: ## Run Playwright browser tests against the running stack
	$(E2E) test

.PHONY: render-sim
render-sim: ## Execute render.yaml locally the way Render would (no Render account needed)
	$(UV) python ../scripts/render_sim.py

.PHONY: test-stack
test-stack: dev smoke test-image e2e ## Start the stack and run every stack level check
