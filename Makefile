.PHONY: serve dev test lint lint-bash lint-py format check install-deps setup help

SHELL := /bin/bash

# Prefer tools on PATH (virtualenv, pipx); fall back to user-base.
RUFF := $(or $(shell command -v ruff 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/ruff)
PYTEST := $(or $(shell command -v pytest 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/pytest)

# ── Main targets ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

serve: ## Start the dashboard (http://localhost:8111)
	sova dashboard

dev: ## Start the dashboard with auto-reload
	sova dashboard --reload

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

test-py: ## Run pytest suite (excludes runtime/stress/chaos)
	$(PYTEST) tests/ -v -m "not runtime and not stress and not chaos"

test-runtime: ## Run runtime, stress, and chaos tests (manual)
	$(PYTEST) tests/ -v -m "runtime or stress or chaos" --timeout=120

test-all: ## Run ALL tests including runtime/stress/chaos
	$(PYTEST) tests/ -v --timeout=120

# ── Linting ───────────────────────────────────────────────────

lint: lint-bash lint-py ## Run all linters

lint-bash: ## ShellCheck on invariant scripts
	shellcheck invariants/*.sh

lint-py: ## Ruff lint + format check
	$(RUFF) check sova/ tests/
	$(RUFF) format --check sova/ tests/

# ── Formatting ────────────────────────────────────────────────

format: ## Auto-format Python code
	$(RUFF) format sova/ tests/
	$(RUFF) check --fix sova/ tests/

# ── Setup ─────────────────────────────────────────────────────

setup: install-deps ## Set up development environment (deps + hooks)
	git config core.hooksPath .githooks
	@echo "Development environment ready. Run 'make check' to verify."

install-deps: ## Install/update all dependencies
	pip install -q -e ".[dev]"
