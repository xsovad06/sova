# Commit Convention Setup

SOVA writes git history in three places: `CommitStep` (the commit message for
work the develop agent left uncommitted), `CreatePRStep` (the PR title), and
`ValidateStep` (folding a pre-push hook fix into the commit it belongs to).
The first two produce a conventional-commit line, validated against the
project's own accepted types and scopes before it is used.

Those two lists are configuration, not a hardcoded table:

| Key | Default | Meaning |
|-----|---------|---------|
| `commit.types` | resolved per project (see below) | Accepted conventional-commit types |
| `commit.scopes` | resolved per project (see below) | Accepted conventional-commit scopes; empty means no restriction |

## How an unset list is resolved

A key counts as configured when a row exists in the project database, `sova.toml` sets it, or its
`SOVA_COMMIT_TYPES`/`SOVA_COMMIT_SCOPES` env override is set; an explicit empty list is configured too, and a
configured list is never replaced. Each unset list resolves on its own, in this order:

1. SOVA's own lists, only when the project is the SOVA repository (`pyproject.toml` declares `sova` and
   `sova/config/models.py` exists, so a worktree counts).
2. A `Valid types:` or `Valid scopes:` line (list marker and bold allowed, backticks and a trailing period
   stripped) in the project's `AGENTS.md`, then `CLAUDE.md`. Entries are lowercased and de-duplicated in order.
3. The `type(scope):` subjects of the last 200 non-merge commits not authored by a bot (author ending `[bot]`),
   most frequent first. A trailing `(#N)` is ignored, a scope must appear twice, and types are limited to `feat,
   fix, refactor, test, docs, chore, ci, perf, build, style`. Scopes used only by chore, build or ci subjects (a
   `chore(deps)` bump) rank last. The derived types are followed by the rest of `feat, fix, refactor, test, docs,
   chore`, so history can add types but never remove them. The history counts only with at least 20 conventional
   subjects; below that, or with none, the types are `feat, fix, refactor, test, docs, chore` and the scopes empty. Git finds the enclosing repository itself, so a project in a subdirectory of a repository (a monorepo
   package) reads the whole history, but only when that repository tracks a file under the project directory
   (a dotfiles repository in your home directory is not your project's history).

Every config load resolves unset keys lazily without writing; the resolution is memoized for a minute per project
(an edited `AGENTS.md`/`CLAUDE.md` is seen at once, new commits within the minute or at the next settings write),
so a reload does not rerun `git log`. `sova install` and `sova commands update` store the result in the project
database (so Settings shows and edits it) for each unset key that the project's docs or git history
define. A list whose history could not be read (not a repository, `unresolved`), whose history is too thin
(`empty`: no commits or fewer than 20 conventional subjects) or yields no scope (`fallback`) is not stored, since
each would be a guess made with no evidence; it keeps resolving until there is history to derive from. A value saved
while the command runs is never overwritten: the rows are inserted only if absent. Running either command twice writes nothing the second
time, and a project whose history later gains scopes keeps its stored list. An empty `commit.scopes` makes the title prompt drop the scope list, accepts any scope or none in
`is_valid_commit_line()`, and builds fallback subjects as `type: subject`.

`sova doctor` warns when a project other than SOVA still has a `commit.scopes` that is mostly SOVA's list and
holds several SOVA-only subsystems such as `supervisor` or `adapters` (an older or partial copy included; the
types are the standard conventional set, so a match proves nothing), and separately when git history could not be
read so a list was not derived.

`commit.types` must be non-empty (`commit.scopes` may be empty, meaning no restriction). Every `commit.types` entry must match
`^[a-z][a-z0-9]*$` (lowercase, alphanumeric, starting with a letter), the
exact shape the parser that validates a generated commit/PR-title line can
match. `commit.scopes` entries may additionally contain `-`/`_`
(`^[a-z][a-z0-9_-]*$`), since a scope is never parsed as strictly as a type
and a real convention routinely hyphenates or underscores scope names (e.g.
`api-gateway`, `health_score`). `sova config set` rejects a value that
doesn't match its field's pattern, with a message naming the offending entry,
rather than accepting it and letting every subsequent commit fail. "ci" is
accepted with or without a scope segment (`ci: ...` or `ci(scope): ...`),
matching `invariants/commit-format.sh`'s own scope-optional handling of it;
every other type requires a scope from the configured list. When `commit.scopes` is empty the commit-subject and
PR-title prompts ask for `"type: subject" or "type(scope): subject"` and let the model name the area, and the
deterministic fallback omits the scope.

## Setting your project's lists

Both keys are list-valued, so `sova config set` accepts either a JSON array or
comma-separated text:

```bash
# From the project directory (or pass --project <path>)
sova config set commit.types "feat, fix, refactor, test, docs, chore, ci, perf"
sova config set commit.scopes "accounts, billing, api, web, infra, docs"

# Confirm what was stored (lists print as JSON)
sova config get commit.types
sova config get commit.scopes
```

The dashboard Settings page exposes the same two keys under the "commit" group.

`CommitStep` also asks an LLM call to propose the commit subject (skipped
once the run's budget is nearly exhausted, in which case the deterministic
builder below runs instead). It runs on the run's own model by default; set
`llm.routing.commit_message` to route it to a cheaper model instead, the same
mechanism `docs/model-selection-architecture.md` documents for other task
types.

## Why this matters

A type or scope outside these lists is rejected and SOVA falls back to a
deterministic builder. That fallback infers the type and scope from the
changed paths when it safely can (a directory or filename matching one of
your configured scopes, e.g. `apps/accounts/models.py` with scope
`accounts`; a change confined to a docs directory (`docs/`, `documentation/`,
`doc/`) or a test directory/suffix (`tests/`, `test/`, `spec/`, `specs/`,
`__tests__/`, or a `test_*`/`*_test.py`/`*_test.go`/`*.test.ts`/`*.spec.ts`
filename) gets type `docs`/`test` when those are configured), and only falls
back further to `"core"` (type `"feat"`) when either list configures it, else
the first configured scope/type (`feat(<scope>): <issue title>`), when the
paths don't confidently resolve to one: a project whose `commit.scopes` omits
`"core"` has that fallback decided by list order, so put a general-purpose
scope first if you have one, rather than relying on an entry that happens to
come first alphabetically or by habit. If the project has a pre-push hook or CI
check that validates commit messages, set both lists to match that check.

Projects whose settings already live in `.claude/sova.db` (anything installed
before these keys existed) resolve the lists lazily as described above on the
next load; no migration is needed.

## Related

- Commit message rules for this repository: `AGENTS.md` ("Commit Messages")
  and `invariants/commit-format.sh`
- Configuration mechanics (database-backed settings, `SOVA_*` overrides):
  `CLAUDE.md` ("Configuration")
