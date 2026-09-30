"""Opt-in smoke tests against a real Codex CLI.

Unlike every other test in this repo, these shell out to the actual
``codex`` binary and make real (billable) requests against OpenAI's API.
They are gated two ways, per issue #946's design decision: the
``codex_smoke`` marker alone is not enough, because ``make test-py`` and CI
both collect this file (``pytest tests/`` runs with no ``-m`` deselection for
this marker). The ``skipif`` below is what actually keeps this test out of
the default run: it requires both ``SOVA_CODEX_SMOKE=1`` and an installed
``codex`` CLI.

Run locally with: ``make test-codex-smoke`` (requires ``codex login`` to
already be authenticated).

Every test here operates on a disposable repository under ``tmp_path``, never
the SOVA checkout: ``disposable_repo`` asserts this explicitly before
yielding. No test pushes, opens a PR, or calls any GitHub API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sova.config.models import CodexConfig
from sova.ipc.control import FileAgentProcess
from sova.ipc.runtime import CodexRuntime

pytestmark = [
    pytest.mark.codex_smoke,
    pytest.mark.timeout(300),  # overrides pyproject.toml's global 30s: a real Codex turn
    # legitimately takes minutes, not seconds.
    pytest.mark.skipif(
        os.environ.get("SOVA_CODEX_SMOKE") != "1" or shutil.which("codex") is None,
        reason="opt-in smoke test: set SOVA_CODEX_SMOKE=1 and install the codex CLI to run",
    ),
]


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607


def _git_common_dir(cwd: Path) -> Path:
    result = _run_git("rev-parse", "--git-common-dir", cwd=cwd)
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


@pytest.fixture
def disposable_repo(tmp_path: Path) -> Path:
    """A throwaway git repo under ``tmp_path``, verified to be outside the SOVA checkout."""
    repo = tmp_path / "codex-smoke-repo"
    repo.mkdir()
    _run_git("init", cwd=repo)
    _run_git("config", "user.name", "SOVA Codex Smoke Test", cwd=repo)
    _run_git("config", "user.email", "codex-smoke@example.invalid", cwd=repo)
    (repo / "README.md").write_text("# codex smoke test fixture\n", encoding="utf-8")
    _run_git("add", "README.md", cwd=repo)
    _run_git("commit", "-m", "initial commit", cwd=repo)

    repo_git_dir = _git_common_dir(repo)
    sova_git_dir = _git_common_dir(Path(__file__).resolve().parent)
    assert not repo_git_dir.is_relative_to(sova_git_dir), (
        f"disposable repo's .git ({repo_git_dir}) resolves inside the SOVA checkout "
        f"({sova_git_dir}); refusing to let Codex write here"
    )
    return repo


@pytest.fixture
def codex_output_dir(tmp_path: Path) -> Path:
    """Where a spawned Codex process writes stdout/stderr.

    Deliberately a sibling of ``disposable_repo``, never a directory inside
    it, so the read-only test's ``git status --porcelain`` comparison is not
    perturbed by the capture files themselves.
    """
    output_dir = tmp_path / "codex-output"
    output_dir.mkdir()
    return output_dir


async def _spawn(
    runtime: CodexRuntime,
    prompt: str,
    cwd: Path,
    output_dir: Path,
    *,
    read_only: bool = False,
) -> FileAgentProcess:
    """Spawn with file-based output, the way the dashboard actually spawns.

    ``output_dir`` is not optional here: a pipe-based spawn (``output_dir=None``)
    returns an ``AgentProcess`` whose stdout/stderr are OS pipes that nothing in
    this module drains, and ``codex exec --json`` emits a JSON line per reasoning
    step and per command. Once more than one pipe buffer's worth of unread output
    accumulates, Codex blocks on write and never exits, so ``await process.wait()``
    would hang until the 300s timeout instead of failing with a diagnosis. Files
    have no such backpressure, and they also leave the captured output on disk for
    a human to read after a failure.
    """
    return await runtime.spawn(prompt, cwd, output_dir=output_dir, run_label="smoke", read_only=read_only)


async def _spawn_and_wait(
    runtime: CodexRuntime,
    prompt: str,
    cwd: Path,
    output_dir: Path,
    *,
    read_only: bool = False,
) -> int:
    process = await _spawn(runtime, prompt, cwd, output_dir, read_only=read_only)
    try:
        return await process.wait()
    finally:
        if process.is_running:
            await process.stop()


def _codex_produced_events(output_dir: Path, run_label: str = "smoke") -> bool:
    """Whether Codex wrote at least one JSONL lifecycle event to captured stdout.

    Proves a run actually reached Codex's event stream. Without it a negative
    assertion ("the repository was not modified") passes vacuously whenever
    the CLI never ran a turn at all, e.g. it exited on an argument error or an
    expired login. An unreadable capture file counts as no events, which fails
    the assertion with a pointer to the stderr capture rather than raising.
    """
    stdout_path = output_dir / f"{run_label}.stdout"
    try:
        content = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            return True
    return False


class TestCodexEditAndTestTask:
    async def test_codex_executes_literal_shell_command_and_edits_repository(
        self, disposable_repo: Path, codex_output_dir: Path
    ) -> None:
        """A representative edit+verify task, framed the same way start_agent()/start_command()
        frame dashboard command prompts: a fenced bash block the model must run literally."""
        prompt = (
            "Run the following command in your bash shell. This is a CLI "
            "command, not a task description, so do not implement the work "
            "yourself. Execute it exactly as written and let it complete:\n\n"
            "```bash\necho smoke-test-ok > result.txt\n```"
        )
        runtime = CodexRuntime(config=CodexConfig(sandbox="workspace-write"))
        exit_code = await _spawn_and_wait(runtime, prompt, disposable_repo, codex_output_dir)

        assert exit_code == 0
        result_file = disposable_repo / "result.txt"
        assert result_file.exists()
        assert result_file.read_text(encoding="utf-8").strip() == "smoke-test-ok"


class TestCodexReadOnlySandbox:
    async def test_read_only_mode_cannot_modify_repository(self, disposable_repo: Path, codex_output_dir: Path) -> None:
        """read_only=True must be enforced by the sandbox, not just advisory prompt text:
        the repository must be byte-identical after the run regardless of what the model
        attempted."""
        before = _run_git("status", "--porcelain", cwd=disposable_repo).stdout

        prompt = (
            "Create a new file named danger.txt in the current directory "
            "containing the text 'should not exist'. Then stop."
        )
        runtime = CodexRuntime(config=CodexConfig(sandbox="workspace-write"))
        await _spawn_and_wait(runtime, prompt, disposable_repo, codex_output_dir, read_only=True)

        # Prove the run actually happened before trusting the negative assertions
        # below: an immediate CLI-level exit would leave the repo untouched too.
        assert _codex_produced_events(codex_output_dir), (
            "Codex produced no JSONL events, so 'the repository was unchanged' proves "
            f"nothing; see {codex_output_dir / 'smoke.stderr'}"
        )

        after = _run_git("status", "--porcelain", cwd=disposable_repo).stdout
        assert after == before
        assert not (disposable_repo / "danger.txt").exists()


class TestCodexFailurePropagation:
    async def test_invalid_model_produces_nonzero_exit(self, disposable_repo: Path, codex_output_dir: Path) -> None:
        """A deterministic failure mode (no real model exists by this name) rather than
        depending on model behavior, so this test is fast and does not burn a real request."""
        runtime = CodexRuntime(config=CodexConfig(model="sova-test-nonexistent-model-xyz"))
        exit_code = await _spawn_and_wait(runtime, "say hello", disposable_repo, codex_output_dir)

        assert exit_code != 0


class TestCodexCancellation:
    async def test_stop_mid_run_leaves_no_running_process(self, disposable_repo: Path, codex_output_dir: Path) -> None:
        runtime = CodexRuntime(config=CodexConfig(sandbox="read-only"))
        process = await _spawn(
            runtime,
            "Take your time: read every file in the current directory, one at a time, "
            "describing each in detail before moving to the next.",
            disposable_repo,
            codex_output_dir,
        )

        assert process.is_running
        await process.stop()

        assert not process.is_running
        exit_code = await process.wait()
        assert exit_code is not None
