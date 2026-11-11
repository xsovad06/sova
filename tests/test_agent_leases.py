"""Tests for sova.utils.agent_leases: the machine-wide count of active pipeline runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from sova.utils import agent_leases
from sova.utils.agent_leases import (
    LEASE_ENV_KEY,
    acquire_lease,
    active_agent_count,
    live_lease_pids,
    release_lease,
)


@pytest.fixture
def lease_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "leases"
    monkeypatch.setattr(agent_leases, "_LEASE_DIR", path)
    monkeypatch.delenv(LEASE_ENV_KEY, raising=False)
    return path


def _write_lease(directory: Path, pid: int, create_time: float | None, name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if create_time is None:
        create_time = psutil.Process(pid).create_time()
    if name is None:
        name = psutil.Process(pid).name()
    path = directory / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, "create_time": create_time, "name": name}), encoding="utf-8")
    return path


def _dead_pid() -> int:
    pid = 2**22 + 12345
    while psutil.pid_exists(pid):
        pid += 1
    return pid


class TestLiveLeases:
    def test_a_missing_directory_is_zero_leases(self, lease_dir: Path) -> None:
        assert live_lease_pids() == set()
        assert not lease_dir.exists()

    def test_a_live_lease_is_counted(self, lease_dir: Path) -> None:
        _write_lease(lease_dir, os.getpid(), None)
        assert live_lease_pids() == {os.getpid()}

    def test_a_dead_pid_is_pruned(self, lease_dir: Path) -> None:
        stale = _write_lease(lease_dir, os.getpid(), None)
        dead = lease_dir / f"{_dead_pid()}.json"
        dead.write_text(json.dumps({"pid": _dead_pid(), "create_time": 1.0, "name": "python"}), encoding="utf-8")
        assert live_lease_pids() == {os.getpid()}
        assert stale.exists()
        assert not dead.exists()

    def test_a_reused_pid_is_rejected_by_its_create_time(self, lease_dir: Path) -> None:
        path = _write_lease(lease_dir, os.getpid(), psutil.Process().create_time() - 3600)
        assert live_lease_pids() == set()
        assert not path.exists()

    def test_a_pid_reused_within_the_create_time_tolerance_is_rejected_by_its_name(self, lease_dir: Path) -> None:
        """A pid reused under a second after the holder started still matches its create time, not its name."""
        recorded = psutil.Process().create_time() - 0.5
        path = _write_lease(lease_dir, os.getpid(), recorded, name="not-this-process")
        assert live_lease_pids() == set()
        assert not path.exists()

    def test_a_create_time_within_the_tolerance_is_live(self, lease_dir: Path) -> None:
        """A clock adjustment shifts a re-read create time slightly; the lease stays live."""
        _write_lease(lease_dir, os.getpid(), psutil.Process().create_time() - 0.5)
        assert live_lease_pids() == {os.getpid()}

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "{not json",
            "[]",
            '{"pid": "x", "create_time": 1, "name": "python"}',
            '{"pid": 1}',
            '{"pid": 1, "create_time": 1}',
            '{"pid": -1, "create_time": 1, "name": "python"}',
            '{"pid": 0, "create_time": 1, "name": "python"}',
            '{"pid": 1, "create_time": NaN, "name": "python"}',
            '{"pid": 1, "create_time": 1, "name": 7}',
        ],
    )
    def test_a_corrupt_file_is_stale(self, lease_dir: Path, content: str) -> None:
        lease_dir.mkdir()
        path = lease_dir / "99.json"
        path.write_text(content, encoding="utf-8")
        assert live_lease_pids() == set()
        assert not path.exists()

    def test_a_non_utf8_file_is_stale(self, lease_dir: Path) -> None:
        lease_dir.mkdir()
        path = lease_dir / "99.json"
        path.write_bytes(b"\xff\xfe")
        assert live_lease_pids() == set()
        assert not path.exists()

    def test_an_unreadable_lease_is_counted_live_and_kept(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lease that cannot be read is unknown, not stale: it counts as an agent and the file stays."""
        path = _write_lease(lease_dir, os.getpid(), None)

        def deny(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", deny)
        assert agent_leases._live_leases() == {path: None}
        assert path.exists()

    def test_a_lease_released_during_the_count_is_skipped(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_lease(lease_dir, os.getpid(), None)

        def gone(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
            raise FileNotFoundError(str(self))

        monkeypatch.setattr(Path, "read_text", gone)
        assert live_lease_pids() == set()

    def test_a_process_that_denies_access_is_counted_live_and_the_others_still_count(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One uninspectable lease must not degrade every spawn to the static split."""
        denied = _write_lease(lease_dir, os.getpid(), None)
        readable = _write_lease(lease_dir, os.getppid(), None)
        real_process = psutil.Process

        def selective(pid: int | None = None) -> psutil.Process:
            if pid == os.getpid():
                raise psutil.AccessDenied(pid)
            return real_process(pid)

        monkeypatch.setattr(agent_leases.psutil, "Process", selective)
        assert agent_leases._live_leases() == {denied: None, readable: os.getppid()}
        assert active_agent_count() == 3
        assert denied.exists()

    def test_a_temp_file_from_an_atomic_write_is_ignored(self, lease_dir: Path) -> None:
        lease_dir.mkdir()
        temp = lease_dir / ".5.json.abc.tmp"
        temp.write_text("{", encoding="utf-8")
        assert live_lease_pids() == set()
        assert temp.exists()

    def test_a_failed_prune_does_not_fail_the_count(self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_lease(lease_dir, os.getpid(), None)
        (lease_dir / "7.json").write_text("junk", encoding="utf-8")

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(Path, "unlink", refuse)
        assert live_lease_pids() == {os.getpid()}

    def test_an_unreadable_directory_raises(self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lease_dir.mkdir()

        def deny(self: Path) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "iterdir", deny)
        with pytest.raises(OSError):
            live_lease_pids()


class TestActiveAgentCount:
    def test_an_unleased_spawn_counts_itself(self, lease_dir: Path) -> None:
        assert active_agent_count() == 1

    def test_an_unleased_caller_adds_one_to_the_leases(self, lease_dir: Path) -> None:
        _write_lease(lease_dir, os.getppid(), None)
        assert active_agent_count() == 2

    def test_a_leased_process_is_already_in_the_count(self, lease_dir: Path) -> None:
        _write_lease(lease_dir, os.getppid(), None)
        lease = acquire_lease()
        try:
            assert active_agent_count() == 2
        finally:
            release_lease(lease)

    def test_each_pipeline_of_one_process_counts_as_an_agent(self, lease_dir: Path) -> None:
        """`sova parallel` and the server's WatchLoop run several pipelines in one process (issue #1157)."""
        leases = [acquire_lease(inherit=False) for _ in range(3)]
        try:
            assert len({lease for lease in leases}) == 3
            assert live_lease_pids() == {os.getpid()}
            assert active_agent_count() == 3
        finally:
            for lease in leases:
                release_lease(lease)
        assert active_agent_count() == 1

    def test_a_process_with_a_lease_and_another_agent_elsewhere_counts_both(self, lease_dir: Path) -> None:
        _write_lease(lease_dir, os.getppid(), None)
        leases = [acquire_lease(inherit=False), acquire_lease(inherit=False)]
        try:
            assert active_agent_count() == 3
        finally:
            for lease in leases:
                release_lease(lease)

    def test_leases_from_two_projects_count_as_two_agents(self, lease_dir: Path) -> None:
        """The lease directory is machine-wide: agents of different projects are counted together."""
        project_a = _write_lease(lease_dir, os.getppid(), None)
        project_a.rename(lease_dir / "project-a.json")
        _write_lease(lease_dir, os.getpid(), None)
        assert agent_leases._live_leases().keys() == {lease_dir / "project-a.json", lease_dir / f"{os.getpid()}.json"}
        # Neither is a lease of the caller's own pipeline, so the spawn from this process is a third agent.
        assert active_agent_count() == 3

    def test_a_caller_outside_a_concurrent_pipeline_is_not_covered_by_it(self, lease_dir: Path) -> None:
        """The server's WatchLoop holds inherit=False leases; a dashboard request spawns an independent agent."""
        import asyncio

        async def scenario() -> tuple[int, int]:
            inside_counts: list[int] = []
            release_pipeline = asyncio.Event()
            pipeline_started = asyncio.Event()

            async def pipeline() -> None:
                with agent_leases.pipeline_lease(inherit=False):
                    inside_counts.append(active_agent_count())
                    pipeline_started.set()
                    await release_pipeline.wait()

            task = asyncio.create_task(pipeline())
            await pipeline_started.wait()
            # A sibling task, created outside the block, is the request handler: it belongs to no pipeline.
            outside = await asyncio.create_task(asyncio.to_thread(active_agent_count))
            release_pipeline.set()
            await task
            return inside_counts[0], outside

        inside, outside = asyncio.run(scenario())
        assert inside == 1
        assert outside == 2

    def test_a_released_lease_no_longer_covers_the_process(self, lease_dir: Path) -> None:
        lease = acquire_lease()
        release_lease(lease)
        assert active_agent_count() == 1

    def test_a_process_nested_in_a_leased_run_is_already_in_the_count(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_lease(lease_dir, os.getppid(), None)
        monkeypatch.setenv(LEASE_ENV_KEY, str(os.getppid()))
        assert active_agent_count() == 1

    def test_a_marker_naming_a_live_non_ancestor_counts_the_spawn(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live lease that is not this process's ancestor is another agent, not the caller's own run."""
        other = subprocess.Popen(
            [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"], stdout=subprocess.PIPE
        )
        try:
            # Wait for the exec to finish: before it the child still reports the parent's process name.
            assert other.stdout is not None
            assert other.stdout.readline().strip() == b"up"
            _write_lease(lease_dir, other.pid, None)
            monkeypatch.setenv(LEASE_ENV_KEY, str(other.pid))
            assert active_agent_count() == 2
        finally:
            other.kill()
            other.wait(timeout=10)
            assert other.stdout is not None
            other.stdout.close()

    def test_a_marker_is_trusted_when_the_ancestors_cannot_be_read(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unreadable ancestors keep the pre-check behaviour: the marked lease is the caller's own run."""
        _write_lease(lease_dir, os.getppid(), None)
        monkeypatch.setenv(LEASE_ENV_KEY, str(os.getppid()))

        def deny(self: psutil.Process) -> list[psutil.Process]:
            raise psutil.AccessDenied(os.getpid())

        monkeypatch.setattr(psutil.Process, "parents", deny)
        assert active_agent_count() == 1

    def test_a_marker_without_a_live_lease_counts_the_spawn(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LEASE_ENV_KEY, str(os.getpid()))
        assert active_agent_count() == 1


class TestAcquireAndRelease:
    def test_acquire_writes_a_live_lease_and_marks_the_environment(self, lease_dir: Path) -> None:
        path = acquire_lease()
        try:
            assert path is not None
            assert path.parent == lease_dir
            assert path.name.startswith(f"{os.getpid()}-")
            assert os.environ[LEASE_ENV_KEY] == str(os.getpid())
            assert live_lease_pids() == {os.getpid()}
        finally:
            release_lease(path)

    def test_a_second_lease_in_the_same_process_is_its_own_file(self, lease_dir: Path) -> None:
        first, second = acquire_lease(), acquire_lease()
        try:
            assert first is not None
            assert second is not None
            assert first != second
            assert len(list(lease_dir.glob("*.json"))) == 2
        finally:
            release_lease(first)
            release_lease(second)

    def test_the_marker_stays_until_the_last_marking_lease_is_released(self, lease_dir: Path) -> None:
        first, second = acquire_lease(), acquire_lease()
        release_lease(first)
        assert os.environ[LEASE_ENV_KEY] == str(os.getpid())
        release_lease(second)
        assert LEASE_ENV_KEY not in os.environ

    def test_a_lease_that_does_not_inherit_leaves_the_environment_alone(self, lease_dir: Path) -> None:
        path = acquire_lease(inherit=False)
        try:
            assert path is not None
            assert path.exists()
            assert LEASE_ENV_KEY not in os.environ
        finally:
            release_lease(path)

    def test_release_removes_the_lease_and_the_marker(self, lease_dir: Path) -> None:
        path = acquire_lease()
        release_lease(path)
        assert path is not None
        assert not path.exists()
        assert LEASE_ENV_KEY not in os.environ

    def test_a_nested_process_skips_acquiring(self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LEASE_ENV_KEY, str(os.getppid()))
        assert acquire_lease() is None
        assert not lease_dir.exists()
        release_lease(None)
        assert os.environ[LEASE_ENV_KEY] == str(os.getppid())

    def test_a_marker_inherited_from_a_run_this_process_outlived_is_ignored(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server an agent restarted keeps the run's marker after the run ends; its children still lease."""
        monkeypatch.setenv(LEASE_ENV_KEY, str(_dead_pid()))
        path = acquire_lease()
        try:
            assert path is not None
            assert path.name.startswith(f"{os.getpid()}-")
            assert os.environ[LEASE_ENV_KEY] == str(os.getpid())
        finally:
            release_lease(path)

    def test_an_unreadable_own_process_runs_without_a_lease(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def deny(pid: int) -> psutil.Process:
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(agent_leases.psutil, "Process", deny)
        assert acquire_lease() is None
        assert LEASE_ENV_KEY not in os.environ
        assert not lease_dir.exists()

    def test_a_write_failure_runs_without_a_lease(self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(agent_leases, "write_text_atomic", refuse)
        assert acquire_lease() is None
        assert LEASE_ENV_KEY not in os.environ
        assert active_agent_count() == 1

    def test_a_failed_release_is_logged_not_raised(self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = acquire_lease()

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError("busy")

        with monkeypatch.context() as patched:
            patched.setattr(Path, "unlink", refuse)
            release_lease(path)
        assert path is not None
        assert path.exists()
        assert live_lease_pids() == {os.getpid()}


class TestPipelineLease:
    def test_the_lease_is_held_in_the_block_and_released_after(self, lease_dir: Path) -> None:
        with agent_leases.pipeline_lease() as lease:
            assert lease is not None
            assert lease.exists()
            assert active_agent_count() == 1
        assert not lease.exists()

    def test_the_lease_is_released_when_the_block_raises(self, lease_dir: Path) -> None:
        with pytest.raises(RuntimeError):
            with agent_leases.pipeline_lease() as lease:
                raise RuntimeError("role crashed")
        assert lease is not None
        assert not lease.exists()


class TestLeaseForAChild:
    """The dashboard's command agents (`claude -p`) are spawned, not run in-process: the supervisor leases for them."""

    def _child(self) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"], stdout=subprocess.PIPE
        )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == b"up"
        return child

    def _stop(self, child: subprocess.Popen[bytes]) -> None:
        child.kill()
        child.wait(timeout=10)
        assert child.stdout is not None
        child.stdout.close()

    def test_a_child_lease_counts_the_child_and_leaves_the_supervisor_uncounted(self, lease_dir: Path) -> None:
        child = self._child()
        try:
            lease = agent_leases.acquire_lease_for(child.pid)
            assert lease is not None
            assert live_lease_pids() == {child.pid}
            # The supervisor spawning another agent is one more.
            assert active_agent_count() == 2
            assert LEASE_ENV_KEY not in os.environ
            release_lease(lease)
            assert live_lease_pids() == set()
            assert not lease.exists()
        finally:
            self._stop(child)

    def test_a_dead_child_lease_is_pruned(self, lease_dir: Path) -> None:
        child = self._child()
        lease = agent_leases.acquire_lease_for(child.pid)
        assert lease is not None
        self._stop(child)
        assert live_lease_pids() == set()
        assert not lease.exists()

    def test_a_process_under_a_leased_child_counts_once_and_takes_no_lease(
        self, lease_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `sova` or check inside the leased agent: the ancestor's lease is its own run."""
        child_lease = _write_lease(lease_dir, os.getppid(), None)
        supervised = child_lease.rename(lease_dir / f"{os.getppid()}-abc{agent_leases._SUPERVISED_SUFFIX}")
        assert active_agent_count() == 1
        assert acquire_lease() is None
        assert supervised.exists()

    def test_an_unreadable_child_identity_takes_no_lease(self, lease_dir: Path) -> None:
        assert agent_leases.acquire_lease_for(_dead_pid()) is None
        assert live_lease_pids() == set()
