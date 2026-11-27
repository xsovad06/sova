.PHONY: serve dev test lint lint-bash lint-py lint-js lint-imports format check install-deps sync setup help css css-watch marketplace commands-render skills-render test-ci next-version changelog

SHELL := /bin/bash

# Prefer tools on PATH (virtualenv, pipx); fall back to user-base.
RUFF := $(or $(shell command -v ruff 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/ruff)
LINT_IMPORTS := $(or $(shell command -v lint-imports 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/lint-imports)
PYTEST := $(or $(shell command -v pytest 2>/dev/null),$(shell python3 -m site --user-base 2>/dev/null)/bin/pytest)

# Worker count for test-py's pytest-xdist run. Override with `make test-py PYTEST_WORKERS=4`,
# or set the pytest-xdist-native PYTEST_XDIST_AUTO_NUM_WORKERS env var. Defaults to 2 so
# concurrent test-py runs on one machine do not each claim every core (issue #1156). It does
# not apply to `make check`, which goes through test-ci's `-n auto` to stay identical to CI
# (issue #1209). `-n auto` reads PYTEST_XDIST_AUTO_NUM_WORKERS too, which SOVA sets for every
# check it runs and agent it starts (`testing.check_workers`, issue #1191).
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

check: lint test-bash test-ci ## Run linter + tests (uses the same pytest invocation as CI; see docs/testing-guidelines.md)

# ── Testing ───────────────────────────────────────────────────

test-bash: lint-bash ## Validate bash scripts (shellcheck + --help)
	@echo ""
	@echo "-- bash scripts --help --"
	@for f in invariants/*.sh .github/scripts/*.sh; do \
		printf "  %-38s" "$$f"; \
		bash "$$f" --help >/dev/null 2>&1 && echo "ok" || echo "FAIL"; \
	done

test-py: ## Run pytest suite (excludes runtime/evaluation)
	$(PYTEST) tests/ -v -m "not runtime and not evaluation" -n $(PYTEST_WORKERS)

test-ci: ## Run the exact pytest invocation CI's matrix legs and `make check` all use (coverage, sys.monitoring core, full marker set)
	COVERAGE_CORE=sysmon $(PYTEST) tests/ -n auto -q --cov=sova --cov-report=xml --cov-report=term:skip-covered --durations=25 --timeout=60

test-runtime: ## Run runtime tests (manual)
	$(PYTEST) tests/ -v -m "runtime" --timeout=120

test-all: ## Run ALL tests including runtime
	$(PYTEST) tests/ -v --timeout=120

test-codex-smoke: ## Run the opt-in Codex CLI smoke test (requires: codex CLI installed and logged in)
	SOVA_CODEX_SMOKE=1 $(PYTEST) tests/runtime/test_codex_smoke.py -v -m codex_smoke

test-evaluation: ## Run the model-evaluation harness against live providers (requires: SOVA_EVAL_MODEL_ID and provider credentials)
	SOVA_EVAL_LIVE=1 $(PYTEST) tests/evaluation/ -v -s -m evaluation

# ── Linting ───────────────────────────────────────────────────

lint: lint-bash lint-py lint-js lint-imports lint-tree ## Run all linters

lint-bash: ## ShellCheck on bash scripts (invariants + CI gate scripts)
	shellcheck invariants/*.sh .github/scripts/*.sh

lint-py: ## Ruff lint + format check
	$(RUFF) check sova/ tests/
	$(RUFF) format --check sova/ tests/

lint-imports: ## Import layering contract (import-linter, pyproject.toml); CI runs the same command
	$(LINT_IMPORTS)

lint-tree: ## Whole-tree invariant scans (every tracked file, not just a diff); CI's invariants job runs the same two
	bash invariants/no-double-dash.sh --all .
	bash invariants/file-encoding.sh --all .

lint-js: ## Syntax-check dashboard JS (static/*.js as scripts, static/js/**/*.js as ES modules, inline template <script> blocks with Jinja stripped); needs node
	@command -v node >/dev/null || { echo "lint-js: node is required"; exit 1; }
	@for f in $$(find sova/dashboard/static -maxdepth 1 -name '*.js' ! -name '*.min.js' | sort); do \
		node --check "$$f" || { echo "lint-js: syntax error in $$f"; exit 1; }; \
	done
	@for f in $$(find sova/dashboard/static/js -name '*.js' | sort); do \
		node --input-type=module --check < "$$f" || { echo "lint-js: syntax error in $$f"; exit 1; }; \
	done
	@python3 .github/scripts/lint_inline_js.py sova/dashboard/templates

# ── Formatting ────────────────────────────────────────────────

format: ## Auto-format Python code
	$(RUFF) format sova/ tests/
	$(RUFF) check --fix sova/ tests/

# ── CSS ───────────────────────────────────────────────────────

# An exact version, so the checked-in bundle is reproducible: tests/test_tailwind_bundle.py rebuilds it
# with this same package and compares, and a floating `@3` would fail that test on every new 3.x release.
TAILWIND := tailwindcss@3.4.19

css: ## Rebuild Tailwind CSS (run after changing template classes)
	npx --yes $(TAILWIND) -o sova/dashboard/static/tailwind.min.css --minify

css-watch: ## Watch and rebuild Tailwind CSS on template changes
	npx --yes $(TAILWIND) -o sova/dashboard/static/tailwind.min.css --minify --watch

# ── Marketplace ───────────────────────────────────────────────

marketplace: ## Regenerate plugins/sova/ from sova/commands/data/commands/ (run after editing a published command)
	python3 -m sova.commands.marketplace_export

commands-render: ## Regenerate .claude/commands/ from sova/commands/data/commands/ (run after editing a managed command)
	python3 -m sova.commands.self_render

skills-render: ## Regenerate .agents/skills/ from sova/commands/data/commands/ and sova/commands/data/skills/ (run after editing a managed command or skill)
	python3 -m sova.commands.skill_render

# ── Release ───────────────────────────────────────────────────

next-version: ## Print the next release version the conventional commits since the last vX.Y.Z tag call for
	@python3 scripts/changelog.py next-version

changelog: ## Regenerate CHANGELOG.md from conventional commits; RELEASE=X.Y.Z titles the unreleased commits as that release
	python3 scripts/changelog.py changelog $(if $(RELEASE),--release $(RELEASE))

# ── Setup ─────────────────────────────────────────────────────

setup: install-deps ## Set up development environment (deps + hooks)
	git config core.hooksPath .githooks
	@echo "Development environment ready. Run 'make check' to verify."

install-deps: ## Install/update all dependencies
	pip install -q -e ".[dev]"

# Extra pip arguments for `make sync`, e.g. SYNC_PIP_ARGS="--user --break-system-packages" outside a venv.
SYNC_PIP_ARGS ?=

sync: ## Install exactly the versions in uv.lock, as CI does (`uv export --locked --extra dev`); needs uv
	@command -v uv >/dev/null || { echo "sync: uv is required (https://docs.astral.sh/uv/)"; exit 1; }
	@constraints=$$(mktemp) && \
		uv export --locked --extra dev --no-emit-project --no-hashes --format requirements.txt -o "$$constraints" && \
		python3 -m pip install $(SYNC_PIP_ARGS) -c "$$constraints" -e ".[dev]"; \
		status=$$?; rm -f "$$constraints"; exit $$status
