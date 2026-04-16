#!/usr/bin/env bash
# Task Source Adapter Interface
# Each adapter must implement these functions:
#
#   adapter_list_tasks    — List available tasks (filtered by milestone/sprint/priority)
#   adapter_get_task      — Get task details (title, description, labels, assignee)
#   adapter_set_status    — Update task status (in-progress, done)
#   adapter_link_pr       — Associate a PR with a task
#
# Adapters are loaded by sourcing the appropriate file based on TASK_SOURCE config.
# The adapter file must define all four functions above.
#
# Usage in orchestrator:
#   TASK_SOURCE="${TASK_SOURCE:-github}"
#   source "$ADAPTER_DIR/${TASK_SOURCE}.sh"
#   adapter_list_tasks
#   adapter_get_task "$ISSUE_NUMBER"

# Validate that an adapter implements the required interface
adapter_validate() {
  local adapter_name="$1"
  local missing=()

  for fn in adapter_list_tasks adapter_get_task adapter_set_status adapter_link_pr; do
    if ! declare -f "$fn" > /dev/null 2>&1; then
      missing+=("$fn")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Error: Adapter '$adapter_name' is missing required functions: ${missing[*]}"
    return 1
  fi
}

# Load a task source adapter
adapter_load() {
  local adapter_name="${1:-github}"
  local adapter_dir
  adapter_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local adapter_file="$adapter_dir/${adapter_name}.sh"

  if [[ ! -f "$adapter_file" ]]; then
    echo "Error: Unknown task source '$adapter_name'. Available adapters:"
    for f in "$adapter_dir"/*.sh; do
      [[ "$(basename "$f")" == "interface.sh" ]] && continue
      echo "  - $(basename "$f" .sh)"
    done
    return 1
  fi

  # shellcheck source=/dev/null
  source "$adapter_file"
  adapter_validate "$adapter_name"
}
