# OpenAI-Only Runbook

How to run SOVA's pipeline with OpenAI models only (OpenAI `llm.provider` for SOVA's own
LLM calls, Codex as the agent runtime for tool-using steps), how to observe that nothing
reached Claude, and how to roll back. This is the reversible pilot path from #1130: it
switches one process tree through environment variables and leaves the project's stored
configuration untouched. Switching a project's stored defaults is a separate decision.

Background reading: [codex-runtime-setup.md](codex-runtime-setup.md) (Codex install, login,
sandbox) and [model-selection-migration-guide.md](model-selection-migration-guide.md)
(`llm.provider = "openai"`).

## Prerequisites

| What | Check |
|---|---|
| Codex CLI on `PATH` (verified with 0.166.0) | `codex --version` |
| Codex logged in (keyring) | `codex login status` prints `API key configured` or a ChatGPT login |
| LiteLLM extra for the `openai` provider | `pip install -e ".[litellm]"`, then `sova doctor` shows `openai: litellm <version>` |
| An OpenAI API key for SOVA's own calls | exported as `OPENAI_API_KEY` for the run only (see below) |
| A designated, disposable test repository, with SOVA installed in a checkout of it | `SOVA_GITHUB_REPO=owner/test-repo` and `sova config get github_repo` prints it |

**Run the pilot only against a disposable test repository.** The run pushes branches, opens
PRs, edits the issue body and changes labels. The production SOVA and GWYM repositories are
out of scope: never point the pilot at them, and `tests/test_live_pilot.py` refuses both. Create
an issue in the test repository that is safe to implement, set `SOVA_GITHUB_REPO` to that
repository for the shell, and confirm it before `sova run`.

Keep the key out of config files and shell profiles. Read it from the macOS Keychain when
the run starts, for example from the entry Codex itself stores:

```bash
OPENAI_API_KEY="$(security find-generic-password -s "Codex Auth" -w \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["OPENAI_API_KEY"])')"
export OPENAI_API_KEY
```

## Switch One Run to OpenAI

`SOVA_<SECTION>_<FIELD>` beats a stored value for every config field, so a run is switched
entirely by environment. Override every model field, not just the provider: a project
tuned for Claude stores Claude IDs in `llm.routing`, `agent.model`, `agent.fallback_models`,
`roles.*_model`, `oversight.analysis_model`, `confidence.model` and
`review.panel.dimension_models`, and any one left over sends that step's call to a Claude model
ID that the OpenAI provider cannot serve. `sova doctor` lists every model field that still
names a Claude, Vertex or Gemini alias or ID when `llm.provider` is `openai` (model aliases,
routes and conflict-resolution models included). Under a runtime other than `codex` the row skips the
fields only that runtime consumes (`agent.model`, `agent.fallback_models`,
`roles.developer_runtime_model`) and passes with a note that the tool steps spawn it, so a mixed
setup is not failed; it is not OpenAI-only until `agent.runtime` is `codex` (the `openai-only models`
row).

```bash
SOL="openai/gpt-6.1-sol"
LUNA="openai/gpt-6-luna"
export SOVA_LLM_PROVIDER=openai
export SOVA_LLM_MODEL="$SOL"
export SOVA_LLM_FALLBACK_MODEL="openai/gpt-6-sol"
export SOVA_LLM_ROUTING="{\"develop\":\"$SOL\",\"extraction\":\"$LUNA\",\"monitor_ci\":\"$SOL\",\"pr_body\":\"$LUNA\",\"review\":\"$SOL\",\"self_review\":\"$SOL\",\"triage\":\"$LUNA\",\"validate\":\"$SOL\"}"
export SOVA_AGENT_MODEL="$SOL"
export SOVA_AGENT_FALLBACK_MODELS='["openai/gpt-6-sol"]'
export SOVA_ROLES_TRIAGE_MODEL="$LUNA"
export SOVA_ROLES_RESEARCHER_MODEL="$SOL"
export SOVA_ROLES_REVIEWER_MODEL="$SOL"
export SOVA_ROLES_PLANNER_MODEL="$SOL"
export SOVA_ROLES_DEVELOPER_MODEL="$SOL"
export SOVA_ROLES_DEVELOPER_RUNTIME_MODEL=gpt-6.1-sol   # a Codex catalog id, not an openai/ one
export SOVA_CONFIDENCE_MODEL="$SOL"
export SOVA_REVIEW_PANEL_DIMENSION_MODELS='{}'
export SOVA_OVERSIGHT_ANALYSIS_MODEL="$SOL"
export SOVA_AGENT_RUNTIME=codex
export SOVA_CODEX_MODEL=gpt-6.1-sol
export SOVA_CODEX_SANDBOX=workspace-write
export SOVA_CODEX_ALLOW_UNENFORCED_BUDGET=true
```

Luna for triage, extraction and PR bodies with Sol for development and review is the
starting hypothesis from epic #1122, not a measured choice. Confirm the result before
running anything:

```bash
for k in llm.provider llm.routing agent.model agent.runtime codex.model confidence.model; do
  printf '%-16s %s\n' "$k" "$(sova config get "$k")"
done
sova doctor   # llm provider: openai; agent runtime: codex (API key configured); openai-only models: OK
```

### Isolate the run from a running server

A `sova server` sharing the project database used to kill any live run it had not
spawned on its recovery sweep; a run started from a terminal was gone within five minutes.
Recovery now kills an unmanaged live run only when it is an orphaned agent the server launched
(it carries the `SOVA_AGENT_RUN=1` marker and its parent is init, the server itself, or a
non-SOVA process such as a subreaper). It never signals itself, and leaves every other live run
(a terminal or `nohup`'d `sova run`, a service `sova watch`, an in-process `sova server` run) alone,
at server start and in the periodic sweep alike. A server started before that fix still kills them. Either
restart the server first, or give the run its own copy of the database, which also keeps pilot
rows out of the dashboard:

```bash
sqlite3 .claude/sova.db ".backup '/path/to/pilot-sova.db'"
export SOVA_DATABASE_URL="sqlite+aiosqlite:////path/to/pilot-sova.db"
```

A SQLite file URL is read for configuration too, so the copy carries the project's
stored settings.

## Run

```bash
sova run <issue>                        # dispatcher picks the role from the issue state
sova run <issue> --role developer --force   # skip the researched-state entry gate only
```

`--force` bypasses only the role's entry gate; every step's output gate still runs.

## What Runs Where

| Step kind | Executor | Network | Writes |
|---|---|---|---|
| Text-only calls (triage, PR body, self-review, extraction) | LiteLLM to OpenAI, in the SOVA process | yes | none |
| `develop`, fix loop, `simplify` and other tool-using steps | `codex exec`, `workspace-write` sandbox | no | the worktree only |
| `research` on a provider without the `runs_commands_with_tools` capability (every provider except `claude-code`: `litellm`, `hybrid`, `anthropic`, `openai`, `ollama`, `vertex`) | the configured agent runtime, read-only (`codex exec` read-only sandbox for Codex) | no | none; SOVA appends the section to the issue |
| Push, PR creation, labels, board, issue body | SOVA process (`git`, `gh`) | yes | GitHub |

The Codex sandbox has no network, so an agent cannot run `gh`. SOVA therefore hands
`/develop` the issue title, body and comments inline when there is no approved spec, and
does every tracker and GitHub write itself. Only the 10 newest comments are inlined, newest first, each of the title, body and comments in a nonce-delimited block, and the whole context is capped at 12k characters (comments trimmed first).

Only Codex enforces `read_only`. The Claude Code and Aider runtimes treat it as advisory, so
research on a text-only provider under `agent.runtime = "claude-code"` is kept read-only by
the prompt alone, and SOVA logs `agent_dispatch.read_only_unenforced` when that happens.

## Observe

- `codex.spawn ... model=gpt-6.1-sol sandbox=workspace-write` per tool-using step, and
  `llm.task_type_route ... model=openai/...` per routed text call.
- Nothing should log `llm.provider.claude_code` or spawn `claude` for the run's process
  tree; grep the run's log for `claude-` model IDs to confirm.
- `CostRecord.cost_source`: OpenAI text calls are `priced`; Codex steps record `$0.00`
  with cost source `unknown`, which is unpriced, not free.
- The agent's own transcript is in the run's `output_lines` (dashboard run log, or the
  pilot database).

## Rollback

Open a new shell, or `unset` the variables above. Nothing was written to the stored
configuration, so the next run uses the project's normal provider and runtime. Codex's
own login is untouched either way.

## Known Limitations

- **No budget cap**: Codex cannot enforce `agent.max_budget`; the run proceeds only because
  `codex.allow_unenforced_budget` accepts unbounded cost, and logs `codex.budget_not_enforced`
  on every spawn.
- **No model fallback for Codex**: `agent.fallback_models` is ignored by Codex
  (`codex.fallback_model_not_supported`); only `codex.model` runs.
- **Unpriced Codex spend**: per-step cost reads `$0.00`. Check OpenAI's usage dashboard for
  the real figure.
- **Develop's no-change check** reports `cost $0.00 < $0.50 threshold` for every Codex
  turn; the check itself (a diff must exist) is still the right one.
- **Stopped runs stay `pending` without a sweep**: a run stopped by SIGTERM leaves its
  status to the dashboard's liveness sweep. With an isolated pilot database no server
  watches it, so mark it `interrupted` in that copy before `sova run <issue> --resume <id>`.

## Pilot Log (2026-11-04)

**This was not the reference pilot.** The first live run was made against the production SOVA
repository, which the issue rules out: it did real pushes and PRs there. It is kept as a record
of what the run found, and the pilot is still to be re-run against a disposable test repository
(see Open Work). What it found, and where each fix landed:

| Finding | Effect | Fix |
|---|---|---|
| `SOVA_<SECTION>_<FIELD>` ignored when the DB stored that field | routing and `agent.model` stayed on Claude | generic env precedence in `_apply_env_overrides()` |
| `SOVA_DATABASE_URL` file URL ignored by `load_config()` | run started with no `github_repo` | `_resolve_db_path()` resolves SQLite file URLs |
| Server recovery killed a live terminal run | run killed 16s into its Codex turn | recovery kills an unmanaged live run only when it is an orphaned, marked agent of the server |
| `/research` sent as plain text to the OpenAI provider | research gate failed | read-only runtime research; SOVA writes the section |
| `/develop` fetched the issue with `gh` inside the sandbox | Codex found no task and changed nothing | issue context passed inline |
| Address PR refused a PR whose issue was still `triaged` | the dashboard's address run failed in a second | Gate 3 applies to development only, not to addressing an open PR |
| Codex 0.166 `login status` wording | doctor reported auth unknown | parser recognizes `API key configured` |
| Check commands and agents inherited `SOVA_DATABASE_URL` | the suite opened the pilot database; 16 writer tests failed | `ORCHESTRATOR_ONLY_VARS` dropped from every check, hook, baseline run, `git commit`, `git push` and the Codex spawn; the nested `sova run` and the Claude Code and Aider agents keep it. The Codex exception is a trade: its `sova config get` and `sova checks record` calls (the `sova-*` skills make them) resolve the project's default database, not the pilot copy, so a recorded check lands there |

The last row was diagnosed by the Codex agent itself during the pilot's fix loop: asked
to make the failing checks pass, it traced them to the inherited URL instead of touching
the tests. It also edited `sova/core/test_baseline.py` along the way, which SOVA's
test-weakening guard reverted, so the fix was moved into this change by hand.

## Automated Coverage and Open Work

- `tests/test_openai_only_scenario.py` (no network, no subprocess): `llm.provider = "openai"` with
  `agent.runtime = "codex"` drives the researcher step and a developer dispatch through the real
  `CodexStreamParser`, with a tripwire on `ClaudeCodeProvider` and `ClaudeCodeRuntime`; it also
  covers a budget cap Codex cannot enforce, an unavailable runtime, and a config that names no
  Claude model.
- `tests/test_live_pilot.py` (`@pytest.mark.evaluation`, skipped unless `SOVA_LIVE_PILOT=1` and a
  test repository, project and issue are named): runs `sova run` once, which dispatches ONE role,
  chosen by the issue's state (a triaged issue gets the researcher), and does not follow the handoff;
  the reviewer and PR-handoff path are not exercised by it. It writes the model, tokens and cost
  source per recorded call, plus each run's role, status and PR number, to a JSON report (default
  `<project>/.claude/live-pilot-*.json`). It reads the database the run writes
  (`SOVA_LIVE_PILOT_DB`, else the SQLite file of `SOVA_DATABASE_URL`; a relative SQLite path is read
  against the project, and a non-SQLite `SOVA_DATABASE_URL` needs `SOVA_LIVE_PILOT_DB`) and only
  runs created after it started. It fails on a paused, failed or unfinished run (those can exit 0),
  and on any recorded model that names Claude, Anthropic, Vertex or Gemini, or carries a provider
  prefix other than `openai/`. A bare ID Codex reported passes, so an unset `codex.model` is not a
  violation. The run is made with the Anthropic and Vertex credentials unset and a `claude` shim
  first on PATH, so a Claude launch that fails before it records a cost still fails the pilot. It
  refuses to start unless `llm.provider` is `openai`, `agent.runtime` is `codex` and no model field
  (aliases, routes and conflict-resolution models included) names Claude, Vertex or Gemini. It
  refuses the SOVA and GWYM repositories. A second issue named by `SOVA_LIVE_PILOT_FAILURE_ISSUE`
  enables the unavailable-model failure case: it sets a bogus `SOVA_CODEX_MODEL` (Codex ignores
  `agent.model`), drops `SOVA_LLM_ROUTING`, and asserts the run fails and names that model.
- Still open on #1130: re-run the live pilot on a disposable test repository and record its
  observed model IDs, tokens, outcome and per-step `CostRecord.cost_source` here; extend the mocked
  scenario to the reviewer and PR-handoff steps; exercise cancellation, a review change request
  and a CI failure as recovery cases.
