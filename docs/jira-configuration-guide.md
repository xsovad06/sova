# JIRA Cloud Configuration Guide

Step-by-step guide for configuring SOVA to work with JIRA Cloud as a task source. Covers status discovery, JQL filter construction, status mapping, and a complete reference configuration.

## Prerequisites

Before starting:

1. A JIRA Cloud instance with API access
2. A JIRA API token (create at https://id.atlassian.com/manage-profile/security/api-tokens)
3. SOVA installed in your project (`sova install /path/to/project`)

## Step 1: Discover Your JIRA Statuses

Every JIRA project has a unique workflow with custom statuses. Before configuring SOVA, you need to discover and classify them.

### List all statuses in your project

```bash
# Replace YOUR_DOMAIN and PROJECT_KEY with your values
curl -s -u "your-email@example.com:YOUR_API_TOKEN" \
  "https://YOUR_DOMAIN.atlassian.net/rest/api/3/project/PROJECT_KEY/statuses" \
  | python3 -m json.tool
```

This returns a JSON array of issue types, each containing a `statuses` array. Extract the unique status names:

```bash
curl -s -u "your-email@example.com:YOUR_API_TOKEN" \
  "https://YOUR_DOMAIN.atlassian.net/rest/api/3/project/PROJECT_KEY/statuses" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
statuses = set()
for issue_type in data:
    for s in issue_type.get('statuses', []):
        cat = s.get('statusCategory', {}).get('name', '?')
        statuses.add((s['name'], cat))
for name, cat in sorted(statuses):
    print(f'  {name:30s} (category: {cat})')
"
```

### Classify statuses

JIRA groups every status into one of three **status categories**: `To Do`, `In Progress`, and `Done`. SOVA uses these categories as a coarse signal, then refines with custom status mapping.

The category matters because **`statusCategory = "Done"` always maps to `done`**, regardless of any custom `jira_status_mapping` entry. The adapter checks the category before consulting the custom mapping.

Organize your statuses into a table like this:

| JIRA Status | Category | SOVA State |
|-------------|----------|------------|
| To Do | To Do | `backlog` |
| Refinement | To Do | `needs_spec` |
| In Progress | In Progress | `in_progress` |
| Code Review | In Progress | `in_review` |
| Done | Done | `done` (automatic) |

## Step 2: Configure the Task Source

### Minimal configuration

Add the `[task_source]` section to your project's `sova.toml`:

```toml
[task_source]
type = "jira"
jira_base_url = "https://your-domain.atlassian.net"
jira_email = "your-email@example.com"
jira_project_key = "MYPROJ"
```

### API token

Store the API token as an environment variable (never in `sova.toml`):

```bash
export SOVA_TASK_JIRA_API_TOKEN="your-api-token-here"
```

Or add it to `.env` in your project root (already in `.gitignore`).

The field `jira_api_token` in `sova.toml` uses `repr=False` to prevent accidental logging, but environment variables are the recommended approach.

### Required fields

| Field | Description | Example |
|-------|-------------|---------|
| `type` | Must be `"jira"` | `"jira"` |
| `jira_base_url` | Your JIRA Cloud instance URL | `"https://acme.atlassian.net"` |
| `jira_email` | Email for API authentication | `"dev@acme.com"` |
| `jira_api_token` | API token (prefer env var `SOVA_TASK_JIRA_API_TOKEN`) | |
| `jira_project_key` | JIRA project key | `"MYPROJ"` |

### Optional fields

| Field | Description | Default |
|-------|-------------|---------|
| `jira_component` | Filter issues by component | `""` (all) |
| `jira_jql_filter` | Additional JQL clause appended to queries | `""` |
| `jira_status_mapping` | Map JIRA status names to SOVA states | `{}` |
| `jira_state_transitions` | Override transition names for state changes | `{}` |
| `jira_track_agent_work` | Track agent work on issues | `false` |

## Step 3: Configure JQL Filters

### How JQL is constructed

SOVA builds JQL queries by combining multiple clauses with `AND`. The query is assembled in this order:

1. `project = PROJECT_KEY` (always present)
2. `statusCategory != Done` or `statusCategory = Done` (based on open/closed filter)
3. `component = "COMPONENT"` (if `jira_component` is set)
4. `(your_jql_filter)` (if `jira_jql_filter` is set, wrapped in parentheses)
5. `labels = "label"` (for each label filter)

For example, with this config:

```toml
[task_source]
type = "jira"
jira_project_key = "RHCLOUD"
jira_component = "RBAC"
jira_jql_filter = 'issuetype in (Bug, Story, Task)'
```

SOVA generates:

```
project = RHCLOUD AND statusCategory != Done AND component = "RBAC" AND (issuetype in (Bug, Story, Task))
```

### Common JQL filter recipes

**Filter by issue type**:
```toml
jira_jql_filter = 'issuetype in (Bug, Story, Task)'
```

**Exclude sub-tasks**:
```toml
jira_jql_filter = 'issuetype != Sub-task'
```

**Filter by sprint**:
```toml
jira_jql_filter = 'sprint in openSprints()'
```

**Filter by priority**:
```toml
jira_jql_filter = 'priority in (High, Highest, Critical)'
```

**Filter by fix version (milestone)**:
```toml
jira_jql_filter = 'fixVersion = "v2.0"'
```

**Combine multiple conditions**:
```toml
jira_jql_filter = 'issuetype in (Bug, Story) AND sprint in openSprints() AND priority != Lowest'
```

### Quoting rules

JQL values containing spaces or special characters must be double-quoted. In TOML, use single-quoted strings to avoid escaping:

```toml
# Single quotes in TOML avoid needing to escape the double quotes in JQL
jira_jql_filter = 'status = "In Progress"'
```

If you must use double-quoted TOML strings, escape the inner quotes:

```toml
jira_jql_filter = "status = \"In Progress\""
```

### Gotcha: do not duplicate the component clause

When `jira_component` is set, SOVA automatically appends `component = "COMPONENT"` to every query. Do not also include a component clause in `jira_jql_filter`, or the query will have a redundant (or conflicting) condition:

```toml
# WRONG: component appears twice in the generated JQL
jira_component = "RBAC"
jira_jql_filter = 'component = "RBAC" AND issuetype = Bug'

# CORRECT: let jira_component handle component filtering
jira_component = "RBAC"
jira_jql_filter = 'issuetype = Bug'
```

## Step 4: Configure Status Mapping

### Default mapping

SOVA ships with these built-in JIRA-to-SOVA status mappings:

| JIRA Status | SOVA State |
|-------------|------------|
| `To Do` | `backlog` |
| `Backlog` | `backlog` |
| `Open` | `backlog` |
| `Refinement` | `needs_spec` |
| `New` | `needs_spec` |
| `In Progress` | `in_progress` |
| `Code Review` | `in_review` |
| `Review` | `in_review` |

Any JIRA status in the `Done` category (regardless of its name) is always mapped to `done`. This is checked before the custom mapping and cannot be overridden.

### Valid SOVA states

These are the valid SOVA task states you can use as mapping targets:

| State | Description |
|-------|-------------|
| `backlog` | Not yet triaged or ready for work |
| `triaged` | Triaged by SOVA, awaiting research |
| `researched` | Researched, ready for development |
| `in_progress` | Currently being developed |
| `in_review` | PR created, under review |
| `done` | Completed and merged |
| `needs_spec` | Requires specification before development |
| `human_only` | Flagged for human-only work, SOVA will skip |

### Custom status mapping

If your JIRA project uses custom status names, add them to `jira_status_mapping`. The keys are exact JIRA status names (case-sensitive), and the values are SOVA state strings:

```toml
[task_source.jira_status_mapping]
"Selected for Development" = "backlog"
"Dev In Progress" = "in_progress"
"QA Review" = "in_review"
"Waiting for Deployment" = "in_review"
```

Custom mappings are merged on top of the defaults. To override a default, specify the same JIRA status name with a different SOVA state:

```toml
[task_source.jira_status_mapping]
# Override the default "New" -> "needs_spec" to treat it as backlog instead
"New" = "backlog"
```

### TOML syntax for status names with spaces

TOML inline tables and dotted keys handle quoted keys differently. For `jira_status_mapping`, use the subsection syntax shown above. The key must be quoted when it contains spaces:

```toml
# Correct: subsection syntax with quoted keys
[task_source.jira_status_mapping]
"Ready for Dev" = "backlog"
"In Code Review" = "in_review"
```

Inline table syntax also works but is harder to read:

```toml
[task_source]
jira_status_mapping = {"Ready for Dev" = "backlog", "In Code Review" = "in_review"}
```

### Unmapped status warning

When SOVA encounters a JIRA status that is not in the default or custom mapping (and is not in the `Done` category), it logs a warning:

```
status.unmapped  status="Awaiting QA"  issue=PROJ-42  hint="Add to [task_source] jira_status_mapping in sova.toml"
```

This warning helps you discover statuses you need to map. The issue defaults to `backlog` when unmapped.

### Resolution precedence

The adapter resolves SOVA state in this order:

1. **`statusCategory = "Done"`**: always maps to `done`, overriding everything below (consistent with GitHub adapter where CLOSED always wins)
2. **`agent:` labels**: if the issue has an `agent:triaged`, `agent:in-progress`, etc. label, that takes priority over status mapping
3. **Custom `jira_status_mapping`**: checked against the JIRA status name
4. **Default mapping**: the built-in status mapping table
5. **Fallback**: `backlog` (with an unmapped warning)

## Step 5: Configure State Transitions (Optional)

When SOVA changes an issue's state (e.g., moving it to "In Progress" when starting development), it triggers JIRA workflow transitions. SOVA tries a list of common transition names by default:

| Target State | Default Transition Names Tried |
|--------------|-------------------------------|
| `backlog` | `To Do`, `Backlog`, `Open` |
| `in_progress` | `In Progress`, `Start Progress` |
| `in_review` | `In Review`, `Review` |
| `done` | `Done`, `Closed`, `Resolved`, `Close` |

If your workflow uses different transition names, override them with `jira_state_transitions`:

```toml
[task_source.jira_state_transitions]
in_progress = "Start Work"
in_review = "Move to Review"
done = "Resolve Issue"
```

The keys are SOVA state strings (the target state), and the values are the JIRA transition name to trigger. The custom transition name is tried first; if it does not match any available transition, the defaults are tried as a fallback.

### Discovering available transitions

To see what transitions are available for a specific issue:

```bash
curl -s -u "your-email@example.com:YOUR_API_TOKEN" \
  "https://YOUR_DOMAIN.atlassian.net/rest/api/3/issue/PROJ-42/transitions" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for t in data.get('transitions', []):
    to_status = t.get('to', {}).get('name', '?')
    print(f'  id={t[\"id\"]:4s}  name=\"{t[\"name\"]}\"  -> {to_status}')
"
```

Note: available transitions depend on the issue's current status. Run this for issues in each status to build a complete picture.

## Reference Configuration

Complete working example based on a real JIRA Cloud project:

```toml
# sova.toml

github_repo = "org/my-project"
github_user = "my-github-user"

[task_source]
type = "jira"
jira_base_url = "https://acme.atlassian.net"
jira_email = "developer@example.com"
jira_project_key = "RHCLOUD"
jira_component = "RBAC"
jira_jql_filter = 'issuetype in (Bug, Story, Task) AND sprint in openSprints()'

[task_source.jira_status_mapping]
"To Do" = "backlog"
"Refinement" = "needs_spec"
"In Progress" = "in_progress"
"Code Review" = "in_review"
"Release Pending" = "in_review"

[task_source.jira_state_transitions]
in_progress = "In Progress"
in_review = "Code Review"
done = "Done"
```

With corresponding environment variable:

```bash
export SOVA_TASK_JIRA_API_TOKEN="your-jira-api-token"
```

### What this configuration does

1. Connects to JIRA Cloud at `acme.atlassian.net`
2. Scopes issues to the `RHCLOUD` project, `RBAC` component
3. Filters to bugs, stories, and tasks in open sprints only
4. Maps five JIRA statuses to SOVA states (anything in the `Done` category is automatically mapped to `done`)
5. Configures explicit transition names for moving issues between states
6. Uses GitHub for PRs and code hosting (JIRA for issue tracking only)

### Verifying the configuration

After configuring, verify SOVA can connect and list issues:

```bash
sova triage --dry-run 1   # Replace 1 with a real issue number
```

If the connection fails, check:

- API token is set in the environment (`echo $SOVA_TASK_JIRA_API_TOKEN`)
- Email matches the account that owns the API token
- `jira_base_url` does not have a trailing slash (SOVA strips it, but be consistent)
- `jira_project_key` is the short key (e.g., `RHCLOUD`), not the project name

## Troubleshooting

### "status.unmapped" warnings

Add the reported status name to `jira_status_mapping` in `sova.toml`. See the warning log for the exact status name and issue key.

### Issues stuck in backlog despite being "In Progress" in JIRA

The most common cause is a missing status mapping. If your JIRA uses a custom status name like "Dev In Progress" instead of the default "In Progress", SOVA falls back to `backlog`. Add the mapping:

```toml
[task_source.jira_status_mapping]
"Dev In Progress" = "in_progress"
```

### Transitions not triggering

Run the transition discovery command (above) for the issue in question. The transition name in `jira_state_transitions` must exactly match one of the available transition names (not the target status name, which may be different).

### "Done" status overrides custom mapping

This is by design. Any JIRA status in the `Done` category is always treated as `done` by SOVA. If you have a status like "Awaiting Deploy" that is in the `Done` category but should not be `done` in SOVA, you need to change its category in your JIRA workflow settings.

### Duplicate component in JQL

If you see unexpected query results, check that you are not specifying `component` in both `jira_component` and `jira_jql_filter`. SOVA auto-appends the component clause when `jira_component` is set.

## Related Documentation

- [Integration Guidelines](integration-guidelines.md): high-level integration patterns for all adapters
- [Security Guidelines](security-guidelines.md): credential handling for JIRA API tokens
- [AGENTS.md](../AGENTS.md): adapter ABC contract and JIRA-aware pipeline outputs
