# geneva-examples — common dev tasks. Everything runs through `uv`.
# Run `make` or `make help` to see available targets.

.DEFAULT_GOAL := help
.PHONY: help install lock lint lint-fix format format-check typecheck \
        test check audit precommit hooks udf-studio clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the dev environment and install the git pre-commit hook
	uv sync --group dev
	uv run pre-commit install

lock: ## Refresh uv.lock (respects the == cluster pins)
	uv lock

lint: ## Lint with ruff
	uv run ruff check

lint-fix: ## Lint and auto-fix with ruff
	uv run ruff check --fix

format: ## Format code with ruff
	uv run ruff format

format-check: ## Check formatting without modifying files
	uv run ruff format --check

typecheck: ## Type-check the package with ty (preview; informational)
	uv run ty check geneva_examples

test: ## Run the test suite with coverage (gate enforced via pyproject)
	uv run pytest

check: lint format-check test ## Run the full CI gate locally (lint + format + tests)

audit: ## Scan locked dependencies for known CVEs (mirrors the CI audit job)
	uv export --frozen --no-emit-project --no-hashes \
		--format requirements-txt -o requirements.txt
	uvx pip-audit -r requirements.txt

precommit: ## Run all pre-commit hooks across the repo
	uv run pre-commit run --all-files

hooks: ## Install the git pre-commit hook
	uv run pre-commit install

udf-studio: ## Launch UDF Studio (Gradio)
	uv run udf-studio

clean: ## Remove caches and coverage artifacts
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov requirements.txt
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
