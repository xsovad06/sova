"""Tests for SOVA merge operations module."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from sova.config.models import IntegrationConfig
from sova.git.merge import (
    MergeQueueStatus,
    _build_merge_args,
    _get_pr_base_branch,
    delete_remote_branch,
    detect_merge_queue,
    get_merge_queue_status,
    handle_post_merge_state,
    merge_pr,
    poll_merge_queue,
    should_use_merge_queue,
)
from sova.utils.shell import ShellResult


def _shell_ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _shell_fail(stderr: str = "error", returncode: int = 1) -> ShellResult:
    return ShellResult(returncode=returncode, stdout="", stderr=stderr)


class TestMergeQueueStatus:
    def test_is_merged(self) -> None:
        s = MergeQueueStatus(in_queue=False, state="MERGED", position=None, estimated_time="")
        assert s.is_merged
        assert not s.is_failed

    def test_is_failed_unmergeable(self) -> None:
        s = MergeQueueStatus(in_queue=True, state="UNMERGEABLE", position=2, estimated_time="")
        assert s.is_failed
        assert not s.is_merged

    def test_is_failed_locked(self) -> None:
        s = MergeQueueStatus(in_queue=True, state="LOCKED", position=None, estimated_time="")
        assert s.is_failed

    def test_queued_not_merged_or_failed(self) -> None:
        s = MergeQueueStatus(in_queue=True, state="QUEUED", position=3, estimated_time="5m")
        assert not s.is_merged
        assert not s.is_failed


class TestBuildMergeArgs:
    def test_squash_no_queue(self) -> None:
        cfg = IntegrationConfig(merge_method="squash", delete_branch=True)
        args = _build_merge_args(42, repo="owner/repo", cfg=cfg, use_queue=False)
        assert "--squash" in args
        assert "--delete-branch" in args
        assert "42" in args

    def test_rebase_no_queue(self) -> None:
        cfg = IntegrationConfig(merge_method="rebase", delete_branch=True)
        args = _build_merge_args(42, repo="owner/repo", cfg=cfg, use_queue=False)
        assert "--rebase" in args
        assert "--delete-branch" in args

    def test_merge_no_queue(self) -> None:
        cfg = IntegrationConfig(merge_method="merge", delete_branch=False)
        args = _build_merge_args(42, repo="owner/repo", cfg=cfg, use_queue=False)
        assert "--merge" in args
        assert "--delete-branch" not in args

    def test_auto_no_queue_no_strategy_flag(self) -> None:
        cfg = IntegrationConfig(merge_method="auto", delete_branch=True)
        args = _build_merge_args(42, repo="owner/repo", cfg=cfg, use_queue=False)
        assert "--squash" not in args
        assert "--rebase" not in args
        assert "--merge" not in args
        assert "--delete-branch" in args

    def test_queue_omits_strategy_and_delete(self) -> None:
        cfg = IntegrationConfig(merge_method="squash", delete_branch=True)
        args = _build_merge_args(42, repo="owner/repo", cfg=cfg, use_queue=True)
        assert "--squash" not in args
        assert "--rebase" not in args
        assert "--merge" not in args
        assert "--delete-branch" not in args
        assert "42" in args


class TestDetectMergeQueue:
    async def test_queue_configured(self) -> None:
        response = json.dumps({"data": {"repository": {"mergeQueue": {"configuration": {"mergeMethod": "SQUASH"}}}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            result = await detect_merge_queue(repo="owner/repo", base_branch="main")
        assert result is True

    async def test_queue_not_configured(self) -> None:
        response = json.dumps({"data": {"repository": {"mergeQueue": {"configuration": None}}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            result = await detect_merge_queue(repo="owner/repo", base_branch="main")
        assert result is False

    async def test_no_merge_queue_field(self) -> None:
        response = json.dumps({"data": {"repository": {"mergeQueue": None}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            result = await detect_merge_queue(repo="owner/repo")
        assert result is False

    async def test_api_failure_returns_none(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_fail(stderr="network error")
            result = await detect_merge_queue(repo="owner/repo")
        assert result is None

    async def test_bad_json_returns_none(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout="not json")
            result = await detect_merge_queue(repo="owner/repo")
        assert result is None


class TestShouldUseMergeQueue:
    async def test_forced_true(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="true")
        result = await should_use_merge_queue(cfg, repo="owner/repo")
        assert result is True

    async def test_forced_false(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="false")
        result = await should_use_merge_queue(cfg, repo="owner/repo")
        assert result is False

    async def test_auto_detected(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="auto")
        with patch("sova.git.merge.detect_merge_queue", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = True
            result = await should_use_merge_queue(cfg, repo="owner/repo")
        assert result is True

    async def test_auto_not_detected(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="auto")
        with patch("sova.git.merge.detect_merge_queue", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = False
            result = await should_use_merge_queue(cfg, repo="owner/repo")
        assert result is False

    async def test_auto_detection_failure_fallback(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="auto")
        with patch("sova.git.merge.detect_merge_queue", new_callable=AsyncMock) as mock_detect:
            mock_detect.return_value = None
            result = await should_use_merge_queue(cfg, repo="owner/repo")
        assert result is False


class TestMergePR:
    async def test_direct_merge_success(self) -> None:
        cfg = IntegrationConfig(merge_method="squash", merge_queue_enabled="false")
        with (
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_q.return_value = False
            mock_run.return_value = _shell_ok(stdout="Merged")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg)
        assert result.success
        assert result.merged
        assert not result.enqueued
        assert not result.needs_poll

    async def test_merge_failure(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="false")
        with (
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_q.return_value = False
            mock_run.return_value = _shell_fail(stderr="merge conflict")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg)
        assert not result.success
        assert not result.merged
        assert "merge conflict" in result.message

    async def test_queue_enqueue(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="true")
        with (
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_q.return_value = True
            mock_run.return_value = _shell_ok(stdout="Added to merge queue")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg)
        assert result.success
        assert not result.merged
        assert result.enqueued
        assert result.needs_poll

    async def test_already_queued_detection(self) -> None:
        cfg = IntegrationConfig(merge_queue_enabled="false")
        with (
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_q.return_value = False
            mock_run.return_value = _shell_ok(stdout="Pull request is already queued to merge")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg)
        assert result.success
        assert result.enqueued
        assert result.needs_poll


class TestGetMergeQueueStatus:
    async def test_pr_merged(self) -> None:
        response = json.dumps({"data": {"repository": {"pullRequest": {"merged": True, "mergeQueueEntry": None}}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            status = await get_merge_queue_status(42, repo="owner/repo")
        assert status.is_merged
        assert not status.in_queue

    async def test_pr_in_queue(self) -> None:
        entry = {"state": "QUEUED", "position": 3, "estimatedTimeToMerge": "5m"}
        response = json.dumps({"data": {"repository": {"pullRequest": {"merged": False, "mergeQueueEntry": entry}}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            status = await get_merge_queue_status(42, repo="owner/repo")
        assert status.in_queue
        assert status.state == "QUEUED"
        assert status.position == 3

    async def test_pr_not_queued(self) -> None:
        response = json.dumps({"data": {"repository": {"pullRequest": {"merged": False, "mergeQueueEntry": None}}}})
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout=response)
            status = await get_merge_queue_status(42, repo="owner/repo")
        assert not status.in_queue
        assert status.state == "NOT_QUEUED"

    async def test_api_failure_returns_unknown(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_fail(stderr="error")
            status = await get_merge_queue_status(42, repo="owner/repo")
        assert status.in_queue
        assert status.state == "UNKNOWN"


class TestPollMergeQueue:
    async def test_immediate_merge(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=1, merge_queue_timeout=30)
        merged = MergeQueueStatus(in_queue=False, state="MERGED", position=None, estimated_time="")
        with patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = merged
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.is_merged

    async def test_ejection(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=1, merge_queue_timeout=30)
        ejected = MergeQueueStatus(in_queue=True, state="UNMERGEABLE", position=2, estimated_time="")
        with patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = ejected
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.is_failed
        assert result.state == "UNMERGEABLE"

    async def test_timeout(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=1, merge_queue_timeout=2)
        queued = MergeQueueStatus(in_queue=True, state="QUEUED", position=5, estimated_time="10m")
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return float(call_count)

        mock_loop = AsyncMock()
        mock_loop.time = advancing_time
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            mock_status.return_value = queued
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.state == "TIMEOUT"

    async def test_timeout_with_large_interval(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=100, merge_queue_timeout=30)
        queued = MergeQueueStatus(in_queue=True, state="QUEUED", position=5, estimated_time="10m")
        time_values = iter([0.0, 0.0, 0.5, 29.0, 31.0])

        mock_loop = AsyncMock()
        mock_loop.time = lambda: next(time_values)
        sleep_durations = []

        async def capture_sleep(duration):
            sleep_durations.append(duration)

        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status,
            patch("asyncio.sleep", side_effect=capture_sleep),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            mock_status.return_value = queued
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.state == "TIMEOUT"
        for d in sleep_durations:
            assert d <= 30, f"Sleep duration {d} exceeded timeout of 30"

    async def test_consecutive_api_failures_early_exit(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=1, merge_queue_timeout=600)
        unknown = MergeQueueStatus(in_queue=True, state="UNKNOWN", position=None, estimated_time="")
        time_val = [0.0]

        def advancing_time():
            time_val[0] += 0.1
            return time_val[0]

        mock_loop = AsyncMock()
        mock_loop.time = advancing_time
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            mock_status.return_value = unknown
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.state == "TIMEOUT"
        assert mock_status.call_count == 5

    async def test_intermittent_api_failures(self) -> None:
        cfg = IntegrationConfig(merge_queue_poll_interval=1, merge_queue_timeout=600)
        queued = MergeQueueStatus(in_queue=True, state="QUEUED", position=3, estimated_time="5m")
        unknown = MergeQueueStatus(in_queue=True, state="UNKNOWN", position=None, estimated_time="")
        merged = MergeQueueStatus(in_queue=False, state="MERGED", position=None, estimated_time="")
        time_val = [0.0]

        def advancing_time():
            time_val[0] += 0.1
            return time_val[0]

        mock_loop = AsyncMock()
        mock_loop.time = advancing_time
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock) as mock_status,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            mock_status.side_effect = [queued, unknown, queued, merged]
            result = await poll_merge_queue(42, repo="owner/repo", cfg=cfg)
        assert result.is_merged
        assert mock_status.call_count == 4


class TestDeleteRemoteBranch:
    async def test_success(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok()
            result = await delete_remote_branch("feat/branch", repo="owner/repo")
        assert result is True

    async def test_failure(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_fail(stderr="not found")
            result = await delete_remote_branch("feat/branch", repo="owner/repo")
        assert result is False


class TestHandlePostMergeState:
    async def test_done_closes_issue(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok()
            await handle_post_merge_state(42, post_merge_state="done", repo="owner/repo")
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert "close" in args

    async def test_on_qa_adds_label(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok()
            await handle_post_merge_state(42, post_merge_state="on_qa", repo="owner/repo")
        args = mock_run.call_args[0]
        assert "agent:on-qa" in args
        assert "edit" in args

    async def test_no_issue_is_noop(self) -> None:
        with patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run:
            await handle_post_merge_state(None, post_merge_state="done", repo="owner/repo")
        mock_run.assert_not_called()

    async def test_unknown_state_skips(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            await handle_post_merge_state(42, post_merge_state="custom_qa", repo="owner/repo")
        mock_run.assert_not_called()


class TestIntegrationConfigDefaults:
    def test_default_values(self) -> None:
        cfg = IntegrationConfig()
        assert cfg.merge_method == "auto"
        assert cfg.delete_branch is True
        assert cfg.merge_queue_enabled == "auto"
        assert cfg.merge_queue_poll_interval == 30
        assert cfg.merge_queue_timeout == 1800
        assert cfg.post_merge_state == "done"

    def test_custom_values(self) -> None:
        cfg = IntegrationConfig(
            merge_method="squash",
            delete_branch=False,
            merge_queue_enabled="true",
            merge_queue_poll_interval=60,
            merge_queue_timeout=900,
            post_merge_state="on_qa",
        )
        assert cfg.merge_method == "squash"
        assert cfg.delete_branch is False
        assert cfg.merge_queue_enabled == "true"
        assert cfg.post_merge_state == "on_qa"


class TestGetPrBaseBranch:
    async def test_success(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout="develop\n")
            result = await _get_pr_base_branch(42, repo="owner/repo")
        assert result == "develop"

    async def test_fallback_on_failure(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_fail(stderr="not found")
            result = await _get_pr_base_branch(42, repo="owner/repo")
        assert result == "main"

    async def test_fallback_on_empty_stdout(self) -> None:
        with (
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_run.return_value = _shell_ok(stdout="")
            result = await _get_pr_base_branch(42, repo="owner/repo")
        assert result == "main"


class TestMergePrBaseBranch:
    async def test_auto_detects_base_branch(self) -> None:
        cfg = IntegrationConfig(merge_method="squash", merge_queue_enabled="false")
        with (
            patch("sova.git.merge._get_pr_base_branch", new_callable=AsyncMock) as mock_base,
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_base.return_value = "develop"
            mock_q.return_value = False
            mock_run.return_value = _shell_ok(stdout="Merged")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg)
        mock_base.assert_called_once()
        mock_q.assert_called_once()
        assert mock_q.call_args.kwargs["base_branch"] == "develop"
        assert result.success

    async def test_explicit_base_branch_skips_lookup(self) -> None:
        cfg = IntegrationConfig(merge_method="squash", merge_queue_enabled="false")
        with (
            patch("sova.git.merge._get_pr_base_branch", new_callable=AsyncMock) as mock_base,
            patch("sova.git.merge.should_use_merge_queue", new_callable=AsyncMock) as mock_q,
            patch("sova.git.merge.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.merge.resolve_gh_env", return_value={}),
        ):
            mock_q.return_value = False
            mock_run.return_value = _shell_ok(stdout="Merged")
            result = await merge_pr(42, repo="owner/repo", cfg=cfg, base_branch="master")
        mock_base.assert_not_called()
        assert mock_q.call_args.kwargs["base_branch"] == "master"
        assert result.success
