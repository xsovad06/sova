.PHONY: serve dev test lint lint-bash lint-py format check install-deps help

SHELL := /bin/bash

# GNU Make 3.81 (macOS default) skips the shell for simple commands,
# bypassing exported PATH. Use full paths for pip-installed tools.
PYTHON_USER_BIN := $(shell python3 -m site --user-base)/bin
RUFF := $(PYTHON_USER_BIN)/ruff
PYTEST := $(PYTHON_USER_BIN)/pytest

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

test-py: ## Run pytest suite
	$(PYTEST) tests/ -v

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

install-deps: ## Install/update all dependencies
	pip install -q -e ".[dev]"
