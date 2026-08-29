SHELL := /bin/bash
COMPOSE := docker compose
RUN := $(COMPOSE) run --rm app

# Absolute path; docker-compose cannot expand `~`.
export CLAUDE_PROJECTS_DIR ?= $(HOME)/.claude/projects

.DEFAULT_GOAL := help

.PHONY: help env build build-db up down destroy logs ps psql \
        init-db discover ingest export-parquet stats shell \
        up-grafana dashboard \
        venv lint fmt typecheck test check rag-stress \
        install-timer uninstall-timer

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example with your real projects dir
	@test -f .env || { \
	  sed 's|^CLAUDE_PROJECTS_DIR=.*|CLAUDE_PROJECTS_DIR=$(CLAUDE_PROJECTS_DIR)|' .env.example > .env; \
	  echo "wrote .env"; }
	@mkdir -p data/csv data/parquet && chmod 777 data/parquet

# --- containers -------------------------------------------------------------
build-db: ## Build the postgres+pg_parquet image (slow: compiles pg_parquet)
	$(COMPOSE) build postgres

build: env ## Build both images
	$(COMPOSE) --profile cli build

up: env ## Start PostgreSQL and wait for it to be healthy
	$(COMPOSE) up -d postgres

down: ## Stop containers (keeps the data volume)
	$(COMPOSE) --profile cli down

destroy: ## Stop containers and delete the data volume
	$(COMPOSE) --profile cli down -v

logs: ## Tail PostgreSQL logs
	$(COMPOSE) logs -f postgres

ps: ## Show container status
	$(COMPOSE) ps

psql: ## Open psql inside the database container
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-claude} -d $${POSTGRES_DB:-claude_analytics}

# --- pipeline ---------------------------------------------------------------
init-db: up ## Apply sql/*.sql
	$(RUN) init-db

discover: ## List transcripts that would be ingested
	$(RUN) discover

ingest: up ## Full pipeline: transcripts -> CSV -> PostgreSQL
	$(RUN) ingest $(ARGS)

export-parquet: ## Export tables/views to data/parquet via pg_parquet
	$(RUN) export-parquet $(ARGS)

stats: ## Print headline usage numbers
	$(RUN) stats

shell: ## Bash prompt inside the app container
	$(COMPOSE) run --rm --entrypoint bash app

# --- dashboard ---------------------------------------------------------------
up-grafana: env ## Start PostgreSQL + Grafana, waits for PostgreSQL to be healthy
	$(COMPOSE) --profile dashboard up -d

dashboard: up-grafana ## Start the dashboard and print its URL
	@echo "Grafana: http://localhost:$${GRAFANA_PORT:-3000}"

# --- local dev (no containers) ----------------------------------------------
venv: ## Create the local .venv from uv.lock
	uv sync

lint: ## ruff check
	uv run ruff check .

fmt: ## ruff format + import fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy
	uv run mypy

test: ## pytest
	uv run pytest

check: lint typecheck test ## Everything CI would run

rag-stress: ## Run the real-model RAG stress battery, write a timestamped report to data/reports/
	uv run --group nlp python scripts/rag_stress.py

# --- scheduling (darwin) -----------------------------------------------------
install-timer: ## Schedule bin/ingest.sh via launchd, every 30min (darwin only, explicit opt-in)
	bin/launchd/install.sh

uninstall-timer: ## Remove the launchd schedule installed by install-timer
	bin/launchd/uninstall.sh
