#!/usr/bin/env bash
# JIRA Task Source Adapter (skeleton)
# Implements the task source interface for JIRA.
#
# Required config:
#   TASK_SOURCE_CONFIG — JIRA project key (e.g., "PROJ")
#   JIRA_BASE_URL     — JIRA instance URL (e.g., "https://company.atlassian.net")
#
# Requires: curl, jq, JIRA API token in ~/.netrc or JIRA_TOKEN env var

JIRA_PROJECT="${TASK_SOURCE_CONFIG:-}"
JIRA_URL="${JIRA_BASE_URL:-}"
JIRA_AUTH=""

_jira_init() {
  if [[ -z "$JIRA_URL" ]]; then
    echo "Error: JIRA_BASE_URL not set"
    return 1
  fi
  if [[ -n "${JIRA_TOKEN:-}" && -n "${JIRA_EMAIL:-}" ]]; then
    JIRA_AUTH="-u ${JIRA_EMAIL}:${JIRA_TOKEN}"
  fi
}

adapter_list_tasks() {
  _jira_init || return 1
  local jql="project=${JIRA_PROJECT} AND status IN ('To Do','Open','Backlog') ORDER BY priority DESC"
  # shellcheck disable=SC2086
  curl -s $JIRA_AUTH \
    -H "Content-Type: application/json" \
    "${JIRA_URL}/rest/api/3/search?jql=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$jql'))")&maxResults=50&fields=summary,status,assignee,labels,priority" \
    2>/dev/null | jq '[.issues[] | {number: .key, title: .fields.summary, labels: [.fields.labels[]?], assignees: [.fields.assignee?.displayName // empty]}]'
}

adapter_get_task() {
  local issue_key="$1"
  _jira_init || return 1
  # shellcheck disable=SC2086
  curl -s $JIRA_AUTH \
    -H "Content-Type: application/json" \
    "${JIRA_URL}/rest/api/3/issue/${issue_key}?fields=summary,description,status,assignee,labels,priority" \
    2>/dev/null | jq '{number: .key, title: .fields.summary, body: .fields.description, labels: [.fields.labels[]?], state: .fields.status.name}'
}

adapter_set_status() {
  local issue_key="$1"
  local status="$2"
  _jira_init || return 1

  local transition_name
  case "$status" in
    in-progress) transition_name="In Progress" ;;
    done)        transition_name="Done" ;;
    *)           echo "Warning: Unknown status '$status'"; return ;;
  esac

  # Get available transitions
  local transition_id
  # shellcheck disable=SC2086
  transition_id=$(curl -s $JIRA_AUTH \
    "${JIRA_URL}/rest/api/3/issue/${issue_key}/transitions" \
    2>/dev/null | jq -r ".transitions[] | select(.name==\"$transition_name\") | .id")

  if [[ -n "$transition_id" ]]; then
    # shellcheck disable=SC2086
    curl -s $JIRA_AUTH \
      -X POST \
      -H "Content-Type: application/json" \
      -d "{\"transition\":{\"id\":\"$transition_id\"}}" \
      "${JIRA_URL}/rest/api/3/issue/${issue_key}/transitions" \
      2>/dev/null
  fi
}

adapter_link_pr() {
  local issue_key="$1"
  local pr_url="$2"
  _jira_init || return 1
  # Add PR link as a remote link on the JIRA issue
  # shellcheck disable=SC2086
  curl -s $JIRA_AUTH \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"object\":{\"url\":\"$pr_url\",\"title\":\"Pull Request\"}}" \
    "${JIRA_URL}/rest/api/3/issue/${issue_key}/remotelink" \
    2>/dev/null
}
