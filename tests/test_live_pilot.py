"""Opt-in live pilot for the OpenAI-only pipeline (#1130). Never runs by default.

It runs ``sova run <issue>`` against a designated, disposable test repository and records, per
recorded step, the model Codex or LiteLLM reported, its tokens and ``CostRecord.cost_source``. One
``sova run`` dispatches ONE role, chosen by the issue's state (a triaged issue gets the researcher), and
never follows the handoff to the next: the reviewer and PR-handoff path are not covered here (the runbook
lists that as open work). The run is made with Anthropic and Vertex credentials unset and a ``claude``
shim first on PATH that records any launch, so a Claude attempt that fails before it records a cost still
fails the pilot. It needs every one of these, and refuses the production SOVA and GWYM repositories
whatever they say:

    SOVA_LIVE_PILOT=1                     explicit opt-in
    SOVA_LIVE_PILOT_REPO=owner/name       the disposable test repository
    SOVA_LIVE_PILOT_PROJECT=/path         a checkout of it with SOVA installed (its config must name the same repo)
    SOVA_LIVE_PILOT_ISSUE=7               an issue there that is safe to push and open a PR for
    SOVA_LIVE_PILOT_DB=/path/pilot.db     optional: the SQLite file the run writes (default: the SQLite file named by
                                          SOVA_DATABASE_URL, else <project>/.claude/sova.db); required when
                                          SOVA_DATABASE_URL names a non-SQLite backend
    SOVA_LIVE_PILOT_REPORT=/path/r.json   optional: where the report goes (default <project>/.claude/live-pilot-*.json)
    SOVA_LIVE_PILOT_FAILURE_ISSUE=8       optional: a second safe issue, enabling the unavailable-model failure case

Export the OpenAI-only environment from docs/openai-only-runbook.md first: the pilot refuses to start a billable
run unless ``llm.provider`` is ``openai``, ``agent.runtime`` is ``codex`` and no model field names a Claude model. The
guard itself is tested on every run (``TestPilotRepoGuard``).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from sova.utils.diagnostics import names_non_openai_model

# Repository names the pilot must never touch, whatever the owner.
_PRODUCTION_REPO_NAMES = frozenset({"sova", "gwym"})
_RUN_TIMEOUT_SECONDS = 3600
# Substrings that mark a Claude or Vertex call whatever the provider prefix: `anthropic/claude-...`,
# `vertex_ai/gemini-...`, `us.anthropic.claude-...`.
_FORBIDDEN_MODEL_PARTS = ("claude", "anthropic", "vertex", "gemini")
# Credentials and routing that would let a Claude or Vertex call succeed; unset for the pilot run.
_BLOCKED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_VERTEX_PROJECT_ID", "CLAUDE_CODE_USE_VERTEX")
# A run in one of these states did not finish its role: a paused or failed run exits 0 or non-zero alike.
_UNFINISHED_STATUSES = frozenset({"failed", "paused", "rejected", "running", "pending"})
# A bogus model that no provider serves, for the failure case.
_BOGUS_MODEL = "openai/not-a-real-model-1130"


def pilot_repo_refusal(repo: str, configured_repo: str) -> str | None:
    """Why *repo* may not host the live pilot, or None when it may."""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return f"SOVA_LIVE_PILOT_REPO must be owner/name, got {repo!r}"
    if name.lower() in _PRODUCTION_REPO_NAMES:
        return f"{repo} is a production repository: the live pilot runs only against a disposable test repository"
    if configured_repo.lower() != repo.lower():
        return f"the project's github_repo is {configured_repo!r}, not {repo!r}"
    return None


class TestPilotRepoGuard:
    @pytest.mark.parametrize("repo", ["xsovad06/sova", "RedHatInsights/SOVA", "someone/gwym", "GWYM/Gwym"])
    def test_production_repositories_are_refused(self, repo: str) -> None:
        assert pilot_repo_refusal(repo, repo) is not None

    @pytest.mark.parametrize("repo", ["sova", "owner/", "/name", "a/b/c", ""])
    def test_malformed_repository_is_refused(self, repo: str) -> None:
        assert pilot_repo_refusal(repo, repo) is not None

    def test_project_config_must_name_the_same_repository(self) -> None:
        assert pilot_repo_refusal("me/pilot-sandbox", "me/another-repo") is not None

    def test_disposable_repository_is_allowed(self) -> None:
        assert pilot_repo_refusal("me/pilot-sandbox", "Me/Pilot-Sandbox") is None


def _sova(project: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed argv, operator-supplied project path)
        [shutil.which("sova") or "sova", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
        env=env,
    )


def claude_blocked_env(shim_dir: Path, base: dict[str, str] | None = None) -> tuple[dict[str, str], Path]:
    """An environment where a Claude or Vertex call cannot succeed, and the marker file a launch would write.

    Anthropic and Vertex credentials are dropped, and a ``claude`` shim is put first on PATH: it appends a line
    to the marker and exits 127. A runtime or step that tries to start the Claude CLI therefore fails loudly
    and leaves evidence even when it fails before it records any cost.
    """
    marker = shim_dir / "claude-launched.txt"
    shim = shim_dir / "claude"
    shim.write_text(f'#!/bin/sh\necho "claude launched: $*" >> "{marker}"\nexit 127\n', encoding="utf-8")
    shim.chmod(0o755)
    env = {key: value for key, value in (base if base is not None else os.environ).items() if key not in _BLOCKED_ENV}
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return env, marker


def _resolve_in_project(path: Path, project: Path) -> Path:
    """A relative path as the ``sova run`` subprocess sees it: against the project, where it runs."""
    return path if path.is_absolute() else project / path


def pilot_db_path(project: Path) -> Path:
    """The SQLite file the pilot run writes: SOVA_LIVE_PILOT_DB, else SOVA_DATABASE_URL's file, else the default."""
    explicit = os.environ.get("SOVA_LIVE_PILOT_DB")
    if explicit:
        return _resolve_in_project(Path(explicit), project)
    url = os.environ.get("SOVA_DATABASE_URL")
    if url:
        parsed = make_url(url)
        # Not existence-checked: the file may not exist until the run creates it.
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            return _resolve_in_project(Path(str(parsed.database)), project)
    return project / ".claude" / "sova.db"


def database_refusal(url: str | None, explicit_db: str | None) -> str | None:
    """Why the pilot cannot read the run's database, or None: only SQLite is readable here."""
    if not url or explicit_db:
        return None
    backend = make_url(url).get_backend_name()
    if backend != "sqlite":
        return f"SOVA_DATABASE_URL names a {backend} database this pilot cannot read: set SOVA_LIVE_PILOT_DB"
    return None


def forbidden_model(model: object, allowed: frozenset[str] = frozenset()) -> bool:
    """True when *model* names Claude, Vertex or Gemini, or is a provider-prefixed ID that is not ``openai/``.

    A Codex record carries the bare ID the CLI reported (``gpt-6.1-sol``), which can differ from, or be, the
    unconfigured ``codex.model`` default, so a bare ID passes unless it names a forbidden provider. A
    LiteLLM record carries its provider prefix, so only ``openai/`` passes. *allowed* lists exact IDs
    (``codex.model``, when set) that pass whatever they look like.
    """
    lowered = str(model).strip().lower()
    if not lowered:
        return True
    if lowered in allowed:
        return False
    if names_non_openai_model(lowered) or any(part in lowered for part in _FORBIDDEN_MODEL_PARTS):
        return True
    return "/" in lowered and not lowered.startswith("openai/")


class TestForbiddenModel:
    @pytest.mark.parametrize(
        "model",
        ["claude-sonnet-5-5", "anthropic/claude-sonnet-5-5", "vertex_ai/claude-opus-5-5", "us.anthropic.claude-x"]
        + ["vertex_ai/gemini-2", "sonnet", "haiku", "gemini-2.5-pro", "ollama/llama3", ""],
    )
    def test_non_openai_models_are_forbidden(self, model: str) -> None:
        assert forbidden_model(model)

    def test_openai_ids_and_the_configured_codex_model_are_allowed(self) -> None:
        assert not forbidden_model("openai/gpt-6.1-sol")
        assert not forbidden_model("GPT-6.1-Sol", frozenset({"gpt-6.1-sol"}))

    @pytest.mark.parametrize("allowed", [frozenset(), frozenset({""}), frozenset({"gpt-6"})])
    def test_a_bare_codex_reported_id_passes_whether_or_not_codex_model_is_set(self, allowed: frozenset[str]) -> None:
        """codex.model defaults to '' and Codex may report a dated or variant ID: neither is a violation."""
        assert not forbidden_model("gpt-6.1-sol", allowed)
        assert not forbidden_model("gpt-6.1-sol-2026-11-04", allowed)

    def test_the_claude_shim_environment_blocks_credentials_and_records_a_launch(self, tmp_path: Path) -> None:
        base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_VERTEX": "1", "KEEP": "1"}
        env, marker = claude_blocked_env(tmp_path, base)
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_USE_VERTEX" not in env
        assert env["KEEP"] == "1"
        assert env["PATH"].startswith(str(tmp_path))
        result = subprocess.run(  # noqa: S603 (the shim written above)
            [str(tmp_path / "claude"), "--version"], capture_output=True, text=True, check=False
        )
        assert result.returncode == 127
        assert "claude launched: --version" in marker.read_text(encoding="utf-8")

    def test_a_relative_sqlite_url_resolves_against_the_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("SOVA_LIVE_PILOT_DB", raising=False)
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite:///pilot.db")
        assert pilot_db_path(tmp_path) == tmp_path / "pilot.db"
        monkeypatch.setenv("SOVA_LIVE_PILOT_DB", "rel/explicit.db")
        assert pilot_db_path(tmp_path) == tmp_path / "rel" / "explicit.db"

    def test_a_postgres_url_needs_an_explicit_database_file(self) -> None:
        url = "postgresql+asyncpg://u:p@host/db"
        assert database_refusal(url, None) is not None
        assert database_refusal(url, "/tmp/pilot.db") is None
        assert database_refusal("sqlite+aiosqlite:///x.db", None) is None
        assert database_refusal(None, None) is None

    def test_pilot_db_path_prefers_the_explicit_file_then_the_database_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("SOVA_LIVE_PILOT_DB", raising=False)
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pilot.db'}")
        assert pilot_db_path(tmp_path) == tmp_path / "pilot.db"
        monkeypatch.setenv("SOVA_LIVE_PILOT_DB", str(tmp_path / "explicit.db"))
        assert pilot_db_path(tmp_path) == tmp_path / "explicit.db"
        monkeypatch.delenv("SOVA_LIVE_PILOT_DB")
        monkeypatch.delenv("SOVA_DATABASE_URL")
        assert pilot_db_path(tmp_path) == tmp_path / ".claude" / "sova.db"


def _max_run_id(db: Path) -> int:
    """The highest task_run id now, so a later read sees only the runs the pilot itself created."""
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_runs").fetchone()[0])


def _runs_since(db: Path, issue: str, after_id: int) -> list[dict[str, object]]:
    """Every run for *issue* created after *after_id*: one ``sova run`` creates the one role's run it dispatches.

    Each carries its role, final status, PR number, error and its cost records: model, tokens, cost and the
    cost source, so the report holds the model identities, usage, cost provenance and the quality result.
    """
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        runs = conn.execute(
            "SELECT id, role, status, pr_number, error_message, termination_reason FROM task_runs "
            "WHERE issue_number = ? AND id > ? ORDER BY id",
            (issue, after_id),
        ).fetchall()
        result: list[dict[str, object]] = []
        for run in runs:
            costs = conn.execute(
                "SELECT phase, model, input_tokens, output_tokens, cost_usd, cost_source FROM cost_records "
                "WHERE task_run_id = ? ORDER BY id",
                (run["id"],),
            ).fetchall()
            steps = conn.execute(
                "SELECT step_name, status FROM step_executions WHERE task_run_id = ? ORDER BY id", (run["id"],)
            ).fetchall()
            result.append(
                {
                    "run_id": run["id"],
                    "role": run["role"],
                    "run_status": run["status"],
                    "pr_number": run["pr_number"],
                    "error_message": run["error_message"],
                    "termination_reason": run["termination_reason"],
                    "step_statuses": {step["step_name"]: step["status"] for step in steps},
                    "cost_records": [{key: row[key] for key in row.keys()} for row in costs],  # noqa: SIM118
                }
            )
    return result


_OPTED_IN = os.environ.get("SOVA_LIVE_PILOT") == "1"
_REPO = os.environ.get("SOVA_LIVE_PILOT_REPO", "")
_PROJECT = os.environ.get("SOVA_LIVE_PILOT_PROJECT", "")
_ISSUE = os.environ.get("SOVA_LIVE_PILOT_ISSUE", "")
_FAILURE_ISSUE = os.environ.get("SOVA_LIVE_PILOT_FAILURE_ISSUE", "")

_LIVE_SKIP = pytest.mark.skipif(
    not (_OPTED_IN and _REPO and _PROJECT and _ISSUE),
    reason="opt-in live pilot: set SOVA_LIVE_PILOT=1, SOVA_LIVE_PILOT_REPO, SOVA_LIVE_PILOT_PROJECT and "
    "SOVA_LIVE_PILOT_ISSUE (docs/openai-only-runbook.md)",
)


def _config_value(project: Path, key: str) -> str:
    return _sova(project, "config", "get", key).stdout.strip().strip('"')


def _guarded_project() -> Path:
    """The pilot project, after refusing a production repository and a configuration that would spend on Claude."""
    project = Path(_PROJECT)
    refusal = pilot_repo_refusal(_REPO, _config_value(project, "github_repo"))
    if refusal is not None:
        pytest.fail(refusal)
    provider, runtime = _config_value(project, "llm.provider"), _config_value(project, "agent.runtime")
    if (provider, runtime) != ("openai", "codex"):
        pytest.fail(f"OpenAI-only needs llm.provider=openai and agent.runtime=codex, got {provider} and {runtime}")
    from sova.config.loader import load_config
    from sova.utils.diagnostics import claude_model_references

    claude = claude_model_references(load_config(project))
    if claude:
        pytest.fail(
            f"model fields still name Claude, Vertex or Gemini (export the runbook environment first): {claude}"
        )
    refusal = database_refusal(os.environ.get("SOVA_DATABASE_URL"), os.environ.get("SOVA_LIVE_PILOT_DB"))
    if refusal is not None:
        pytest.fail(refusal)
    return project


def _write_report(project: Path, issue: str, name: str, payload: dict[str, object]) -> Path:
    default = project / ".claude" / f"live-pilot-{name}-{issue}.json"
    report = Path(os.environ.get("SOVA_LIVE_PILOT_REPORT") or default)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"repo": _REPO, "issue": issue, **payload}, indent=2), encoding="utf-8")
    return report


@pytest.mark.evaluation
@_LIVE_SKIP
class TestLivePilot:
    def test_openai_only_run_records_models_tokens_and_cost_source(self, tmp_path: Path) -> None:
        project = _guarded_project()
        db = pilot_db_path(project)
        before = _max_run_id(db)
        env, claude_marker = claude_blocked_env(tmp_path)
        result = _sova(project, "run", _ISSUE, env=env)
        runs = _runs_since(db, _ISSUE, before)
        launched = claude_marker.read_text(encoding="utf-8") if claude_marker.exists() else ""
        _write_report(
            project, _ISSUE, "success", {"exit_code": result.returncode, "runs": runs, "claude_launches": launched}
        )

        assert not launched, f"a step tried to start the Claude CLI: {launched}"
        assert result.returncode == 0, result.stderr[-2000:]
        assert runs, f"sova run created no run for issue {_ISSUE} in {db}"
        unfinished = [run for run in runs if run["run_status"] in _UNFINISHED_STATUSES]
        assert not unfinished, f"a role did not finish (a paused run also exits 0): {unfinished}"
        records = [record for run in runs for record in run["cost_records"]]  # type: ignore[attr-defined]
        assert records, f"no cost records for issue {_ISSUE} in {db} after run {before}"
        configured = _config_value(project, "codex.model").lower()
        allowed = frozenset({configured}) if configured else frozenset()
        wrong = [record for record in records if forbidden_model(record["model"], allowed)]
        assert not wrong, f"calls that reached a non-OpenAI model: {wrong}"
        assert all(record["cost_source"] for record in records), "a call recorded no cost source"

    @pytest.mark.skipif(not _FAILURE_ISSUE, reason="set SOVA_LIVE_PILOT_FAILURE_ISSUE to a second safe issue")
    def test_unavailable_model_fails_the_run_instead_of_degrading(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = _guarded_project()
        # Codex runs codex.model whatever agent.model says, and an explicit route outranks agent.model, so the
        # bogus model goes on every setting the run actually uses and the routes are dropped.
        monkeypatch.setenv("SOVA_CODEX_MODEL", _BOGUS_MODEL.removeprefix("openai/"))
        for key in ("SOVA_AGENT_MODEL", "SOVA_LLM_MODEL", "SOVA_LLM_FALLBACK_MODEL"):
            monkeypatch.setenv(key, _BOGUS_MODEL)
        monkeypatch.delenv("SOVA_LLM_ROUTING", raising=False)
        db = pilot_db_path(project)
        before = _max_run_id(db)

        env, _ = claude_blocked_env(tmp_path)  # built after the overrides above, which live in os.environ
        result = _sova(project, "run", _FAILURE_ISSUE, env=env)
        runs = _runs_since(db, _FAILURE_ISSUE, before)
        _write_report(project, _FAILURE_ISSUE, "failure", {"exit_code": result.returncode, "runs": runs})

        assert result.returncode != 0, "a run on an unavailable model must not succeed"
        evidence = " ".join([result.stdout, result.stderr, *(str(run["error_message"]) for run in runs)]).lower()
        assert "not-a-real-model-1130" in evidence, "the failure does not name the unavailable model"
        assert not any(run["run_status"] == "done" for run in runs)
