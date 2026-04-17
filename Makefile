.PHONY: serve test lint lint-bash lint-py format check install-deps help

SHELL := /bin/bash
VENV := dashboard/.venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff
PYTEST := $(VENV)/bin/pytest

# ── Main targets ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

serve: $(VENV) ## Start the dashboard (http://localhost:8111)
	./pak dashboard

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

test-py: $(VENV) ## Run dashboard pytest suite
	cd dashboard && .venv/bin/pytest tests/ -v

# ── Linting ───────────────────────────────────────────────────

lint: lint-bash lint-py ## Run all linters

lint-bash: ## ShellCheck on all bash scripts
	shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh

lint-py: $(VENV) ## Ruff lint + format check on dashboard
	$(RUFF) check dashboard/app/
	$(RUFF) format --check dashboard/app/

# ── Formatting ────────────────────────────────────────────────

format: $(VENV) ## Auto-format dashboard Python code
	$(RUFF) format dashboard/app/
	$(RUFF) check --fix dashboard/app/

# ── Setup ─────────────────────────────────────────────────────

install-deps: $(VENV) ## Install/update all dependencies
	$(PIP) install -q -r dashboard/requirements.txt

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install -q -r dashboard/requirements.txt
