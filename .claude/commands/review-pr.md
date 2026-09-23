---
name: review-pr
description: Review another person's pull request -- fetch, analyze, and post structured review on GitHub. Provide PR number.
user-invocable: true
category: pr
inputs:
  - pr_number
outputs:
  - review_findings
  - review_verdict
---

# Review PR

Act as a senior engineer reviewing a teammate's pull request. Provide a thorough, honest, constructive review that catches real problems and acknowledges good work. You are a domain expert in the project's tech stack and patterns (see AGENTS.md).

**CRITICAL: Complete ALL Steps.** You MUST execute through Step 7 (posting the review on GitHub) before producing any final summary. Post the review directly via the GitHub API. Do NOT ask for confirmation or approval before posting. A text-only response (without a tool call) may cause the process to exit, so always post first, then summarize.

PR: $ARGUMENTS

## 1. Fetch PR State

Gather all PR data in parallel:

```bash
# Metadata
gh pr view <PR_NUMBER> --json title,body,author,state,additions,deletions,files,commits,reviewRequests,labels,baseRefName,headRefName,headRefOid,statusCheckRollup

# Full diff (also saved: Step 7 needs it to place inline comments).
# Temp files are per-PR: several reviews can run concurrently on one machine,
# and a shared path would pair one PR's findings with another PR's diff.
gh pr diff <PR_NUMBER> | tee "/tmp/sova-review-<PR_NUMBER>-diff.txt"

# Commits
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/commits --jq '.[] | "\(.sha) \(.commit.message)"'

# Top-level comments
gh pr view <PR_NUMBER> --json comments --jq '.comments[] | "---\n\(.author.login) (\(.createdAt)):\n\(.body)\n"'

# Inline review comments
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments --jq '.[] | "---\n\(.user.login) on \(.path):\(.line // .original_line) (\(.created_at)):\n\(.body)\n"'

# Reviews
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --jq '.[] | "\(.user.login) (\(.submitted_at)): \(.state)\n\(.body)\n"'

# CI checks
gh pr checks <PR_NUMBER>
```

Extract: author, linked issue, `headRefOid` (the commit under review; it becomes the `sha` field in Step 6), whether AI-generated (bot prefixes, agent comments).

**CI failures do NOT block the review.** If CI checks are failing, note the failures briefly in the review summary (what failed, likely cause if obvious) but proceed with the full code review. CI issues are a separate concern -- the review's job is to evaluate code quality, correctness, and design. A PR with failing CI still needs its code reviewed.

## 1.5. Catalog Existing Bot Findings (if external reviews are configured)

Skip this step if the project does not use automated reviewers (no `[external_reviews]` section in `sova.toml`).

Before starting your own analysis, extract actionable findings already posted by automated reviewers (CodeRabbit, SonarCloud, Dependabot, etc.) from the reviews and inline comments fetched in Step 1. Identify bots by `user.type == "Bot"` or known bot logins.

For each bot finding, record the source, file:line, and a one-line description. Hold this as a reference table for deduplication in Step 4.

## 2. Cross-Reference Comment Threads vs Actual Code

For AI-generated PRs where agents may claim to have pushed fixes that never landed:

For each thread where someone said "Fixed in commit X":
1. Check if commit X exists in the current commit list
2. Verify the actual diff reflects the claimed change
3. Build a **ghost commit table** of any claimed-but-missing fixes

If ghost commits are found, this is a **blocking finding**.

## 3. Read Changed Files in Full

For every file touched in the diff:
- Read the **entire file** on the PR branch to understand surrounding context
- Identify the module's role in the architecture
- Note related files that interact with the changed code

Read related files as needed -- review with full understanding, not in isolation.

## 4. Deep Analysis

**Bot deduplication** (when Step 1.5 was performed): before recording a finding, check the bot findings table from Step 1.5. If a bot already flagged the same issue (same file, same concern), do NOT create a standalone finding -- note it for the "Confirmed Bot Findings" section in Step 6 instead. If you disagree with a bot, record your disagreement as a regular finding.

Review across these dimensions, in priority order. Reference `AGENTS.md` and `docs/*-guidelines.md` for project-specific rules.

### Security (Critical)
- Auth/permission checks correct?
- Tenant/scope isolation -- no cross-tenant data leaks?
- Input validation on all user-provided data?
- No injection risks?

### Correctness (Critical)
- Does the logic solve the stated problem?
- Edge cases: empty inputs, missing params, boundary values?
- Backward compatibility -- existing behavior still works?
- Error paths handled?

### Consistency (High)
- New code follows the same patterns as existing code?
- Similar operations handled the same way?
- Error messages consistent with existing format?

### Performance (High)
- N+1 query patterns?
- Queries inside loops?
- Large datasets without pagination?

### Test Coverage (Medium)
- New code paths covered?
- Edge cases and error paths tested?
- Tests assert meaningful behavior?

### Code Quality (Low)
- Business logic in the right layer?
- DRY -- duplicated logic?
- Dead code, unused imports?

## 5. Check Scope

- Does the PR include unrelated changes? Flag them.
- Is the PR too large? Suggest splitting if >500 lines of non-spec/non-test changes.
- Are all changes covered by the ticket scope?

## 6. Format Findings via Shared Formatter

Collect your findings into a JSON object. Save it to a temporary file:

```bash
cat > "/tmp/sova-review-<PR_NUMBER>-findings.json" <<'REVIEW_JSON'
{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 42,
      "severity": 7,
      "category": "bug",
      "description": "Concise description of the issue",
      "suggestion": "Specific fix recommendation"
    }
  ],
  "summary": "### PR Summary\nOne paragraph: what the PR does, who authored it, how many commits/files.\n\n### Ghost Commits\n(table if any, omit section if none)\n\n### Confirmed Bot Findings\n(if Step 1.5 was performed, omit if none)",
  "positives": ["Good thing 1", "Good thing 2"],
  "sha": "<headRefOid from Step 1>"
}
REVIEW_JSON
```

**JSON field requirements:**
- `findings`: array of objects with `file`, `line` (nullable), `severity` (1-10 integer), `category`, `description`, `suggestion` (empty string if none)
- `summary`: the PR Summary paragraph, optionally followed by Ghost Commits and Confirmed Bot Findings sections (use `\n` for newlines)
- `positives`: 2-3 things the code does well (omit key or pass empty array to skip the section)
- `sha`: the full `headRefOid` fetched in Step 1. It anchors the verdict to the reviewed commit, so the dashboard can tell a verdict that still stands from one superseded by later pushes. Omit only if unknown (the verdict is then treated as current until addressed).

**Scoring guidance**: bump to 3+ (not 1-2) if the finding removes code/duplication, improves error handling, fixes misleading docs, or eliminates dead code. Reserve 1-2 only for purely subjective preferences (naming, comment wording, formatting not caught by linter).

Format the review body through the shared SOVA formatter:

```bash
REVIEW_BODY=$(python3 -c "import sys; from sova.roles._review_format import format_from_json; print(format_from_json(sys.stdin.read()))" < "/tmp/sova-review-<PR_NUMBER>-findings.json") || REVIEW_BODY=""
```

The formatter produces: `<!-- sova-review: {verdict} sha={sha} -->` marker, `## Review:` heading, severity-sorted findings with `[LABEL N/10]` scores, `### What's Done Well` section (if positives provided), and `### Verdict` section. The verdict is determined automatically from the highest finding severity (7+ = block, any lower non-zero severity = revise, no findings = approve).

**Fallback**: if `python3` fails (SOVA not installed, import error, malformed JSON), `REVIEW_BODY` will be empty. In that case, write the review body manually: first line `<!-- sova-review: {verdict} sha={headRefOid} -->`, then `### Findings` heading, then findings as `- **[LABEL N/10]** [category] \`file:line\`: description. Fix: suggestion`. Determine the verdict from the highest severity in your JSON: 7+ = block, any findings (severity 1-6) = revise, no findings = approve. A finding left as `approve` causes the dashboard to show "Integrate PR" and skip address-review entirely.

## 7. Post Review on GitHub

Post the review immediately. Do NOT ask for confirmation.

Use the event that matches your verdict:

- **Approve** verdict: use `event=APPROVE`
- **Request changes** verdict: use `event=REQUEST_CHANGES`
- **Comment only** verdict: use `event=COMMENT`

Post findings as **inline review comments**, not just a summary body. Every
finding that lands on a line present in the diff becomes its own review thread,
which is what makes the remaining work trackable: the dashboard counts
unresolved threads, so a PR shows at a glance which findings are still open and
an address cycle closes them one by one. A body-only review leaves nothing to
resolve. Findings that do not map to a diff line stay in the body.

The payload is built by the same SOVA helper the Reviewer role uses, so a
command-driven review and an autonomous one produce identical output:

```bash
# Set EVENT based on your verdict above: APPROVE, REQUEST_CHANGES or COMMENT
export EVENT=REQUEST_CHANGES

build_payload() {
  EVENT="$1" python3 -c "import os, sys; from sova.roles._review_comments import build_review_payload_from_json; print(build_review_payload_from_json(open(sys.argv[1]).read(), open(sys.argv[2]).read(), os.environ['EVENT']))" \
    "/tmp/sova-review-<PR_NUMBER>-findings.json" "/tmp/sova-review-<PR_NUMBER>-diff.txt" \
    > "/tmp/sova-review-<PR_NUMBER>-payload.json"
}

build_payload "$EVENT"
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --method POST --input "/tmp/sova-review-<PR_NUMBER>-payload.json"
```

Three fallbacks, in order, each mirroring what the Reviewer role does:

1. **Self-review** (422 mentioning "your own pull request"): GitHub rejects
   `APPROVE` and `REQUEST_CHANGES` on your own PR. This path is not a rare
   corner case: it fires on every self-review. Rebuild with `build_payload
   COMMENT`, then append the required note to the payload's `body` field
   before retrying:
   ```bash
   build_payload COMMENT
   P="/tmp/sova-review-<PR_NUMBER>-payload.json"
   python3 -c "import json, sys; d=json.load(open(sys.argv[1])); d['body']+='\n\n(Posted as comment -- GitHub does not allow self-reviews with formal approval/rejection state.)'; json.dump(d, open(sys.argv[1],'w'))" "$P"
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --method POST --input "$P"
   ```
2. **Rejected inline comment** (422 naming a line or position): one finding
   pointed at a line GitHub will not accept. Strip the comments and retry so the
   review still lands:
   ```bash
   P="/tmp/sova-review-<PR_NUMBER>-payload.json"
   python3 -c "import json, sys; d=json.load(open(sys.argv[1])); d['comments']=[]; json.dump(d, open(sys.argv[1],'w'))" "$P"
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews --method POST --input "$P"
   ```
3. **Helper unavailable** (SOVA not importable or the diff file missing, so the
   payload file is empty or invalid JSON): fall back to the body-only form,
   using the `REVIEW_BODY` from Step 6:
   ```bash
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/reviews -f event="$EVENT" -f body="$REVIEW_BODY"
   ```

For any other 422, report the failure instead of silently falling back.

Report the review URL and the inline comment count after posting.

## Cross-References

- **Reviewing your own code?** Use `/review` instead (self-review with auto-fix)
- **Need to address review comments on your PR?** Use `/address-pr`

## Rules

- Be constructive and specific. Every finding must have a concrete suggestion.
- Do not nitpick style if the code passes the project's linter.
- Do not invent problems. If the code is solid, say so.
- Do not review generated files (migrations, lock files) unless they look wrong.
- Respect the author's approach -- suggest alternatives only when there's a concrete problem.
- Do not restate findings already posted by bot reviewers (CodeRabbit, SonarCloud, etc.). Acknowledge agreement in the "Confirmed Bot Findings" section instead.
- Keep the review concise.
- NEVER use emojis or icons in the review output.
