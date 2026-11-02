"""Detect a safety-critical command whose installed copy predates its canonical source.

A project runs the copy of a command installed under ``.claude/commands/``,
not the canonical template, so a fix to a squash or merge recipe reaches a
project only once someone syncs it there. Nothing surfaced the gap, and the
#1251 incident ran an unfixed squash recipe that silently reverted changes the
base had gained. :func:`find_stale_command` is the single primitive both the
supervisor's ``stale_commands`` gate and ``start_command()``'s pre-spawn check
call, so the two entry points cannot disagree about what "stale" means.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sova.agents.claude_code import ClaudeCodeAdapter
from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import diff_commands
from sova.config.models import ProjectConfig
from sova.git.worktree import git_tracked_paths
from sova.utils.logging import get_logger

log = get_logger(component="commands.staleness")

# Where a SOVA checkout keeps the templates its rendered command copies come
# from, relative to the checkout root. It duplicates the layout
# get_canonical_dir() resolves, so a test pins the two together.
BRANCH_TEMPLATE_DIR = Path("sova/commands/data/commands")

_GIT_TIMEOUT_SECONDS = 10

# Commands that rewrite, push or merge a PR branch, where running an outdated
# recipe can silently lose or revert work.
SAFETY_CRITICAL_COMMANDS = frozenset({"address-pr", "pr", "rearrange-commits", "review-full", "integrate-pr"})


def find_stale_command(
    project_dir: Path,
    command: str,
    cfg: ProjectConfig | None = None,
    *,
    run_dir: Path | None = None,
) -> str | None:
    """Return why ``command``'s installed copy cannot be trusted as current, or None.

    Only a safety-critical command that is actually installed is judged:
    without an installed copy in ``project_dir``, ``_resolve_command_prompt()``
    inlines the recipe from SOVA's own commands instead of sending a slash
    command, so no installed copy executes. An installed copy is stale when its
    manifest hash differs from the rendered canonical (synced from an older
    version), or when it has no manifest entry at all (no record that the
    project ever received the current recipe). A local edit on top of a current
    copy is not stale: the manifest still matches canonical, so the project's
    customization is respected.

    ``run_dir`` is the worktree the command will run in, when that is not
    ``project_dir``. Claude Code resolves the slash command from the worktree,
    and ``ensure_claude_artifacts()`` never overwrites a copy the branch tracks,
    so a project that commits ``.claude/commands/`` runs the branch's own copy,
    which can predate a fix the primary checkout already has. The copy and
    manifest are then read from ``run_dir``, while config still comes from
    ``project_dir``: a worktree carries no ``sova.db`` of its own. A copy the
    branch does not track (a project that gitignores ``.claude/``) is refreshed
    when the worktree is reused, so it is stale only when that refresh failed,
    and the message says so instead of suggesting a rebase. When git cannot say
    whether the branch tracks it, the message names both fixes and when each applies.

    A branch that edits ``BRANCH_TEMPLATE_DIR/<command>.md`` re-renders its own
    copy, which then differs from the primary checkout's canonical and would
    block forever, since rebasing cannot remove the branch's own fix. That copy
    is current when it matches the render of the branch's tracked template and
    the base has not changed the template since the fork point, which both
    ``origin/<base_branch>`` and the primary's canonical template must confirm
    (the remote-tracking ref is never fetched, so it can lag); see
    :func:`_matches_branch_template`.

    Raises on an unreadable install or config; callers decide whether that
    fails open.
    """
    if command not in SAFETY_CRITICAL_COMMANDS:
        return None

    adapter = ClaudeCodeAdapter()
    filename = f"{command}.md"
    project_commands = adapter.commands_dir(project_dir)
    if project_commands is None or not (project_commands / filename).is_file():
        return None

    branch_dir = run_dir if run_dir is not None and run_dir.resolve() != project_dir.resolve() else None
    commands_dir = project_commands if branch_dir is None else adapter.commands_dir(branch_dir)
    if commands_dir is None or not (commands_dir / filename).is_file():
        return None

    if cfg is None:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)

    canonical_dir = get_canonical_dir()
    diff = diff_commands(canonical_dir, commands_dir, cfg, adapter=adapter)
    if filename in diff.changed:
        problem = "predates the current SOVA version of it"
    elif filename in diff.new:
        problem = "has no sync record, so it cannot be verified as current"
    else:
        return None

    if branch_dir is not None and _matches_branch_template(
        branch_dir, commands_dir, filename, canonical_dir / filename, cfg, adapter
    ):
        return None

    if branch_dir is None:
        where = f"Installed /{command}"
        fix = (
            f"Run `sova commands update --project {project_dir}` (add `--force` to replace a locally customized copy)."
        )
    elif (tracked := _is_tracked(branch_dir, commands_dir / filename)) is True:
        where = f"The copy of /{command} committed on the branch checked out at {branch_dir}"
        fix = "Rebase the branch onto its base branch to pick up the current copy."
    elif tracked is False:
        # Untracked: a copy of the primary checkout's, made when the worktree
        # was created and normally refreshed on reuse, so reaching here means
        # that refresh failed. Rebasing cannot change an untracked file.
        where = f"The copy of /{command} in the worktree at {branch_dir}"
        fix = (
            f"It is not tracked by the branch and could not be refreshed from {project_dir}: "
            f"delete {commands_dir / filename} so the next run copies the current one."
        )
    else:
        # git could not say, so neither fix is known to be safe: deleting a
        # committed copy dirties the tree, and a rebase cannot change an
        # untracked one.
        where = f"The copy of /{command} in the worktree at {branch_dir}"
        fix = (
            "Whether the branch tracks it could not be determined (git ls-files failed). "
            f"If `git ls-files` run in {branch_dir} lists {commands_dir.relative_to(branch_dir) / filename}, "
            f"rebase the branch onto its base branch; otherwise delete {commands_dir / filename} so the next "
            f"run copies the current one from {project_dir}."
        )
    return f"{where} {problem}. {fix}"


def _is_tracked(run_dir: Path, path: Path) -> bool | None:
    """Whether the branch checked out at ``run_dir`` tracks ``path``, or ``None`` if git could not say.

    Decides only which fix a stale-copy message gives. A git failure is not
    "untracked": that reading would tell an operator to delete a committed file.
    """
    relative = path.relative_to(run_dir).as_posix()
    found = git_tracked_paths(run_dir, pathspec=relative)
    return None if found is None else relative in found


def _matches_branch_template(
    branch_dir: Path,
    commands_dir: Path,
    filename: str,
    canonical_template: Path,
    cfg: ProjectConfig,
    adapter: ClaudeCodeAdapter,
) -> bool:
    """Whether the branch's copy of ``filename`` renders from a template the branch carries and the base left alone.

    Both must hold: the copy's manifest hash matches the render of the branch's
    tracked template (same rendering as the primary comparison), and the base
    has not changed that template since the fork point, which
    :func:`_base_left_template_alone` proves against both ``origin/<base_branch>``
    and ``canonical_template``. A base change means the copy is behind and a
    rebase is the fix. Every doubt answers False so the caller keeps its stale
    verdict: a missing or untracked template, a template the comparison skips
    (unparseable, unreadable, or not for this runtime), or a git failure.
    """
    templates_dir = branch_dir / BRANCH_TEMPLATE_DIR
    if not (templates_dir / filename).is_file() or _is_tracked(branch_dir, templates_dir / filename) is not True:
        return False

    try:
        diff = diff_commands(templates_dir, commands_dir, cfg, adapter=adapter)
    except (OSError, UnicodeDecodeError):
        # Defensive floor: diff_commands() reads through helpers that already
        # swallow these, so a skipped template shows up as missing from
        # `unchanged` below rather than as an exception.
        log.warning(
            "staleness.branch_template_unreadable", filename=filename, branch_dir=str(branch_dir), exc_info=True
        )
        return False
    # A template the comparison skips (no valid frontmatter, a read failure, a
    # runtime it is not for) is in neither `changed` nor `new`, which would read
    # as a match. Only a positive match counts.
    if filename not in diff.unchanged:
        return False

    template = (BRANCH_TEMPLATE_DIR / filename).as_posix()
    if not _base_left_template_alone(branch_dir, cfg.base_branch, template, canonical_template):
        return False

    log.info("staleness.branch_template_current", filename=filename, branch_dir=str(branch_dir))
    return True


def _base_left_template_alone(branch_dir: Path, base_branch: str, template: str, canonical_template: Path) -> bool:
    """Whether the base has not changed ``template`` since the branch forked from it.

    Reads the local remote-tracking ref and never fetches: this runs on a
    worker thread before every ``start_command()`` spawn into a worktree, where
    a network call would add latency and an outage failure mode. That ref can
    lag the base, and a lagging ref would show the fork point and itself as
    identical while a base fix to the template has already reached
    ``canonical_template``, the file the stale verdict was computed against. So
    ``origin/<base_branch>`` must agree with the fork point and
    ``canonical_template`` must be byte-identical to the template at the fork
    point; either disagreeing means the base moved. A missing ref, unrelated
    histories, a template absent at the fork point, an unreadable canonical
    file, or any git failure answers False.
    """
    base_ref = f"origin/{base_branch}"
    fork_sha = _git_output(branch_dir, "merge-base", "HEAD", base_ref)
    if fork_sha is None:
        return False
    # `git diff --quiet` exits 0 for no difference and 1 for a difference; any
    # other code is a failure and must not read as "unchanged".
    diff = _git(branch_dir, "diff", "--quiet", fork_sha, base_ref, "--", template)
    if diff is None or diff.returncode != 0:
        return False

    # Blob ids, not file contents: git hashes the raw bytes, so no text decoding
    # can fail on the way to the comparison.
    at_fork = _git_output(branch_dir, "rev-parse", "--verify", "--quiet", f"{fork_sha}:{template}")
    current = _git_output(branch_dir, "hash-object", "--no-filters", "--", str(canonical_template))
    if at_fork is not None and current is not None and at_fork != current:
        # Either the base moved (a rebase fixes it) or the primary checkout is
        # behind the fork point (a rebase cannot, it needs a pull there); the
        # caller's message names only the rebase, so make the second case visible.
        log.info(
            "staleness.canonical_differs_from_fork",
            template=template,
            fork_sha=fork_sha,
            branch_dir=str(branch_dir),
        )
    return at_fork is not None and at_fork == current


def _git_output(cwd: Path, *args: str) -> str | None:
    """Stripped stdout of a git command that exited 0, or ``None`` when it failed or could not run."""
    result = _git(cwd, *args)
    return result.stdout.strip() if result is not None and result.returncode == 0 else None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in ``cwd``, or ``None`` when it could not run at all."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("staleness.git_failed", args=list(args), cwd=str(cwd), exc_info=True)
        return None
