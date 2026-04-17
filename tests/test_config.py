"""Tests for SOVA configuration loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sova.config.loader import load_config
from sova.config.models import ProjectConfig


def test_default_config() -> None:
    """Default config has sensible values."""
    cfg = ProjectConfig()
    assert cfg.base_branch == "main"
    assert cfg.task_source.type == "github"
    assert cfg.agent.model == "opus"
    assert cfg.agent.max_budget == Decimal("10.00")
    assert cfg.review.enabled is True
    assert cfg.review.max_rounds == 2
    assert cfg.commit.format == "conventional"
    assert cfg.roles.default == "developer"
    assert cfg.triage.auto_label is True
    assert cfg.triage.min_confidence == 0.7


def test_load_from_toml(tmp_path: Path) -> None:
    """Load config from a TOML file."""
    toml_content = """
[project]
github_repo = "user/repo"
github_user = "testuser"
base_branch = "develop"
test_cmd = "pytest"

[agent]
model = "sonnet"
max_budget = 5.00

[task_source]
type = "jira"

[review]
enabled = false
max_rounds = 3

[triage]
min_confidence = 0.8

[roles]
default = "researcher"

[roles.nicknames]
reviewer = "Koda"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"
    assert cfg.github_user == "testuser"
    assert cfg.base_branch == "develop"
    assert cfg.test_cmd == "pytest"
    assert cfg.agent.model == "sonnet"
    assert cfg.agent.max_budget == Decimal("5")
    assert cfg.task_source.type == "jira"
    assert cfg.review.enabled is False
    assert cfg.review.max_rounds == 3
    assert cfg.triage.min_confidence == 0.8
    assert cfg.roles.default == "researcher"
    assert cfg.roles.nicknames == {"reviewer": "Koda"}


def test_load_from_legacy_conf(tmp_path: Path) -> None:
    """Load config from a legacy shell-sourceable .conf file."""
    conf_dir = tmp_path / ".claude" / "scripts"
    conf_dir.mkdir(parents=True)
    conf_file = conf_dir / "pak-agent.conf"
    conf_file.write_text("""
# Project config
GITHUB_REPO="owner/myproject"
GITHUB_USER="myuser"
BASE_BRANCH="main"
TASK_SOURCE="github"
AGENT_MODEL="sonnet"
MAX_BUDGET="15.00"
REVIEW_ENABLED="false"
REVIEW_MAX_ROUNDS=1
CI_POLL_INTERVAL=30
WORKTREE_COPY_FILES=".env,.secrets"
NO_AI_COAUTHOR="true"
COMMIT_FORMAT="freeform"
WATCH_AUTO_SELECT_ISSUES="false"
""")

    cfg = load_config(tmp_path)
    assert cfg.github_repo == "owner/myproject"
    assert cfg.github_user == "myuser"
    assert cfg.agent.model == "sonnet"
    assert cfg.agent.max_budget == Decimal("15")
    assert cfg.review.enabled is False
    assert cfg.review.max_rounds == 1
    assert cfg.ci.poll_interval == 30
    assert cfg.worktree.copy_files == [".env", ".secrets"]
    assert cfg.commit.no_ai_coauthor is True
    assert cfg.commit.format == "freeform"
    assert cfg.watch.auto_select_issues is False


def test_toml_takes_precedence_over_legacy(tmp_path: Path) -> None:
    """TOML file takes precedence over legacy .conf."""
    # Create both files
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text("""
[project]
github_repo = "from-toml"
""")

    conf_dir = tmp_path / ".claude" / "scripts"
    conf_dir.mkdir(parents=True)
    conf_file = conf_dir / "pak-agent.conf"
    conf_file.write_text('GITHUB_REPO="from-legacy"\n')

    cfg = load_config(tmp_path)
    assert cfg.github_repo == "from-toml"


def test_load_nonexistent_dir_returns_defaults(tmp_path: Path) -> None:
    """Loading from a directory without config files returns defaults."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    cfg = load_config(empty_dir)
    assert cfg.base_branch == "main"
    assert cfg.task_source.type == "github"


def test_shared_knowledge_path_expansion() -> None:
    """Shared knowledge dir expands ~ to home."""
    cfg = ProjectConfig()
    path = cfg.shared_knowledge_path
    assert "~" not in str(path)
    assert path.is_absolute()
