.PHONY: serve test lint lint-bash lint-py format check install-deps help

SHELL := /bin/bash

# ── Main targets ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

serve: ## Start the dashboard (http://localhost:8111)
	sova dashboard

test: test-bash test-py ## Run all tests

check: lint test ## Run linter + tests (CI-equivalent)

# ── Testing ───────────────────────────────────────────────────

test-bash: lint-bash ## Validate bash scripts (shellcheck + --help)
	@echo ""
	@echo "-- invariant scripts --help --"
	@for f in invariants/*.sh; do \
		printf "  %-30s" "$$f"; \
		bash "$$f" --help >/dev/null 2>&1 && echo "ok" || echo "FAIL"; \
	done

test-py: ## Run pytest suite
	pytest tests/ -v

# ── Linting ───────────────────────────────────────────────────

lint: lint-bash lint-py ## Run all linters

lint-bash: ## ShellCheck on invariant scripts
	shellcheck invariants/*.sh

lint-py: ## Ruff lint + format check
	ruff check sova/ tests/
	ruff format --check sova/ tests/

# ── Formatting ────────────────────────────────────────────────

format: ## Auto-format Python code
	ruff format sova/ tests/
	ruff check --fix sova/ tests/

# ── Setup ─────────────────────────────────────────────────────

install-deps: ## Install/update all dependencies
	pip install -q -e ".[dev]"
