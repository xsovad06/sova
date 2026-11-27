"""Per-project commit types and scopes (issue #1520): resolver, lazy loading, persistence, doctor."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine

from sova.config import commit_lists
from sova.config.commit_lists import (
    FALLBACK_TYPES,
    _git_subjects,
    clear_resolution_cache,
    derive_from_subjects,
    is_sova_repository,
    parse_docs_lists,
    resolve_commit_lists,
    resolve_commit_lists_cached,
)
from sova.config.db_loader import read_stored_settings
from sova.config.loader import (
    _looks_like_sova_scopes,
    commit_convention_findings,
    invalidate_config_cache,
    load_config,
    persist_commit_lists,
    unset_commit_kinds,
)
from sova.config.models import DEFAULT_COMMIT_SCOPES, DEFAULT_COMMIT_TYPES
from sova.db.models import Base, ProjectSetting


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _track(path: Path, name: str = "tracked.txt") -> None:
    """Add a tracked file under *path*: the resolver only trusts a repository that tracks the project's files."""
    (path / name).write_text("x\n", encoding="utf-8")
    _git(path, "add", name)


def _repo(path: Path, subjects: list[str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if subjects:
        _track(path)
    for subject in subjects:
        _git(path, "commit", "-q", "--allow-empty", "-m", subject)
    return path


def _make_db(path: Path, rows: dict[str, str] | None = None) -> None:
    (path / ".claude").mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{path / '.claude' / 'sova.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if rows:
            conn.execute(ProjectSetting.__table__.insert(), [{"key": k, "value": v} for k, v in rows.items()])
    engine.dispose()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    monkeypatch.delenv("SOVA_COMMIT_SCOPES", raising=False)
    monkeypatch.delenv("SOVA_COMMIT_TYPES", raising=False)
    clear_resolution_cache()


@pytest.fixture(autouse=True)
def _small_evidence_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixtures here commit a handful of subjects; tests of the real threshold put it back.
    monkeypatch.setattr("sova.config.commit_lists.MIN_CONVENTIONAL_SUBJECTS", 1)


@pytest.fixture
def no_git_above(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare tmp_path has no repository above it, whatever the machine's temp directory sits inside."""
    monkeypatch.setattr("sova.config.commit_lists._has_git_ancestor", lambda _dir: False)


class TestParseDocsLists:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("Valid scopes: api, web, infra", ["api", "web", "infra"]),
            ("- **Valid scopes:** `API`, web,  api, Infra.", ["api", "web", "infra"]),
            ("**Valid scopes**: a_b, c-d", ["a_b", "c-d"]),
            ("valid SCOPES: web", ["web"]),
            ("Valid scopes:", []),
            ("Valid scopes: ., ;", []),
            ("* __Valid scopes__:__ api, web", ["api", "web"]),
            ("+ Valid scopes : api", ["api"]),
            ("Not valid scopes: api", []),
        ],
    )
    def test_scope_line_is_normalized(self, tmp_path: Path, line: str, expected: list[str]) -> None:
        (tmp_path / "AGENTS.md").write_text(f"# Rules\n\n{line}\n", encoding="utf-8")
        assert parse_docs_lists(tmp_path).get("scopes", []) == expected

    def test_agents_md_wins_and_claude_md_fills_the_other_list(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("Valid scopes: api\n", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("Valid scopes: web\nValid types: feat, fix\n", encoding="utf-8")
        assert parse_docs_lists(tmp_path) == {"scopes": ["api"], "types": ["feat", "fix"]}

    def test_parse_docs_lists_first_non_empty_line_per_kind_wins(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("Valid scopes:\nValid scopes: api\nValid scopes: web\n", encoding="utf-8")
        assert parse_docs_lists(tmp_path) == {"scopes": ["api"]}

    def test_parse_docs_lists_long_whitespace_line_is_linear(self, tmp_path: Path) -> None:
        line = "Valid scopes:" + " " * 50_000 + "x\t" * 10_000 + "!"
        (tmp_path / "AGENTS.md").write_text(line + "\n" + " " * 50_000 + "valid\n", encoding="utf-8")
        started = time.monotonic()
        parse_docs_lists(tmp_path)
        assert time.monotonic() - started < 1.0

    def test_a_non_utf8_file_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_bytes(b"Valid scopes: \xff\xfe\n")
        (tmp_path / "CLAUDE.md").write_text("Valid scopes: web\n", encoding="utf-8")
        assert parse_docs_lists(tmp_path) == {"scopes": ["web"]}


class TestDeriveFromSubjects:
    def test_squash_suffix_stripped_and_rare_scope_dropped(self) -> None:
        derived = derive_from_subjects(
            [
                "feat(api): add thing (#125)",
                "fix(api): repair thing",
                "fix(web): once",
                'Revert "feat(api): add thing"',
                "wip(api): not a type",
                "plain subject",
            ]
        )
        assert derived == {"types": ["fix", "feat"], "scopes": ["api"]}

    def test_scopeless_ci_counts_as_a_type(self) -> None:
        assert derive_from_subjects(["ci: add job"]) == {"types": ["ci"], "scopes": []}

    @pytest.mark.parametrize(
        "subject",
        ["fix(api): a (#1)", "fix(api): a(#12)", "  fix(api): a   (#123)  ", "fix(api): a (#1)\t"],
    )
    def test_derive_from_subjects_squash_suffix_with_surrounding_whitespace(self, subject: str) -> None:
        assert derive_from_subjects([subject, "fix(api): b"]) == {"types": ["fix"], "scopes": ["api"]}

    def test_derive_from_subjects_long_whitespace_run_is_linear(self) -> None:
        subject = "fix(api): a" + " " * 50_000 + "(#" + " " * 50_000
        started = time.monotonic()
        derive_from_subjects([subject])
        assert time.monotonic() - started < 1.0


class TestIsSovaRepository:
    def _sova(self, root: Path, name: str = "sova") -> Path:
        (root / "sova" / "config").mkdir(parents=True)
        (root / "sova" / "config" / "models.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
        return root

    def test_a_worktree_path_is_still_sova(self, tmp_path: Path) -> None:
        assert is_sova_repository(self._sova(tmp_path / ".claude" / "worktrees" / "1520"))

    def test_another_project_with_a_sova_directory_is_not(self, tmp_path: Path) -> None:
        assert not is_sova_repository(self._sova(tmp_path, name="other"))

    def test_is_sova_repository_without_pyproject(self, tmp_path: Path) -> None:
        root = self._sova(tmp_path)
        (root / "pyproject.toml").unlink()
        assert not is_sova_repository(root)

    def test_is_sova_repository_with_invalid_toml(self, tmp_path: Path) -> None:
        root = self._sova(tmp_path)
        (root / "pyproject.toml").write_text("[project\nname = ", encoding="utf-8")
        assert not is_sova_repository(root)


class TestGitSubjects:
    def test_git_subjects_spawn_failure_is_none(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat: a"])
        with patch("sova.utils.shell.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            assert _git_subjects(repo) is None

    def test_git_subjects_other_git_failure_is_none(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat: a"])
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: bad object HEAD")
        with patch("sova.utils.shell.subprocess.run", return_value=failed):
            assert _git_subjects(repo) is None

    def test_git_subjects_outside_a_repository_is_none(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert _git_subjects(tmp_path) is None


class TestResolveCommitLists:
    def test_sova_repository_keeps_its_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "sova" / "config").mkdir(parents=True)
        (tmp_path / "sova" / "config" / "models.py").write_text("", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "sova"\n', encoding="utf-8")
        resolved = resolve_commit_lists(tmp_path)
        assert resolved["scopes"].values == list(DEFAULT_COMMIT_SCOPES)
        assert resolved["types"].values == list(DEFAULT_COMMIT_TYPES)

    def test_docs_win_over_git_and_lists_resolve_independently(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(git): a", "feat(git): b"])
        (repo / "AGENTS.md").write_text("Valid scopes: api, web\n", encoding="utf-8")
        resolved = resolve_commit_lists(repo)
        assert (resolved["scopes"].values, resolved["scopes"].source) == (["api", "web"], "docs")
        assert (resolved["types"].values, resolved["types"].source) == (list(FALLBACK_TYPES), "git")

    def test_no_commits_gives_empty_source_fallback_types_and_no_scopes(self, tmp_path: Path) -> None:
        resolved = resolve_commit_lists(_repo(tmp_path, []))
        assert resolved["types"].values == list(FALLBACK_TYPES)
        assert resolved["scopes"].values == []
        assert {entry.source for entry in resolved.values()} == {"empty"}

    @pytest.mark.usefixtures("no_git_above")
    def test_unreadable_history_is_unresolved_not_fallback(self, tmp_path: Path) -> None:
        resolved = resolve_commit_lists(tmp_path)
        assert {entry.source for entry in resolved.values()} == {"unresolved"}


class TestLoadConfigResolvesLazily:
    def test_unset_lists_come_from_the_project_without_writing(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "fix(billing): b", "docs(accounts): c", "docs(accounts): d"])
        _make_db(repo, {"github_repo": '"a/b"'})
        commit = load_config(repo).commit
        assert commit.scopes == ["accounts", "billing"]
        assert read_stored_settings(repo) == {"github_repo": "a/b"}

    def test_an_explicit_empty_scope_list_is_kept(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo, {"commit.scopes": "[]"})
        commit = load_config(repo).commit
        assert commit.scopes == []
        assert commit.types == list(FALLBACK_TYPES)

    def test_an_env_override_counts_as_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        monkeypatch.setenv("SOVA_COMMIT_SCOPES", '["envscope"]')
        assert load_config(repo).commit.scopes == ["envscope"]
        assert unset_commit_kinds(repo) == ("types",)


class TestPersistCommitLists:
    def test_stores_unset_keys_once_and_never_overwrites(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo, {"commit.types": '["feat", "fix"]'})

        first = persist_commit_lists(repo)
        assert set(first) == {"scopes"}
        stored = read_stored_settings(repo)
        assert stored is not None
        assert stored["commit.scopes"] == ["billing"]
        assert stored["commit.types"] == ["feat", "fix"]

        _git(repo, "commit", "-q", "--allow-empty", "-m", "feat(newscope): x")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "feat(newscope): y")
        invalidate_config_cache(repo)
        assert persist_commit_lists(repo) == {}
        assert read_stored_settings(repo) == stored

    def test_an_unreadable_database_stores_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A locked database reads as no rows, so every stored key would look unset and be overwritten.
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo, {"commit.scopes": '["kept"]'})
        monkeypatch.setattr("sova.config.db_loader._try_load_from_db", lambda _dir: None)
        monkeypatch.setattr("sova.config.db_loader.db_read_failed", lambda: True)
        assert persist_commit_lists(repo) == {}
        monkeypatch.undo()
        assert read_stored_settings(repo) == {"commit.scopes": ["kept"]}

    @pytest.mark.usefixtures("no_git_above")
    def test_unreadable_history_stores_nothing(self, tmp_path: Path) -> None:
        _make_db(tmp_path)
        assert persist_commit_lists(tmp_path) == {}
        assert read_stored_settings(tmp_path) == {}


class TestDoctorFindings:
    @pytest.mark.usefixtures("no_git_above")
    def test_default_lists_and_unresolved_history_are_separate_findings(self, tmp_path: Path) -> None:
        _make_db(tmp_path, {"commit.scopes": json_list(DEFAULT_COMMIT_SCOPES)})
        names = [name for name, _ in commit_convention_findings(tmp_path)]
        assert names == ["commit convention", "commit.scopes"]

    def test_a_derived_project_has_no_findings(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo)
        assert commit_convention_findings(repo) == []


def json_list(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(list(values))


class TestEmptyHistoryIsNotPersisted:
    def test_persist_waits_for_commits_then_stores_derived_scopes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, [])
        _track(repo)
        _make_db(repo)
        resolved = resolve_commit_lists(repo)
        assert {entry.source for entry in resolved.values()} == {"empty"}

        assert persist_commit_lists(repo) == {}
        assert read_stored_settings(repo) == {}
        assert load_config(repo).commit.scopes == []

        _git(repo, "commit", "-q", "--allow-empty", "-m", "feat(billing): a")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "fix(billing): b")
        invalidate_config_cache(repo)
        stored = persist_commit_lists(repo)
        assert set(stored) == {"types", "scopes"}
        persisted = read_stored_settings(repo)
        assert persisted is not None
        assert persisted["commit.scopes"] == ["billing"]


class TestGitHistoryLocation:
    def test_a_subdirectory_of_a_repository_reads_the_whole_history(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        package = repo / "packages" / "app"
        package.mkdir(parents=True)
        _track(package)
        assert _git_subjects(package) == ["feat(billing): b", "feat(billing): a"]
        assert resolve_commit_lists(package)["scopes"].values == ["billing"]

    def test_an_empty_repository_is_empty_not_unresolved_under_any_locale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Detection is by exit status, so a localized git message cannot turn it into "unresolved".
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        monkeypatch.setenv("LANGUAGE", "de")
        assert _git_subjects(_repo(tmp_path, [])) == []

    @pytest.mark.usefixtures("no_git_above")
    def test_a_plain_directory_is_unresolved(self, tmp_path: Path) -> None:
        assert _git_subjects(tmp_path) is None

    def test_an_inherited_git_dir_does_not_redirect_the_history_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A git hook exports GIT_DIR: without stripping it, another repository's history would be read.
        other = _repo(tmp_path / "other", ["feat(elsewhere): a", "feat(elsewhere): b"])
        mine = _repo(tmp_path / "mine", ["feat(billing): a", "feat(billing): b"])
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
        assert _git_subjects(mine) == ["feat(billing): b", "feat(billing): a"]


class TestResolutionCache:
    def test_a_reload_does_not_rerun_git_until_the_cache_is_cleared(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        with patch("sova.config.commit_lists._git_subjects", wraps=_git_subjects) as spy:
            first = resolve_commit_lists_cached(repo)
            second = resolve_commit_lists_cached(repo)
            assert first == second
            assert spy.call_count == 1
            clear_resolution_cache(repo)
            resolve_commit_lists_cached(repo)
            assert spy.call_count == 2

    def test_editing_a_doc_is_seen_at_once(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        assert resolve_commit_lists_cached(repo)["scopes"].values == ["billing"]
        doc = repo / "AGENTS.md"
        doc.write_text("Valid scopes: api\n", encoding="utf-8")
        assert resolve_commit_lists_cached(repo)["scopes"].values == ["api"]

    def test_the_entry_expires(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        resolve_commit_lists_cached(repo)
        monkeypatch.setattr("sova.config.commit_lists._RESOLUTION_TTL_S", 0.0)
        with patch("sova.config.commit_lists._git_subjects", wraps=_git_subjects) as spy:
            resolve_commit_lists_cached(repo)
        assert spy.call_count == 1


class TestPersistFromDocs:
    def test_docs_lists_are_stored_over_git_history(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(gitscope): a", "feat(gitscope): b"])
        (repo / "AGENTS.md").write_text("Valid types: feat, fix\nValid scopes: api, web\n", encoding="utf-8")
        _make_db(repo)
        stored = persist_commit_lists(repo)
        assert {kind: (entry.values, entry.source) for kind, entry in stored.items()} == {
            "types": (["feat", "fix"], "docs"),
            "scopes": (["api", "web"], "docs"),
        }
        persisted = read_stored_settings(repo)
        assert persisted is not None
        assert persisted["commit.types"] == ["feat", "fix"]
        assert persisted["commit.scopes"] == ["api", "web"]


class TestCommandsUpdateWiring:
    def _invoke(self, repo: Path) -> object:
        from typer.testing import CliRunner

        from sova.cli.app import app

        done = MagicMock(updated=0, skipped=0, conflicts=[])
        with (
            patch("sova.cli.commands.commands.update_commands", return_value=done) as sync,
            patch("sova.cli.commands.commands.get_canonical_dir", return_value=repo),
        ):
            result = CliRunner().invoke(app, ["commands", "update", "--project", str(repo)])
        self.sync = sync
        return result

    def test_update_stores_the_docs_lists(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, [])
        (repo / "AGENTS.md").write_text("Valid types: feat, fix\nValid scopes: api, web\n", encoding="utf-8")
        _make_db(repo)
        result = self._invoke(repo)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        persisted = read_stored_settings(repo)
        assert persisted is not None
        assert persisted["commit.types"] == ["feat", "fix"]
        assert persisted["commit.scopes"] == ["api", "web"]

    def test_a_failing_persist_does_not_stop_the_sync(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, [])
        _make_db(repo)
        with patch("sova.cli.commands.commands.persist_commit_lists", side_effect=RuntimeError("boom")):
            result = self._invoke(repo)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        self.sync.assert_called_once()


class TestDoctorWiring:
    def test_a_non_sova_project_on_sovas_scopes_gets_the_warning_row(self, tmp_path: Path) -> None:
        from sova.utils.diagnostics import _check_commit_convention

        repo = _repo(tmp_path, ["feat(billing): a"])
        _make_db(repo, {"commit.scopes": json_list(DEFAULT_COMMIT_SCOPES)})
        checks = _check_commit_convention(repo)
        assert [(check.name, check.passed, check.required) for check in checks] == [("commit.scopes", False, False)]

    def test_default_types_alone_are_not_flagged(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a"])
        _make_db(repo, {"commit.types": json_list(DEFAULT_COMMIT_TYPES), "commit.scopes": '["billing"]'})
        assert commit_convention_findings(repo) == []

    def test_run_all_checks_includes_the_commit_convention_rows(self, tmp_path: Path) -> None:
        import asyncio

        from sova.utils import diagnostics as mod

        row = mod.DiagnosticCheck("commit.scopes", False, "x", False)
        with (
            patch.object(mod, "_check_gh_cli", new_callable=AsyncMock, return_value=[]),
            patch.object(mod, "_check_commit_convention", return_value=[row]),
        ):
            checks = asyncio.run(mod.run_all_checks(tmp_path))
        assert row in checks

    def test_findings_from_a_subdirectory_use_the_owning_project(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo)
        package = repo / "pkg"
        package.mkdir()
        _track(package)
        assert commit_convention_findings(package) == commit_convention_findings(repo) == []


class TestEmptyScopePrompts:
    def test_commit_subject_prompt_offers_a_free_choice_of_scope(self, tmp_path: Path) -> None:
        import asyncio

        from sova.config.models import CommitConfig, ProjectConfig
        from sova.core.context import ExecutionContext
        from sova.core.steps import commit as commit_mod

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(commit=CommitConfig(types=["feat", "fix"], scopes=[])),
            adapter=MagicMock(),
            issue_number="1",
            role="developer",
        )
        survey = MagicMock()
        survey.summary.return_value = "a.py | 1"
        seen: dict[str, str] = {}

        async def fake_invoke(prompt: str, **_: object) -> MagicMock:
            seen["prompt"] = prompt
            return MagicMock(is_error=True, stop_reason="x")

        with patch.object(commit_mod, "invoke", fake_invoke):
            asyncio.run(commit_mod.CommitStep()._propose_commit_subject(ctx, "Add thing", survey))
        prompt = seen["prompt"]
        assert '"type: subject" or "type(scope): subject"' in prompt
        assert "only when type is" not in prompt
        assert "scope must be a short lowercase name" in prompt

    def test_pr_title_prompt_offers_a_free_choice_of_scope(self, tmp_path: Path) -> None:
        from sova.config.models import CommitConfig, ProjectConfig
        from sova.core.context import ExecutionContext
        from sova.core.steps._pr_body import build_prompt

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(commit=CommitConfig(types=["feat", "fix"], scopes=[])),
            adapter=MagicMock(),
            issue_number="1",
            role="developer",
        )
        prompt = build_prompt(
            ctx, task_title="t", issue_body="", commit_log="c", diff_stat="s", diff_content="d", request_title=True
        )
        assert '"type: subject" or "type(scope): subject"' in prompt
        assert "only when type is" not in prompt
        assert "scope is a short lowercase name" in prompt

    def test_configured_scopes_keep_the_strict_form(self) -> None:
        from sova.core.steps.commit_convention import describe_line_form, describe_scope_choice

        assert "only when type is" in describe_line_form(["api"])
        assert describe_scope_choice(["api", "web"]) == "one of: api, web"


def _commit_as(path: Path, author: str, subject: str) -> None:
    _git(path, "-c", f"user.name={author}", "commit", "-q", "--allow-empty", "-m", subject)


class TestReviewFindings:
    def test_a_dependency_bot_scope_never_leads_the_scopes(self) -> None:
        subjects = ["chore(deps): bump x"] * 6 + ["feat(billing): a", "feat(billing): b", "fix(accounts): c"] * 1
        subjects += ["fix(accounts): d"]
        scopes = derive_from_subjects(subjects)["scopes"]
        assert scopes[0] != "deps"
        assert scopes[-1] == "deps"

    def test_bot_authored_commits_are_left_out_of_the_history(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a"])
        _commit_as(repo, "dependabot[bot]", "chore(deps): bump x")
        _commit_as(repo, "Dev", "fix(billing): b")
        assert _git_subjects(repo) == ["fix(billing): b", "feat(billing): a"]

    def test_git_types_extend_the_conventional_set_instead_of_replacing_it(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["perf(core): a", "perf(core): b", "feat(core): c"])
        types = resolve_commit_lists(repo)["types"]
        assert types.source == "git"
        assert types.values == ["perf", "feat", "fix", "refactor", "test", "docs", "chore"]

    def test_too_little_history_is_not_evidence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sova.config.commit_lists.MIN_CONVENTIONAL_SUBJECTS", 20)
        repo = _repo(tmp_path, ["feat(init): a", "feat(init): b", "feat(init): c"])
        _make_db(repo)
        resolved = resolve_commit_lists(repo)
        assert {entry.source for entry in resolved.values()} == {"empty"}
        assert resolved["scopes"].values == []
        assert persist_commit_lists(repo) == {}
        assert read_stored_settings(repo) == {}

    def test_a_history_with_no_conventional_subject_is_not_persisted(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["Add thing", "Fix other thing"])
        _make_db(repo)
        assert {entry.source for entry in resolve_commit_lists(repo).values()} == {"empty"}
        assert persist_commit_lists(repo) == {}
        assert read_stored_settings(repo) == {}

    def test_a_fallback_source_is_never_persisted(self, tmp_path: Path) -> None:
        # Enough conventional history, but no scope used twice: scopes resolve to the fallback and stay lazy.
        repo = _repo(tmp_path, ["feat: a", "fix: b"])
        _make_db(repo)
        resolved = resolve_commit_lists(repo)
        assert resolved["scopes"].source == "fallback"
        assert set(persist_commit_lists(repo)) == {"types"}
        stored = read_stored_settings(repo)
        assert stored is not None
        assert "commit.scopes" not in stored

    def test_an_enclosing_repository_that_tracks_nothing_here_is_unresolved(self, tmp_path: Path) -> None:
        outer = _repo(tmp_path, ["feat(dotfiles): a", "feat(dotfiles): b"])
        project = outer / "project"
        project.mkdir()
        assert _git_subjects(project) is None
        assert {entry.source for entry in resolve_commit_lists(project).values()} == {"unresolved"}

    def test_unreadable_work_tree_probe_is_logged(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: dubious ownership")
        with (
            patch("sova.utils.shell.subprocess.run", return_value=failed),
            patch.object(commit_lists.log, "warning") as warning,
        ):
            assert _git_subjects(tmp_path) is None
        assert warning.call_args.args[0] == "commit_lists.git_log_failed"
        assert "dubious ownership" in warning.call_args.kwargs["stderr"]

    def test_the_resolution_cache_holds_one_entry_per_project_and_kinds(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        resolve_commit_lists_cached(repo)
        for index in range(3):
            (repo / "AGENTS.md").write_text(f"Valid scopes: api{index}\n", encoding="utf-8")
            time.sleep(0.01)
            assert resolve_commit_lists_cached(repo)["scopes"].values == [f"api{index}"]
        assert len(commit_lists._resolution_cache) == 1

    def test_an_explicit_value_saved_after_the_unset_check_survives_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo)
        real = unset_commit_kinds

        def racing(project_dir: Path):  # type: ignore[no-untyped-def]
            unset = real(project_dir)
            if unset:
                # The user saves commit.scopes in Settings between the check and the write.
                engine = create_engine(f"sqlite:///{repo / '.claude' / 'sova.db'}")
                with engine.begin() as conn:
                    conn.execute(ProjectSetting.__table__.insert(), [{"key": "commit.scopes", "value": '["mine"]'}])
                engine.dispose()
            return unset

        monkeypatch.setattr("sova.config.loader.unset_commit_kinds", racing)
        persist_commit_lists(repo)
        stored = read_stored_settings(repo)
        assert stored is not None
        assert stored["commit.scopes"] == ["mine"]

    @pytest.mark.parametrize(
        ("scopes", "expected"),
        [
            (list(DEFAULT_COMMIT_SCOPES), True),
            (["dashboard", "adapters", "supervisor", "scheduler", "legacy-scope"], True),
            (["supervisor", "adapters", "ipc"], True),
            (["api", "web", "dashboard"], False),
            (["dashboard", "docs", "core"], False),
            ([], False),
        ],
    )
    def test_a_partial_or_older_copy_of_sovas_scopes_is_flagged(self, scopes: list[str], expected: bool) -> None:
        assert _looks_like_sova_scopes(scopes) is expected


class TestConfigCacheSeesGuidanceEdits:
    def test_an_edited_agents_md_reaches_a_cached_load_config(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, ["feat(billing): a", "feat(billing): b"])
        _make_db(repo, {"github_repo": '"a/b"'})
        guidance = repo / "AGENTS.md"
        guidance.write_text("Valid scopes: alpha, beta\n", encoding="utf-8")
        assert load_config(repo).commit.scopes == ["alpha", "beta"]
        guidance.write_text("Valid scopes: gamma\n", encoding="utf-8")
        # Force a different mtime on a filesystem with a coarse clock.
        stat = guidance.stat()
        os.utime(guidance, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert load_config(repo).commit.scopes == ["gamma"]
