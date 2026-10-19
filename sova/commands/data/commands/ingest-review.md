---
name: ingest-review
description: Ingest PR review feedback into agent memory for continuous learning. Provide PR number.
user-invocable: true
category: learning
inputs:
  - pr_number
outputs:
  - feedback_ingested
---

# Ingest PR Review Feedback

Parse structured review findings from TaskRun records and update agent memory.

## Instructions

1. Get the PR number from `$ARGUMENTS`. If empty, ask the user. **Validate it
   is a positive integer before using it anywhere below**: reject non-numeric
   input and ask the user to clarify rather than passing raw argument text
   into generated code. `<PR_NUMBER>` in every snippet in this command means
   this validated integer, never the raw `$ARGUMENTS` text.

2. Query the database for reviewer TaskRun records linked to this PR. Substitute
   the validated integer for `<PR_NUMBER>`: it becomes part of a Python
   expression evaluated via `python3 -c`, so unvalidated text here is a code
   injection risk, not just a bad query:
   ```bash
   # Find the reviewer run's handoff data
   python3 -c "
   import asyncio, json, os
   os.environ.setdefault('SOVA_DATABASE_URL', 'sqlite+aiosqlite:///.claude/sova.db')
   from sova.db.session import init_db, get_session
   from sova.db.models import TaskRun
   from sqlalchemy import select

   async def main():
       await init_db(run_migrations=False)
       async with await get_session() as session:
           stmt = select(TaskRun).where(
               TaskRun.pr_number == <PR_NUMBER>,
               TaskRun.role.in_(['reviewer', 'command:review-pr']),
               TaskRun.status == 'done',
           ).order_by(TaskRun.id.desc()).limit(1)
           run = (await session.execute(stmt)).scalar_one_or_none()
           if not run or not run.handoff_json:
               print('NO_FINDINGS')
               return
           print(json.dumps(run.handoff_json, indent=2))

   asyncio.run(main())
   "
   ```

3. If the output is `NO_FINDINGS`, do not stop here: the SOVA reviewer may simply not have run a TaskRun for this PR while external reviewers (CodeRabbit, human reviewers) still left comments. Continue to step 4 (there is nothing to parse from this source, so skip it) and step 5. Only report "No reviewer findings found for PR #N (no SOVA reviewer TaskRun and no external review comments)" and stop if step 5 also finds nothing.

4. Parse the `pending_findings` array from the handoff JSON (skip this step if step 2 returned `NO_FINDINGS`). Each finding has:
   - `file`: file path
   - `line`: line number
   - `severity`: 1-10 score
   - `category`: type of issue (bug, style, performance, etc.)
   - `description`: what the issue is
   - `suggestion`: how to fix it

5. Also fetch external review comments (CodeRabbit, human reviewers). Review
   bodies and inline (line-anchored) comments come from two different sources:
   `gh pr view` only returns top-level review bodies, and inline comments must
   be fetched separately via the PR review-comments API:
   ```bash
   gh pr view <PR_NUMBER> --json reviews,comments --jq '(.reviews[] | {author: .author.login, state: .state, body: .body}), (.comments[] | {author: .author.login, body: .body})'
   gh api --paginate repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq '.[] | {author: .user.login, path: .path, line: .line, body: .body}'
   ```
   `--paginate` matters here: a CodeRabbit review on a sizable PR routinely exceeds the API's default 30-per-page limit, and without it everything past the first page is silently dropped.

   **Normalize each external comment into the same `{file, line, severity, category, description}` shape step 4's findings use**, so steps 6-9 have a defined input on this path too (an external comment has no `severity`/`category` field of its own):
   - `file`/`line`: the inline comment's `path`/`line` fields above; a top-level review body with no inline anchor has neither.
   - `severity`/`category`: derive from a stated reviewer label where one exists (CodeRabbit's `_Critical_`/`_Major_`/`_Minor_` or its `_Potential issue_` marker map to roughly 8/5/4 and category `bug`), otherwise default to severity 4, category `style` for unlabelled human prose. Do not use LLM summarization to invent a severity the comment never stated (the Rules section below forbids LLM summarization for parsing structured data; this default is how an unstructured comment still gets a defined, documented value rather than an improvised one).
   - `description`: the comment body itself.

6. Classify findings into memory categories:
   - **Severity >= 7**: likely a "common_mistake", so check `.claude/agent-memory/common-mistakes.md` (or the cookbook's mistakes section) for existing entries
   - **Severity 1-6 with "style" or "naming" category**: "style preference"
   - **Severity 4-6 with "bug" category**: "review pattern" worth codifying
   - **Repeated patterns across findings**: "review pattern" worth codifying
   - **Test-related findings**: "test coverage gap"

7. Read the existing memory: `.claude/agent-memory/common-mistakes.md` if present, and the cookbook (the relevant domain files of `.claude/agent-memory/cookbook/` when that directory exists, otherwise `.claude/agent-memory/cookbook.md`).

8. Before writing anything, check whether each lesson is already recorded in
   a higher tier: `.claude/rules/*.md`, `AGENTS.md`, and `docs/*-guidelines.md`.
   - **Already a rule, and the PR followed it**: skip it; nothing to learn.
   - **Already a rule, but the PR still broke it**: the rule is not being
     enforced. Record it in the mistakes section as an enforcement gap naming
     the rule and the command that should have caught it (for example "review
     did not check X"), instead of restating the rule.
   - **Not recorded anywhere**: continue to step 9.

9. Update the cookbook in the PRIMARY checkout (resolve `PRIMARY_ROOT` from `git rev-parse --git-common-dir`; a worktree's copy of `.claude/agent-memory/` may be gitignored and is deleted with the worktree): `$PRIMARY_ROOT/.claude/agent-memory/cookbook/<domain>.md` when that directory exists (domains listed in its `README.md`), otherwise `$PRIMARY_ROOT/.claude/agent-memory/cookbook.md`:
   - Append new findings to the matching domain (no duplicates), one bullet each:
     `- **<rule as a short imperative>** (PR #123; confirmed: 1): <rule and why>.`
     File by topic, never under a per-PR heading: per-PR sections cannot be
     deduplicated or promoted. Cite the PR inline.
   - A recurring mistake goes in `$PRIMARY_ROOT/.claude/agent-memory/common-mistakes.md` as `- **<mistake>** (occurrences: N): <how to catch it in a diff>.`
     If it already exists, increment its count rather than adding a duplicate;
     the `review` workflow checks every diff against this list.
   - If a finding is high-impact (severity >= 8) and `.claude/agent-memory/MEMORY.md` exists, also add it there

10. Report what was learned and which files were updated.

## Cross-References

- **Review learnings during the PR**: the `address-pr` workflow's knowledge-capture step records each review round's lessons in the primary checkout's cookbook; run this command afterwards only for anything that step missed
- **Extract broader knowledge**: Run `/extract-knowledge` for session-wide lessons

## Rules

- Only record actionable, specific lessons, not generic advice
- Parse structured data from DB records; do NOT use LLM summarization
- NEVER use emojis in any output
