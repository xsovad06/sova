---
name: quarterly-report
description: Generate a quarterly impact report from git and GitHub data.
user-invocable: true
---

# Quarterly Impact Report Generator

Generate a comprehensive quarterly impact report pulling data from git history and GitHub (PRs, Issues). Useful for goal progress updates, manager 1:1s, and promotion readiness.

## Parameters

The user may optionally specify:
- A quarter (e.g., "Q1 2026", "Q2 2026"). Default: current quarter based on today's date.
- A specific repo. Default: current repository.

Determine quarter date ranges:
- Q1: January 1 - March 31
- Q2: April 1 - June 30
- Q3: July 1 - September 30
- Q4: October 1 - December 31

## Data Collection

Run ALL of the following data collection commands in parallel:

### 1. Git Commits
```bash
git log --author="$(git config user.name)" --since="QUARTER_START" --until="QUARTER_END" --format="%H|%ad|%s" --date=short
```
Count total commits, unique days with commits.

### 2. Git Line Stats
```bash
git log --author="$(git config user.name)" --since="QUARTER_START" --until="QUARTER_END" --shortstat --format=""
```
Sum insertions and deletions.

### 3. GitHub PRs Authored (Merged)
```bash
gh pr list --author @me --state merged --limit 100 --json number,title,mergedAt,additions,deletions,changedFiles,createdAt
```
Filter to the quarter by mergedAt date. Calculate total PRs, additions/deletions, avg cycle time.

### 4. GitHub PRs Reviewed
```bash
gh search prs --reviewed-by @me --repo <OWNER/REPO> --limit 100 --json number,title,updatedAt,state
```
Filter to the quarter. Count total reviews given.

### 5. GitHub Issues Completed
```bash
gh issue list --assignee @me --state closed --limit 100 --json number,title,labels,closedAt
```
Filter to the quarter by closedAt date. Count by label/type. List each with number, title, and labels.

### 6. Open/In-Progress Work
```bash
gh issue list --assignee @me --state open --json number,title,labels,milestone
```

## Report Structure

---

### Quarterly Impact Report: [Quarter] [Year]

**Author:** [User Name] | **Team:** [Team Name]
**Period:** [Start Date] - [End Date]
**Generated:** [Today's Date]

---

#### Executive Summary
3-5 sentences summarizing the quarter's impact. Focus on business outcomes, not activity counts.

#### Deliverables
For each GitHub Issue completed, list:
- **#[NUMBER]** - Summary (Labels)

Group by theme if possible.

#### Code Impact

| Metric | Value |
|--------|-------|
| PRs Merged | N |
| Commits | N |
| Lines Added | +N |
| Lines Removed | -N |
| Files Changed | N |
| Avg PR Cycle Time | N days |
| Code Reviews Given | N |
| Active Coding Days | N |

#### Key Contributions
Highlight 3-5 most impactful items with brief context on business value.

#### Cross-Team Impact
Work that impacted other teams, reviews for others, or knowledge sharing.

#### Carrying Forward
In-progress work continuing into the next quarter.

---

## Cross-References

- **Daily context**: Run `/standup` for daily overview
- **Sprint planning**: Run `/sprint-plan` for current sprint context

## Rules

- No emojis or icons. Professional plain text only.
- Use concrete numbers, not vague qualifiers.
- Connect work to business outcomes, not just activity.
- Keep the executive summary under 5 sentences.
