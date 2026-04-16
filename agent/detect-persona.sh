#!/usr/bin/env bash
# Persona Auto-Detection
# Scans project files and returns the best-matching persona name.
#
# Usage:
#   persona=$(./detect-persona.sh /path/to/project)
#
# Returns persona name (e.g., "django", "react") or empty string if no match.

PROJECT_DIR="${1:-.}"

detect() {
  cd "$PROJECT_DIR" 2>/dev/null || exit 1

  # Odoo: __manifest__.py with Odoo-style keys
  if find . -maxdepth 3 -name "__manifest__.py" -exec grep -l "'name'" {} \; 2>/dev/null | head -1 | grep -q .; then
    echo "odoo"
    return
  fi

  # Django: manage.py + django in requirements
  if [[ -f "manage.py" ]]; then
    if grep -rq "django" requirements*.txt pyproject.toml setup.py setup.cfg 2>/dev/null; then
      echo "django"
      return
    fi
  fi

  # FastAPI: fastapi in requirements
  if grep -rq "fastapi" requirements*.txt pyproject.toml setup.py setup.cfg 2>/dev/null; then
    echo "fastapi"
    return
  fi

  # Go: go.mod
  if [[ -f "go.mod" ]]; then
    echo "go-service"
    return
  fi

  # Rust: Cargo.toml
  if [[ -f "Cargo.toml" ]]; then
    echo "rust"
    return
  fi

  # React: package.json with react dependency
  if [[ -f "package.json" ]] && grep -q '"react"' package.json 2>/dev/null; then
    echo "react"
    return
  fi

  # Frontend (generic): package.json without react
  if [[ -f "package.json" ]]; then
    echo "frontend"
    return
  fi

  # RBAC: if we find RBAC-related files
  if find . -maxdepth 3 \( -name "rbac*" -o -name "permissions*" -o -name "roles*" \) 2>/dev/null | head -1 | grep -q .; then
    echo "rbac"
    return
  fi

  # No match
  echo ""
}

detect
