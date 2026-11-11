"""Environment sanitization for LLM subprocesses and project check commands.

SOVA launches the Claude Code CLI from two places: as a long-lived agent
process (``sova/ipc/runtime.py``) and as a per-step LLM invocation
(``sova/llm/providers/claude_code.py``). Both inherited the server's
environment verbatim, so any third-party provider routing variable present
when the server started silently redirected every child away from the
provider configured in ``llm.provider``.

Scrubbing at the spawn boundary is the only authoritative fix. Cleaning shell
profiles cannot help a server that has already inherited them, because the
agent inherits from the server rather than from the user's login shell.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from sova.utils.agent_leases import active_agent_count
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="utils.env")


# Select the orchestrator's own database. A project's checks and a coding agent that
# runs them create their own, and inheriting the URL points every explicit project
# path at the orchestrator's database instead: SOVA's own suite fails sixteen writer
# tests that way. A nested `sova run` still needs it, so it is never in SCRUBBED_VARS.
# Only CodexRuntime scrubs it from its agent, a deliberate trade: the Codex agent runs the
# project's suite itself, and that suite must not open the orchestrator's database. The cost
# is that its `sova config get` / `sova checks record` calls (the rendered skills make them
# too) resolve the project's default database instead. Settings still agree for the
# SOVA_<SECTION>_<FIELD> overrides, which stay in its environment, and for a pilot copy of
# the project database; a `sova checks record` from Codex lands in the default database.
# Claude Code and Aider agents keep the URL so those calls resolve the orchestrator's database.
ORCHESTRATOR_ONLY_VARS: frozenset[str] = frozenset({"SOVA_DATABASE_URL"})


def scrub_orchestrator_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment minus ORCHESTRATOR_ONLY_VARS, and nothing else.

    A pure scrub: it never reads a config, so a git-only call (a rebase continue) that runs no
    project suite does not resolve settings from the ambient working directory.
    """
    source = os.environ if env is None else env
    return {key: value for key, value in source.items() if key not in ORCHESTRATOR_ONLY_VARS}


def scrub_check_env(
    env: Mapping[str, str] | None = None,
    *,
    config: ProjectConfig | None = None,
    project_dir: Path | None = None,
    quiet: bool = False,
) -> dict[str, str]:
    """Return scrub_orchestrator_env() plus the pytest worker cap, for a call that may run the project's suite.

    A copy, so the pipeline keeps its connection and every other variable reaches the check. The cap
    (with_pytest_worker_cap()) is read from *config* when given, else from the config loaded for
    *project_dir*, else from the one for the current working directory, which belongs to whoever runs
    the call (a server process may sit in another project). Pass *config* or *project_dir* wherever
    either is in hand; a call that runs no suite should use scrub_orchestrator_env().

    Every decision is logged at INFO as ``env.worker_cap``. *quiet* moves it to DEBUG for a git helper
    (commit, push) that passes through here only so a hook that runs the suite is capped: it runs on every
    call and the line explains no slow check.
    """
    return with_pytest_worker_cap(scrub_orchestrator_env(env), config, project_dir, quiet=quiet)


# pytest-xdist reads this for `-n auto`. `make check` runs `-n auto` so it stays the exact CI
# invocation (issue #1209), which on its own starts one worker per core for every concurrent
# check: four agents on a 14-core machine started about 56. Setting it in the environment of
# every check SOVA runs and every agent it dispatches caps the workers without touching the
# command, and CI, which never sets it, keeps every core.
PYTEST_WORKERS_ENV_KEY = "PYTEST_XDIST_AUTO_NUM_WORKERS"
# Exported beside PYTEST_WORKERS_ENV_KEY whenever SOVA (not an operator) sets it, so a nested SOVA process that
# inherits the value can tell what kind it is. A value from `testing.check_workers` is marked CONFIG and holds for
# the subtree (unless a nested project sets its own). A value from the machine split is marked COMPUTED: it was
# sized for the agents running when the process was spawned, so every check recomputes it from the live count.
# A value with no marker is the operator's.
WORKER_CAP_SOURCE_ENV_KEY = "SOVA_WORKER_CAP_SOURCE"
WORKER_CAP_SOURCE_CONFIG = "config"
WORKER_CAP_SOURCE_COMPUTED = "computed"


# Mirrors ProjectConfig.max_parallel_agents' default, for a config that cannot be loaded.
DEFAULT_MAX_PARALLEL_AGENTS = 2

MIN_PYTEST_WORKERS = 2

# Constants, not config: tuned after measurement, and `testing.check_workers` stays the one operator
# override. The reserve is memory left for the OS, the agents' own Claude processes and the dashboard;
# the estimate is one pytest-xdist worker's resident set under SOVA's own suite.
MEMORY_RESERVE_BYTES = 4 * 1024**3
WORKER_MEMORY_BYTES = 512 * 1024**2


def pytest_worker_count(check_workers: int, max_parallel_agents: int, *, cpu_count: int | None = None) -> int:
    """Workers for one check from static inputs: ``check_workers``, else the cores split by *max_parallel_agents*.

    The degraded mode of ``decide_pytest_workers()`` when the live agent count or the available memory
    cannot be read. At least 2.
    """
    if check_workers > 0:
        return check_workers
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(MIN_PYTEST_WORKERS, cpus // max(1, max_parallel_agents))


# The log event every worker-cap decision is reported under, whichever source decided it.
_WORKER_CAP_EVENT = "env.worker_cap"


def worker_shares(*, cpu_count: int, available_bytes: int, active_agents: int) -> tuple[int, int]:
    """The (cpu share, memory share) of workers one of *active_agents* agents may start.

    The memory share counts only what is left once ``MEMORY_RESERVE_BYTES`` is set aside, and is 0 below it.
    """
    active = max(1, active_agents)
    cpu_share = cpu_count // active
    mem_share = max(0, available_bytes - MEMORY_RESERVE_BYTES) // active // WORKER_MEMORY_BYTES
    return cpu_share, mem_share


def auto_workers(*, cpu_count: int, available_bytes: int, active_agents: int) -> int:
    """``max(2, min(cpu share, memory share))``: the workers one of *active_agents* agents may start."""
    cpu_share, mem_share = worker_shares(
        cpu_count=cpu_count, available_bytes=available_bytes, active_agents=active_agents
    )
    return max(MIN_PYTEST_WORKERS, min(cpu_share, mem_share))


def _log_cap(quiet: bool, **fields: object) -> None:
    """Log a worker-cap decision: INFO for a real check or agent spawn, DEBUG for a *quiet* git helper."""
    (log.debug if quiet else log.info)(_WORKER_CAP_EVENT, **fields)


def _auto_pytest_workers(max_parallel_agents: int, *, quiet: bool = False) -> int:
    """``max(2, min(cpu share, memory share))`` for the agents active now, or the static split when unreadable.

    Logs every decision as ``env.worker_cap``. A lease directory or memory reading that fails returns
    ``pytest_worker_count(0, max_parallel_agents)`` and says why, so a degraded value is told apart from
    a computed one in the log.
    """
    cpus = os.cpu_count() or 1
    try:
        active = active_agent_count()
    except OSError as exc:
        return _degraded_workers(max_parallel_agents, f"leases unreadable: {exc}")
    try:
        available = int(psutil.virtual_memory().available)
    except Exception as exc:  # noqa: BLE001 (psutil raises OSError or its own errors and any means unknown)
        return _degraded_workers(max_parallel_agents, f"memory unreadable: {exc}")
    cpu_share, mem_share = worker_shares(cpu_count=cpus, available_bytes=available, active_agents=active)
    workers = auto_workers(cpu_count=cpus, available_bytes=available, active_agents=active)
    _log_cap(
        quiet,
        source="computed",
        workers=workers,
        cpu_cores=cpus,
        active_agents=active,
        available_mb=available // 1024**2,
        cpu_share=cpu_share,
        mem_share=mem_share,
        # The tighter share, and whether it was below the floor (so `workers` is the floor, not the share).
        limited_by="memory" if mem_share < cpu_share else "cpu",
        floor_applied=min(cpu_share, mem_share) < MIN_PYTEST_WORKERS,
    )
    return workers


def _degraded_workers(max_parallel_agents: int, reason: str) -> int:
    workers = pytest_worker_count(0, max_parallel_agents)
    log.warning(_WORKER_CAP_EVENT, source="fallback", workers=workers, reason=reason)
    return workers


def _cap_config(config: ProjectConfig | None, project_dir: Path | None) -> ProjectConfig | None:
    """*config*, else the config loaded for *project_dir* (cwd when None), else None when it cannot be loaded.

    A config that cannot be loaded degrades to the defaults (``testing.check_workers = 0``,
    ``max_parallel_agents = 2``) rather than failing the spawn, as configured_passthrough() does.
    """
    if config is not None:
        return config
    try:
        from sova.config.loader import load_config

        return load_config(project_dir)
    except Exception:  # noqa: BLE001 (config may fail for many reasons; the cap falls back to its defaults)
        log.debug("env.worker_cap_config_unavailable", exc_info=True)
        # Not ProjectConfig(): a bad SOVA_* override makes that constructor raise again.
        return None


def decide_pytest_workers(
    config: ProjectConfig | None = None,
    project_dir: Path | None = None,
    *,
    inherited: str | None = None,
    quiet: bool = False,
) -> tuple[int | str, str]:
    """The (workers, marker) for one check: ``testing.check_workers``, else *inherited*, else the machine split.

    Reads *config*, or the config loaded for *project_dir* (cwd when None). The project's own explicit
    ``testing.check_workers`` wins over *inherited*, a value a parent SOVA process set from its config, which
    in turn wins over the split. The marker is WORKER_CAP_SOURCE_CONFIG for the first two and
    WORKER_CAP_SOURCE_COMPUTED for the split, which a nested process recomputes rather than keeps.
    """
    cfg = _cap_config(config, project_dir)
    if cfg is not None and cfg.testing.check_workers > 0:
        _log_cap(quiet, source="config", workers=cfg.testing.check_workers)
        return cfg.testing.check_workers, WORKER_CAP_SOURCE_CONFIG
    if inherited is not None:
        _log_cap(quiet, source="inherited", workers=inherited)
        return inherited, WORKER_CAP_SOURCE_CONFIG
    max_parallel = cfg.max_parallel_agents if cfg is not None else DEFAULT_MAX_PARALLEL_AGENTS
    return _auto_pytest_workers(max_parallel, quiet=quiet), WORKER_CAP_SOURCE_COMPUTED


def with_pytest_worker_cap(
    env: dict[str, str],
    config: ProjectConfig | None = None,
    project_dir: Path | None = None,
    *,
    quiet: bool = False,
) -> dict[str, str]:
    """Set PYTEST_WORKERS_ENV_KEY in *env* (in place, and returned) unless the operator already set it.

    An empty or whitespace-only value counts as unset, as it does for pytest-xdist itself. A value with no
    WORKER_CAP_SOURCE_ENV_KEY marker is the operator's: it wins, is neither clamped nor rebalanced, and is
    logged as ``operator``. A value SOVA set is marked, and what happens to it depends on the mark. A
    ``config`` value (a parent's ``testing.check_workers``) is kept for the subtree, unless this project sets
    its own ``testing.check_workers``. A ``computed`` value was sized for the agents active when its
    process was spawned, so it is replaced by a split of the agents active now: an agent started alone does
    not keep every core for the hour while later agents split the rest.
    """
    existing = env.get(PYTEST_WORKERS_ENV_KEY, "").strip()
    marker = env.get(WORKER_CAP_SOURCE_ENV_KEY)
    if existing and marker not in (WORKER_CAP_SOURCE_CONFIG, WORKER_CAP_SOURCE_COMPUTED):
        _log_cap(quiet, source="operator", workers=existing)
        return env
    inherited = existing if existing and marker == WORKER_CAP_SOURCE_CONFIG else None
    workers, source = decide_pytest_workers(config, project_dir, inherited=inherited, quiet=quiet)
    env[PYTEST_WORKERS_ENV_KEY] = str(workers)
    env[WORKER_CAP_SOURCE_ENV_KEY] = source
    return env


# Third-party provider routing and model pinning. When present, these make the
# Claude Code CLI talk to Vertex AI or Bedrock, or pin a model, regardless of
# what SOVA configured. SOVA owns provider and model selection, so inheriting
# any of them is always a misconfiguration.
PROVIDER_ROUTING_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
    }
)

# Identity of an enclosing interactive Claude Code session. These leak in when
# the SOVA server is started from inside a Claude Code session; a spawned agent
# that inherits them reports as, and can talk to, the parent session.
PARENT_SESSION_VARS: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SSE_PORT",
    }
)

# The server's own service-manager environment (systemd notify/watchdog, the log paths a launchd plist
# exports). An agent that inherits them and starts a nested ``sova server`` or ``sova dashboard`` would
# believe it runs under the service: it would leave its own agents running at shutdown, take over SIGHUP,
# reopen the outer server's log files, and send READY=1 to the outer unit's notify socket.
# Keep in sync with the names read in ``sova/scheduler/service.py``.
SERVICE_MANAGER_VARS: frozenset[str] = frozenset(
    {
        "NOTIFY_SOCKET",
        "WATCHDOG_USEC",
        "WATCHDOG_PID",
        "SOVA_SERVER_LOG",
        "SOVA_SERVER_ERR_LOG",
    }
)

# Codex credential. Folded into SCRUBBED_VARS so a CODEX_API_KEY set in
# the SOVA server's own environment (e.g. for someone else's automation
# script) is stripped from every spawned child by default, not just Codex's.
# Deliberately NOT reused via agent.env_passthrough, since that list is global
# across runtimes and would re-leak the key into ClaudeCodeRuntime/AiderRuntime
# children too; CodexRuntime re-injects it explicitly, scoped to its own spawn.
CODEX_CREDENTIAL_VARS: frozenset[str] = frozenset({"CODEX_API_KEY"})

# OpenAI credential. No agent runtime in this repo authenticates with
# OPENAI_API_KEY directly, so it is scrubbed from every spawned child by
# default. The one scoped exception is sova/ipc/runtime.py's spawn_direct(),
# the launcher for the developer/researcher/planner pipeline roles: that
# child runs a full WorkflowEngine which may read OPENAI_API_KEY in-process
# (sova/llm/litellm_provider.py) if the project configures an OpenAI-backed
# litellm model, so spawn_direct() re-admits it via extra_env
# (_openai_extra_env(), mirroring _codex_extra_env()), scoped to that one
# call site only. ClaudeCodeRuntime/CodexRuntime spawns never see it.
#
# AiderRuntime is deliberately NOT given the same carve-out: it forwards an
# arbitrary caller-supplied model string straight to Aider's --model flag,
# and today's only caller resolves that string from agent.model, documented
# as "Claude model to use for agent work". An OpenAI-backed Aider model is
# out of scope until that changes; if it ever does, scope the exception to
# AiderRuntime's own spawn() rather than widening agent.env_passthrough.
# Not reused via agent.env_passthrough for the same reason CODEX_CREDENTIAL_VARS
# isn't: that escape hatch is global across runtimes, and would leak the key
# into ClaudeCodeRuntime/AiderRuntime/CodexRuntime children too.
OPENAI_CREDENTIAL_VARS: frozenset[str] = frozenset({"OPENAI_API_KEY"})

SCRUBBED_VARS: frozenset[str] = (
    PROVIDER_ROUTING_VARS | PARENT_SESSION_VARS | SERVICE_MANAGER_VARS | CODEX_CREDENTIAL_VARS | OPENAI_CREDENTIAL_VARS
)

# Credential vars can never be re-admitted via agent.env_passthrough, even if
# a deployment's passthrough list names them explicitly: that escape hatch
# exists for provider-routing vars (Vertex/Bedrock), not for leaking a
# runtime-specific credential into every spawned child.
_PASSTHROUGH_INELIGIBLE: frozenset[str] = CODEX_CREDENTIAL_VARS | OPENAI_CREDENTIAL_VARS | SERVICE_MANAGER_VARS

# Credential variables are deliberately NOT scrubbed by default: the anthropic
# provider reads ANTHROPIC_API_KEY from the environment, and removing it here
# would break that path. However, an unrelated provider's child (e.g. Codex)
# must never receive it just because it happened to be set in the server's own
# environment; ``scrub_agent_env()``'s ``extra_scrub`` parameter lets a caller
# remove these on a per-spawn basis without touching the shared default.
ANTHROPIC_CREDENTIAL_VARS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})


def configured_passthrough() -> tuple[str, ...]:
    """Read ``agent.env_passthrough`` without making config load fatal.

    Spawning must keep working when config is unavailable (bare worktree,
    uninstalled project), so any failure degrades to scrubbing everything.
    Shared by every ``scrub_agent_env()`` call site so the escape hatch
    applies uniformly, whether the caller spawns a pipeline agent
    (``sova/ipc/runtime.py``) or invokes the CLI directly for a single step
    (``sova/llm/providers/claude_code.py``).
    """
    try:
        from sova.config.loader import load_config

        return tuple(load_config().agent.env_passthrough)
    except Exception:  # noqa: BLE001 (config may fail for many reasons; scrubbing defaults to strict)
        log.debug("env.passthrough_unavailable", exc_info=True)
        return ()


def scrub_agent_env(
    env: Mapping[str, str] | None = None,
    *,
    passthrough: Iterable[str] = (),
    extra_scrub: Iterable[str] = (),
) -> dict[str, str]:
    """Return a copy of ``env`` with inherited provider overrides removed.

    Args:
        env: Source environment. ``None`` reads the current process env.
        passthrough: Variable names to preserve despite being scrubbed by
            default. Set via ``agent.env_passthrough`` for deployments that
            intentionally route the Claude CLI through Vertex AI or Bedrock.
            A name in ``_PASSTHROUGH_INELIGIBLE`` (a credential, e.g.
            ``CODEX_API_KEY``/``OPENAI_API_KEY``) is stripped from this set
            unconditionally: that escape hatch is for provider-routing vars,
            not for re-leaking a credential into every spawned child.
        extra_scrub: Variable names to remove in addition to ``SCRUBBED_VARS``,
            for a single spawn rather than every one. Applied unconditionally,
            after the ``passthrough`` keep-set: a name here is removed even if
            it was also named in ``passthrough``, since ``env_passthrough`` is
            a global escape hatch and must not be able to re-admit a
            credential a specific caller has deliberately excluded (e.g.
            ``CodexRuntime`` scrubbing ``ANTHROPIC_API_KEY`` from its own
            children regardless of what a Vertex/Bedrock deployment opted
            back in for Claude Code).

    Returns:
        A new dict safe to hand to a spawned subprocess.
    """
    source = os.environ if env is None else env
    keep = {name.strip() for name in passthrough if name and name.strip()} - _PASSTHROUGH_INELIGIBLE
    always_remove = {name.strip() for name in extra_scrub if name and name.strip()}

    result: dict[str, str] = {}
    removed: list[str] = []
    for key, value in source.items():
        if key in always_remove:
            removed.append(key)
            continue
        if key in SCRUBBED_VARS and key not in keep:
            removed.append(key)
            continue
        result[key] = value

    if removed:
        # Names only. Values may be credentials and are never logged.
        log.info("env.scrubbed", removed=sorted(removed), passthrough=sorted(keep))
    return result
