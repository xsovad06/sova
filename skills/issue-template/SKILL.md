---
name: issue-template
description: Canonical issue body structure for {{ project_name }}, using the exact headings the SOVA triage quality gate scores. Auto-activates when creating, drafting, or rewriting an issue or ticket for the tracker.
allowed_tools: Read, Grep, Glob, Bash
---

# Issue Body Template

When creating, drafting, or rewriting an issue for the tracker, produce the body
below. The headings are not stylistic: SOVA's triage role scores them
deterministically. A body that scores below the threshold is rewritten by an LLM
enrichment pass, or downgraded to `needs_spec` when enrichment is disabled or
falls short, so the issue waits instead of shipping. An issue written with this
template scores 8/8 and goes straight to the pipeline.

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
- [ ] All existing tests pass (`{{ check_cmd }}`)
- [ ] No new lint warnings (`{{ lint_cmd }}`)

## Files / Modules to Change
- [ ] `path/to/module.py`: <what changes there>
- [ ] `tests/test_module.py`: <coverage added>

## Out of Scope / Constraints
- <What must NOT be touched, and any constraint the implementation must respect.>

## Dependencies
None

## References
- Related: #<issue-number>
- Code: `path/to/module.py`
```

## Scoring Rules

`compute_quality_score()` in `sova/roles/triage.py` awards 8 points:

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
  already-checked `- [x]` items fails the check, which costs 2 points and caps
  the body at 6/8. The check is also required independently of the total, so no
  threshold is low enough to let the issue through without it.
- **Start the body directly at `## Objective`.** No conversational framing.
  Six line-initial patterns cost the LLM-leak point, matched case-insensitively
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
- **The heading must be exactly `## Dependencies`** on its own line: the parser
  anchors on that and skips a heading with trailing text.
- **Write `None` when there are none.** Omitting the section is also valid, but
  keeping it makes the body uniform and prevents a stray `#<number>` elsewhere
  from being mistaken for a dependency later.
- **`Part of epic #<number>` is safe here.** Issues labeled as epics are skipped
  by the dependency gate, so an epic reference never deadlocks a child.
- **Self-references are dropped**, so quoting the issue's own number is
  harmless.

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
- [ ] All existing tests pass (`{{ check_cmd }}`)

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

- Title: imperative and specific, with no issue number and no type prefix.
  "Cache the CodeRabbit quota lookup" beats "Fix quota".
- Apply the labels the tracker already uses rather than inventing new ones. On
  GitHub, read the existing set with `gh label list`.
- Multi-issue tracking containers get the epic label. They are skipped by the
  dependency gate and closed automatically once every child is done.

## Jira-Backed Trackers

The same markdown headings apply. The Jira adapter converts the body through
`_build_adf_doc()`, which keeps each paragraph's text verbatim, so the headings
and checkboxes survive the round trip and the quality scorer runs on them
unchanged. Five differences:

- **Write the body as literal markdown text, not with Jira's rich-text editor.**
  `_extract_text()` reads only the text nodes directly under a block, so a
  bullet list built with the editor's list control is dropped entirely on the
  way back out. An `## Acceptance Criteria` section written that way returns as
  a bare heading with no `- [ ]` left in it, which fails `has_acceptance_criteria`
  and therefore `meets_threshold()` at any threshold. Typing `- [ ]` as plain
  text inside a paragraph survives.
- Put the summary in Jira's `summary` field. Do not repeat it as an `#` heading
  inside the body.
- Use issue keys such as `PROJ-123` in `## Dependencies`. `parse_dependencies()`
  matches GitHub's numeric `#<number>` syntax only, so Jira dependency lines are
  documentation for humans and are not machine-parsed into the blocking graph.
- Use the `parent` field for epic membership instead of a "Part of epic" line.
- Satisfy `## References` with URLs or backticked code paths, since `#<number>`
  refs do not exist.
