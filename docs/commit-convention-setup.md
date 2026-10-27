# Commit Convention Setup

SOVA writes git history in three places: `CommitStep` (the commit message for
work the develop agent left uncommitted), `CreatePRStep` (the PR title), and
`ValidateStep` (folding a pre-push hook fix into the commit it belongs to).
The first two produce a conventional-commit line, validated against the
project's own accepted types and scopes before it is used.

Those two lists are configuration, not a hardcoded table:

| Key | Default | Meaning |
|-----|---------|---------|
| `commit.types` | `feat, fix, refactor, test, docs, chore, ci` | Accepted conventional-commit types |
| `commit.scopes` | SOVA's own scope list (`core`, `dashboard`, `cli`, ...) | Accepted conventional-commit scopes |

The defaults are SOVA's own convention, so a project that happens to share it
needs no configuration at all.

Both lists must be non-empty. Every `commit.types` entry must match
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
every other type requires a scope from the configured list.

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
come first alphabetically or by habit. Until `commit.scopes` is set, though,
that
fallback-of-last-resort still has only SOVA's own list to choose from, so a
project whose real scopes are `accounts`/`billing` keeps getting a scope from
SOVA's list on anything it can't match by path, which is exactly the drift
this configuration exists to stop. If the project has a pre-push hook or CI
check that validates commit messages, set both lists to match that check.

Projects whose settings already live in `.claude/sova.db` (anything installed
before these keys existed) keep the SOVA defaults until the two commands above
are run; the keys are additive and no migration is needed.

## Related

- Commit message rules for this repository: `AGENTS.md` ("Commit Messages")
  and `invariants/commit-format.sh`
- Configuration mechanics (database-backed settings, `SOVA_*` overrides):
  `CLAUDE.md` ("Configuration")
