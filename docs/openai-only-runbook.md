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
stored settings. **Keep the copy outside the project checkout.** The sync step runs
`git stash --include-untracked` in the primary checkout, so a database there whose `-wal` and `-shm`
files are not gitignored is moved out from under the run: every later write fails, and the run
finishes with its record stuck at `sync` (the first developer run of the 2026-11-06 pilot lost its
record this way). The same applies to `.claude/sova.log`, and the commit step stages an untracked
`.sova/test-baseline.json` into the PR, so a test repository needs `.claude/` runtime files and
`.sova/` ignored. The test repository also needs real `test_cmd` and `lint_cmd` (defaults
`make test` and `make lint`): without them the baseline records exit code 2 and validation has
nothing to check.

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
- **Unpriced Codex spend**: per-step cost reads `$0.00` until #1394. The real figure is in the Codex
  rollout logs (`~/.codex/sessions/**/*.jsonl`): each session's last `token_count` event carries its
  cumulative `input_tokens`, `cached_input_tokens` and `output_tokens`, and `session_meta.cwd` names the
  worktree, so a pilot's sessions can be priced at the model's rates (input includes cached input, and
  output includes reasoning).
- **Develop's no-change check** reports `cost $0.00 < $0.50 threshold` for every Codex
  turn; the check itself (a diff must exist) is still the right one.
- **Stopped runs stay `pending` without a sweep**: a run stopped by SIGTERM leaves its
  status to the dashboard's liveness sweep. With an isolated pilot database no server
  watches it, so mark it `interrupted` in that copy before `sova run <issue> --resume <id>`.

## Pilot Log (2026-11-06, disposable repository)

The reference pilot, run against the private disposable repository `dsova06/sova-pilot-sandbox` (a
small `stats` package with pytest and a pytest CI workflow) with the environment above, a pilot
database outside the checkout, and `tests/test_live_pilot.py` (`SOVA_LIVE_PILOT=1`), one role per
`sova run`. The runs used the #1396 fix (cost records labelled with the model that served them)
through `PYTHONPATH`, before it merged. Codex spend comes from the rollout logs in `~/.codex/sessions`
(token usage per session, at $2/M input, $0.10/M cached input, $10/M output), since SOVA records Codex
as `$0` `unknown` until #1394; text-call spend is what LiteLLM priced.

| Run | Role, issue | Outcome | Models (recorded) | Tokens in / out | Spend | `cost_source` |
|---|---|---|---|---|---|---|
| 3 | researcher, #2 | `## Research` written; paused at the spec gate (`awaiting_approval`) | research `gpt-6.1-sol` (Codex, read-only); spec `gpt-6.1-sol` | 100,684 / 2,024; 849 / 887 | $0.0748 Codex; $0.0106 | `unknown`; `priced` |
| 5 | developer, #1 | `done`: all 17 steps, PR #4 opened, sandbox CI green, handed to the reviewer | develop, simplify, self-review `gpt-6.1-sol` (Codex); routed develop call and PR body `gpt-6-luna`; commit subject `gpt-6.1-sol` | 280,229 / 3,353 develop, 92,474 / 558 simplify, 83,626 / 772 self-review; 386 / 126, 1,894 / 967, 347 / 12 | $0.2614 Codex; $0.0016 | `unknown` for Codex; `priced` for text |
| (C) | reviewer, #1 | APPROVE posted on the PR head, `sova:approved` label, no findings | `openai/gpt-6.1-sol` (from `sova.log`: role and `review` route) | not recorded | not recorded | none: a terminal reviewer run wrote no `TaskRun` or `CostRecord` (#1403) |
| 7 | reviewer, #1, rerun on the #1403 fix | `done`: APPROVE re-posted on PR #4's head | review `gpt-6.1-sol` | 2,176 / 109 | $0.0065 | `priced` |
| 6 | developer, #2, `codex.model = not-a-real-model-1130` | `failed` at `develop`, naming the model: "does not exist or you do not have access to it" | `not-a-real-model-1130` | 0 / 0 (two Codex attempts) | $0 | `unknown` |

No run launched Claude: the `claude` shim on `PATH` recorded nothing, Anthropic and Vertex credentials
were unset, and every recorded model is an OpenAI one. Total spend was about $0.70: $0.668 Codex across
every attempt below (including a killed research turn and the lost developer run), plus about $0.03 of
priced text calls.

Quality: PR #4 implements `median()` correctly with the issue's cases and more (odd, even, tuple,
duplicates, negatives, empty input, input order, package export). It also added a `make check` target
to the `Makefile`, outside the issue's file list, and the reviewer approved without flagging it.

What the pilot found:

| Finding | Effect | Status |
|---|---|---|
| conftest's `_isolate_sova_env` strips every `SOVA_*` variable, including the runbook's overrides | the pilot's guard refused every run (failed closed, $0) | fixed in `tests/test_live_pilot.py`: the overrides are read at import and restored for the live class |
| the suite's 30s pytest-timeout default applied to the live class | `sova run` killed 30s into research ($0.06) | fixed: the live class gets the run timeout plus a margin |
| provider cost records labelled `resolved_model or "claude"` | an OpenAI spec call stored as `claude`, failing the no-Claude check | #1396, fixed before the reference runs |
| `SyncStep` runs `git stash --include-untracked` in the primary checkout | a pilot database there with un-ignored `-wal`/`-shm` lost every write after `sync`, and `sova.log` lost the rest of the run; the run still pushed and opened a PR ($0.20, discarded) | #1402; until then keep the database outside the checkout (see Isolate): `sova install` writes no ignore rules for SOVA's own runtime files |
| the commit step stages an untracked `.sova/test-baseline.json` | SOVA's baseline file landed in the PR | #1402; until then ignore `.sova/` in the project |
| `test_cmd`/`lint_cmd` default to `make test`/`make lint` | in a repository without a Makefile the baseline recorded exit code 2 and validation had nothing to run | operator setup: give the test repository real commands |
| a terminal `sova run` of the reviewer role records no `TaskRun` and no `CostRecord` | the review's model, tokens and cost are only in `sova.log` and the posted review | #1403: fixed, and the rerun (run 7) recorded its row and a priced `CostRecord` |

## Pilot Log (2026-11-04)

**This was not the reference pilot.** The first live run was made against the production SOVA
repository, which the issue rules out: it did real pushes and PRs there. It is kept as a record
of what the run found; the reference pilot is the 2026-11-06 log above. What it found, and where
each fix landed:

| Finding | Effect | Fix |
|---|---|---|
| `SOVA_<SECTION>_<FIELD>` ignored when the DB stored that field | routing and `agent.model` stayed on Claude | generic env precedence in `_apply_env_overrides()` |
| `SOVA_DATABASE_URL` file URL ignored by `load_config()` | run started with no `github_repo` | `_sync_url_for()` resolves SQLite file URLs |
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

- `tests/test_openai_only_scenario.py` (no network, and any process launch fails the test, so no git,
  gh, codex or claude runs): `llm.provider = "openai"` with `agent.runtime = "codex"` drives the
  researcher step, a developer dispatch, `create_pr`, `handoff_to_reviewer` and the reviewer role,
  with a tripwire on `ClaudeCodeProvider`, `AnthropicAPIProvider` and `ClaudeCodeRuntime`. Tool-using
  steps replay Codex output through the real `CodexStreamParser`; the PR body and the review go
  through the real OpenAI provider and `llm.routing` with only `litellm` faked, so the test checks
  that each call reached the routed `openai/` model and that the cost was recorded as `priced`. The
  forge and tracker are mocks. Recovery cases: a blocking review finding hands back to the developer,
  whose address-review fix runs on Codex and hands back for re-review; a red CI check is fixed on
  Codex, pushed (mocked) and re-polled green; a cancelled agent turn stops the Codex process and
  propagates the cancellation instead of retrying elsewhere. It also covers a budget cap Codex cannot
  enforce, an unavailable runtime, and a config that names no Claude model.
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
- The live pilot now passes for the researcher, developer and reviewer roles and the unavailable-model
  case (2026-11-06 log above); the reviewer passed once #1403 recorded its run.
