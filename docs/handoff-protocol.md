# Agent Handoff Protocol

The handoff protocol enables agent chaining: when one agent finishes and needs human input before the next step, it writes a structured state file. The dashboard reads this file and presents actionable widgets that trigger follow-up agents.

This replaces the need for long-running agents that pause for approval. Instead, each agent is short-lived and self-contained, producing a handoff file on exit.

## Handoff File

**Location**: `.claude/agent-control/handoff.json`

**Lifecycle**:
1. Agent completes its work and writes `handoff.json`
2. Dashboard detects the file and renders action widgets
3. User clicks an action in the dashboard
4. Dashboard starts a new agent (orchestrator mode or Claude Code command)
5. New agent reads `handoff.json` for context, does its work
6. New agent overwrites `handoff.json` with its own result (or deletes it if done)

## Schema

```json
{
  "id": "string (UUID v4)",
  "created_at": "string (ISO 8601 timestamp)",
  "source": "string (command name that produced this handoff)",
  "issue": "string (GitHub Issue number, optional)",
  "pr_number": "integer (GitHub PR number, optional)",
  "branch": "string (git branch name, optional)",
  "status": "string (awaiting_action | completed | failed)",
  "summary": "string (human-readable description of what happened)",
  "details": {
    "actions_taken": ["string (what the agent did)"],
    "ci_status": "string (pending | passed | failed | unknown, optional)",
    "error": "string (error message if status is failed, optional)"
  },
  "next_actions": [
    {
      "id": "string (unique action identifier)",
      "label": "string (button text)",
      "description": "string (tooltip / explanation)",
      "mode": "string (agent | claude-command | shell)",
      "command": "string (command file name for claude-command mode, optional)",
      "args": {
        "key": "value (arguments passed to the command)"
      },
      "style": "string (approve | danger | neutral)"
    }
  ]
}
```

## Fields

### Top-Level

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier for this handoff (UUID v4) |
| `created_at` | string | yes | ISO 8601 timestamp of when the handoff was created |
| `source` | string | yes | Name of the command/mode that produced this handoff |
| `issue` | string | no | GitHub Issue number (e.g., `#42`) |
| `pr_number` | integer | no | GitHub PR number |
| `branch` | string | no | Git branch name |
| `status` | string | yes | One of: `awaiting_action`, `completed`, `failed` |
| `summary` | string | yes | Human-readable summary of what happened |
| `details` | object | yes | Structured details about the agent's work |
| `next_actions` | array | yes | Available follow-up actions (empty if `completed`) |

### `details` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `actions_taken` | array | yes | List of actions the agent performed |
| `ci_status` | string | no | CI check status if relevant |
| `error` | string | no | Error message if status is `failed` |

### `next_actions` Array Items

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier for this action |
| `label` | string | yes | Short label for the button |
| `description` | string | yes | Longer explanation shown as tooltip |
| `mode` | string | yes | Execution mode (see below) |
| `command` | string | no | Command file name (for `claude-command` mode) |
| `args` | object | yes | Arguments passed to the command |
| `style` | string | yes | Visual style: `approve` (green), `danger` (red), `neutral` (gray) |

### Execution Modes

| Mode | Runs | Description |
|------|------|-------------|
| `agent` | `sova run --<mode> <args>` | Delegates to the SOVA workflow engine |
| `claude-command` | `claude -p "<command contents>"` | Runs a Claude Code command file directly |
| `shell` | Raw shell command | For simple operations (e.g., `gh pr merge`) |

## Style Mapping

The `style` field controls how the button appears in the dashboard:

| Style | Color | Usage |
|-------|-------|-------|
| `approve` | Green | Positive actions (merge, approve, proceed) |
| `danger` | Red | Destructive or irreversible actions (abort, close, force-push) |
| `neutral` | Gray | Informational or wait actions (wait for CI, skip) |

## Example: integrate-pr Handoff

After `integrate-pr` rebases, pushes, and waits for CI:

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "created_at": "2026-04-16T14:30:00Z",
  "source": "integrate-pr",
  "issue": "#42",
  "pr_number": 15,
  "branch": "feat/add-caching",
  "status": "awaiting_action",
  "summary": "Rebased PR #15 onto main, pushed, and CI passed. Ready to merge.",
  "details": {
    "actions_taken": [
      "Pulled latest main",
      "Rebased feat/add-caching onto main (no conflicts)",
      "Force-pushed with lease",
      "CI checks passed"
    ],
    "ci_status": "passed"
  },
  "next_actions": [
    {
      "id": "merge-now",
      "label": "Merge PR",
      "description": "Squash-merge PR #15 and run post-merge cleanup",
      "mode": "claude-command",
      "command": "approve-merge",
      "args": {"pr": 15, "issue": "#42"},
      "style": "approve"
    },
    {
      "id": "abort",
      "label": "Abort",
      "description": "Cancel this workflow (no changes made)",
      "mode": "shell",
      "command": "echo",
      "args": {"message": "Workflow cancelled by user"},
      "style": "danger"
    }
  ]
}
```

## Example: approve-merge Completion

After `approve-merge` finishes:

```json
{
  "id": "f9e8d7c6-b5a4-3210-fedc-ba0987654321",
  "created_at": "2026-04-16T14:35:00Z",
  "source": "approve-merge",
  "issue": "#42",
  "pr_number": 15,
  "branch": "feat/add-caching",
  "status": "completed",
  "summary": "PR #15 merged and cleaned up.",
  "details": {
    "actions_taken": [
      "Squash-merged PR #15",
      "Deleted remote branch feat/add-caching",
      "Deleted local branch"
    ]
  },
  "next_actions": []
}
```

## Integration with Checkpoint System

The handoff protocol is complementary to the existing checkpoint/request system:

- **Checkpoints** (`request.json` / `response.json`): Used by the orchestrator during a running session. The agent pauses and waits for a response. Synchronous within a single agent run.
- **Handoffs** (`handoff.json`): Used between agent runs. One agent finishes, writes what to do next, and a new agent picks it up. Asynchronous across multiple agent runs.

Both are rendered in the dashboard's control page. Checkpoints appear as modal dialogs (interrupt flow), handoffs appear as persistent action panels (suggest next steps).
