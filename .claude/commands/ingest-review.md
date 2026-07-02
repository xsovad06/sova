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

1. Get the PR number from `$ARGUMENTS`. If empty, ask the user.

2. Query the database for reviewer TaskRun records linked to this PR:
   ```bash
   # Find the reviewer run's handoff data
   python3 -c "
   import asyncio, json, os
   os.environ.setdefault('SOVA_DATABASE_URL', 'sqlite+aiosqlite://.claude/sova.db')
   from sova.db.session import init_db, get_session
   from sova.db.models import TaskRun
   from sqlalchemy import select

   async def main():
       await init_db(run_migrations=False)
       async with get_session() as session:
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

3. If the output is `NO_FINDINGS`, report "No reviewer findings found for PR #N" and stop.

4. Parse the `pending_findings` array from the handoff JSON. Each finding has:
   - `file`: file path
   - `line`: line number
   - `severity`: 1-10 score
   - `category`: type of issue (bug, style, performance, etc.)
   - `description`: what the issue is
   - `suggestion`: how to fix it

5. Also fetch external review comments (CodeRabbit, human reviewers):
   ```bash
   gh pr view <PR_NUMBER> --json reviews,comments --jq '.reviews[] | {author: .author.login, state: .state, body: .body}'
   ```

6. Classify findings into memory categories:
   - **Severity >= 7**: likely a "common_mistake" -- check `.claude/agent-memory/cookbook.md` for existing entries
   - **Severity 4-6 with "style" or "naming" category**: "style preference"
   - **Repeated patterns across findings**: "review pattern" worth codifying
   - **Test-related findings**: "test coverage gap"

7. Read existing memory file:
   - `.claude/agent-memory/cookbook.md`

8. Update `.claude/agent-memory/cookbook.md`:
   - Append new findings under the matching domain section (no duplicates)
   - If a mistake already exists, increment its `[Nx]` count
   - If a finding is high-impact (severity >= 8), also add it to `.claude/agent-memory/MEMORY.md`

9. Report what was learned and which files were updated.

## Cross-References

- **Run automatically after merge**: `/after-merge` includes this step
- **Extract broader knowledge**: Run `/extract-knowledge` for session-wide lessons

## Rules

- Only record actionable, specific lessons -- not generic advice
- Parse structured data from DB records; do NOT use LLM summarization
- NEVER use emojis in any output
