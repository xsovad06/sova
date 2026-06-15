---
name: agent-readiness
description: Assess and improve a repository's readiness for AI-assisted development.
user-invocable: true
category: meta
inputs: []
outputs:
  - readiness_score
  - recommendations
---

Assess the current repository's readiness for AI-assisted development, then offer to improve it step by step. $ARGUMENTS

Whenever you need to ask the user a question, always use the AskUserQuestion tool -- never ask as plain text.

When a step involves a discussion with the user, tell them they can say "done" or "skip" to move on to the next step.

## Step 1. Assess (before)

Present this explanation:

> This skill helps you build a layered documentation system for AI-assisted development. Each file has a distinct role:
>
> - **`docs/*-guidelines.md`** -- Detailed, domain-specific playbooks (security, testing, database, etc.) with concrete rules agents follow
> - **`AGENTS.md`** -- The onboarding doc for any AI agent: cross-cutting conventions + an index pointing to the guideline files
> - **`CLAUDE.md`** -- A thin, Claude Code-specific layer that imports AGENTS.md and adds Claude-only behavior (build commands, etc.)
> - **`.coderabbit.yaml`** -- Points CodeRabbit (AI code reviewer) to the guideline files so it enforces your conventions during PR reviews
> - **`README.md`** -- The front door: high-level project context for humans and agents alike
> - **`CONTRIBUTING.md`** -- Contribution conventions for both humans and agents
> - **`docs/ARCHITECTURE.md`** -- Institutional knowledge about the system's design and key architectural decisions
>
> We'll check what's already in place, then walk through each file step by step.

Check every requirement and present results:

| # | Requirement | How to check |
|---|-------------|--------------|
| 1 | Domain-specific guideline files (`docs/*-guidelines.md`) | Glob for files -- pass if at least one exists |
| 2 | AGENTS.md with AI-specific guidance and docs index | Check repo root |
| 3 | CLAUDE.md imports AGENTS.md (`@AGENTS.md`) | Check repo root |
| 4 | CodeRabbit configured (`.coderabbit.yaml`) | Check for `knowledge_base.code_guidelines.filePatterns` |
| 5 | README.md with foundational context | Check repo root |
| 6 | CONTRIBUTING.md with contribution conventions | Check repo root |
| 7 | docs/ARCHITECTURE.md with institutional knowledge | Check for file |

## Step 2. Generate or update domain-specific guideline files

> Guideline files (`docs/*-guidelines.md`) are the deepest layer. They contain detailed, domain-specific rules -- concrete conventions from your repo, not generic knowledge. AGENTS.md will point to these files.

Ask the user if they want to generate or update guideline files. If they decline, skip.

### 2a. Identify relevant domains

Start from this curated list: security, performance, error-handling, api-contracts, database, testing, integration.

Merge with any domains already in AGENTS.md. Use an Explore agent (Sonnet) to scan the repo and determine relevance. Present filtered list to user for confirmation.

### 2b. Explore and generate guidance

For each confirmed domain, launch a background agent (Opus). Each agent must:
1. Thoroughly explore the repo from its domain perspective
2. Identify conventions, patterns, libraries, and practices
3. If `docs/<domain>-guidelines.md` exists, read and incorporate it
4. Return complete guideline content (do NOT write files)

Each guideline must not exceed 200 lines. Focus on repo-specific conventions, not general knowledge.

### 2c. Verify guideline accuracy

For each domain, launch a verification agent (Sonnet) that:
1. **Reference accuracy** -- Check every file path, class name, function name against the codebase
2. **Factual claims** -- Verify library/framework behavior claims via WebSearch
3. **Absolute rules vs code** -- Grep for counter-examples to "Never"/"Always" rules
4. **Cross-document consistency** -- Detect contradictions between guideline files
5. Return corrected version (do NOT write files)

## Step 3. Generate or update AGENTS.md

> AGENTS.md is the onboarding doc for any AI agent. It captures cross-cutting conventions and includes an index pointing to the detailed guideline files. Unlike CLAUDE.md, it's agent-agnostic.

Ask user if they want to generate/update. If they decline, skip.

Launch a background agent (Opus) to explore the repo and propose AGENTS.md content including:
- Docs index from step 3a
- Cross-cutting conventions spanning multiple domains
- Architectural context not obvious from single files
- Common pitfalls specific to this repo

Present to user for review. Write when approved.

## Step 4. Generate or update CLAUDE.md

> CLAUDE.md is the Claude Code-specific layer on top of AGENTS.md. It uses `@AGENTS.md` to import agent guidance, then adds Claude Code-exclusive content.

**Belongs in CLAUDE.md**: `@AGENTS.md` import, build/test/lint commands, pre-commit hooks, Claude-specific preferences.

**Does NOT belong**: coding conventions, architecture, domain rules (those go in AGENTS.md/docs/).

Launch a background agent (Sonnet) to propose content. Present to user. Write when approved.

## Step 5. Configure CodeRabbit

Create or update `.coderabbit.yaml` pointing to `docs/*-guidelines.md`:

```yaml
knowledge_base:
  code_guidelines:
    filePatterns:
      - "docs/*-guidelines.md"
```

If file exists, merge without overwriting existing settings.

## Step 6. Generate or update README.md

Launch a background agent (Opus) to propose README content covering:
- Project purpose and description
- Tech stack and key dependencies
- Project structure overview
- How to build and run
- Links to further documentation

Present to user. Write when approved.

## Step 7. Assess (after)

Re-check all requirements. Present before/after comparison table.

## Step 8. Create a pull request (optional)

Ask user if they want a PR. If yes:
1. Branch: `improve-agent-readiness`
2. Stage all changed/new files
3. Commit with descriptive message
4. Push and create PR via `gh pr create`
5. Display PR link

## Cross-References

- **Knowledge extraction**: `/extract-knowledge` for session-level learning
- **After setup**: Use `/develop-full` for the full development workflow
