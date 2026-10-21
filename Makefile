.PHONY: serve dev test lint lint-bash lint-py format check install-deps setup help css css-watch marketplace commands-render skills-render

SHELL := /bin/bash

# Prefer tools on PATH (virtualenv, pipx); fall back to user-base.
RUFF := $(or $(shell command -v ruff 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/ruff)
PYTEST := $(or $(shell command -v pytest 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/pytest)

# Worker count for test-py's pytest-xdist run. Override with `make test-py PYTEST_WORKERS=4`,
# or set the pytest-xdist-native PYTEST_XDIST_AUTO_NUM_WORKERS env var. Defaults to 2 to keep
# `make check` under the 600s CI timeout on resource-constrained machines (issue #1156).
PYTEST_WORKERS ?= $(or $(PYTEST_XDIST_AUTO_NUM_WORKERS),2)

# ── Main targets ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

serve: ## Start the dashboard (http://localhost:8111)
	sova dashboard

dev: ## Start the dashboard with auto-reload
	sova dashboard --reload

test: test-bash test-py ## Run all tests

check: lint test ## Run linter + tests (close to CI, not identical: see docs/testing-guidelines.md)

# ── Testing ───────────────────────────────────────────────────

test-bash: lint-bash ## Validate bash scripts (shellcheck + --help)
	@echo ""
	@echo "-- bash scripts --help --"
	@for f in invariants/*.sh .github/scripts/*.sh; do \
		printf "  %-38s" "$$f"; \
		bash "$$f" --help >/dev/null 2>&1 && echo "ok" || echo "FAIL"; \
	done

test-py: ## Run pytest suite (excludes runtime/stress/chaos/evaluation)
	$(PYTEST) tests/ -v -m "not runtime and not stress and not chaos and not evaluation" -n $(PYTEST_WORKERS)

test-ci: ## Run the pytest suite exactly as ci.yml invokes it (no marker deselection; CI's own matrix legs share this target)
	$(PYTEST) tests/ -n auto -q --cov=sova --cov-report=xml --cov-report=term:skip-covered --durations=25 --timeout=60

test-runtime: ## Run runtime, stress, and chaos tests (manual)
	$(PYTEST) tests/ -v -m "runtime or stress or chaos" --timeout=120

test-all: ## Run ALL tests including runtime/stress/chaos
	$(PYTEST) tests/ -v --timeout=120

test-codex-smoke: ## Run the opt-in Codex CLI smoke test (requires: codex CLI installed and logged in)
	SOVA_CODEX_SMOKE=1 $(PYTEST) tests/runtime/test_codex_smoke.py -v -m codex_smoke

test-evaluation: ## Run the model-evaluation harness against live providers (requires: SOVA_EVAL_MODEL_ID and provider credentials)
	SOVA_EVAL_LIVE=1 $(PYTEST) tests/evaluation/ -v -s -m evaluation

# ── Linting ───────────────────────────────────────────────────

lint: lint-bash lint-py ## Run all linters

lint-bash: ## ShellCheck on bash scripts (invariants + CI gate scripts)
	shellcheck invariants/*.sh .github/scripts/*.sh

lint-py: ## Ruff lint + format check
	$(RUFF) check sova/ tests/
	$(RUFF) format --check sova/ tests/

# ── Formatting ────────────────────────────────────────────────

format: ## Auto-format Python code
	$(RUFF) format sova/ tests/
	$(RUFF) check --fix sova/ tests/

# ── CSS ───────────────────────────────────────────────────────

css: ## Rebuild Tailwind CSS (run after changing template classes)
	npx tailwindcss@3 -o sova/dashboard/static/tailwind.min.css --minify

css-watch: ## Watch and rebuild Tailwind CSS on template changes
	npx tailwindcss@3 -o sova/dashboard/static/tailwind.min.css --minify --watch

# ── Marketplace ───────────────────────────────────────────────

marketplace: ## Regenerate plugins/sova/ from sova/commands/data/commands/ (run after editing a published command)
	python3 -m sova.commands.marketplace_export

commands-render: ## Regenerate .claude/commands/ from sova/commands/data/commands/ (run after editing a managed command)
	python3 -m sova.commands.self_render

skills-render: ## Regenerate .agents/skills/ from sova/commands/data/commands/ and sova/commands/data/skills/ (run after editing a managed command or skill)
	python3 -m sova.commands.skill_render

# ── Setup ─────────────────────────────────────────────────────

setup: install-deps ## Set up development environment (deps + hooks)
	git config core.hooksPath .githooks
	@echo "Development environment ready. Run 'make check' to verify."

install-deps: ## Install/update all dependencies
	pip install -q -e ".[dev]"
