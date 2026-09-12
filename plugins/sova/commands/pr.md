---
description: Create a pull request with a standard template, analyzing all commits and changes on the current branch
argument-hint: "[issue-reference]"
example: "/sova:pr Closes #128"
---

## Name
sova:pr

## Synopsis
```
/sova:pr [issue-reference]
```

## Description

The `sova:pr` command creates a pull request for the current branch, analyzing every commit and file change on the branch to produce a summary and test plan rather than a generic one-line description. If an issue reference is supplied, it links the pull request to that issue using the tracker's linking convention (for example, `Closes #N` on GitHub).

## Implementation

1. **Sync with the base branch**
   - Fetch and rebase (or merge, per project convention) onto the latest base branch before opening the pull request, so CI runs against current code.
   - Stash any uncommitted changes first, and restore them after.

2. **Analyze the branch**
   - Read every commit message on the branch (`git log <base>..HEAD`).
   - Read the full diff (`git diff <base>..HEAD`) to understand the actual code changes, not just the commit messages.

3. **Draft the pull request**
   - **Title**: short, imperative, following the project's commit/PR title conventions.
   - **Summary**: what changed and why, in a few bullet points, written for a reviewer who has not seen the branch.
   - **Test plan**: how the change was verified (tests added, manual verification steps, edge cases checked).
   - **Issue link**: if an issue reference was given, include it using the tracker's linking syntax so the issue closes automatically on merge.

4. **Push and open the pull request**
   - Push the branch to the remote.
   - Create the pull request using the project's PR template if one exists (e.g. `.github/PULL_REQUEST_TEMPLATE.md`), populating it with the drafted content instead of leaving placeholders.

5. **Report**
   - Return the pull request URL and number.

## Return Value
- **Claude agent text**: the pull request URL and number, and a summary of what was included.

## Examples

1. **PR with no issue reference**:
   ```
   /sova:pr
   ```

2. **PR linked to an issue**:
   ```
   /sova:pr Closes #128
   ```

## Arguments
- `$1` (optional): an issue reference to link in the pull request body.

## See Also
- `/sova:review`: self-review the changes before opening the pull request
- `/sova:develop`: implement the change this pull request contains
