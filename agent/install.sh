#!/usr/bin/env bash
set -euo pipefail

# Project Automation Kit — Per-project installer
# Run this from your project repository root to install the agent.
#
# Usage:
#   pak install /path/to/project                   # Full install (agent + dashboard)
#   pak install /path/to/project --no-dashboard    # Agent only
#   pak install /path/to/project --update          # Quick sync (script + personas only)
#
# Or directly:
#   /path/to/pak/agent/install.sh                  # Full install
#   /path/to/pak/agent/install.sh --no-dashboard   # Agent only
#   /path/to/pak/agent/install.sh --update         # Quick sync

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATES_DIR="$PAK_ROOT/templates"
PERSONAS_DIR="$PAK_ROOT/personas"
DASHBOARD_DIR="$PAK_ROOT/dashboard"

# Target directories (relative to current working directory = your repo root)
SCRIPTS_DIR=".claude/scripts"
MEMORY_DIR=".claude/agent-memory"
ASSETS_DIR=".claude/assets"
COMMANDS_DIR=".claude/scripts/commands"

# Parse flags
INSTALL_DASHBOARD=true
UPDATE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --no-dashboard) INSTALL_DASHBOARD=false ;;
    --update) UPDATE_ONLY=true ;;
  esac
done

# ── Migration: gwym-agent -> pak-agent ────────────────────────────────────────
# Automatically rename old files if found, preserving user config.
migrate_legacy_files() {
  if [[ -f "$SCRIPTS_DIR/gwym-agent.sh" ]]; then
    rm "$SCRIPTS_DIR/gwym-agent.sh"
    echo "Migrated: removed gwym-agent.sh"
  fi
  if [[ -f "$SCRIPTS_DIR/gwym-agent.conf.default" ]]; then
    rm "$SCRIPTS_DIR/gwym-agent.conf.default"
    echo "Migrated: removed gwym-agent.conf.default"
  fi
  if [[ -f "$SCRIPTS_DIR/gwym-agent.conf" && ! -f "$SCRIPTS_DIR/pak-agent.conf" ]]; then
    mv "$SCRIPTS_DIR/gwym-agent.conf" "$SCRIPTS_DIR/pak-agent.conf"
    echo "Migrated: renamed gwym-agent.conf -> pak-agent.conf"
  elif [[ -f "$SCRIPTS_DIR/gwym-agent.conf" ]]; then
    rm "$SCRIPTS_DIR/gwym-agent.conf"
    echo "Migrated: removed gwym-agent.conf (pak-agent.conf already exists)"
  fi
}

migrate_legacy_files

# Quick update mode: sync agent script, personas, and commands
if $UPDATE_ONLY; then
  if [[ ! -f "$SCRIPTS_DIR/pak-agent.sh" ]]; then
    echo "Error: No existing install found. Run install.sh without --update first."
    exit 1
  fi
  cp "$SCRIPT_DIR/orchestrator.sh" "$SCRIPTS_DIR/pak-agent.sh"
  chmod +x "$SCRIPTS_DIR/pak-agent.sh"
  cp "$SCRIPT_DIR/pak-agent.conf.default" "$SCRIPTS_DIR/pak-agent.conf.default"
  if [[ -d "$PERSONAS_DIR" ]]; then
    cp "$PERSONAS_DIR"/*.md "$SCRIPTS_DIR/personas/" 2>/dev/null || true
    cp "$PERSONAS_DIR"/*.mcp.json "$SCRIPTS_DIR/personas/" 2>/dev/null || true
    # Remove personas that no longer exist in source
    for f in "$SCRIPTS_DIR/personas/"*.md; do
      [[ -f "$f" ]] || continue
      base=$(basename "$f")
      if [[ ! -f "$PERSONAS_DIR/$base" ]]; then
        rm -f "$f" "${f%.md}.mcp.json"
      fi
    done
  fi
  if [[ -d "$PAK_ROOT/commands" ]]; then
    mkdir -p "$COMMANDS_DIR"
    cp "$PAK_ROOT/commands/"*.md "$COMMANDS_DIR/" 2>/dev/null || true
    # Remove commands that no longer exist in source
    for f in "$COMMANDS_DIR/"*.md; do
      [[ -f "$f" ]] || continue
      base=$(basename "$f")
      if [[ ! -f "$PAK_ROOT/commands/$base" ]]; then
        rm -f "$f"
      fi
    done
  fi
  echo "Agent updated in $(pwd)"
  exit 0
fi

echo "Project Automation Kit — Setup"
echo ""
echo "Installing into: $(pwd)"
echo ""

# Create directories
mkdir -p "$SCRIPTS_DIR/personas" "$MEMORY_DIR" "$ASSETS_DIR" "$COMMANDS_DIR"

# Copy the agent script
if [[ -f "$SCRIPTS_DIR/pak-agent.sh" ]]; then
  echo "Updating: $SCRIPTS_DIR/pak-agent.sh"
else
  echo "Installing: $SCRIPTS_DIR/pak-agent.sh"
fi
cp "$SCRIPT_DIR/orchestrator.sh" "$SCRIPTS_DIR/pak-agent.sh"
chmod +x "$SCRIPTS_DIR/pak-agent.sh"

# Copy persona files (always update to latest, remove stale ones)
if [[ -d "$PERSONAS_DIR" ]]; then
  cp "$PERSONAS_DIR"/*.md "$SCRIPTS_DIR/personas/" 2>/dev/null || true
  cp "$PERSONAS_DIR"/*.mcp.json "$SCRIPTS_DIR/personas/" 2>/dev/null || true
  for f in "$SCRIPTS_DIR/personas/"*.md; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f")
    if [[ ! -f "$PERSONAS_DIR/$base" ]]; then
      rm -f "$f" "${f%.md}.mcp.json"
    fi
  done
  echo "Copied personas: $(find "$SCRIPTS_DIR/personas/" -maxdepth 1 -type f -exec basename {} \; | tr '\n' ' ')"
fi

# Copy agent-specific commands (always update to latest, remove stale ones)
if [[ -d "$PAK_ROOT/commands" ]]; then
  cp "$PAK_ROOT/commands/"*.md "$COMMANDS_DIR/" 2>/dev/null || true
  for f in "$COMMANDS_DIR/"*.md; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f")
    if [[ ! -f "$PAK_ROOT/commands/$base" ]]; then
      rm -f "$f"
    fi
  done
  echo "Copied agent commands: $(find "$COMMANDS_DIR/" -maxdepth 1 -name '*.md' 2>/dev/null | sed 's|.*/||' | tr '\n' ' ')"
fi

# Copy config template (don't overwrite existing config)
if [[ ! -f "$SCRIPTS_DIR/pak-agent.conf" ]]; then
  cp "$SCRIPT_DIR/pak-agent.conf.default" "$SCRIPTS_DIR/pak-agent.conf"
  echo "Created config: $SCRIPTS_DIR/pak-agent.conf"
  echo "  Edit this file to set your project-specific values (GITHUB_REPO, TEST_CMD, etc.)"
else
  echo "Config already exists: $SCRIPTS_DIR/pak-agent.conf (not overwritten)"
fi

# Copy config default (for reference)
cp "$SCRIPT_DIR/pak-agent.conf.default" "$SCRIPTS_DIR/pak-agent.conf.default"

# Copy agent icon if available
if [[ -f "$PAK_ROOT/assets/agent-icon.png" ]]; then
  cp "$PAK_ROOT/assets/agent-icon.png" "$ASSETS_DIR/" 2>/dev/null || true
fi

# Initialize memory files from templates (don't overwrite existing)
for template in "$TEMPLATES_DIR"/agent-memory/*.md; do
  [[ -f "$template" ]] || continue
  filename=$(basename "$template")
  if [[ ! -f "$MEMORY_DIR/$filename" ]]; then
    cp "$template" "$MEMORY_DIR/$filename"
    echo "Created memory file: $MEMORY_DIR/$filename"
  else
    echo "Memory file exists: $MEMORY_DIR/$filename (not overwritten)"
  fi
done

# --- Dashboard installation ---
if $INSTALL_DASHBOARD && [[ -d "$DASHBOARD_DIR" ]]; then
  echo ""
  echo "Installing dashboard..."

  # Ensure dashboard dependencies are installed (one-time)
  if [[ ! -d "$DASHBOARD_DIR/.venv" ]]; then
    echo "  Installing Python dependencies (one-time)..."
    python3 -m venv "$DASHBOARD_DIR/.venv"
    "$DASHBOARD_DIR/.venv/bin/pip" install -q -r "$DASHBOARD_DIR/requirements.txt"
    echo "  Dependencies installed."
  else
    echo "  Dashboard dependencies already installed."
  fi

  # Create the launcher script
  cat > "$SCRIPTS_DIR/agent-dashboard.sh" << LAUNCHER
#!/usr/bin/env bash
set -euo pipefail

# Agent Dashboard launcher — auto-generated by pak/install.sh
# Source: $DASHBOARD_DIR

DASHBOARD_DIR="$DASHBOARD_DIR"
REPO_ROOT="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="\$REPO_ROOT/.claude"

# Verify dashboard source exists
if [[ ! -d "\$DASHBOARD_DIR" ]]; then
  echo "Error: Dashboard source not found at \$DASHBOARD_DIR"
  echo "  Re-run the install script or update the path."
  exit 1
fi

# Ensure deps are up to date
if [[ ! -d "\$DASHBOARD_DIR/.venv" ]]; then
  echo "Installing dependencies..."
  python3 -m venv "\$DASHBOARD_DIR/.venv"
  "\$DASHBOARD_DIR/.venv/bin/pip" install -q -r "\$DASHBOARD_DIR/requirements.txt"
fi

PORT="\${1:-8111}"

echo "PAK Agent Dashboard"
echo "  Data: \$DATA_DIR"
echo "  URL:  http://localhost:\$PORT"
echo ""

cd "\$DASHBOARD_DIR"
AGENT_DATA_DIR="\$DATA_DIR" .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "\$PORT"
LAUNCHER

  chmod +x "$SCRIPTS_DIR/agent-dashboard.sh"
  echo "Installed: $SCRIPTS_DIR/agent-dashboard.sh"
else
  if ! $INSTALL_DASHBOARD; then
    echo ""
    echo "Dashboard skipped (--no-dashboard)"
  fi
fi

# Ensure agent directories are gitignored
GITIGNORE=".gitignore"
declare -a IGNORE_ENTRIES=(
  ".claude/worktrees/"
  ".claude/agent-memory/"
  ".claude/scripts/pak-agent.conf"
  ".claude/assets/"
)

for entry in "${IGNORE_ENTRIES[@]}"; do
  if ! grep -qF "$entry" "$GITIGNORE" 2>/dev/null; then
    echo "$entry" >> "$GITIGNORE"
    echo "Added to .gitignore: $entry"
  fi
done

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit $SCRIPTS_DIR/pak-agent.conf with your project settings"
echo "     Or run: pak setup .  (interactive setup wizard)"
echo "  2. Run: pak run 42  (to work on issue #42)"
if $INSTALL_DASHBOARD && [[ -d "$DASHBOARD_DIR" ]]; then
  echo "  3. Dashboard: pak dashboard  (http://localhost:8111)"
fi
echo ""
echo "Required tools: claude, gh, git, jq"
echo "Optional: terminal-notifier (brew install terminal-notifier), sqlite3, VSCode CLI"
