# Common commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

# uv warns when a foreign virtualenv is active; the project always uses backend/.venv.
unexport VIRTUAL_ENV
export COREPACK_ENABLE_DOWNLOAD_PROMPT := 0

UV  := cd backend && uv run
WEB := pnpm --dir web

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install backend and web dependencies
	cd backend && uv sync --frozen
	$(WEB) install --frozen-lockfile

.PHONY: hooks
hooks: ## Install the git commit hooks
	$(UV) pre-commit install --config ../.pre-commit-config.yaml

## ---------------------------------------------------------------- quality

.PHONY: lint
lint: ## Lint and format-check backend and web
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(WEB) lint
	$(WEB) format:check

.PHONY: fmt
fmt: ## Format backend and web
	$(UV) ruff check --fix .
	$(UV) ruff format .
	$(WEB) format

.PHONY: typecheck
typecheck: ## Type check backend (mypy) and web (tsc)
	$(UV) mypy app tests
	$(WEB) typecheck

## ---------------------------------------------------------------- tests

.PHONY: test
test: test-backend test-web ## Run all unit and integration tests

.PHONY: test-backend
test-backend: ## Run backend tests
	$(UV) pytest

.PHONY: test-web
test-web: ## Run web unit tests
	$(WEB) test

.PHONY: build-web
build-web: ## Build the web console
	$(WEB) build

.PHONY: ci
ci: lint typecheck test build-web ## Run every CI step locally

## ---------------------------------------------------------------- local stack (phase 1)

.PHONY: dev down reset seed migrate
dev down reset seed: ## Local stack commands (arrive in phase 1)
	@echo "'make $@' arrives in phase 1" && exit 2
migrate: ## Apply migrations (arrives in phase 3)
	@echo "'make migrate' arrives in phase 3" && exit 2
