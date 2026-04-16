#!/usr/bin/env bash
# Linear Task Source Adapter (skeleton)
# Implements the task source interface for Linear.
#
# Required config:
#   TASK_SOURCE_CONFIG — Linear team key (e.g., "ENG")
#   LINEAR_API_KEY     — Linear API key
#
# Requires: curl, jq

LINEAR_TEAM="${TASK_SOURCE_CONFIG:-}"
LINEAR_API="https://api.linear.app/graphql"

_linear_query() {
  local query="$1"
  curl -s -X POST "$LINEAR_API" \
    -H "Content-Type: application/json" \
    -H "Authorization: ${LINEAR_API_KEY:-}" \
    -d "{\"query\": \"$query\"}" \
    2>/dev/null
}

adapter_list_tasks() {
  _linear_query "{ team(id: \\\"$LINEAR_TEAM\\\") { issues(filter: { state: { type: { in: [\\\"backlog\\\", \\\"unstarted\\\"] } } }, first: 50) { nodes { identifier title state { name } assignee { name } labels { nodes { name } } priority } } } }" \
    | jq '[.data.team.issues.nodes[] | {number: .identifier, title: .title, labels: [.labels.nodes[].name], assignees: [.assignee.name // empty]}]'
}

adapter_get_task() {
  local issue_id="$1"
  _linear_query "{ issue(id: \\\"$issue_id\\\") { identifier title description state { name } assignee { name } labels { nodes { name } } priority } }" \
    | jq '.data.issue | {number: .identifier, title: .title, body: .description, labels: [.labels.nodes[].name], state: .state.name}'
}

adapter_set_status() {
  local issue_id="$1"
  local status="$2"
  echo "Warning: Linear adapter set_status not yet implemented for '$status'"
}

adapter_link_pr() {
  local issue_id="$1"
  local pr_url="$2"
  # Linear auto-links PRs via branch naming conventions (e.g., eng-123-feature)
  :
}
