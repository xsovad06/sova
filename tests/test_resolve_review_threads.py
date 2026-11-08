"""ResolveReviewThreadsStep replies to and resolves the addressed threads and posts the address summary.

The step runs straight after ``push``, before CI. It resolves only the threads recorded by
``address_review`` and only when the pushed head moved; the ``sova-addressed`` marker is the one
action exempt from that rule because it is what the address-review budget counts (#1265); on an unmoved head
it is posted as "No changes were pushed" and lists no finding as addressed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import PRReview
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.steps.resolve_review_threads import (
    ResolveReviewThreadsStep,
    _already_replied,
    _head_moved,
    _local_head,
    _post_address_summary,
    _remote_tip,
    _reply_and_resolve,
)
from sova.core.workflow import WorkflowEngine

_BASE = "1111111111111111111111111111111111111111"
_HEAD = "892372e3512475e42638ffe7cab9a14d0734a2b5"
_MOD = "sova.core.steps.resolve_review_threads"


def _ctx(findings: list[dict], tmp_path: Path, *, threads: list[str] | None = None) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_pr_reviews = AsyncMock(return_value=[])
    adapter.post_pr_review = AsyncMock()
    ctx = ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(github_user="xsovad06", github_repo="user/repo"),
        adapter=adapter,
        issue_number="",
        role="developer",
        pr_number=1063,
        branch_name="fix/x",
    )
    ctx.addressed_review_findings = findings
    ctx.addressed_review_thread_ids = threads or []
    ctx.address_cycle_base_head = _BASE
    ctx.address_cycle_recorded = True
    ctx.pipeline_variant = "address_review"
    return ctx


def _git(head: str | None = _HEAD, remote: str | None = _HEAD, diff_rc: int = 1) -> AsyncMock:
    """A ``run`` stub answering ``git rev-parse HEAD``, ``git diff --quiet`` and ``git ls-remote``.

    ``diff_rc`` is the ``git diff --quiet`` exit code: 1 = the tree differs from the base, 0 = identical.
    """

    async def fake(*args: str, **kwargs: object) -> MagicMock:
        if "diff" in args:
            return MagicMock(success=diff_rc == 0, returncode=diff_rc, stdout="", stderr="")
        if "rev-parse" in args:
            return MagicMock(success=head is not None, stdout=f"{head}\n" if head else "", stderr="")
        if "ls-remote" in args:
            ok = remote is not None
            return MagicMock(success=ok, stdout=f"{remote}\trefs/heads/fix/x\n" if remote else "", stderr="")
        raise AssertionError(f"unexpected command {args}")

    return AsyncMock(side_effect=fake)


def _thread(thread_id: str, *, resolved: bool = False, outdated: bool = False, last_body: str | None = None) -> dict:
    last = [{"body": last_body}] if last_body is not None else []
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": []},
        "lastComment": {"nodes": last},
    }


class TestHeadMoved:
    @pytest.mark.asyncio
    async def test_moved_and_on_remote(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=_git()):
            assert await _head_moved(ctx, _HEAD) is True

    @pytest.mark.asyncio
    async def test_head_equal_to_base_is_not_a_move(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        ctx.address_cycle_base_head = _HEAD
        with patch(f"{_MOD}.run", new=_git()):
            assert await _head_moved(ctx, _HEAD) is False

    @pytest.mark.asyncio
    async def test_rewritten_history_with_identical_tree_is_not_a_move(self, tmp_path: Path) -> None:
        """rearrange_commits can change HEAD's SHA with no code change; that must not resolve any thread."""
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=_git(diff_rc=0)):
            assert await _head_moved(ctx, _HEAD) is False

    @pytest.mark.asyncio
    async def test_failed_tree_comparison_is_not_a_move(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=_git(diff_rc=128)):
            assert await _head_moved(ctx, _HEAD) is False

    @pytest.mark.asyncio
    async def test_head_not_on_remote_is_not_a_move(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=_git(remote=_BASE)):
            assert await _head_moved(ctx, _HEAD) is False

    @pytest.mark.asyncio
    async def test_remote_lookup_failure_is_not_a_move(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=_git(remote=None)):
            assert await _head_moved(ctx, _HEAD) is False

    @pytest.mark.asyncio
    async def test_unknown_base_is_not_a_move(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        ctx.address_cycle_base_head = ""
        with patch(f"{_MOD}.run", new=_git()):
            assert await _head_moved(ctx, _HEAD) is False


class TestLocalHeadAndRemoteTip:
    @pytest.mark.asyncio
    async def test_remote_tip_without_branch_name_is_unknown(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        ctx.branch_name = ""
        run_mock = _git()
        with patch(f"{_MOD}.run", new=run_mock):
            assert await _remote_tip(ctx) is None
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remote_tip_authenticates_as_the_configured_user(self, tmp_path: Path) -> None:
        """ls-remote must use the same gh identity as push(), or a private HTTPS remote reads as unmoved."""
        ctx = _ctx([], tmp_path)
        env = {"GH_TOKEN": "tok"}
        run_mock = _git()
        with (
            patch(f"{_MOD}.run", new=run_mock),
            patch(f"{_MOD}.resolve_gh_env", new=AsyncMock(return_value=env)) as resolve_env,
        ):
            assert await _remote_tip(ctx) == _HEAD
        resolve_env.assert_awaited_once_with("xsovad06")
        assert run_mock.call_args.kwargs["env"] == env

    @pytest.mark.asyncio
    async def test_remote_tip_falls_back_to_ambient_env_when_gh_is_missing(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with (
            patch(f"{_MOD}.run", new=_git()),
            patch(f"{_MOD}.resolve_gh_env", new=AsyncMock(side_effect=FileNotFoundError("gh"))),
        ):
            assert await _remote_tip(ctx) is None

    @pytest.mark.asyncio
    async def test_remote_tip_spawn_error_is_unknown(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=AsyncMock(side_effect=FileNotFoundError("cwd gone"))):
            assert await _remote_tip(ctx) is None

    @pytest.mark.asyncio
    async def test_remote_tip_with_empty_output_is_unknown(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=AsyncMock(return_value=MagicMock(success=True, stdout="", stderr=""))):
            assert await _remote_tip(ctx) is None

    @pytest.mark.asyncio
    async def test_local_head_spawn_error_is_unknown(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch(f"{_MOD}.run", new=AsyncMock(side_effect=FileNotFoundError("cwd gone"))):
            assert await _local_head(ctx) is None


class TestAlreadyReplied:
    def test_matches_the_last_comment_body(self) -> None:
        assert _already_replied(_thread("t1", last_body="  Addressed in `abc1234`.\n"), "Addressed in `abc1234`.")

    def test_other_last_comment_does_not_match(self) -> None:
        assert not _already_replied(_thread("t1", last_body="please fix"), "Addressed in `abc1234`.")

    def test_thread_without_comments_does_not_match(self) -> None:
        assert not _already_replied(_thread("t1"), "Addressed in `abc1234`.")
        assert not _already_replied({"id": "t1", "lastComment": None}, "Addressed in `abc1234`.")
        assert not _already_replied({"id": "t1", "lastComment": {"nodes": [{"body": None}]}}, "x")


class TestPostAddressSummary:
    @pytest.mark.asyncio
    async def test_posts_comment_review_anchored_to_pushed_head(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "severity": 5, "description": "bug"}], tmp_path)
        posted = await _post_address_summary(ctx, _HEAD, head_moved=True)

        assert posted is True
        ctx.adapter.post_pr_review.assert_awaited_once()
        args, kwargs = ctx.adapter.post_pr_review.call_args
        assert args[0] == 1063
        assert kwargs["event"] == "COMMENT"
        assert kwargs["comments"] == []
        assert kwargs["body"].startswith(f"<!-- sova-addressed: sha={_HEAD} -->")
        assert "## Address Review: Round 1" in kwargs["body"]
        assert "`a.py:1`: bug" in kwargs["body"]

    @pytest.mark.asyncio
    async def test_round_counts_earlier_summaries(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.get_pr_reviews = AsyncMock(
            return_value=[
                PRReview("me", "COMMENTED", "<!-- sova-review: revise -->", "2026-09-17T21:00:00Z", False),
                PRReview(
                    "me",
                    "COMMENTED",
                    "<!-- sova-addressed: sha=abcdef1 -->\n## Address Review: Round 1",
                    "2026-09-17T22:00:00Z",
                    False,
                ),
            ]
        )
        await _post_address_summary(ctx, _HEAD, head_moved=True)

        assert "## Address Review: Round 2" in ctx.adapter.post_pr_review.call_args.kwargs["body"]

    @pytest.mark.asyncio
    async def test_findings_free_cycle_still_posts_its_marker(self, tmp_path: Path) -> None:
        """A cycle with no findings is still a cycle the address budget must see."""
        ctx = _ctx([], tmp_path)
        posted = await _post_address_summary(ctx, _HEAD, head_moved=True)

        assert posted is True
        body = ctx.adapter.post_pr_review.call_args.kwargs["body"]
        assert body.startswith(f"<!-- sova-addressed: sha={_HEAD} -->")
        assert "No review findings were pending" in body

    @pytest.mark.asyncio
    async def test_marker_already_on_pushed_head_is_not_posted_twice(self, tmp_path: Path) -> None:
        """A resumed run whose first attempt already posted the marker must not post a second one."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.get_pr_reviews = AsyncMock(
            return_value=[
                PRReview("me", "COMMENTED", f"<!-- sova-addressed: sha={_HEAD} -->", "2026-09-17T22:00:00Z", False)
            ]
        )
        posted = await _post_address_summary(ctx, _HEAD, head_moved=True)

        assert posted is True
        ctx.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmoved_head_posts_even_with_a_marker_on_it(self, tmp_path: Path) -> None:
        """Two no-op cycles on one head are two cycles: the second must still count against the budget."""
        ctx = _ctx([], tmp_path)
        ctx.adapter.get_pr_reviews = AsyncMock(
            return_value=[
                PRReview("me", "COMMENTED", f"<!-- sova-addressed: sha={_HEAD} -->", "2026-09-17T22:00:00Z", False)
            ]
        )
        posted = await _post_address_summary(ctx, _HEAD, head_moved=False)

        assert posted is True
        ctx.adapter.post_pr_review.assert_awaited_once()
        assert "## Address Review: Round 2" in ctx.adapter.post_pr_review.call_args.kwargs["body"]

    @pytest.mark.asyncio
    async def test_post_failure_is_non_fatal_and_reported(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("gh exploded"))

        assert await _post_address_summary(ctx, _HEAD, head_moved=True) is False

    @pytest.mark.asyncio
    async def test_round_lookup_failure_still_posts_the_summary(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.get_pr_reviews = AsyncMock(side_effect=RuntimeError("gh exploded"))

        assert await _post_address_summary(ctx, _HEAD, head_moved=True) is True
        ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_head_leaves_marker_unanchored(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        posted = await _post_address_summary(ctx, None, head_moved=False)

        assert posted is True
        assert ctx.adapter.post_pr_review.call_args.kwargs["body"].startswith("<!-- sova-addressed -->")


class TestReplyAndResolve:
    @pytest.mark.asyncio
    async def test_replies_then_resolves_each_open_thread(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path, threads=["t1", "t2"])
        with (
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1"), _thread("t2", outdated=True)]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)) as reply,
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            replied, resolved = await _reply_and_resolve(ctx, _HEAD)

        assert (replied, resolved) == (2, 2)
        assert [c.args[0] for c in reply.await_args_list] == ["t1", "t2"]
        assert reply.await_args_list[0].args[1] == f"Addressed in `{_HEAD[:7]}`."
        assert [c.args[0] for c in resolve.await_args_list] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_thread_resolved_by_a_human_is_left_alone(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path, threads=["t1", "t2"])
        with (
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1", resolved=True), _thread("t2")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)) as reply,
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            replied, resolved = await _reply_and_resolve(ctx, _HEAD)

        assert (replied, resolved) == (1, 1)
        reply.assert_awaited_once()
        resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thread_outside_the_addressed_set_stays_open(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path, threads=["t1"])
        with (
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1"), _thread("opened-later")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)),
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            await _reply_and_resolve(ctx, _HEAD)

        assert [c.args[0] for c in resolve.await_args_list] == ["t1"]

    @pytest.mark.asyncio
    async def test_one_failing_thread_does_not_stop_the_rest(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path, threads=["t1", "t2", "t3"])
        with (
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1"), _thread("t2"), _thread("t3")]),
            ),
            patch(
                "sova.adapters.external_reviews.reply_to_review_thread",
                new=AsyncMock(side_effect=[False, True, True]),
            ),
            patch(
                "sova.adapters.external_reviews.resolve_review_thread",
                new=AsyncMock(side_effect=[False, True]),
            ) as resolve,
        ):
            replied, resolved = await _reply_and_resolve(ctx, _HEAD)

        # t1: reply failed so it is left open unresolved. t2: resolve failed. t3: fully done.
        assert (replied, resolved) == (2, 1)
        assert [c.args[0] for c in resolve.await_args_list] == ["t2", "t3"]

    @pytest.mark.asyncio
    async def test_thread_already_replied_to_is_resolved_without_a_second_reply(self, tmp_path: Path) -> None:
        """An earlier attempt replied but did not resolve: the resume resolves, it does not reply again."""
        ctx = _ctx([], tmp_path, threads=["t1", "t2"])
        replied_body = f"Addressed in `{_HEAD[:7]}`."
        with (
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1", last_body=replied_body), _thread("t2", last_body="other")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)) as reply,
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            replied, resolved = await _reply_and_resolve(ctx, _HEAD)

        assert (replied, resolved) == (1, 2)
        assert [c.args[0] for c in reply.await_args_list] == ["t2"]
        assert [c.args[0] for c in resolve.await_args_list] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_thread_fetch_failure_touches_nothing(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path, threads=["t1"])
        with (
            patch("sova.adapters.external_reviews.fetch_review_threads", new=AsyncMock(return_value=None)),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock()) as reply,
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock()) as resolve,
        ):
            assert await _reply_and_resolve(ctx, _HEAD) == (0, 0)

        reply.assert_not_awaited()
        resolve.assert_not_awaited()


class TestStepExecute:
    @staticmethod
    def _patches(
        threads: list[dict] | None = None, *, head: str | None = _HEAD, remote: str | None = _HEAD, diff_rc: int = 1
    ):
        return (
            patch(f"{_MOD}.run", new=_git(head, remote, diff_rc)),
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=threads if threads is not None else [_thread("t1")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)),
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)),
        )

    @pytest.mark.asyncio
    async def test_moved_head_resolves_threads_and_posts_summary(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        git, fetch, reply, resolve = self._patches()
        with git, fetch, reply as reply_mock, resolve as resolve_mock:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        assert result.summary == "1 threads resolved, address summary posted"
        reply_mock.assert_awaited_once()
        resolve_mock.assert_awaited_once()
        ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unmoved_head_posts_the_marker_but_touches_no_thread(self, tmp_path: Path) -> None:
        """The LLM changed nothing: the cycle is still counted, but no thread is answered."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        ctx.address_cycle_base_head = _HEAD
        git, fetch, reply, resolve = self._patches()
        with git, fetch as fetch_mock, reply as reply_mock, resolve as resolve_mock:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.summary == "address summary posted"
        fetch_mock.assert_not_awaited()
        reply_mock.assert_not_awaited()
        resolve_mock.assert_not_awaited()
        ctx.adapter.post_pr_review.assert_awaited_once()
        body = ctx.adapter.post_pr_review.call_args.kwargs["body"]
        assert body.startswith(f"<!-- sova-addressed: sha={_HEAD} -->")
        assert "No changes were pushed" in body
        assert "a.py" not in body

    @pytest.mark.asyncio
    async def test_rewritten_history_with_same_tree_touches_no_thread(self, tmp_path: Path) -> None:
        """HEAD and the remote tip differ from the base but the trees match: no reply, no resolve, summary posted."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        git, fetch, reply, resolve = self._patches(diff_rc=0)
        with git, fetch as fetch_mock, reply as reply_mock, resolve as resolve_mock:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.summary == "address summary posted"
        fetch_mock.assert_not_awaited()
        reply_mock.assert_not_awaited()
        resolve_mock.assert_not_awaited()
        ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_head_not_on_remote_touches_no_thread(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        git, fetch, reply, resolve = self._patches(remote=_BASE)
        with git, fetch, reply as reply_mock, resolve as resolve_mock:
            await ResolveReviewThreadsStep().execute(ctx)

        reply_mock.assert_not_awaited()
        resolve_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_findings_free_cycle_posts_empty_case_and_touches_no_thread(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        git, fetch, reply, resolve = self._patches()
        with git, fetch as fetch_mock, reply, resolve:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.summary == "address summary posted"
        fetch_mock.assert_not_awaited()
        assert "No review findings were pending" in ctx.adapter.post_pr_review.call_args.kwargs["body"]

    @pytest.mark.asyncio
    async def test_thread_lookup_crash_still_posts_the_summary(self, tmp_path: Path) -> None:
        """A spawn error (gh missing) in the thread work must not strand the cycle record."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        git, _, reply, resolve = self._patches()
        with (
            git,
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(side_effect=FileNotFoundError(2, "gh")),
            ),
            reply,
            resolve,
        ):
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        assert result.summary == "address summary posted"
        ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_summary_posted_once_across_a_resumed_unmoved_head(self, tmp_path: Path) -> None:
        """The head did not move and the first attempt died after posting: the re-run posts nothing more."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        ctx.address_cycle_base_head = _HEAD
        git, fetch, reply, resolve = self._patches()
        with git, fetch, reply, resolve:
            await ResolveReviewThreadsStep().execute(ctx)
            assert ctx.address_summary_posted is True
            state = ctx.address_cycle_state()

            resumed = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
            resumed.restore_address_cycle(state)
            resumed.adapter.get_pr_reviews = AsyncMock(
                return_value=[
                    PRReview("me", "COMMENTED", f"<!-- sova-addressed: sha={_HEAD} -->", "2026-09-17T22:00:00Z", False)
                ]
            )
            await ResolveReviewThreadsStep().execute(resumed)

        ctx.adapter.post_pr_review.assert_awaited_once()
        resumed.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_summary_post_is_not_claimed(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("gh exploded"))
        git, fetch, reply, resolve = self._patches()
        with git, fetch, reply, resolve:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        assert result.summary == "1 threads resolved"

    @pytest.mark.asyncio
    async def test_missing_working_dir_posts_unanchored_and_resolves_nothing(self, tmp_path: Path) -> None:
        """A pruned worktree makes the spawn raise: the cycle is still recorded, no thread is resolved."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        ctx.worktree_dir = tmp_path / "pruned"
        reply = AsyncMock(return_value=True)
        with (
            patch(f"{_MOD}.run", new=AsyncMock(side_effect=FileNotFoundError(2, "No such file or directory"))),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=reply),
        ):
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.summary == "address summary posted"
        reply.assert_not_awaited()
        assert ctx.adapter.post_pr_review.call_args.kwargs["body"].startswith("<!-- sova-addressed -->")

    @pytest.mark.asyncio
    async def test_no_cycle_recorded_does_nothing(self, tmp_path: Path) -> None:
        """A resume past address_review with nothing persisted must not post an empty summary."""
        ctx = _ctx([], tmp_path)
        ctx.address_cycle_recorded = False
        git, fetch, reply, resolve = self._patches()
        with git, fetch, reply, resolve:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        ctx.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nothing_posted_outside_address_review_pipeline(self, tmp_path: Path) -> None:
        """A custom configured pipeline including this step outside address-review must not post."""
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        ctx.pipeline_variant = "developer"
        git, fetch, reply, resolve = self._patches()
        with git, fetch, reply as reply_mock, resolve:
            result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        reply_mock.assert_not_awaited()
        ctx.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nothing_posted_without_a_pr(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        ctx.pr_number = None
        result = await ResolveReviewThreadsStep().execute(ctx)

        assert result.success is True
        ctx.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_can_skip_and_gate(self, tmp_path: Path) -> None:
        step = ResolveReviewThreadsStep()
        ctx = _ctx([], tmp_path)
        assert await step.can_skip(ctx) is False
        ctx.pr_number = None
        assert await step.can_skip(ctx) is True
        ctx = _ctx([], tmp_path)
        ctx.completed_steps = frozenset({step.name})
        assert await step.can_skip(ctx) is True
        assert (await step.validate_output(ctx)).passed is True


class TestEngineRun:
    """The step runs inside WorkflowEngine, ahead of a CI step that fails (the issue's main criterion)."""

    @pytest.mark.asyncio
    async def test_threads_and_marker_survive_a_failing_monitor_ci(self, tmp_path: Path) -> None:
        class FakeAddress(BaseStep):
            name = "address_review"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                ctx.address_cycle_recorded = True
                ctx.address_cycle_base_head = _BASE
                ctx.addressed_review_thread_ids = ["t1"]
                ctx.addressed_review_findings = [{"file": "a.py", "line": 1, "description": "bug"}]
                return StepResult(success=True, summary="addressed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        class FailingCI(BaseStep):
            name = "monitor_ci"
            max_retries = 0

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=False, summary="CI timed out", error="CI timed out")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        ctx = _ctx([], tmp_path)
        ctx.address_cycle_recorded = False
        ctx.addressed_review_thread_ids = []
        ctx.address_cycle_base_head = ""
        engine = WorkflowEngine(steps=[FakeAddress(), ResolveReviewThreadsStep(), FailingCI()], ctx=ctx)
        with (
            patch(f"{_MOD}.run", new=_git()),
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)) as reply,
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            result = await engine.run()

        assert result.success is False
        reply.assert_awaited_once()
        resolve.assert_awaited_once()
        ctx.adapter.post_pr_review.assert_awaited_once()


class TestOverrideWithoutTheStep:
    """A persisted ``[pipelines] address_review`` list that predates the step still posts the marker once."""

    @staticmethod
    def _fake_address() -> BaseStep:
        class FakeAddress(BaseStep):
            name = "address_review"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                ctx.address_cycle_recorded = True
                ctx.address_cycle_base_head = _BASE
                ctx.addressed_review_thread_ids = ["t1"]
                ctx.addressed_review_findings = [{"file": "a.py", "line": 1, "description": "bug"}]
                return StepResult(success=True, summary="addressed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        return FakeAddress()

    @pytest.mark.asyncio
    async def test_resolve_external_reviews_posts_the_marker_exactly_once(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _ctx([], tmp_path)
        ctx.address_cycle_recorded = False
        ctx.addressed_review_thread_ids = []
        ctx.address_cycle_base_head = ""
        engine = WorkflowEngine(steps=[self._fake_address(), ResolveExternalReviewsStep()], ctx=ctx)
        with (
            patch(f"{_MOD}.run", new=_git()),
            patch("sova.core.steps.resolve_external_reviews._dismiss_bot_reviews", new=AsyncMock(return_value=0)),
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)),
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)) as resolve,
        ):
            result = await engine.run()

        assert result.success is True
        ctx.adapter.post_pr_review.assert_awaited_once()
        assert ctx.adapter.post_pr_review.call_args.kwargs["body"].startswith("<!-- sova-addressed")
        resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_pipeline_does_not_post_a_second_marker(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path, threads=["t1"])
        with (
            patch(f"{_MOD}.run", new=_git()),
            patch("sova.core.steps.resolve_external_reviews._dismiss_bot_reviews", new=AsyncMock(return_value=0)),
            patch(
                "sova.adapters.external_reviews.fetch_review_threads",
                new=AsyncMock(return_value=[_thread("t1")]),
            ),
            patch("sova.adapters.external_reviews.reply_to_review_thread", new=AsyncMock(return_value=True)),
            patch("sova.adapters.external_reviews.resolve_review_thread", new=AsyncMock(return_value=True)),
        ):
            await ResolveReviewThreadsStep().execute(ctx)
            await ResolveExternalReviewsStep().execute(ctx)

        ctx.adapter.post_pr_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_outside_address_review_nothing_is_posted(self, tmp_path: Path) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _ctx([], tmp_path)
        ctx.pipeline_variant = "developer"
        with patch("sova.core.steps.resolve_external_reviews._dismiss_bot_reviews", new=AsyncMock(return_value=0)):
            await ResolveExternalReviewsStep().execute(ctx)

        ctx.adapter.post_pr_review.assert_not_awaited()
