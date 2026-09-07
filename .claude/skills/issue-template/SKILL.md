---
name: issue-template
description: Canonical issue body structure for the SOVA tracker, using the exact headings compute_quality_score() scores, plus this repo's label taxonomy. Auto-activates when creating, drafting, or rewriting a GitHub issue for SOVA.
allowed_tools: Read, Grep, Glob, Bash
---

# SOVA Issue Body Template

When creating, drafting, or rewriting an issue for the SOVA tracker, produce the
body below. The headings are not stylistic: `compute_quality_score()` in
`sova/roles/triage.py` scores them deterministically. A body scoring below
`triage.min_quality_score` is rewritten by the `auto_enrich` LLM pass, or
downgraded to `needs_spec` when enrichment is disabled or falls short, so the
issue waits instead of shipping. An issue written with this template scores 8/8
and goes straight to the pipeline. The structure matches
`.github/ISSUE_TEMPLATE/task.md`, so the tracker template and this skill cannot
drift apart.

## Canonical Body

```markdown
## Objective
<One sentence: what needs to be done and why it matters.>

## Detailed Description
<The current behavior, the desired behavior, and the mechanism that gets from
one to the other. Name the functions, data, and call sites that change. Two to
six sentences beats a paragraph of background.>

## Acceptance Criteria
- [ ] <Binary, testable outcome.>
- [ ] <Second binary outcome.>
- [ ] All existing tests pass (`make check`)
- [ ] No new lint warnings (`make lint`)

## Files / Modules to Change
- [ ] `sova/module/file.py`: <what changes there>
- [ ] `tests/test_module.py`: <coverage added>

## Out of Scope / Constraints
- <What must NOT be touched, and any constraint the implementation must respect.>

## Dependencies
None

## References
- Related: #<issue-number>
- Code: `sova/module/file.py`
```

## Scoring Rules

`compute_quality_score()` awards 8 points:

| Points | Requirement |
|--------|-------------|
| 1 | `## Objective` heading |
| 1 | `## Detailed Description` heading |
| 2 | `## Acceptance Criteria` heading AND a literal `- [ ]` somewhere in the body |
| 1 | `## Files / Modules to Change` (or `## Files to Change`, or `## Files`) |
| 1 | `## Out of Scope / Constraints` (or `## Out of Scope`, `## Constraints`, `## Scope Boundaries`) |
| 1 | `## References` heading, or any issue ref, URL, or backticked file path |
| 1 | No LLM preamble or postamble |

Non-negotiable rules, in the order they break issues:

- **Every canonical section is exactly `##`.** The scorer's regex is `^##\s+`.
  A section written as `###`, or bolded as `**Objective**`, scores zero.
- **Acceptance criteria need at least one unchecked `- [ ]`.** A list of
  already-checked `- [x]` items fails `has_acceptance_criteria`, which costs 2
  points and caps the body at 6/8. `meets_threshold()` also requires it
  independently of the total, so no `triage.min_quality_score` is low enough to
  let the issue through without it.
- **Start the body directly at `## Objective`.** No conversational framing.
  Six line-initial patterns cost the `no_llm_leaks` point, matched case-insensitively
  against every line of the body:
  - `Here's a/an/the`, and the `Here is a/an/the` spelling
  - `I've` or `I have` followed by `created`, `written`, `drafted`,
    `prepared`, or `updated`
  - `Let me `, except `Let me know`, which is explicitly exempt
  - `Sure`, `Certainly`, or `Absolutely` followed by `,`, `!`, or `.`
  - `I'll `
  - `Feel free to ` or `Don't hesitate to `

  The match is anchored to the start of a line, so all of these stay safe
  mid-sentence.
- **Keep every section, even when empty.** Write `None` or
  `To be determined by research` rather than deleting a heading.

`## Objective`, `## Detailed Description`, `## Acceptance Criteria`, and
`## References` are required. `## Files / Modules to Change`,
`## Out of Scope / Constraints`, and `## Dependencies` are recommended and may
hold a single placeholder line.

## Dependencies Section Rules

`parse_dependencies()` in `sova/supervisor/dependency_graph.py` reads this
section to build the blocking graph, and it treats every `#<number>` it finds
as a real dependency.

- **One reference per line, no prose.** A phrase such as "unlike #123" in this
  section creates a phantom blocking edge that stalls the issue until #123
  closes. Put narrative references under `## References` instead.
- **Exactly one `## Dependencies` section.** A second one is ignored with a
  warning, so its entries silently do nothing.
- **The heading must be exactly `## Dependencies`** on its own line:
  `extract_section()` anchors on that and skips a heading with trailing text.
- **Write `None` when there are none.** Omitting the section is also valid, but
  keeping it makes the body uniform and prevents a stray `#<number>` elsewhere
  from being mistaken for a dependency later.
- **`Part of epic #<number>` is safe here.** Issues labeled `type: epic` are
  skipped by the dependency gate, so an epic reference never deadlocks a child.
- **Self-references are dropped** by `exclude_self`, so quoting the issue's own
  number is harmless.

## Worked Example

```markdown
## Objective
Cache the CodeRabbit quota lookup so the supervisor stops burning a GraphQL
call per poll cycle.

## Detailed Description
`_sync_quota()` in `sova/supervisor/coderabbit_quota.py` calls the GitHub API on
every poll, which at a 60 second interval consumes roughly 60 of the 5000 hourly
GraphQL points before any real work happens. Add a module-level cache keyed by
repo with a 300 second TTL and a single-flight lock so concurrent callers share
one request. An API failure must return `None` rather than caching an error, so
the next cycle retries instead of serving a poisoned entry.

## Acceptance Criteria
- [ ] A second `_sync_quota()` call within the TTL makes no API call
- [ ] Concurrent callers issue exactly one request
- [ ] An API failure is not cached and the next call retries
- [ ] All existing tests pass (`make check`)

## Files / Modules to Change
- [ ] `sova/supervisor/coderabbit_quota.py`: add the cache and single-flight lock
- [ ] `tests/test_coderabbit_quota.py`: TTL, concurrency, and failure cases

## Out of Scope / Constraints
- Do not change the quota thresholds or the gate that consumes them
- Do not add a persistent cache; in-memory per-process is sufficient

## Dependencies
None

## References
- Code: `sova/supervisor/coderabbit_quota.py`
- Related: the supervisor caching layer in `.claude/rules/architecture.md`
```

## Titles and Labels

Title: imperative and specific, with no issue number and no type prefix.
"Cache the CodeRabbit quota lookup" beats "Fix quota".

SOVA's label taxonomy:

- `type:` one of `feature`, `task`, `infra`, `bug`, `epic`
- `priority:` one of `critical`, `high`, `medium`, `low`
- `area:` one of `agent`, `dashboard`, `commands`, `personas`, `invariants`,
  `knowledge`, `docs`
- `agent:` applied by the pipeline, not by hand, except `agent:human-only`

Apply `type:` and `area:` on creation and `priority:` when it is known. Leave
`agent:` labels to triage. A multi-issue tracking container gets
`type: epic` plus `agent:human-only`: epics are excluded from
`get_ready_tasks()`, skipped by the dependency gate, and closed automatically
once every child reaches DONE.
