#!/usr/bin/env bash
# Manual Task Source Adapter
# For projects without an issue tracker. Tasks are provided directly via CLI.
#
# Usage:
#   pak run "Implement user authentication"
#   pak run --title "Add search" --description "Full-text search for articles"

adapter_list_tasks() {
  echo "[]"
  echo "Manual mode: provide task directly via CLI argument."
}

adapter_get_task() {
  local task_input="$1"
  # If the input is a JSON string, pass through; otherwise wrap it
  if echo "$task_input" | jq . > /dev/null 2>&1; then
    echo "$task_input"
  else
    jq -n --arg title "$task_input" '{number: "manual", title: $title, body: "", labels: [], state: "open"}'
  fi
}

adapter_set_status() {
  # No-op for manual mode
  :
}

adapter_link_pr() {
  # No-op for manual mode
  :
}
