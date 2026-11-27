"""Conversation actions: the fixed registry of changes a feed conversation may propose and a user confirms.

The conversation agent never executes a change. It proposes ``(kind, params, reason)``; ``propose_action()`` checks
the proposal against ``KINDS`` (an unknown kind, a bad parameter or a target that does not exist in this project is a
``ProposalRejected`` with the reason, and nothing is stored) and returns the preview that says exactly what would
happen. ``confirm_action()`` is the only path that executes: it recomputes the preview first and executes only when
its fingerprint equals the one the user's card showed; a changed preview executes nothing and is returned for the
card to show instead. Every action runs through the existing service function with its own guards (duplicate,
budget, suitability, the cleanup safety checks), and no kind has a force option or a bulk variant.

The caller owns the proposal's storage and its one-confirm-one-execution compare-and-set (pending to executing),
and passes ``project_dir``, the configured ``github_user`` and (when the dashboard knows one) the confirming user in
an ``ActionContext``, never through an ambient contextvar or the working directory. The start, stop, resume,
mark-step-done-and-resume and handoff kinds call ``control_service`` and ``handoff_service.execute_action()``, which
resolve their agent pool from the request's project context, so the caller must confirm inside the request scoped to
``ctx.project_dir`` (as every dashboard route already runs) and never from deferred work. Those kinds check that
the request's project is ``ctx.project_dir`` before executing and fail otherwise, so a preview of one project never
runs against another.

A reason that reaches the card (and the agent's next turn) is server-authored text only: an unexpected exception is
logged with its traceback and reported as a fixed message, never as its own text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sova.config.loader import load_config
from sova.dashboard.services import feed_narration as narrate
from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="dashboard.conversation_actions")

_MAX_TITLE_CHARS = 256
_MAX_BODY_CHARS = 60_000
_MAX_REASON_CHARS = 2_000
_MAX_LABELS = 20
_MAX_LABEL_CHARS = 50  # GitHub's own limit
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
# Repo paths a draft names, as the issue template's "Files / Modules to Change" section lists them.
_FILE_PATH_RE = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.(?:py|md|js|html|css|sh|toml|ya?ml|json))\b")
_MAX_OVERLAP_PATHS = 5
_GH_TIMEOUT_S = 30
_STASH_INDEX_RE = re.compile(r"^stash@\{\d+\}:\s*")
_DEFAULT_START_ROLE = "developer"
# An id must fit a signed 64-bit column: 18 digits always do, and the digit cap keeps int() off huge strings.
_MAX_ID_DIGITS = 18
_MAX_ID = 2**63 - 1
# Params that may be long or hold pasted text, left out of logs and feed metadata.
_UNLOGGED_PARAMS = frozenset({"body"})


class ProposalRejected(ValueError):
    """A proposal the registry refuses: unknown kind, invalid parameters, or a target that does not exist."""


class ActionFailed(RuntimeError):
    """An action the service refused or that failed while executing; the message is the server's reason."""


@dataclass(frozen=True)
class ActionContext:
    """Who confirms and for which project: passed explicitly into every preview and execution.

    ``github_user`` is the configured tracker identity the actions run as. ``confirmed_by`` is the dashboard user who
    clicked confirm, when the dashboard knows one; ``confirming_user`` falls back to ``github_user`` without it.
    """

    project_dir: Path
    github_user: str
    confirmed_by: str = ""

    @property
    def confirming_user(self) -> str:
        """Who the audit records as confirming: the dashboard user, else the configured ``github_user``."""
        return self.confirmed_by.strip() or self.github_user


@dataclass(frozen=True)
class Proposal:
    """A validated proposal: ``params`` are normalized, so equal proposals have equal params."""

    kind: str
    params: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class Preview:
    """Exactly what confirming would do now: ``lines`` for the card, ``blocked`` the reason it cannot run ("" if it
    can), ``facts`` everything the decision rests on, and ``fingerprint`` a digest of kind, params and facts."""

    kind: str
    summary: str
    lines: tuple[str, ...]
    blocked: str
    facts: dict[str, Any]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "lines": list(self.lines),
            "blocked": self.blocked,
            "facts": self.facts,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ConfirmResult:
    """How a confirm ended: ``executed``, ``changed`` (the preview moved, nothing ran), ``refused`` (the preview
    is blocked, nothing ran) or ``failed`` (the service refused or failed while executing; ``reason`` says why).

    ``warning`` is set on an ``executed`` result that only partly succeeded (an issue filed but not added to the
    board); the card shows it, and the Activity line names it."""

    status: Literal["executed", "changed", "refused", "failed"]
    preview: Preview
    reason: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    warning: str = ""


@dataclass(frozen=True)
class _Draft:
    summary: str
    lines: tuple[str, ...]
    blocked: str
    facts: dict[str, Any]


def _fingerprint(kind: str, params: dict[str, Any], facts: dict[str, Any]) -> str:
    payload = json.dumps({"kind": kind, "params": params, "facts": facts}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Parameter checks ------------------------------------------------------------


def _only(params: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise ProposalRejected(f"Unknown parameter(s): {', '.join(extra)}")


def _ascii_digits(text: str) -> bool:
    """True for a plain decimal number: ``str.isdigit()`` also accepts "²", which ``int()`` cannot parse."""
    return text.isascii() and text.isdecimal()


def _bounded_id(text: str) -> int | None:
    """The positive id ``text`` spells, or None when it is not a plain number that fits a 64-bit column."""
    if not _ascii_digits(text):
        return None
    # int() counts leading zeros toward its digit limit, so only the significant digits are converted.
    digits = text.lstrip("0")
    if len(digits) > _MAX_ID_DIGITS:
        return None
    number = int(digits) if digits else 0
    return number if 0 < number <= _MAX_ID else None


def _positive_int(params: dict[str, Any], key: str) -> int:
    value = params.get(key)
    number: int | None = None
    if isinstance(value, str):
        number = _bounded_id(value.strip().lstrip("#"))
    elif isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_ID:
        number = value
    if number is None:
        shown = value if not isinstance(value, str) or len(value) <= 40 else f"{value[:40]}..."
        raise ProposalRejected(f"{key} must be a positive number, got {shown!r}")
    return number


def _text(params: dict[str, Any], key: str, limit: int, *, required: bool = True) -> str:
    value = params.get(key, "")
    if not isinstance(value, str):
        raise ProposalRejected(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise ProposalRejected(f"{key} is required")
    if len(value) > limit:
        raise ProposalRejected(f"{key} is longer than {limit} characters")
    return value


def _issue_ref(params: dict[str, Any]) -> str:
    """An issue number or a JIRA key, normalized ("#42" and "042" become "42")."""
    value = params.get("issue")
    text = ""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
    elif isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_ID:
        text = str(value)
    number = _bounded_id(text)
    if number is not None:
        return str(number)
    if len(text) <= 100 and _JIRA_KEY_RE.match(text):
        return text
    shown = value if not isinstance(value, str) or len(value) <= 40 else f"{value[:40]}..."
    raise ProposalRejected(f"issue must be an issue number or a JIRA key, got {shown!r}")


# Kinds -----------------------------------------------------------------------


@dataclass(frozen=True)
class ActionKind:
    """One registry entry: how to validate, preview and execute a kind of action."""

    name: str
    label: str
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    preview: Callable[[Proposal, ActionContext], Awaitable[_Draft]]
    execute: Callable[[Proposal, ActionContext, _Draft], Awaitable[dict[str, Any]]]


# Clean up after a run.


def _validate_cleanup(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"issue"})
    return {"issue": _issue_ref(params)}


def _gc_facts(gc: Any) -> dict[str, Any]:
    return {
        "worktrees": [[e.path, e.branch, e.reason] for e in gc.removed_worktrees],
        "branches": [[e.branch, e.reason] for e in gc.removed_branches],
        "skipped": [[e.path or e.branch, e.reason] for e in gc.skipped],
        # Without the "stash@{n}: " index, which shifts whenever any stash is pushed and would read as a change.
        "stashes": [_STASH_INDEX_RE.sub("", stash) for stash in gc.stashes_found],
        "errors": list(gc.errors),
    }


async def _configured_repo(ctx: ActionContext) -> str:
    cfg = await asyncio.to_thread(load_config, ctx.project_dir)
    return cfg.github_repo or ""


async def _preview_cleanup(proposal: Proposal, ctx: ActionContext) -> _Draft:
    from sova.git.worktree import cleanup_by_issue_state

    issue = proposal.params["issue"]
    gc = await cleanup_by_issue_state(
        project_dir=ctx.project_dir,
        dry_run=True,
        issue=issue,
        repo=await _configured_repo(ctx),
        github_user=ctx.github_user,
    )
    facts = _gc_facts(gc)
    lines = [f"Remove worktree {path} ({branch or 'no branch'})" for path, branch, _ in facts["worktrees"]]
    lines += [f"Delete local branch {branch}" for branch, _ in facts["branches"]]
    lines += [f"Skip {target}: {reason}" for target, reason in facts["skipped"]]
    lines += [f"Stash reported, not dropped: {stash}" for stash in facts["stashes"]]
    blocked = "; ".join(facts["errors"])
    if not blocked and not facts["worktrees"] and not facts["branches"]:
        blocked = f"Nothing of {_ref(issue)} can be removed"
    return _Draft(f"Clean up after {_ref(issue)}", tuple(lines), blocked, facts)


async def _execute_cleanup(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.git.worktree import cleanup_by_issue_state

    # Only what the card listed may be removed: a target that became eligible since the preview is left alone.
    listed = {path for path, _, _ in draft.facts["worktrees"]} | {branch for branch, _ in draft.facts["branches"]}
    gc = await cleanup_by_issue_state(
        project_dir=ctx.project_dir,
        issue=proposal.params["issue"],
        repo=await _configured_repo(ctx),
        github_user=ctx.github_user,
        only=listed,
    )
    facts = _gc_facts(gc)
    removed = [path for path, _, _ in facts["worktrees"]] + [branch for branch, _ in facts["branches"]]
    if not removed:
        # A target that became ineligible since the preview (a dirty worktree, a started agent) is skipped with no
        # error, so an empty removal is a failure whether or not an error was recorded.
        why = (
            f"failed: {'; '.join(gc.errors)}"
            if gc.errors
            else "skipped: " + ("; ".join(f"{t}: {r}" for t, r in facts["skipped"]) or "nothing eligible")
        )
        raise ActionFailed(f"Nothing was removed; {why}")
    result: dict[str, Any] = {
        "removed_worktrees": facts["worktrees"],
        "removed_branches": facts["branches"],
        "skipped": facts["skipped"],
    }
    if gc.errors:
        # A partial removal changed the disk, so it is an executed action with a warning (and an Activity line)
        # naming what is gone next to what failed, as a filed-but-not-on-the-board issue is.
        result[_WARNING_KEY] = f"Removed {', '.join(removed)}; failed: {'; '.join(gc.errors)}"
    return result


# Mark a failed step done and resume.


def _validate_mark_step(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"run_id", "step"})
    return {"run_id": _positive_int(params, "run_id"), "step": _text(params, "step", 50)}


async def _preview_mark_step(proposal: Proposal, ctx: ActionContext) -> _Draft:
    from sova.dashboard.services.agent_db import find_failed_step

    run_id, step = proposal.params["run_id"], proposal.params["step"]
    found = await find_failed_step(run_id, project_dir=ctx.project_dir)
    if found is None:
        raise ProposalRejected(f"Run #{run_id} does not exist in this project")
    # A step already marked done whose resume was refused (a start guard): confirming again only resumes, so the
    # audit is kept and the manual override is not applied twice.
    resume_only = not found.step_name and found.marked_step == step
    facts = {
        "run_status": found.run_status,
        "failed_step": found.step_name,
        "issue": found.issue,
        "role": found.role,
        "resume_only": resume_only,
    }
    blocked = ""
    if found.run_status != "failed":
        blocked = f"Run #{run_id} is {found.run_status}, not failed"
    elif found.step_name != step and not resume_only:
        blocked = f"The failed step of run #{run_id} is {found.step_name or 'unknown'}, not {step}"
    elif not found.issue:
        blocked = f"Run #{run_id} has no issue to resume"
    elif not resume_only and not ctx.confirming_user.strip():
        # The mark records who confirmed it; an unattributed manual override is never written.
        blocked = "github_user is not configured; set it with sova config set github_user <login>"
    resume = f"Resume run #{run_id} ({found.role}) from the step after {step}, through the start guards"
    if resume_only:
        lines = (f"Step {step} of run #{run_id} is already marked done", resume)
        return _Draft(f"Resume run #{run_id} after {step}", lines, blocked, facts)
    lines = (
        f"Mark step {step} of run #{run_id} done, recording {ctx.confirming_user or 'no user'} and the reason",
        resume,
    )
    return _Draft(f"Mark {step} done and resume run #{run_id}", lines, blocked, facts)


async def _execute_mark_step(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import control_service
    from sova.dashboard.services.agent_db import mark_failed_step_done

    _require_request_project(ctx)
    run_id, step = proposal.params["run_id"], proposal.params["step"]
    if not draft.facts["resume_only"]:
        refusal = await mark_failed_step_done(
            run_id,
            step_name=step,
            confirmed_by=ctx.confirming_user,
            reason=proposal.reason,
            project_dir=ctx.project_dir,
        )
        if refusal:
            raise ActionFailed(refusal)
    try:
        started = await control_service.start_agent(
            draft.facts["issue"], role=draft.facts["role"], resume_run_id=run_id, enforce_suitability=True
        )
    except Exception as exc:  # noqa: BLE001 (the step is already marked done and the card must say so)
        log.warning("conversation_actions.resume_crashed", run_id=run_id, step=step, exc_info=True)
        raise ActionFailed(
            f"Step {step} is marked done, but the resume failed unexpectedly; see the server log. "
            "Proposing the same action again resumes the run"
        ) from exc
    if error := _start_error(started):
        raise ActionFailed(
            f"Step {step} is marked done, but the resume was refused: {error}. "
            "Proposing the same action again resumes the run once that clears"
        )
    return {"marked_done": step, "run_id": run_id, "resumed_run_id": started.get("run_id")}


# Reset a stuck issue.


def _validate_issue_number(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"issue"})
    return {"issue": _positive_int(params, "issue")}


async def _progression_engine(ctx: ActionContext) -> Any:
    from sova.adapters import create_adapter
    from sova.db.session import get_session_factory
    from sova.supervisor.progression import TaskProgressionEngine

    cfg = await asyncio.to_thread(load_config, ctx.project_dir)
    return TaskProgressionEngine(
        config=cfg,
        adapter=create_adapter(cfg),
        project_dir=ctx.project_dir,
        session_factory=await get_session_factory(ctx.project_dir),
    )


async def _preview_reset(proposal: Proposal, ctx: ActionContext) -> _Draft:
    issue = proposal.params["issue"]
    blockers = await (await _progression_engine(ctx)).stuck_reset_blockers(issue)
    lines = (f"Roll #{issue} back from in progress to its state before the last run (or triaged)",)
    return _Draft(f"Reset stuck issue #{issue}", lines, "; ".join(blockers), {"blockers": blockers})


async def _execute_reset(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    # The confirm emits the Activity line, so the reset's own supervisor line is not emitted too.
    result = await (await _progression_engine(ctx)).reset_stuck_issue(proposal.params["issue"], emit_feed=False)
    if not result.get("reset"):
        raise ActionFailed(str(result.get("reason") or result.get("error") or "The issue was not reset"))
    return result


# Start, stop or resume a run.


def _validate_start(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"issue", "role"})
    # No role means the developer, named here so the preview, the fingerprint and the start all use the same one
    # (a role-less start_agent() would let the tracker state pick another role after the user confirmed).
    role = _text(params, "role", 50, required=False) or _DEFAULT_START_ROLE
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", role):
        raise ProposalRejected(f"role must be a role name, got {role!r}")
    return {"issue": _positive_int(params, "issue"), "role": role}


async def _active_run_detail(issue: int, ctx: ActionContext) -> str:
    from sova.db.session import get_session_factory
    from sova.supervisor.gates.already_running import check_already_running

    block = await check_already_running(issue, await get_session_factory(ctx.project_dir))
    return block.detail if block is not None else ""


async def _preview_start(proposal: Proposal, ctx: ActionContext) -> _Draft:
    issue, role = proposal.params["issue"], proposal.params["role"]
    running = await _active_run_detail(issue, ctx)
    lines = (f"Start the {role} agent on #{issue}, through the duplicate, budget and suitability guards",)
    return _Draft(f"Start the {role} agent on #{issue}", lines, running, {"running": running})


async def _execute_start(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import control_service

    _require_request_project(ctx)
    started = await control_service.start_agent(
        str(proposal.params["issue"]), role=proposal.params["role"], enforce_suitability=True
    )
    if error := _start_error(started):
        raise ActionFailed(error)
    return {"run_id": started.get("run_id")}


def _validate_run_id(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"run_id"})
    return {"run_id": _positive_int(params, "run_id")}


async def _run_status(run_id: int, ctx: ActionContext) -> str:
    from sova.dashboard.services.agent_db import find_failed_step

    found = await find_failed_step(run_id, project_dir=ctx.project_dir)
    if found is None:
        raise ProposalRejected(f"Run #{run_id} does not exist in this project")
    return found.run_status


async def _preview_stop(proposal: Proposal, ctx: ActionContext) -> _Draft:
    from sova.core.state import TASK_RUN_TERMINAL

    run_id = proposal.params["run_id"]
    status = await _run_status(run_id, ctx)
    blocked = f"Run #{run_id} is {status}, not running" if status in TASK_RUN_TERMINAL else ""
    return _Draft(f"Stop run #{run_id}", (f"Stop run #{run_id} only",), blocked, {"run_status": status})


async def _execute_stop(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import control_service

    _require_request_project(ctx)
    result = await control_service.stop_agent(run_id=proposal.params["run_id"])
    if result.get("status") != "stopped":
        raise ActionFailed(str(result.get("message") or "The run was not stopped"))
    return result


async def _preview_resume(proposal: Proposal, ctx: ActionContext) -> _Draft:
    run_id = proposal.params["run_id"]
    status = await _run_status(run_id, ctx)
    blocked = "" if status == "awaiting_approval" else f"Run #{run_id} is {status}, not awaiting approval"
    lines = (f"Resume run #{run_id}, which waits for approval",)
    return _Draft(f"Resume run #{run_id}", lines, blocked, {"run_status": status})


async def _execute_resume(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import control_service

    _require_request_project(ctx)
    result = await control_service.resume_from_approval(proposal.params["run_id"])
    if error := _start_error(result):
        raise ActionFailed(error)
    return result


# Execute or dismiss a handoff action.


def _validate_handoff(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"issue", "action_id"})
    return {"issue": _issue_ref(params), "action_id": _text(params, "action_id", 100)}


def _validate_handoff_dismiss(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"issue"})
    return {"issue": _issue_ref(params)}


_HANDOFF_ACTION_KEYS = ("id", "action", "command", "label")


def _handoff_actions(handoff: dict | None) -> list[dict]:
    actions = handoff.get("next_actions") if isinstance(handoff, dict) else None
    return [a for a in actions or [] if isinstance(a, dict)]


def _action_names(action: dict) -> set[str]:
    """An action's names (id, action, command, label) as text; a list or dict value (agent-written JSON) is none."""
    return {str(v) for k in _HANDOFF_ACTION_KEYS if isinstance(v := action.get(k), (str, int)) and v != ""}


def _handoff_action_ids(handoff: dict | None) -> list[str]:
    """Every name ``handoff_service.execute_action()`` matches an action by (id, action, command, label)."""
    return sorted({name for a in _handoff_actions(handoff) for name in _action_names(a)})


def _selected_handoff_action(handoff: dict | None, action_id: str) -> dict | None:
    """The action ``handoff_service.execute_action()`` would run: the first whose id, action, command or label match."""
    return next((a for a in _handoff_actions(handoff) if action_id in _action_names(a)), None)


def _handoff_digest(handoff: dict | None) -> str:
    return hashlib.sha256(json.dumps(handoff, sort_keys=True, default=str).encode("utf-8")).hexdigest()


async def _read_handoff(ctx: ActionContext, issue: str) -> dict | None:
    from sova.dashboard.services.handoff_service import get_handoff

    return await asyncio.to_thread(get_handoff, ctx.project_dir, issue)


async def _preview_handoff(proposal: Proposal, ctx: ActionContext) -> _Draft:
    issue, action_id = proposal.params["issue"], proposal.params["action_id"]
    handoff = await _read_handoff(ctx, issue)
    # The selected action itself is a fact, so a handoff that keeps the same names but changes what the action
    # does changes the fingerprint.
    selected = _selected_handoff_action(handoff, action_id)
    blocked = "" if selected is not None else f"{_ref(issue)} has no pending handoff action {action_id}"
    lines = [f"Run handoff action {action_id} of {_ref(issue)}, through the start guards"]
    if selected is not None and selected.get("command"):
        lines.append(f"Command: {selected['command']}")
    facts = {"actions": _handoff_action_ids(handoff), "selected": selected}
    return _Draft(f"Run {action_id} on {_ref(issue)}", tuple(lines), blocked, facts)


async def _execute_handoff(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import handoff_service

    _require_request_project(ctx, handoffs=True)
    # execute_action() re-reads the handoff file, so confirm it still holds the action the user was shown.
    current = _selected_handoff_action(await _read_handoff(ctx, proposal.params["issue"]), proposal.params["action_id"])
    if current != draft.facts["selected"]:
        raise ActionFailed("The handoff action changed; nothing was run")
    result = await handoff_service.execute_action(proposal.params["action_id"], proposal.params["issue"])
    if result.outcome is not handoff_service.ActionOutcome.STARTED:
        raise ActionFailed(result.detail or f"The handoff action did not start ({result.outcome.value})")
    return result.payload


async def _preview_handoff_dismiss(proposal: Proposal, ctx: ActionContext) -> _Draft:
    issue = proposal.params["issue"]
    handoff = await _read_handoff(ctx, issue)
    exists = handoff is not None
    blocked = "" if exists else f"{_ref(issue)} has no pending handoff"
    lines = (f"Archive and clear the handoff of {_ref(issue)} only",)
    # The digest binds the dismissal to the handoff the user reviewed: a replacement changes the fingerprint.
    facts = {"exists": exists, "handoff": _handoff_digest(handoff) if exists else ""}
    return _Draft(f"Dismiss the handoff of {_ref(issue)}", lines, blocked, facts)


async def _execute_handoff_dismiss(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.dashboard.services import handoff_service

    issue = proposal.params["issue"]
    handoff = await _read_handoff(ctx, issue)
    if handoff is None or _handoff_digest(handoff) != draft.facts["handoff"]:
        raise ActionFailed("The handoff changed or is gone; nothing was cleared")
    cleared = await asyncio.to_thread(handoff_service.clear_handoff, ctx.project_dir, issue, expected=handoff)
    if not cleared:
        raise ActionFailed("The handoff changed or is gone; nothing was cleared")
    handoff_service.invalidate_handoff_snapshot()
    return {"cleared": True}


# File an issue, or add the draft to an existing issue as a comment.


def _validate_file_issue(params: dict[str, Any]) -> dict[str, Any]:
    _only(params, {"title", "body", "labels", "comment_on"})
    labels = params.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(lb, str) and lb.strip() for lb in labels):
        raise ProposalRejected("labels must be a list of label names")
    if len(labels) > _MAX_LABELS:
        raise ProposalRejected(f"labels may name at most {_MAX_LABELS} labels")
    if any(len(lb.strip()) > _MAX_LABEL_CHARS for lb in labels):
        raise ProposalRejected(f"a label may be at most {_MAX_LABEL_CHARS} characters")
    comment_on = params.get("comment_on")
    # Only an absent value means "file a new issue": anything supplied (False, 0.0, "x") must be a valid target.
    omitted = comment_on is None or comment_on == "" or (type(comment_on) is int and comment_on == 0)
    return {
        "title": _text(params, "title", _MAX_TITLE_CHARS),
        "body": _text(params, "body", _MAX_BODY_CHARS),
        "labels": sorted({lb.strip() for lb in labels}),
        "comment_on": None if omitted else _positive_int(params, "comment_on"),
    }


async def _gh_json(project_dir: Path, *args: str) -> Any:
    """A gh command's parsed JSON output, or None when gh failed, is missing, timed out or printed something else."""
    try:
        result = await run("gh", *args, cwd=project_dir, timeout=_GH_TIMEOUT_S)
    except OSError:  # gh is not installed or cannot be executed
        log.warning("conversation_actions.gh_unavailable", args=args[:2], exc_info=True)
        return None
    if not result.success:
        log.warning("conversation_actions.gh_failed", args=args[:2], stderr=result.stderr[:200])
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


async def _overlapping_issues(body: str, project_dir: Path, repo_args: tuple[str, ...]) -> list[list[Any]] | None:
    """Open issues of the configured repository whose body names a file the draft names, as ``[number, title]``;
    None when a search failed."""
    paths = list(dict.fromkeys(_FILE_PATH_RE.findall(body)))[:_MAX_OVERLAP_PATHS]
    # The searches run concurrently, so a slow gh holds a propose or confirm for one timeout, not one per path.
    results = await asyncio.gather(
        *(
            _gh_json(
                project_dir, "issue", "list", *repo_args, "--state", "open", "--search", f'"{path}" in:body',
                "--limit", "10", "--json", "number,title",
            )
            for path in paths
        )
    )  # fmt: skip
    found: dict[int, str] = {}
    for hits in results:
        if not isinstance(hits, list):
            return None
        found.update({int(h["number"]): str(h.get("title", "")) for h in hits if isinstance(h, dict) and "number" in h})
    return [[number, title] for number, title in sorted(found.items())]


async def _preview_file_issue(proposal: Proposal, ctx: ActionContext) -> _Draft:
    from sova.roles.triage import MAX_QUALITY_SCORE, compute_quality_score

    cfg = await asyncio.to_thread(load_config, ctx.project_dir)
    if cfg.task_source.type != "github":
        raise ProposalRejected("Filing an issue is only supported for GitHub-backed projects")
    params = proposal.params
    comment_on = params["comment_on"]
    # The configured repository, as the confirmed issue or comment uses, never the one the checkout resolves.
    repo_args = ("--repo", cfg.github_repo) if cfg.github_repo else ()
    if comment_on is not None:
        state = await _gh_json(ctx.project_dir, "issue", "view", str(comment_on), *repo_args, "--json", "state")
        open_ = isinstance(state, dict) and state.get("state") == "OPEN"
        blocked = "" if open_ else f"#{comment_on} is closed or could not be read"
        lines = (f"Add the draft to #{comment_on} as a comment; no issue is created",)
        return _Draft(f"Comment on #{comment_on}", lines, blocked, {"target_open": open_})
    score = compute_quality_score(params["body"]).total
    threshold = cfg.triage.min_quality_score
    board = cfg.task_source.github_project_number
    overlap = await _overlapping_issues(params["body"], ctx.project_dir, repo_args)
    lines = [
        f"File issue: {params['title']}",
        f"Labels: {', '.join(params['labels']) or 'none'}",
        f"Quality score {score}/{MAX_QUALITY_SCORE} (filing needs {threshold})",
        f"Add it to {_board_label(board)}",
    ]
    if overlap is None:
        lines.append("Could not check for open issues naming the same files")
    lines += [f"Overlaps open issue #{number}: {title}" for number, title in overlap or []]
    blocked = ""
    if score < threshold:
        blocked = (
            f"Quality score {score}/{MAX_QUALITY_SCORE} is below triage.min_quality_score {threshold}; "
            "the draft needs revising"
        )
    facts = {"score": score, "threshold": threshold, "overlap": overlap, "board": board}
    return _Draft(f"File issue: {params['title']}", tuple(lines), blocked, facts)


def _board_label(board: int) -> str:
    return f"project board {board}" if board else "the repository owner's only project board"


async def _execute_file_issue(proposal: Proposal, ctx: ActionContext, draft: _Draft) -> dict[str, Any]:
    from sova.adapters import create_adapter
    from sova.git.board import add_issues_to_board, issue_url_owner

    params = proposal.params
    cfg = await asyncio.to_thread(load_config, ctx.project_dir)
    adapter = create_adapter(cfg)
    try:
        if params["comment_on"] is not None:
            await adapter.post_comment(str(params["comment_on"]), params["body"])
            return {"commented_on": params["comment_on"]}
        task = await adapter.create_issue(title=params["title"], body=params["body"], labels=params["labels"])
    except Exception as exc:  # noqa: BLE001 (any tracker failure is reported on the card as a fixed reason)
        # The exception text can carry a response body or URL, so it stays in the server log.
        log.warning("conversation_actions.tracker_failed", kind=proposal.kind, exc_info=True)
        raise ActionFailed("The tracker refused the change; see the server log") from exc
    # The owner comes from the new issue's URL: the server's working directory may be another project's repo.
    try:
        owner = issue_url_owner(task.url) if task.url else ""
        outcomes = await add_issues_to_board([task.url], draft.facts["board"], repo_owner=owner) if owner else []
        board = "; ".join(o.line() for o in outcomes) or "no issue URL to add to the board"
    except Exception:  # noqa: BLE001 (the issue exists, so a board crash is a warning, never a failed action)
        # A failed result would invite a re-proposal that files a duplicate.
        log.warning("conversation_actions.board_step_crashed", issue=task.id, exc_info=True)
        outcomes, board = [], "adding it to the board failed unexpectedly; see the server log"
    result: dict[str, Any] = {"number": task.id, "url": task.url, "board": board}
    if not any(o.url and o.status == "added" for o in outcomes):
        # Filed but not on the board: a partial outcome the card and the Activity line must show, never plain "Done".
        result[_WARNING_KEY] = f"Filed #{task.id}, not added to the board: {board}"
    return result


# Registry --------------------------------------------------------------------


_WARNING_KEY = "warning"


def _ref(issue: str | int) -> str:
    return narrate.ref_label(str(issue))


def _loggable(params: dict[str, Any]) -> dict[str, Any]:
    """``params`` without the free text (a draft body), for logs and feed metadata."""
    return {k: v for k, v in params.items() if k not in _UNLOGGED_PARAMS}


def _require_request_project(ctx: ActionContext, *, handoffs: bool = False) -> None:
    """Refuse to execute when the request's agent pool (or handoff store) belongs to another project than
    ``ctx.project_dir``: those services resolve their project from the request, not from ``ctx``."""
    from sova.dashboard.services import agent_pool, handoff_service

    want = ctx.project_dir.resolve()
    resolved = [agent_pool.current_project_dir()]
    if handoffs:
        resolved.append(handoff_service.current_project_dir())
    if any(Path(path).resolve() != want for path in resolved):
        log.warning("conversation_actions.project_mismatch", project_dir=str(want), resolved=[str(p) for p in resolved])
        raise ActionFailed("This request is scoped to another project than the proposal's; nothing was run")


def _start_error(result: Any) -> str:
    """The refusal reason of a start-shaped result, or "" when it started."""
    if not isinstance(result, dict):
        return "The server returned no result"
    if "error" in result or result.get("status") == "error":
        return str(result.get("error") or result.get("message") or "The start was refused")
    return ""


KINDS: dict[str, ActionKind] = {
    kind.name: kind
    for kind in (
        ActionKind("cleanup_after_run", "Clean up", _validate_cleanup, _preview_cleanup, _execute_cleanup),
        ActionKind(
            "mark_step_done_and_resume", "Mark done and resume", _validate_mark_step, _preview_mark_step,
            _execute_mark_step,
        ),
        ActionKind("reset_stuck_issue", "Reset", _validate_issue_number, _preview_reset, _execute_reset),
        ActionKind("start_run", "Start", _validate_start, _preview_start, _execute_start),
        ActionKind("stop_run", "Stop", _validate_run_id, _preview_stop, _execute_stop),
        ActionKind("resume_run", "Resume", _validate_run_id, _preview_resume, _execute_resume),
        ActionKind("handoff_action", "Run", _validate_handoff, _preview_handoff, _execute_handoff),
        ActionKind(
            "handoff_dismiss", "Dismiss handoff", _validate_handoff_dismiss, _preview_handoff_dismiss,
            _execute_handoff_dismiss,
        ),
        ActionKind("file_issue", "File", _validate_file_issue, _preview_file_issue, _execute_file_issue),
    )
}  # fmt: skip


def validate_proposal(kind: str, params: object, reason: object) -> Proposal:
    """The normalized proposal, or ``ProposalRejected`` naming what is wrong (nothing is looked up)."""
    entry = KINDS.get(kind) if isinstance(kind, str) else None
    if entry is None:
        raise ProposalRejected(f"Unknown action kind {str(kind)[:40]!r}; known kinds: {', '.join(sorted(KINDS))}")
    if not isinstance(params, dict):
        raise ProposalRejected("params must be an object")
    reason_text = _text({"reason": reason}, "reason", _MAX_REASON_CHARS)
    return Proposal(kind, entry.validate(params), reason_text)


def _as_preview(proposal: Proposal, draft: _Draft) -> Preview:
    fingerprint = _fingerprint(proposal.kind, proposal.params, draft.facts)
    return Preview(proposal.kind, draft.summary, draft.lines, draft.blocked, draft.facts, fingerprint)


async def build_preview(proposal: Proposal, ctx: ActionContext) -> Preview:
    """What confirming ``proposal`` would do now. Raises ``ProposalRejected`` when its target does not exist."""
    return _as_preview(proposal, await KINDS[proposal.kind].preview(proposal, ctx))


async def propose_action(kind: str, params: object, reason: object, ctx: ActionContext) -> tuple[Proposal, Preview]:
    """Validate a proposal and build its first preview; ``ProposalRejected`` means nothing may be stored.

    Never executes anything. A blocked preview is still a valid proposal (the card shows why it cannot run yet).
    """
    proposal = validate_proposal(kind, params, reason)
    try:
        preview = await build_preview(proposal, ctx)
    except ProposalRejected:
        raise
    except Exception as exc:  # noqa: BLE001 (a preview that cannot be built is a rejection with a reason)
        log.warning("conversation_actions.propose_preview_failed", kind=proposal.kind, exc_info=True)
        raise ProposalRejected("What this would do could not be checked; see the server log") from exc
    log.info(
        "conversation_actions.proposed",
        kind=proposal.kind,
        params=_loggable(proposal.params),
        blocked=bool(preview.blocked),
    )
    return proposal, preview


async def confirm_action(proposal: Proposal, shown_fingerprint: str, ctx: ActionContext) -> ConfirmResult:
    """Recompute the preview and execute only when it still equals what the card showed and is not blocked.

    The caller must already have moved the stored proposal from pending to executing (one execution per confirm).
    An executed action emits one Activity feed line; a changed, refused or failed one emits none.
    """
    entry = KINDS[proposal.kind]
    try:
        draft = await entry.preview(proposal, ctx)
    except ProposalRejected as exc:
        gone = _as_preview(proposal, _Draft(str(exc), (), str(exc), {}))
        return ConfirmResult("refused", gone, reason=str(exc))
    except Exception:  # noqa: BLE001 (an unreadable preview is a failed confirm and never an uncaught 500)
        log.warning("conversation_actions.preview_failed", kind=proposal.kind, exc_info=True)
        reason = "What this would do could not be checked; see the server log"
        return ConfirmResult("failed", _as_preview(proposal, _Draft(reason, (), reason, {})), reason=reason)
    preview = _as_preview(proposal, draft)
    if preview.fingerprint != shown_fingerprint:
        log.info("conversation_actions.preview_changed", kind=proposal.kind, params=_loggable(proposal.params))
        return ConfirmResult("changed", preview, reason="What this would do has changed; review the new preview")
    if preview.blocked:
        return ConfirmResult("refused", preview, reason=preview.blocked)
    try:
        raw = await entry.execute(proposal, ctx, draft)
        # A service's own object is copied (never mutated) and a non-dict one wrapped, so normalizing cannot raise
        # after the action ran.
        result = dict(raw) if isinstance(raw, dict) else {"result": raw}
        warning = str(result.pop(_WARNING_KEY, "") or "")
    except ActionFailed as exc:
        log.info("conversation_actions.failed", kind=proposal.kind, reason=str(exc))
        return ConfirmResult("failed", preview, reason=str(exc))
    except Exception:  # noqa: BLE001 (the proposal must end failed with a reason instead of staying executing)
        # The exception text can carry paths, SQL or a response body, so the card gets a fixed reason.
        log.warning("conversation_actions.execute_crashed", kind=proposal.kind, exc_info=True)
        return ConfirmResult("failed", preview, reason="The action failed unexpectedly; see the server log")
    log.info(
        "conversation_actions.executed",
        kind=proposal.kind,
        params=_loggable(proposal.params),
        by=ctx.confirming_user,
        warning=warning,
    )
    emit_safe(
        narrate.conversation_action_done(preview.summary, warning=warning),
        severity=FeedEventSeverity.warning if warning else FeedEventSeverity.info,
        detail=proposal.reason,
        category="conversation",
        metadata={"kind": proposal.kind, "params": _loggable(proposal.params), "confirmed_by": ctx.confirming_user},
    )
    return ConfirmResult("executed", preview, warning=warning, result=result)
