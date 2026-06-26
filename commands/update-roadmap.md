---
name: update-roadmap
description: Update roadmap (docs/roadmap.html) and vision/strategy docs to match current GitHub Issue states. Portable across projects.
user-invocable: true
category: core
---

# Update Roadmap

Sync `docs/roadmap.html` and the project's vision/strategy document with current GitHub Issue states. Works on any project that follows the standard roadmap HTML structure (4 sections: dependency graph, execution timeline, Gantt chart, key notes).

Context: $ARGUMENTS

## Phase 1: Discover Roadmap Files

```bash
# Find the roadmap HTML and vision/strategy markdown
ls docs/roadmap.html 2>/dev/null
ls docs/vision-and-strategy.md docs/VISION.md 2>/dev/null
```

If `docs/roadmap.html` does not exist, stop and tell the user. If neither vision file exists, proceed with roadmap.html only.

## Phase 2: Extract Referenced Issues

Parse `docs/roadmap.html` to find all issue numbers referenced in the file.

```bash
grep -oP '#\d+' docs/roadmap.html | sort -t'#' -k2 -n -u
```

## Phase 3: Fetch Current Issue States

For each referenced issue, fetch the current state from GitHub:

```bash
# Get all referenced issue numbers and their states in one call
gh issue list --state all --limit 200 --json number,state,title --jq '.[] | "\(.number) \(.state) \(.title)"'
```

Compare against what's marked as done in the roadmap. Build two lists:
- **Newly closed**: Issues marked open/active in roadmap but closed on GitHub -- these need to be marked done
- **Reopened**: Issues marked done in roadmap but open on GitHub -- flag these for the user

## Phase 4: Update roadmap.html

For each newly closed issue, apply changes to ALL FOUR sections of `docs/roadmap.html`:

### Section 1: Mermaid Dependency Graph
Change the node's class to `:::done` and append `(DONE)` to the label:
```
BEFORE: I203["#203 Security + CSP\nopen redirect, onclick"]:::high
AFTER:  I203["#203 Security + CSP\n(DONE)"]:::done
```

#### Vertical Layout Preservation
The graph uses multiple small Mermaid diagrams stacked via HTML, not one giant diagram:
- **Critical path**: one diagram with tiers as subgraphs connected by edges
- **Parallel tracks**: separate diagrams in a `parallel-grid` CSS 2-column layout
- **Independent items**: HTML `task-row` lists (no Mermaid needed)
- **Cross-group deps**: noted textually in a `notes cross-deps` box
- **Max 3-4 child subgraphs** side-by-side within a single diagram

### Section 2: Execution Timeline
Add `done` class to the `task-row` div and replace badges with `badge-done`:
```html
BEFORE: <div class="task-row">
          <span class="task-badge badge-autonomous">SOVA</span>
          <span class="task-badge badge-high">high</span>
AFTER:  <div class="task-row done">
          <span class="task-badge badge-done">done</span>
```

Remove dependency arrows (`dep-arrow` divs) that reference completed issues, since those dependencies are now satisfied.

### Section 3: Gantt Chart
Add the `done` keyword:
```
BEFORE: :crit, t203, 2026-06-19, 3d
AFTER:  :done, t203, 2026-06-18, 1d
```
Update the date to the actual completion date if known (from git log or issue close date).

### Section 4: Key Notes
- Move completed issue numbers to the "Done" / "Recently completed" list
- Remove them from the active agent strategy lists
- Update counts

### Subtitle date
Update the date in the subtitle element to today's date.

## Phase 5: Update Vision/Strategy Document

If a vision/strategy markdown file exists (`docs/vision-and-strategy.md` or `docs/VISION.md`):

1. Read the roadmap section
2. For each newly closed issue, mark the corresponding line item as done with strikethrough:
   ```
   BEFORE: - PWA support (manifest, service worker, icons)
   AFTER:  - ~~PWA support (manifest, service worker, icons)~~ (done: #49)
   ```
3. Update any stale infrastructure references (e.g., "Nginx" -> "Caddy" if deployment changed)
4. Do NOT rewrite prose, strategy, or vision sections -- only update factual status

## Phase 6: Verify and Report

```bash
git diff --stat docs/roadmap.html docs/vision-and-strategy.md docs/VISION.md 2>/dev/null
```

Report what changed:

```text
## Roadmap Updated

### Issues marked done
- #NNN: <title>
- #NNN: <title>

### Issues reopened (flagged)
- #NNN: <title> -- marked done in roadmap but OPEN on GitHub

### Files modified
- docs/roadmap.html: N issues updated across 4 sections
- docs/vision-and-strategy.md: N line items marked done

### No changes needed
- (list if everything was already in sync)
```

## Rules

- **Read before edit** -- always read the current file content before modifying
- **All four sections** -- when updating roadmap.html, update dependency graph, timeline, Gantt, AND key notes. Missing one creates inconsistency
- **Preserve structure** -- match existing format, indentation, and conventions
- **No prose changes** -- only update issue status, dates, and counts
- **Idempotent** -- running twice produces no diff
- **Flag conflicts** -- if an issue is done in roadmap but open on GitHub, flag it rather than silently reverting
- **Don't stage or commit** -- leave changes unstaged for the user to review
- NEVER use emojis in any output
