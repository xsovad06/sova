# Issue Organization with Phase Milestones

This guide explains how to organize project issues using milestone-based phases so the SOVA dashboard Priority Queue displays them with colored badges, grouped sections, and correct priority ordering.

## How It Works

The dashboard Priority Queue reads the **milestone** field from each issue (via the task adapter) and uses it for visual grouping:

1. **Badge label**: extracted from the milestone title before the colon (e.g., `Phase 2: Dashboard Polish` -> `Phase 2`)
2. **Sort order**: numeric phase extracted via regex (`Phase N` or `PN`), lower numbers appear first
3. **Color**: each phase number (1-7) has a distinct color in the dashboard theme
4. **Within-phase sort**: issues within the same phase are sorted by priority labels (`priority:critical` > `priority:high` > `priority:medium` > `priority:low`)

Issues without a milestone show as `--` (gray) in a "No phase" section at the bottom.

## Color Map

| Milestone | Badge | Color | Typical Use |
|-----------|-------|-------|-------------|
| Phase 1: ... | Phase 1 | Red | Immediate / in-progress work |
| Phase 2: ... | Phase 2 | Yellow | Next up / planned |
| Phase 3: ... | Phase 3 | Blue | Medium-term |
| Phase 4: ... | Phase 4 | Purple | Lower priority |
| Phase 5: ... | Phase 5 | Green | Release gate / polish |
| Phase 6: ... | Phase 6 | Light blue | Future / deferred |
| Phase 7: ... | Phase 7 | Gray | Backlog / someday |
| (none) | -- | Dark gray | Unphased (contributor issues, etc.) |

## Naming Convention

Milestones must follow the pattern `Phase N: Short Description` or `PN: Short Description`:

```
Phase 1: Ship It
Phase 2: Dashboard Polish
P3: Pipeline Extensions
```

The regex `(?:Phase\s*|P)(\d+)` extracts the numeric phase. Names that don't match this pattern are treated as unphased.

## Recommended Starter Phases

For a **new project** adopting SOVA, start with these 4 milestones:

| Milestone | Purpose |
|-----------|---------|
| Phase 1: Now | Work actively in progress or starting this sprint |
| Phase 2: Next | Planned for the next sprint / iteration |
| Phase 3: Later | On the roadmap but not yet scheduled |
| Phase 4: Future | Ideas, research, deferred work |

Rename and expand as the project matures. For example, SOVA itself uses:

| Milestone | Purpose |
|-----------|---------|
| Phase 1: Start Now | Agent Runtime + Status Badges (Tier 1) |
| Phase 2: Dashboard Polish | Step Visualization + Kanban (Tier 2) |
| Phase 3: Pipeline Extensions | Spec Pipeline + Model Routing + Planner (Tier 3) |
| Phase 4: LLM Infrastructure | Ollama local model support (Tier 4) |
| Phase 5: Release | Velocity measurement, blog post, v0.1.0 tag |
| Phase 6: Future | A2A, event journal, YAML workflows, etc. |

## Creating Milestones

### GitHub (CLI)

```bash
gh api repos/OWNER/REPO/milestones -f title="Phase 1: Now" -f state=open
gh api repos/OWNER/REPO/milestones -f title="Phase 2: Next" -f state=open
gh api repos/OWNER/REPO/milestones -f title="Phase 3: Later" -f state=open
gh api repos/OWNER/REPO/milestones -f title="Phase 4: Future" -f state=open
```

### Assigning Issues

```bash
gh issue edit 42 --milestone "Phase 1: Now"
```

### Jira

Use **Fix Versions** named with the same `Phase N: Description` pattern. The Jira adapter reads `fixVersions[0].name` as the milestone equivalent.

### Linear

Use **Cycles** or **Projects** with phase-prefixed names. The Linear adapter (planned) will map these to the same badge system.

## Adapter Mapping

| Task Source | Phase Source | Field |
|-------------|-------------|-------|
| GitHub | Milestones | `issue.milestone.title` |
| Jira | Fix Versions | `issue.fields.fixVersions[0].name` |
| Linear | Cycles / Projects | (planned) |

The adapter ABC's `_parse_issue()` method extracts the milestone string. The dashboard's `_milestone_badge()` and `_extract_phase_order()` functions in `queue_service.py` handle display and sorting regardless of the adapter backend.

## Best Practices

- **Keep it simple**: 3-4 active phases is enough. More than 7 dilutes the visual signal.
- **Move issues, don't rename phases**: when work shifts priority, change the issue's milestone, not the phase name.
- **Leave contributor issues unphased**: good-first-issues and external contributor tasks work best without a phase milestone -- they appear in the neutral "No phase" section.
- **Close completed phases**: when all issues in a phase are done, close the milestone via the GitHub UI. This keeps the milestone picker clean.
- **Pair with the supervisor dashboard**: the `/supervisor` page shows an interactive D3.js dependency graph alongside milestones for the full picture.
- **One source of truth**: milestones drive the dashboard. Don't also use GitHub Projects V2 "Phase" field -- it causes confusion (the dashboard ignores it).

## Integration Points

- **`sova setup`**: (planned) the setup wizard will offer to create starter milestones during project onboarding
- **`sova triage`**: when triaging an issue, the agent can suggest a phase based on priority and complexity
- **`/spec` command**: spec generation can recommend a phase based on dependencies and urgency
- **Planner role**: the issueless planner can propose phase reassignments during sprint planning
