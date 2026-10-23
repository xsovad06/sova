"""Tests that Dependabot's PRs can pass this repository's own checks.

Every Dependabot PR used to fail three checks for reasons unrelated to the
dependency it bumped: ``invariants/branch-naming.sh`` rejected its
``dependabot/...`` branch, ``invariants/commit-format.sh`` rejected its default
``build(deps): ...`` subject, and the Fork PR Safety Gate treated it as an
outside contributor (its ``author_association`` is ``NONE``) and then crashed
posting its comment. These tests pin each fix, plus the grouping config that
keeps a week's bumps down to a few PRs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import InvariantRepo

ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
FORK_GATE = ROOT / ".github" / "workflows" / "fork-pr-gate.yml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestBranchNaming:
    @pytest.mark.parametrize(
        "branch",
        [
            "dependabot/uv/mcp-2.3.0",
            "dependabot/uv/uvicorn-standard--gte-0.54.0-and-lt-1.0",
            "dependabot/github_actions/actions/checkout-7",
            "dependabot/github_actions/SonarSource/sonarqube-scan-action-8",
            "dependabot/uv/python-minor-patch-1a2b3c4d5e",
        ],
    )
    def test_dependabot_branches_pass(self, invariant_repo: InvariantRepo, branch: str) -> None:
        assert invariant_repo.run("branch-naming.sh", GITHUB_HEAD_REF=branch).returncode == 0

    @pytest.mark.parametrize("branch", ["dependabotx/uv/mcp", "my-dependabot/mcp", "feature/thing", "fix/Upper"])
    def test_other_nonconforming_branches_still_fail(self, invariant_repo: InvariantRepo, branch: str) -> None:
        assert invariant_repo.run("branch-naming.sh", GITHUB_HEAD_REF=branch).returncode == 1


class TestCommitFormat:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            pytest.param("chore(deps): bump mcp from 1.30.0 to 2.3.0", 0, id="deps scope"),
            pytest.param("ci: bump the github-actions group with 4 updates", 0, id="scopeless ci"),
            pytest.param("build(deps): bump mcp from 1.30.0 to 2.3.0", 1, id="dependabot default still rejected"),
        ],
    )
    def test_subject(self, invariant_repo: InvariantRepo, subject: str, expected: int) -> None:
        invariant_repo.start_branch()
        invariant_repo.write("pyproject.toml", "[project]\n")
        invariant_repo.commit(subject)
        assert invariant_repo.run("commit-format.sh").returncode == expected


class TestDependabotConfig:
    def test_every_ecosystem_is_grouped(self) -> None:
        for update in _load(DEPENDABOT)["updates"]:
            assert update.get("groups"), f"{update['package-ecosystem']} has no groups: one PR per dependency"

    def test_python_majors_stay_ungrouped(self) -> None:
        (uv,) = [u for u in _load(DEPENDABOT)["updates"] if u["package-ecosystem"] == "uv"]
        for name, group in uv["groups"].items():
            assert "major" not in group.get("update-types", []), f"group {name} would bundle a major Python bump"
            assert "patterns" not in group, f"group {name} matches by pattern, which includes majors"

    @pytest.mark.parametrize("ecosystem", ["uv", "github-actions"])
    def test_commit_prefix_passes_commit_format(self, invariant_repo: InvariantRepo, ecosystem: str) -> None:
        (update,) = [u for u in _load(DEPENDABOT)["updates"] if u["package-ecosystem"] == ecosystem]
        prefix = update["commit-message"]["prefix"]
        assert "include" not in update["commit-message"], "include: scope would append a second (deps) scope"
        invariant_repo.start_branch()
        invariant_repo.write("pyproject.toml", "[project]\n")
        # Dependabot joins a prefix ending in a letter or ")" with ": "
        invariant_repo.commit(f"{prefix}: bump example from 1.0 to 2.0")
        assert invariant_repo.run("commit-format.sh").returncode == 0


class TestForkGate:
    @pytest.fixture
    def gate_job(self) -> dict[str, Any]:
        return _load(FORK_GATE)["jobs"]["gate"]

    def test_same_repo_prs_are_skipped(self, gate_job: dict[str, Any]) -> None:
        condition = " ".join(gate_job["if"].split())
        assert condition.startswith("github.event.pull_request.head.repo.full_name != github.repository && ("), (
            "the fork check must apply to every association, not just one branch of the OR"
        )

    def test_every_gh_pr_command_names_the_repo(self, gate_job: dict[str, Any]) -> None:
        for step in gate_job["steps"]:
            for line in step.get("run", "").splitlines():
                if line.strip().startswith("gh pr "):
                    assert "--repo" in line, f"step {step['name']!r} has no checkout to infer the repo from: {line}"
