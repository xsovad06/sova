# Configuration

**Note**: SOVA uses database-backed configuration (`.claude/sova.db`). Edit it with `sova config set <key> <value>`, the dashboard Settings page, or `SOVA_*` environment variables. Read a single value (for scripts or commands) with `sova config get <key>`; lists and dicts print as JSON, and a secret key always prints `(secret)`. A legacy `sova.toml` is still read once and auto-migrated into the database if one is present. See [Configuration Troubleshooting](../CLAUDE.md#configuration) for details.

The settings are shown below in TOML form for reference (this is a rendering of the stored keys, not a file SOVA writes). Minimal example (required fields only):

```toml
github_repo = "owner/repo"
github_user = "owner"

[task_source]
type = "github"
```

Key optional sections:

```toml
[agent]
model = "opus"              # LLM model for agent steps
max_budget = 10.0           # Max USD per run ($10 default)

[ci]
max_fix_attempts = 2        # Auto-fix CI failures (0 to disable)
max_wait = 900              # Seconds to wait for CI checks

[pipeline]
auto_handoff = true         # Developer auto-hands off to Reviewer
auto_address_review = true  # Reviewer auto-triggers address-review

[telemetry]
hub_url = "https://your-hub.example.com"  # Push run summaries to a central hub (SOVA_TELEMETRY_HUB_URL)
hub_token = ""                             # Bearer token (SOVA_TELEMETRY_HUB_TOKEN)
machine_id = ""                            # Auto-derived from hostname+username when empty (SOVA_TELEMETRY_MACHINE_ID)

[mcp.atlassian]
enabled = false             # Opt-in Confluence + Jira sidecar (mcp-atlassian, needs uvx)
jira_url = "https://issues.example.com"
confluence_url = "https://docs.example.com"
auth_type = "pat"           # "pat" for on-prem, "api_token" for Cloud (paired with email)
read_only = true            # Agents read context; writes still go through the task adapter
```

`sova install` writes enabled MCP sidecars to `<project>/.mcp.json` and approves them in `.claude/settings.json`. Leave `token` empty and export `SOVA_MCP_ATLASSIAN_TOKEN` to keep the credential out of that file. See [Integration Guidelines](integration-guidelines.md) for details.

SOVA generates commit subjects and PR titles as conventional-commit lines, validated against
`commit.types` and `commit.scopes`. When unset, each list is read from a `Valid types:`/`Valid scopes:` line
in your AGENTS.md or CLAUDE.md, else derived from your recent git history (SOVA's own lists apply only to the SOVA
repository); `sova install` and `sova commands update` store the result, and `sova doctor` warns when
`commit.scopes` still matches SOVA's defaults. See [Commit Convention Setup](commit-convention-setup.md).

## LDAP (optional)

LDAP reviewer and assignee suggestions are opt-in and have no preconfigured directory. Set the directory yourself:

```toml
[ldap]
enabled = true
server = "ldaps://ldap.example.com"       # required; ldaps:// unless ldap.allow_insecure
base_dn = "ou=users,dc=example,dc=com"    # required
group_base_dn = "ou=groups,dc=example,dc=com"  # needed only for ldap.team_mapping
uid_attribute = "uid"                     # "sAMAccountName" on Active Directory

[ldap.attribute_map]                      # Person field -> directory attribute
job_title = "corpJobTitle"                # merged over the defaults below
team_lead = "corpLead"
organization = ""                         # an empty value turns a field off
```

The default `attribute_map` reads the standard inetOrgPerson attributes: `job_title` from `title`, `cost_center` from `departmentNumber`, `organization` from `o` and `location` from `l`. `cost_center_desc`, `team_lead` and `geo` have no standard attribute and stay empty until you map them.

**Upgrading from an older SOVA that shipped a built-in directory:** an install with only `ldap.enabled = true` now builds no LDAP client and logs `ldap.not_configured` (`sova doctor` reports it). Set `ldap.server` and `ldap.base_dn`, plus `ldap.group_base_dn` for `ldap.team_mapping`, and map any custom attributes your directory used for title, department or location in `ldap.attribute_map`. LDAP also needs the optional package: `pip install sova[ldap]`.

For the full configuration reference, see [`sova/config/models.py`](../sova/config/models.py). Environment variables override TOML values using the `SOVA_` prefix (e.g., `SOVA_BASE_BRANCH=develop`).
