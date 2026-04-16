---
name: sync-instructions
description: Sync portable project instructions into a target project -- merge commands, skills, and templates without overwriting customizations.
user-invocable: true
---

# Sync Project Instructions

Sync the portable project-instructions template into a target project, intelligently merging with any existing commands, skills, and configuration.

**Target**: $ARGUMENTS (path to target project, or leave empty to select interactively)

## Source

The portable template lives at: `/Users/dsova/Documents/RedHat/Code/project-instructions/`

## Instructions

### Phase 1: Discover Target State

1. **Resolve the target project path** from `$ARGUMENTS`. If empty, ask the user.

2. **Scan the target project** for existing AI configuration:
   ```bash
   # Commands
   ls -la <TARGET>/.claude/commands/ 2>/dev/null
   # Skills
   ls -la <TARGET>/.claude/skills/ 2>/dev/null
   # Config files
   ls -la <TARGET>/AGENTS.md <TARGET>/CLAUDE.md <TARGET>/.claude/CLAUDE.md 2>/dev/null
   # Domain guidelines
   ls -la <TARGET>/docs/*-guidelines.md 2>/dev/null
   # Agent memory
   ls -la <TARGET>/.claude/agent-memory/ 2>/dev/null
   # CodeRabbit
   ls -la <TARGET>/.coderabbit.yaml 2>/dev/null
   ```

3. **Scan the source template**:
   ```bash
   ls -la /Users/dsova/Documents/RedHat/Code/project-instructions/commands/
   ls -la /Users/dsova/Documents/RedHat/Code/project-instructions/templates/
   ```

### Phase 2: Compare and Classify

For each command in the template, classify it against the target:

| Status | Meaning |
|--------|---------|
| **NEW** | Command does not exist in target -- will be added |
| **OUTDATED** | Command exists but template version is newer/more complete |
| **CUSTOMIZED** | Command exists and has project-specific changes -- needs manual merge |
| **IDENTICAL** | Command is the same -- skip |

Build the classification by:
1. Check if the file exists in the target
2. If it exists, diff the template version against the target version
3. If the target version has project-specific references (repo names, tools, frameworks), mark as CUSTOMIZED
4. If the target version is a strict subset of the template, mark as OUTDATED

Present the comparison table to the user:

```
| Command              | Status     | Action                        |
|----------------------|------------|-------------------------------|
| develop.md           | NEW        | Will copy from template       |
| test.md              | OUTDATED   | Will update (no customizations)|
| pr.md                | CUSTOMIZED | Needs manual merge            |
| review.md            | IDENTICAL  | Skip                          |
```

Also report on config files:

```
| File                 | Target Has | Template Has | Action           |
|----------------------|------------|--------------|------------------|
| AGENTS.md            | Yes        | Template     | Compare          |
| CLAUDE.md            | Yes        | Template     | Compare          |
| .coderabbit.yaml     | No         | Template     | Offer to create  |
| agent-memory/        | No         | N/A          | Offer to init    |
```

### Phase 3: User Approval

Ask the user what to sync using AskUserQuestion:
- **All NEW commands** (safe, no conflicts)
- **All OUTDATED commands** (safe, target had no customizations)
- **CUSTOMIZED commands** (needs review -- show diff for each)
- **Config templates** (AGENTS.md, CLAUDE.md)
- **Agent memory initialization**
- **Gitignore setup** (add entries for synced commands)

### Phase 4: Execute Sync

For each approved action:

1. **NEW commands**: Copy directly
   ```bash
   cp /Users/dsova/Documents/RedHat/Code/project-instructions/commands/<file> <TARGET>/.claude/commands/
   ```

2. **OUTDATED commands**: Replace with template version
   ```bash
   cp /Users/dsova/Documents/RedHat/Code/project-instructions/commands/<file> <TARGET>/.claude/commands/
   ```

3. **CUSTOMIZED commands**: Show a side-by-side or unified diff and let the user decide per-file:
   - **Replace**: Use template version (lose customizations)
   - **Keep**: Keep target version as-is
   - **Merge**: Open both versions, merge manually with the user

4. **Config templates**: If target doesn't have AGENTS.md/CLAUDE.md, copy templates. If it does, show diff and let user decide.

5. **Agent memory**: Initialize if missing:
   ```bash
   mkdir -p <TARGET>/.claude/agent-memory
   touch <TARGET>/.claude/agent-memory/{MEMORY,learnings,review-feedback,common-mistakes,task-history}.md
   ```

6. **Gitignore setup**: Add entries to target's `.gitignore` for synced commands:
   ```bash
   # Append to .gitignore if not already present
   cat >> <TARGET>/.gitignore <<'EOF'

   # Synced from project-instructions template (managed externally)
   # To update: run /sync-instructions from team-productivity-utils
   .claude/commands/develop.md
   .claude/commands/develop-full.md
   .claude/commands/develop-explain.md
   .claude/commands/test.md
   .claude/commands/review.md
   .claude/commands/review-pr.md
   .claude/commands/pr.md
   .claude/commands/address-pr.md
   .claude/commands/address-sourcery.md
   .claude/commands/after-merge.md
   .claude/commands/rearrange-commits.md
   .claude/commands/ingest-review.md
   .claude/commands/extract-knowledge.md
   .claude/commands/quarterly-report.md
   .claude/commands/standup.md
   .claude/commands/find-task.md
   .claude/commands/sprint-plan.md
   .claude/commands/jira.md
   .claude/commands/agent-readiness.md
   .claude/commands/sync-instructions.md
   EOF
   ```
   Only add entries for commands that were actually synced. Preserve any existing `.gitignore` entries.

### Phase 5: Report

Print summary:
- Commands synced (NEW + OUTDATED)
- Commands skipped (IDENTICAL + user-declined)
- Commands needing manual merge (CUSTOMIZED)
- Config files created/updated
- Gitignore entries added

Remind the user:
- Customize `CLAUDE.md` with project-specific build/test/lint commands
- Customize `AGENTS.md` with project-specific conventions
- Or run `/agent-readiness` to generate them interactively
- Synced commands are gitignored -- updates come from re-running `/sync-instructions`

## Cross-References

- **First-time setup?** Run `/agent-readiness` in the target project instead
- **Documentation**: See `/Users/dsova/Documents/RedHat/Code/project-instructions/PORTING.md`

## Rules

- NEVER overwrite customized files without user approval
- NEVER delete existing target commands not in the template
- Always show diffs before replacing customized files
- NEVER use emojis in any output
