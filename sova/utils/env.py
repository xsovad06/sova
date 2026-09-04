"""Environment sanitization for spawned LLM subprocesses.

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

from sova.utils.logging import get_logger

log = get_logger(component="utils.env")


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

SCRUBBED_VARS: frozenset[str] = PROVIDER_ROUTING_VARS | PARENT_SESSION_VARS

# Credential variables are deliberately NOT scrubbed: the anthropic provider
# reads ANTHROPIC_API_KEY from the environment, and removing it here would
# break that path. Provider-aware credential scrubbing is tracked separately.


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
    except Exception:
        log.debug("env.passthrough_unavailable", exc_info=True)
        return ()


def scrub_agent_env(
    env: Mapping[str, str] | None = None,
    *,
    passthrough: Iterable[str] = (),
) -> dict[str, str]:
    """Return a copy of ``env`` with inherited provider overrides removed.

    Args:
        env: Source environment. ``None`` reads the current process env.
        passthrough: Variable names to preserve despite being scrubbed by
            default. Set via ``agent.env_passthrough`` for deployments that
            intentionally route the Claude CLI through Vertex AI or Bedrock.

    Returns:
        A new dict safe to hand to a spawned subprocess.
    """
    source = os.environ if env is None else env
    keep = {name.strip() for name in passthrough if name and name.strip()}

    result: dict[str, str] = {}
    removed: list[str] = []
    for key, value in source.items():
        if key in SCRUBBED_VARS and key not in keep:
            removed.append(key)
            continue
        result[key] = value

    if removed:
        # Names only. Values may be credentials and are never logged.
        log.info("env.scrubbed", removed=sorted(removed), passthrough=sorted(keep))
    return result
