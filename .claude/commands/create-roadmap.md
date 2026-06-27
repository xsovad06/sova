---
name: create-roadmap
description: Generate a visual roadmap (docs/roadmap.html) from GitHub Issues -- dependency graph, timeline, Gantt chart, key notes. Portable across projects.
user-invocable: true
category: core
inputs:
  - project_dir
  - grouping_strategy
outputs:
  - docs/roadmap.html
---

# Create Roadmap

Generate a self-contained visual roadmap HTML page from GitHub Issues. Produces a dark-themed page with four sections: Mermaid dependency graph, execution timeline, Gantt chart, and key notes.

Context: $ARGUMENTS

## Phase 1: Discover Project Context

```bash
gh repo view --json nameWithOwner,description --jq '"\(.nameWithOwner) -- \(.description)"'
ls docs/roadmap.html 2>/dev/null && echo "EXISTS: docs/roadmap.html already exists"
```

If `docs/roadmap.html` already exists, ask the user whether to **regenerate** (overwrite) or **abort** (use `/update-roadmap` instead).

## Phase 2: Fetch Issues and Metadata

```bash
gh issue list --state all --limit 500 --json number,title,state,labels,milestone,body \
  --jq '.[] | {number, title, state, labels: [.labels[].name], milestone: .milestone.title}'
```

From each issue, extract:
- **Number, title, state** (open/closed)
- **Labels** for categorization (`type:`, `priority:`, `area:` prefixes)
- **Milestones** for phasing
- **Dependencies** from issue body: "depends on #N", "blocked by #N", "after #N"

## Phase 3: Organize Into Groups

Organize issues into logical groups. Priority order for grouping strategy:

1. **Milestones** -- if issues have milestones, use them as tier/phase names
2. **Labels** -- group by `area:` or `type:` labels
3. **User input** -- if no clear grouping, ask the user to define groups

For each group, determine:
- **Sequential** (depends on another group) -- connected with `-->` arrows
- **Parallel** (independent work) -- chained with `~~~` invisible links
- **Issue classification** from labels:
  - Closed -- done (gray, dashed)
  - `priority:critical` -- critical (red)
  - `priority:high` -- high (amber)
  - `priority:medium` -- medium (blue)
  - Autonomous/agentic -- autonomous (green)
  - Hybrid/guided -- guided (purple)
  - Human-only -- human (amber)

Within each group, identify chains (sequences of dependent issues) and standalone issues.

## Phase 4: Generate docs/roadmap.html

```bash
mkdir -p docs
```

Generate the HTML file with ALL four sections. Follow the template structure and rules below.

### HTML Structure

```html
<!-- Maintenance comment header (see below) -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{Project} Roadmap</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>/* CSS -- see palette below */</style>
</head>
<body>
  <h1>{Project} Roadmap</h1>
  <p class="subtitle">Dependency graph and execution plan -- target {milestone} -- updated {YYYY-MM-DD}</p>

  <!-- Section 1: Dependency Graph (Mermaid flowchart) -->
  <!-- Section 2: Execution Timeline (HTML grid) -->
  <!-- Section 3: Gantt Timeline (Mermaid gantt) -->
  <!-- Section 4: Key Notes (HTML cards) -->

  <script>
    mermaid.initialize({
      startOnLoad: true,
      securityLevel: 'loose',
      fontFamily: 'Inter, system-ui, sans-serif',
      theme: 'dark',
      themeVariables: {
        primaryColor: '#3b82f6',
        primaryTextColor: '#e2e8f0',
        lineColor: '#64748b',
        secondaryColor: '#1e293b',
        tertiaryColor: '#334155',
        fontSize: '13px'
      },
      flowchart: {
        useMaxWidth: true,
        htmlLabels: true,
        curve: 'basis',
        padding: 15,
        nodeSpacing: 30,
        rankSpacing: 40
      },
      gantt: {
        useMaxWidth: true,
        leftPadding: 120,
        barHeight: 22,
        barGap: 4,
        topPadding: 30,
        numberSectionStyles: 4
      }
    });
  </script>
</body>
</html>
```

### CSS Color Palette

Use these CSS variables and generate a complete stylesheet following this design system:

```css
:root {
  --bg: #0f172a;
  --surface: #1e293b;
  --border: #334155;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --brand: #3b82f6;
  --brand-light: #60a5fa;
  --green: #22c55e;
  --amber: #f59e0b;
  --red: #ef4444;
  --purple: #a855f7;
}
```

Key CSS classes to generate:
- `body` -- dark background, Inter font, 2rem padding
- `.diagram-container` -- surface background, border, 12px radius, overflow-x auto
- `.legend` -- flex row of colored dots with labels
- `.timeline` -- CSS grid (120px label | 1fr content) with vertical line
- `.task-row` -- flex row with number, title, badge(s), hover highlight
- `.task-badge` -- small rounded pill (`badge-autonomous`, `badge-guided`, `badge-human`, `badge-critical`, `badge-high`, `badge-done`)
- `.task-row.done` -- 0.5 opacity, strikethrough title
- `.dep-arrow` -- small muted text showing dependency
- `.notes` -- surface card for key notes, two-column grid at desktop
- `@media print` -- light theme overrides for printing

Import Google Fonts: `Inter:wght@400;500;600;700`.

### Dependency Graph -- Vertical Layout via Multiple Diagrams

The dependency graph MUST grow vertically. Use **multiple small Mermaid diagrams** stacked via HTML block flow, NOT one giant diagram:

1. **Critical path** -- one `<pre class="mermaid">` with all sequential tiers connected by edges. Tiers are subgraphs, chains are nested subgraphs within tiers. Max 3-4 chains side-by-side per tier.
2. **Parallel tracks** -- each independent group gets its OWN `<pre class="mermaid">` block, placed in a 2-column CSS grid (`class="parallel-grid"`). Good for groups with internal dependencies (Health Audit, Knowledge System, Security).
3. **Independent items** -- groups with no dependencies (GFI, deferred) use HTML `task-row` lists instead of Mermaid. Simpler and more compact.
4. **Cross-group dependencies** -- noted textually in a `<div class="notes cross-deps">` box, since they span separate diagrams.

**Critical path pattern:**
```
flowchart TD
  subgraph PHASE1["Phase 1: Name"]
    direction TB
    subgraph CHAIN_A["Chain A"]
      direction TB
      N1["#1 Task"]:::autonomous --> N2["#2 Task"]:::autonomous
    end
    subgraph CHAIN_B["Chain B"]
      direction TB
      N3["#3 Task"]:::guided --> N4["#4 Task"]:::done
    end
  end
  subgraph PHASE2["Phase 2: Name"]
    direction TB
    ...
  end
  PHASE1 --> PHASE2
```

**Parallel track pattern (2-col grid):**
```html
<div class="parallel-grid">
  <div class="diagram-container">
    <div class="grid-label">Group Name</div>
    <pre class="mermaid">
flowchart TD
  N1["#1 Task"]:::autonomous --> N2["#2 Task"]:::autonomous
  classDef autonomous fill:...
    </pre>
  </div>
  <div class="diagram-container">
    <div class="grid-label">Another Group</div>
    <pre class="mermaid">...</pre>
  </div>
</div>
```

**Independent items pattern (HTML list):**
```html
<div class="diagram-container">
  <div class="task-row">
    <span class="task-number">#42</span>
    <span class="task-title">Task description</span>
    <span class="task-badge badge-autonomous">agentic</span>
  </div>
</div>
```

**CSS classes to include:** `.subsection-title` (section headers), `.parallel-grid` (2-col grid), `.grid-label` (group header inside diagram-container), `.cross-deps` (notes box for cross-group deps).

**Node ID convention:** `I{issue_number}` (e.g., `I42["#42 Add login page"]`)

**Node labels:** max 2-3 lines using `\n`. For done issues, replace description with `(DONE)`.

### Mermaid classDef Definitions

Include in EVERY Mermaid flowchart block:
```
classDef critical fill:#7f1d1d,stroke:#ef4444,stroke-width:2px,color:#fecaca
classDef high fill:#78350f,stroke:#f59e0b,stroke-width:1.5px,color:#fef3c7
classDef medium fill:#1e3a5f,stroke:#3b82f6,stroke-width:1px,color:#bfdbfe
classDef autonomous fill:#14532d,stroke:#22c55e,stroke-width:1.5px,color:#bbf7d0
classDef guided fill:#3b0764,stroke:#a855f7,stroke-width:1px,color:#e9d5ff
classDef human fill:#78350f,stroke:#f59e0b,stroke-width:1.5px,color:#fef3c7
classDef done fill:#1e293b,stroke:#475569,stroke-width:1px,color:#64748b,stroke-dasharray:5
```

### Legend Colors

```
Critical priority -- red dot (#ef4444)
High priority -- amber dot (#f59e0b)
Medium priority -- blue dot (#3b82f6)
Agentic -- green dot (#22c55e)
Hybrid / guided -- purple dot (#a855f7)
Done -- gray dot (#64748b)
```

### Section 2: Execution Timeline

HTML grid layout with phases as rows:
- Left column: phase label (tier name, date range)
- Right column: task-row items with number, title, badge(s)
- Phase dividers between groups
- Dependency arrows as `<div class="dep-arrow">depends on #N</div>`

### Section 3: Gantt Chart

Mermaid gantt diagram with sections matching the timeline phases:
```
gantt
  title {Project} Roadmap
  dateFormat YYYY-MM-DD
  axisFormat %b %d

  section Phase 1
    #42 Task title    :done, t42, 2026-01-15, 3d
    #43 Another task   :t43, after t42, 2d
```

For completed issues, add the `done` keyword. Use `after tN` for dependencies.

Override font size for Gantt:
```
%%{init: {'theme': 'dark', 'themeVariables': {'fontSize': '12px'}}}%%
```

### Section 4: Key Notes

Two-column grid with summary cards:
- **Left card**: critical path summary (phases, key milestones, recently completed)
- **Right card**: execution strategy (agent strategy, issue counts by category)

### Maintenance Guide Block

Include the maintenance guide as a non-rendering `<script>` block (NOT an HTML comment -- `--` inside HTML comments breaks parsing). Place it just before the closing mermaid init `<script>`:

```html
<script type="text/x-maintenance" id="maintenance-guide">
  ============================================================================
  {PROJECT} ROADMAP -- Maintenance Guide
  ============================================================================

  HOW TO UPDATE
  ~~~~~~~~~~~~~

  1. ISSUE COMPLETED
     - Dependency graph: change node class to :::done, append (DONE) to label
     - Timeline: add class="done" to task-row, change badge to badge-done
     - Gantt: add "done" keyword after the colon
     - Key Notes: move to "Recently completed" list

  2. NEW ISSUE ADDED
     - Add to ALL FOUR sections
     - Use correct classDef and badge class
     - If it has deps, add arrows in graph and dep-arrow in timeline

  3. VERTICAL LAYOUT
     - Sequential groups: connect with arrows (-->)
     - Independent groups: chain with invisible links (~~~)
     - Never leave a subgraph unconnected -- it floats horizontally
     - Parent subgraphs use direction TB for internal vertical flow
     - Max 3-4 child subgraphs side-by-side within a parent

  STYLE REFERENCE
  ~~~~~~~~~~~~~~~
  classDef: critical (red), high (amber), medium (blue),
            autonomous (green), guided (purple), human (amber), done (gray dashed)
  Badges:   badge-critical, badge-high, badge-autonomous,
            badge-guided, badge-human, badge-done

  Use /update-roadmap to sync with current GitHub Issue states.
  ============================================================================
</script>
```

IMPORTANT: Never use HTML comments (`<!-- -->`) for blocks containing `--`, `-->`, `~~~`, or other sequences that break HTML comment parsing. Use `<script type="text/x-maintenance">` instead -- browsers ignore unknown script types completely.

## Phase 5: Verify and Report

```bash
open docs/roadmap.html 2>/dev/null || xdg-open docs/roadmap.html 2>/dev/null
```

Report:
```
## Roadmap Created

- **File**: docs/roadmap.html
- **Issues included**: N total (M open, K closed)
- **Groups**: {list of group names}
- **Dependencies mapped**: N dependency arrows

Open in browser to verify the layout.
Use `/update-roadmap` to sync with GitHub Issues in the future.
```

## Rules

- **Vertical layout** -- the dependency graph MUST use parent subgraphs + `~~~` invisible links to grow vertically, not horizontally
- **All four sections** -- always generate dependency graph, timeline, Gantt, AND key notes
- **Self-contained HTML** -- only external deps: Mermaid CDN + Google Fonts CDN
- **Dark theme** -- use the CSS variable palette above
- **Consistent node IDs** -- `I{number}` format for issue nodes
- **Short labels** -- Mermaid node labels max 2-3 lines with `\n`
- **Print-friendly** -- include `@media print` overrides
- **Read before edit** -- if regenerating, read the existing file first
- **Don't stage or commit** -- leave unstaged for user review
- NEVER use emojis in any output
