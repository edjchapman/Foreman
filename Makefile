.PHONY: help up down build logs migrate makemigrations test lint fmt typecheck audit ci preflight shell \
        worker beat relay deploy tf-check \
        check check-links check-anchors stack-check \
        check-commit-msg check-stale-branches sweep-branches lint-md

# Default goal prints help.
.DEFAULT_GOAL := help

help: ## Print available targets
	@grep -E '^[a-z][a-zA-Z0-9_-]*:.*##' $(MAKEFILE_LIST) \
		| awk 'BEGIN { FS = ":.*##" } { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }'

# === Local stack (Docker) ===

up: ## Start the local stack (Django + Postgres) with live reload
	docker compose up --build

down: ## Stop the stack and remove volumes
	docker compose down -v

build: ## Build the web image
	docker compose build

logs: ## Tail stack logs
	docker compose logs -f

# === App (host, via uv) ===

migrate: ## Apply migrations
	uv run python manage.py migrate

makemigrations: ## Generate migrations
	uv run python manage.py makemigrations

test: ## Run the test suite
	uv run pytest

lint: ## Lint + format-check (no changes)
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint + format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Static type-check (mypy strict + django/DRF stubs; no DB needed)
	uv run mypy

ci: lint typecheck ## What CI runs: lint + types + coverage-gated tests (fails under 90%)
	uv run pytest --cov --cov-report=term-missing --cov-report=xml --cov-fail-under=90

audit: ## Audit dependencies for known advisories (CVEs); fails on any finding
	uv run --group audit pip-audit --strict

preflight: ci audit check ## Full pre-PR gate: ci (lint + types + tests) + audit + docs
	@echo "preflight: all gates green — safe to push."

shell: ## Django shell
	uv run python manage.py shell

# === Celery (M2: async worker + outbox relay) ===

worker: ## Run a Celery worker (host)
	uv run celery -A config worker -l info

beat: ## Run Celery beat — schedules the outbox relay
	uv run celery -A config beat -l info

relay: ## Dispatch the outbox once (no beat) — claims + publishes PENDING events
	uv run python manage.py shell -c "from jobs.tasks import dispatch_outbox; print(dispatch_outbox())"

# === Deploy (M5: Railway — see docs/deploy.md) ===

deploy: ## Deploy VERSION=<x.y.z> to Railway (pins image tags; web gates worker/beat)
	@./scripts/railway-deploy.sh "$(VERSION)"

tf-check: ## Terraform gate: fmt + validate the platform module (needs terraform CLI)
	terraform -chdir=deploy/terraform fmt -check -recursive
	terraform -chdir=deploy/terraform init -backend=false -input=false >/dev/null
	terraform -chdir=deploy/terraform validate

# === Validation (docs/hygiene gate — run by check.yml, scheduled-check.yml, pre-commit) ===

check: check-links check-anchors stack-check ## Run the docs/hygiene gate (no toolchain needed)
	@echo "All checks passed."

check-links: ## Verify internal markdown links resolve
	@./scripts/check-links.sh

check-anchors: ## Verify markdown anchor fragments resolve to heading slugs
	@python3 scripts/check_anchors.py

stack-check: ## Stack lint/test gate lives in 'make ci' (no-op here so 'make check' stays toolchain-free)
	@echo "stack-check: no-op — foreman's lint/test gate is 'make ci' (needs uv + Postgres)."

# === On-demand tooling (not part of 'make check') ===

check-commit-msg: ## Validate a commit subject (FILE=<path> or pipe via --stdin)
	@./scripts/check-commit-msg.sh $${FILE:---stdin}

check-stale-branches: ## Surface stale local branches (requires gh + jq)
	@./scripts/check-stale-branches.sh

sweep-branches: ## Delete bucket-A stale branches; dry-run unless APPLY=1
	@./scripts/sweep-stale-branches.sh

lint-md: ## Run markdownlint locally against **/*.md (requires npx)
	@npx --yes markdownlint-cli2 "**/*.md"
