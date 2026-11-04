# GitHub Token Setup for SOVA Subprocesses

**Problem**: When SOVA spawns agent subprocesses, the `gh` CLI cannot access GitHub authentication tokens stored in macOS Keychain. This causes workflow failures with:
```
gh.token_resolve_failed: no oauth token found for github.com account xsovad06
Process exited with code -15 (SIGTERM)
```

**Solution**: Export the GitHub token as an environment variable so subprocesses can access it.

## Setup Instructions

### 1. Verify GitHub CLI Authentication

```bash
gh auth status --show-token
```

You should see:
```
[Check] Logged in to github.com account <your-username> (keyring)
```

### 2. Export Token to Shell Environment

Add the following to **both** `~/.zshrc` and `~/.bashrc`:

**~/.zshrc**:
```bash
# GitHub CLI token (for subprocess access to keychain token)
export GH_TOKEN=$(gh auth token 2>/dev/null)
```

**~/.bashrc**:
```bash
# GitHub CLI token (for subprocess access to keychain token)
export GH_TOKEN=$(gh auth token 2>/dev/null)
```

### 3. Reload Shell Configuration

```bash
# For current zsh session
source ~/.zshrc

# For current bash session
source ~/.bashrc
```

### 4. Verify Token is Available

```bash
echo $GH_TOKEN | head -c 20
# Output should be: gho_XXXXXXXXXXXXXXXXXXXX
```

Verify `gh` sees the token:
```bash
gh auth status --show-token
# Should now show: (GH_TOKEN) instead of (keyring)
```

## Why This is Needed

SOVA spawns Python subprocesses for agent workflows (develop step, etc.). These subprocesses:
- Cannot access macOS Keychain directly
- Need the GitHub token as an explicit environment variable
- Use the `GH_TOKEN` env var when available (preferred path)

**Priority**: `GH_TOKEN` env var > Keychain > GitHub CLI config

## Troubleshooting

**Problem**: `GH_TOKEN` not set after reloading shell
```bash
# Check if gh is installed and working
which gh
gh --version

# Check if token can be extracted
gh auth token
```

**Problem**: Agent still failing with `gh.token_resolve_failed`
```bash
# Verify token is in subprocess environment
(python3 -c "import os; print('GH_TOKEN' in os.environ)")

# Test gh CLI with explicit token
GH_TOKEN=$(gh auth token) gh auth status
```

**Problem**: Multiple GitHub accounts
If you have multiple GitHub accounts configured, ensure the active account is the one you want SOVA to use:
```bash
gh auth status
# Lists all accounts; the first one is active

# Switch if needed
gh auth switch --user <target-username>
```

## Project Board Scopes

Moved from `.claude/rules/workflow.md` (issue #1338).

- **Add a newly created issue to the board**: `gh project item-add 2 --owner xsovad06 --url <issue-url>`.
  - Missing `project` scope: the command fails with a scope error; run `gh auth refresh -s project` and retry rather than leaving the issue off the board. If `GH_TOKEN`, `GITHUB_TOKEN` or `GH_ENTERPRISE_TOKEN` is set in the environment, `gh auth refresh` refuses to run while it supplies auth; in that case rotate to a replacement token with the `project` scope instead.
  - Missing `read:org` scope: `gh`'s own owner-type resolution inside `project item-add` can demand `read:org` even to add an item to a user-owned board (this repo's board is owned by `xsovad06`, a user, not an org). `sova board add` (`sova/cli/commands/board.py`) falls back to a GraphQL mutation in that case, the single copy to edit when the mutation changes.

## Related Issues

- Issue #882: Calendar filtering (failed due to token access)
- See `.zshrc` and `.bashrc` in this repository for other token setup patterns (Jira, etc.)
