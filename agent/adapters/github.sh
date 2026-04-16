#!/usr/bin/env bash
# GitHub Task Source Adapter
# Implements the task source interface for GitHub Issues.
#
# Required config:
#   GITHUB_REPO    — owner/name (e.g., "user/repo")
#   ISSUE_MILESTONE — optional milestone filter
#   ISSUE_LABELS    — optional comma-separated label filter

adapter_list_tasks() {
  # List open issues, optionally filtered by milestone and labels
  local args=("issue" "list" "--repo" "$GITHUB_REPO" "--state" "open" "--json" "number,title,labels,assignees,milestone" "--limit" "50")

  if [[ -n "${ISSUE_MILESTONE:-}" ]]; then
    args+=("--milestone" "$ISSUE_MILESTONE")
  fi

  if [[ -n "${ISSUE_LABELS:-}" ]]; then
    args+=("--label" "$ISSUE_LABELS")
  fi

  gh "${args[@]}" 2>/dev/null || echo "[]"
}

adapter_get_task() {
  local issue_number="$1"
  gh issue view "$issue_number" --repo "$GITHUB_REPO" --json "number,title,body,labels,assignees,milestone,state" 2>/dev/null
}

adapter_set_status() {
  local issue_number="$1"
  local status="$2"

  case "$status" in
    in-progress)
      # GitHub doesn't have a native "in progress" state.
      # Add a label if desired; the orchestrator handles this via project boards.
      gh issue edit "$issue_number" --repo "$GITHUB_REPO" --add-label "in-progress" 2>/dev/null || true
      ;;
    done)
      gh issue close "$issue_number" --repo "$GITHUB_REPO" 2>/dev/null || true
      ;;
    *)
      echo "Warning: Unknown status '$status' for GitHub adapter"
      ;;
  esac
}

adapter_link_pr() {
  local issue_number="$1"
  local pr_url="$2"
  # GitHub auto-links PRs via "Closes #N" in PR body (handled by orchestrator).
  # This is a no-op for GitHub since the link is established at PR creation time.
  :
}
