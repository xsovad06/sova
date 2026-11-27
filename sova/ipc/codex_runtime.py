"""Codex agent runtime: the Codex CLI backend for the AgentRuntime ABC.

Holds everything specific to ``codex exec``: auth and stdin-prompt probes,
argument and environment construction, and ``CodexRuntime``. The ABC, the
shared spawn helpers and the runtime registry stay in ``sova.ipc.runtime``,
which resolves ``CodexRuntime`` lazily inside ``create_runtime()``: importing
this module from ``runtime.py`` at module level would be a cycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path

from sova.config.models import CodexConfig
from sova.ipc.codex import CodexStreamParser
from sova.ipc.control import AgentProcess, FileAgentProcess
from sova.ipc.runtime import (
    _HEADLESS_PREAMBLE_CORE,
    _HEADLESS_PREAMBLE_EXECUTE_SUFFIX,
    _VERSION_CHECK_TIMEOUT,
    CONVERSATION_MCP_SERVER,
    AgentRuntime,
    ConversationTurnUnsupportedError,
    StreamParser,
    _check_cli_available,
    _inject_agent_marker,
    _spawn_agent_process,
)
from sova.llm.errors import ConfigurationError
from sova.llm.models import CostSource, StreamEvent
from sova.utils.env import (
    ANTHROPIC_CREDENTIAL_VARS,
    ORCHESTRATOR_ONLY_VARS,
)
from sova.utils.logging import get_logger
from sova.utils.shell import run

# Same component as before the move out of sova.ipc.runtime, so log filters keep matching Codex events.
log = get_logger(component="ipc.runtime")

# Codex gets the provider-neutral core only, no Claude-specific tail. See CodexRuntime.transform_prompt().
_HEADLESS_PREAMBLE_CODEX = _HEADLESS_PREAMBLE_CORE + _HEADLESS_PREAMBLE_EXECUTE_SUFFIX

# codex login status is a local keychain/credential-store read, matching the
# rationale for _AUTH_CHECK_TIMEOUT in the Claude Code provider.
_CODEX_AUTH_PROBE_TIMEOUT = _VERSION_CHECK_TIMEOUT

_AUTH_UNKNOWN_DETAIL = "auth state unknown"


def _resolve_codex_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Read ``CODEX_API_KEY`` from ``env``, falling back to the process environment.

    ``env`` is the caller-supplied child environment (``spawn(env=...)``), so a
    caller that curated its own environment gets its own key rather than the
    server's: a per-project key must not be silently replaced by whatever
    happens to be in ``os.environ``. ``None`` means "no curated env", which is
    the probe path and the default spawn path.

    A blank or whitespace-only value is treated as absent: SOVA must not inject
    or report an empty credential as present.
    """
    source = os.environ if env is None else env
    value = source.get("CODEX_API_KEY", "").strip()
    return value or None


def _resolve_codex_cost_source(billing_mode: str = "unknown") -> CostSource:
    """Whether a Codex spawn is subscription-backed, API-metered, or unpriced.

    A set ``CODEX_API_KEY`` is positive evidence the CLI bills per-token
    against that key, and SOVA has no Codex pricing source to turn that into
    a dollar figure, so that case always stays ``CostSource.UNKNOWN``
    regardless of *billing_mode*.

    Absence of that env var is NOT positive evidence of a subscription:
    ``codex login --api-key`` stores a metered API key in Codex's own
    keyring, invisible to this check, and ``codex login status`` (see
    ``_interpret_codex_auth_probe()``) reports only logged-in/logged-out,
    never billing mode. Treating "no env var" as "subscription" would invert
    the issue's own constraint against inferring API billing from a ChatGPT
    subscription login, and silently misclassify a keyring-stored API key as
    free. So the only way to reach ``CostSource.SUBSCRIPTION`` is an explicit
    operator declaration via ``codex.billing_mode = "subscription"``
    (``CodexConfig.billing_mode``); the only other value, the default
    ``"unknown"``, stays ``CostSource.UNKNOWN``. Issue #1128.

    Deliberately stateless (takes *billing_mode* as a parameter and reads
    ``os.environ`` fresh on every call, rather than caching on ``self``):
    ``CodexRuntime`` is a module-level singleton shared by every concurrent
    Codex spawn, so a cached answer would leak one spawn's auth mode into
    another's. This only benefits callers that re-resolve per use:
    ``create_stream_parser()`` calls this fresh for every spawn, so an
    operator who switches auth mode between runs is not stuck on a stale
    answer there. ``CodexRuntime.__init__``'s own convenience parser
    (``self._parser``, used only by the diagnostics-only ``parse_output()``)
    calls this once at construction time and keeps that answer for the
    runtime's lifetime; a ``CODEX_API_KEY`` exported after construction, or a
    ``codex.billing_mode`` edit applied via ``reload_runtime()``, is invisible
    to that one shared parser even though this function itself stayed
    stateless.

    Known limitation: the ``CODEX_API_KEY`` check reads the server's own
    environment, not the curated child environment a caller may have passed
    to ``spawn(env=...)``, which is what ``_codex_extra_env()`` actually
    forwards to the Codex child. ``AgentRuntime.create_stream_parser()``
    takes no arguments and is called independently of ``spawn()``, so the
    per-spawn env is not reachable here. The two agree for every caller today
    (both dashboard spawn sites build their env from ``os.environ``), but a
    caller that supplied a per-project ``CODEX_API_KEY`` absent from
    ``os.environ`` would miss this check. Fixing it properly means threading
    a per-spawn context into the ABC, deferred rather than guessed at here.
    """
    if _resolve_codex_api_key() is not None:
        return CostSource.UNKNOWN
    return CostSource.SUBSCRIPTION if billing_mode == "subscription" else CostSource.UNKNOWN


def _codex_exec_args(prompt: str | None, *, model: str | None, sandbox: str, network_access: bool = False) -> list[str]:
    """Build the ``codex exec --json --sandbox ... [--model M] [-- PROMPT]`` argv.

    A networked command gets a Codex config override while retaining the
    workspace-write filesystem sandbox. A read-only sandbox cannot opt in.

    Shared by ``CodexRuntime.spawn()`` and ``ReviewerRole._gather_repo_context()``
    (the reviewer's optional read-only repo-context sub-call) so the two
    ``codex exec`` invocation paths cannot silently diverge from each other,
    the same reasoning that already applies to the Claude CLI's two
    invocation paths (see ``build_claude_cli_args()``). ``prompt`` must
    already be whatever text the caller wants on argv: this function does
    not call ``transform_prompt()``, since the repo-context sub-call
    deliberately skips the headless guardrail preamble (it is not a pipeline
    agent).

    ``prompt=None`` omits the PROMPT positional entirely, for
    ``CodexRuntime.spawn()``'s stdin delivery path (see
    ``_resolve_stdin_capable()``): the caller is responsible for delivering
    the prompt via ``stdin_payload`` instead in that case.

    ``"--"`` is passed unconditionally when ``prompt`` is given: ``codex
    exec`` takes PROMPT as a positional argument (clap-based parser), so an
    argv value starting with ``"-"`` would otherwise be misread as an
    unrecognized option.
    """
    args: list[str] = ["codex", "exec", "--json", "--sandbox", sandbox]
    if network_access:
        if sandbox != "workspace-write":
            raise ConfigurationError("Codex network commands require the workspace-write sandbox")
        args.extend(["--config", "sandbox_workspace_write.network_access=true"])
    if model:
        args.extend(["--model", model])
    if prompt is not None:
        args.extend(["--", prompt])
    return args


def _codex_extra_env(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Build the ``extra_env`` override that re-admits ``CODEX_API_KEY``.

    ``CODEX_API_KEY`` is in ``SCRUBBED_VARS`` by default (see
    ``sova.utils.env``), so it is absent from the ``env`` a ``CodexRuntime``
    spawn would otherwise receive. This re-injects it after scrubbing, scoped
    to two callers: ``CodexRuntime.spawn()``/``_probe_codex_auth()`` scope it
    to a single ``codex exec`` child, while ``agent_lifecycle.py`` passes it
    to the reviewer's own ``spawn_direct()`` process (gated on
    ``review.repo_context_agent``), not a Codex child at all. The key still
    never reaches a Claude Code or Aider child: any such child spawned from
    that reviewer process re-scrubs its own environment via
    ``scrub_agent_env()`` at its own spawn boundary.
    """
    api_key = _resolve_codex_api_key(env)
    return {"CODEX_API_KEY": api_key} if api_key else None


async def _probe_codex_auth() -> str | None:
    """Run ``codex login status`` once and return its combined output.

    A local credential-store read only (never a model request or any other
    billable API call). Uses the same environment a real Codex spawn would see
    (shared scrub, cross-provider credentials removed, ``CODEX_API_KEY``
    re-injected if set) so the probe's answer matches what agents actually
    experience.

    Returns ``None`` on subprocess spawn failure or timeout, mirroring
    ``_probe_auth()`` in the Claude Code provider (which runs the equivalent
    ``claude auth status`` probe through the same ``shell.run()`` helper).
    ``codex login status`` has no documented machine-readable output contract,
    so stdout and stderr are returned as one blob for the caller to interpret
    defensively rather than assumed to be JSON. The exit code is deliberately
    not returned: its meaning is undocumented too, so only the text is worth
    reading.
    """
    probe_env = _inject_agent_marker(None, extra_scrub=ANTHROPIC_CREDENTIAL_VARS, extra_env=_codex_extra_env())
    try:
        result = await run("codex", "login", "status", env=probe_env, timeout=_CODEX_AUTH_PROBE_TIMEOUT)
    except OSError:
        log.debug("codex.auth_probe_failed", exc_info=True)
        return None
    if result.timed_out:
        return None
    return f"{result.stdout}\n{result.stderr}".strip()


_CODEX_STDIN_PROBE_TIMEOUT = _VERSION_CHECK_TIMEOUT

# Codex's system and managed configuration layers: `--ignore-user-config` skips only $CODEX_HOME/config.toml, and
# the layers merge, so an MCP server defined here would sit beside the one a conversation turn passes.
CODEX_SYSTEM_CONFIG_FILES = (Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"))


def codex_project_config_files(cwd: str | Path) -> list[Path]:
    """The project-scoped ``.codex/config.toml`` candidates Codex merges for a run in *cwd*: *cwd* and each parent."""
    start = Path(cwd).resolve()
    return [directory / ".codex" / "config.toml" for directory in (start, *start.parents)]


def codex_system_mcp_files(files: Iterable[Path] = CODEX_SYSTEM_CONFIG_FILES) -> list[str]:
    """The Codex config files that define MCP servers. An unreadable one is listed too: it fails closed."""
    found: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            found.append(str(path))
            continue
        if data.get("mcp_servers"):
            found.append(str(path))
    return found


# A clause mentioning "stdin" alongside one of these is describing the absence
# of stdin support, not its presence (e.g. "PROMPT is not read from stdin").
# Scanning per clause within a PROMPT block, rather than the whole blob, keeps an
# unrelated flag elsewhere in --help that happens to mention "stdin" from forcing
# this same negation check onto text that has nothing to do with it.
_STDIN_NEGATION_MARKERS = (
    "not ",
    "cannot",
    "can't",
    "no stdin",
    "unsupported",
    "doesn't",
    "does not",
    "won't",
    "never",
)

# clap (Codex CLI's arg parser) names the PROMPT positional inside <> when
# required or [] when optional on its own definition line. Requiring one of
# these markers, not just the word "stdin", keeps an unrelated option's
# incidental stdin mention (e.g. "--input reads additional config from
# stdin") from being misread as PROMPT's own stdin support.
_PROMPT_ARG_MARKERS = ("<prompt>", "[prompt]")


class CodexStdinProbeAmbiguousError(RuntimeError):
    """Raised when ``codex exec --help`` succeeded but its PROMPT argument
    line could not be located at all.

    This is distinct from a probe that could not run (CLI missing, timed
    out, non-zero exit): those are infrastructure failures and keep the
    pre-#1148 fallback to argv delivery, since a broken or absent CLI can't
    attempt a spawn either way. This error is the one case the probe spec
    calls out explicitly: real output came back but didn't match the
    documented shape at all (e.g. a future CLI's ``--help`` restructures its
    positional-argument section), so neither "stdin supported" nor "stdin
    unsupported" can be asserted. Silently defaulting to argv here would mean
    a CLI format change permanently and invisibly reverts every spawn to the
    argv-visibility hazard this probe exists to close, with nothing in the
    logs to say why. Raising instead surfaces that drift at spawn time as an
    explicit, actionable error (issue #1148).
    """


def _prompt_argument_blocks(lines: list[str]) -> Iterator[str]:
    """Yield every PROMPT argument block: its definition line plus wrapped continuation lines.

    clap (and many other CLI arg parsers) wraps a long argument description
    onto further lines indented deeper than the argument's own definition
    line, with no blank line separating them. Scanning only the single line
    carrying the ``<prompt>``/``[prompt]`` marker misses a stdin mention that
    landed on one of those continuation lines, which silently failed this
    probe closed on the installed CLI's actual (multiline) ``--help`` output
    even when stdin delivery really was documented: every build gets argv
    delivery instead of stdin, defeating the hazard this probe exists to
    close (issue #1148). This groups each wrapped block before any "stdin"
    mention is checked, so a description that happens to wrap still counts.

    The CLI's synopsis/usage line (e.g. ``usage: codex exec [options]
    [prompt]``) also carries a ``[prompt]`` marker but never describes stdin
    support; it is skipped so it cannot shadow the real Arguments section
    entry that follows it. Every remaining marker line yields its own block,
    since the installed CLI's actual PROMPT documentation might not be the
    first marker line found.

    A continuation line is identified by strictly deeper indentation than
    the marker line itself, and the block ends at the first blank line or a
    line indented no deeper than the marker line (the start of the next
    argument/option's own definition). Yields nothing when no line carries
    a PROMPT marker at all.
    """
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("usage:"):
            continue
        # The marker must lead the stripped line (clap's own positional
        # argument definition, e.g. "[PROMPT]  Description..."), not appear
        # embedded inside another option's own definition (e.g.
        # "--template <PROMPT>  ..."), which would otherwise match this
        # substring check and let that unrelated option's description
        # stand in for PROMPT's own stdin support.
        if not any(stripped.startswith(marker) for marker in _PROMPT_ARG_MARKERS):
            continue
        indent = len(line) - len(line.lstrip())
        block = [line]
        for cont in lines[idx + 1 :]:
            if not cont.strip():
                break
            cont_indent = len(cont) - len(cont.lstrip())
            if cont_indent <= indent:
                break
            block.append(cont)
        yield "\n".join(block)


async def _probe_codex_stdin_prompt_support() -> bool:
    """Check whether the installed Codex CLI documents a stdin-delivered prompt.

    Reads ``codex exec --help`` (a local, non-billing, non-model-invoking
    read, like the ``--version`` check in ``_check_cli_available()``) and
    looks for the CLI's own PROMPT argument documentation mentioning stdin.
    Never guesses from a hardcoded version string: Codex CLI versioning is
    external, so a hardcoded cutoff would silently become wrong on the next
    release.

    Uses the shared ``run()`` helper, like ``_probe_codex_auth()`` above,
    including the same scrubbed environment (``--help`` needs no credential
    at all, so no ``extra_env`` re-injection is needed here). A probe that
    could not run at all (CLI missing, timed out, non-zero exit) fails
    closed to ``False`` (argv delivery, today's behavior): wrongly assuming
    stdin support risks the child blocking on an interactive terminal read
    instead of the prompt it was supposed to receive, a hang strictly worse
    than the argv-visibility hazard this probe exists to close. Confirmation
    requires the line to be PROMPT's own argument-definition line (see
    ``_PROMPT_ARG_MARKERS``); an unrelated option that happens to mention
    "stdin" must not match, and a line mentioning "stdin" in a negated sense
    (e.g. "not read from stdin") is a confirmed negative, not an ambiguous
    one. The PROMPT line is grouped with its wrapped continuation lines
    (``_prompt_argument_blocks()``) before any of this is checked, since a
    real CLI's description commonly wraps onto further, more-indented lines
    rather than staying on PROMPT's own line.

    If real ``--help`` output came back but no PROMPT argument line can be
    found at all, that is neither a confirmed yes nor a confirmed no: it
    means this probe no longer understands the installed CLI's ``--help``
    shape. That case raises ``CodexStdinProbeAmbiguousError`` instead of
    silently defaulting to argv, so a future CLI format change is a loud
    spawn-time error rather than a silent, permanent reversion to the
    argv-visibility hazard (issue #1148).
    """
    probe_env = _inject_agent_marker(None, extra_scrub=ANTHROPIC_CREDENTIAL_VARS)
    try:
        result = await run("codex", "exec", "--help", env=probe_env, timeout=_CODEX_STDIN_PROBE_TIMEOUT)
    except OSError:
        log.debug("codex.stdin_probe_failed", exc_info=True)
        return False

    if result.timed_out or not result.success:
        return False

    lines = result.stdout.lower().splitlines()
    found_any = False
    for block in _prompt_argument_blocks(lines):
        found_any = True
        # Negation is checked per clause, not per line: a line like "if not
        # provided as an argument, read from stdin" negates "provided as an
        # argument", not "read from stdin". Checking the whole line would
        # misread that unrelated "not" as negating stdin support. The
        # block's lines are joined with a space (not kept as separate
        # lines) before splitting into clauses, so a sentence that wraps
        # exactly at a continuation line's boundary (e.g. "...is not
        # read\nfrom stdin...") still reads as one clause rather than
        # splitting the negation away from the "stdin" mention it governs.
        joined = " ".join(block.splitlines())
        for clause in re.split(r"[.,]", joined):
            if "stdin" in clause and not any(marker in clause for marker in _STDIN_NEGATION_MARKERS):
                return True

    if not found_any:
        raise CodexStdinProbeAmbiguousError(
            "codex exec --help did not contain a recognizable PROMPT argument line; "
            "cannot confirm stdin prompt delivery support"
        )
    return False


# The positive status is also a line of its own: a mid-line mention ("Error: No API key configured") is not one.
_CODEX_API_KEY_CONFIGURED_RE = re.compile(r"^\s*api key configured\b", re.MULTILINE)
_CODEX_NO_API_KEY_RE = re.compile(r"^\s*(?:no api key(?: configured)?|api key not configured)\b", re.MULTILINE)


def _interpret_codex_auth_probe(text: str | None) -> tuple[bool | None, str]:
    """Interpret ``codex login status`` output.

    Returns ``(authenticated, detail)``:

    - ``True``: the CLI reports an authenticated session.
    - ``False``: the CLI reports it is logged out.
    - ``None``: the output could not be confidently interpreted (unknown CLI
      build, unexpected format, probe never ran). Callers fail open on this
      case, matching ``_interpret_auth_probe()`` in the Claude Code provider:
      a CLI build variation must never block agent spawning.

    ``codex login status`` has no documented machine-readable contract, so
    text is scanned defensively rather than assumed to be JSON. A JSON
    payload is still attempted first in case a future CLI version emits one;
    a bare scalar (valid JSON, but not an object) carries no fields to read and falls through to
    the plain-text scan below, matching ``_extract_auth_detail()`` in
    ``doctor.py`` for ``gh auth status``.
    """
    if not text:
        return None, _AUTH_UNKNOWN_DETAIL

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("loggedIn", "logged_in", "authenticated"):
            # Strict identity, not truthiness: a null or string value is a
            # shape this code does not understand, and must fall through to
            # "unknown" (fail open) rather than be read as a definite logout.
            if data.get(key) is True:
                return True, "authenticated"
            if data.get(key) is False:
                return False, "not authenticated (run: codex login)"
        # A dict this code could not read is unknown, full stop. Falling back
        # to the phrase scan here would scan the JSON source itself, where a
        # key name ("authenticated") matches regardless of its value.
        return None, _AUTH_UNKNOWN_DETAIL

    lowered = text.lower()
    negatives = ("not logged in", "not authenticated", "logged out", "no credentials")
    # codex-cli's "No API key configured" is a status line of its own. Anchored to the line start,
    # a ChatGPT login whose status mentions an API key, or "proxy not configured", is not a logout.
    if any(phrase in lowered for phrase in negatives) or _CODEX_NO_API_KEY_RE.search(lowered):
        return False, "not authenticated (run: codex login)"
    # codex-cli 0.166 replaced "Logged in using an API key" with "API key configured",
    # adding that the key itself is only checked on the first request.
    if _CODEX_API_KEY_CONFIGURED_RE.search(lowered):
        return True, "API key configured"
    if any(phrase in lowered for phrase in ("logged in", "authenticated")):
        return True, "authenticated"

    return None, _AUTH_UNKNOWN_DETAIL


def _toml_string(value: str) -> str:
    """*value* as a TOML basic string: quotes, backslashes and control characters escaped, everything else literal.

    ``json.dumps()`` is not TOML: it writes a character outside the Basic Multilingual Plane as a surrogate pair
    (two ``\\u`` escapes), which TOML rejects, so a checkout path holding one would break the whole override.
    """
    out = []
    for char in value:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


class CodexRuntime(AgentRuntime):
    """Runtime that spawns Codex CLI processes.

    Codex (https://developers.openai.com/codex/) is OpenAI's coding agent
    CLI. Invoked non-interactively via ``codex exec`` with JSON Lines output
    and an explicit ``--sandbox`` policy, resolved per spawn (see
    ``spawn()``'s ``read_only`` handling below) rather than hardcoded.

    Credential handling: local setup should rely on Codex's own keyring
    storage (``codex login`` / ``cli_auth_credentials_store = "keyring"``);
    SOVA never reads, copies, or persists that credential. An optional
    ``CODEX_API_KEY`` in SOVA's own process environment is forwarded only to
    the Codex child that needs it (see ``_codex_extra_env()``), and is
    scrubbed from every other spawned runtime by default (``SCRUBBED_VARS``
    in ``sova.utils.env``). ``check_available()`` uses ``codex login status``
    to distinguish missing CLI, logged-out CLI, and authenticated CLI without
    ever making a model request.

    ``parse_output()`` delegates to a ``CodexStreamParser`` held on this
    runtime object. That parser is stateful and this runtime is a
    module-level singleton (``get_runtime()``), so ``parse_output()`` is a
    single-stream convenience only, unsafe to use for two concurrent Codex
    agents. ``create_stream_parser()`` is what the dashboard's stream tailer
    actually calls; ``sova/ipc/codex.py`` documents both the parser's own
    semantics and why a fresh instance per spawned process is required.

    ``transform_prompt()`` prepends ``_HEADLESS_PREAMBLE_CODEX``, the
    provider-neutral subset of Claude Code's ``_HEADLESS_PREAMBLE`` (pipeline
    boundary, literal command execution, worktree conflict recovery,
    no-background-waiting; the ``/compact``-referencing CONTEXT MANAGEMENT
    section is Claude Code-specific and is deliberately omitted). This closes
    the pipeline-boundary guardrail gap that used to be tracked here against
    epic #940 (issue #946).

    Prompt delivery is stdin when the installed Codex CLI documents support
    for it, argv otherwise: ``_probe_codex_stdin_prompt_support()`` reads
    ``codex exec --help`` once per runtime instance (cached on
    ``self._stdin_capable``) and ``spawn()`` only omits the PROMPT positional
    (delivering the prompt via ``stdin_payload`` instead, the same mechanism
    ``ClaudeCodeRuntime.spawn()`` uses) when that probe answered yes. There
    are two distinct failure modes, handled differently: a probe that could
    not run at all (CLI missing, timed out, non-zero exit) keeps the prompt
    on argv exactly as before (issue #1127), which still widens the known
    ``ps``/``pkill -f`` argv-visibility hazard (see ``sova/llm/cli_args.py``)
    on that fallback path, since the guardrail preamble makes the argv
    string longer. A probe that *did* run but whose output no longer
    matches the documented PROMPT-argument shape (a future CLI restructuring
    its ``--help``) is a different, louder failure: it raises
    ``CodexStdinProbeAmbiguousError`` out of ``spawn()`` instead of silently
    defaulting to argv (issue #1148), since silent fallback there would mean
    a CLI format change permanently and invisibly reopens the
    argv-visibility hazard with nothing in the logs to say why. The preamble's COMMAND
    INTERPRETATION clause deliberately avoids embedding a literal
    ``sova run N``-shaped example for exactly this reason (a fixed example
    substring would put every Codex agent's argv inside reach of a
    ``pkill -f`` matching it, including a ``command:*`` run whose own
    prompt never mentions sova at all). The caller's own prompt text still
    ends up on argv on the fallback path: a non-pipeline role (reviewer,
    custom) spawned through this runtime wraps its command in a
    ``sova run ...`` invocation (``agent_lifecycle.py``'s ``cmd_parts``), so
    that literal substring still reaches argv for those roles even with the
    static example gone. Never reap SOVA processes by matching prompt text.

    Model and sandbox policy come from ``CodexConfig`` (``[codex]`` in
    ``sova.toml``), passed in at construction via ``create_runtime(codex=...)``.
    The caller-supplied ``model`` argument to ``spawn()`` is a Claude model id
    resolved from ``agent.model`` and is never forwarded to ``codex exec``.
    ``spawn(read_only=True)`` overrides ``codex.sandbox`` to ``"read-only"``
    for that one call, regardless of the configured default; it never appends
    a second ``--sandbox`` pair.
    """

    def __init__(self, config: CodexConfig | None = None) -> None:
        super().__init__()
        self._config = config if config is not None else CodexConfig()
        # Threaded through here too, not just create_stream_parser(), so this
        # convenience parser's provenance (used by parse_output(), a
        # diagnostics-only single-stream path) agrees with the construction-time
        # codex.billing_mode rather than always reporting UNKNOWN. Resolved once
        # here and kept for this runtime instance's lifetime: unlike
        # create_stream_parser(), which every real spawn calls fresh, this one
        # shared parser does NOT see a CODEX_API_KEY exported or a
        # codex.billing_mode edit (applied via reload_runtime()) after
        # construction. parse_output() is unused on both live Codex paths today
        # (create_stream_parser() is always preferred), so this staleness has no
        # production consequence yet.
        self._parser = CodexStreamParser(
            model=self._config.model,
            cost_source=_resolve_codex_cost_source(self._config.billing_mode),
        )
        # Cached on first spawn: None means "not probed yet", so the real
        # probe only ever runs once per runtime instance rather than once per
        # spawn. An ambiguous outcome is cached too (as the raised
        # exception instance), not just a definite True/False, so a CLI
        # build whose --help the probe cannot read still only pays the
        # subprocess cost once per instance instead of re-probing (and
        # re-raising) on every spawn. See _resolve_stdin_capable().
        self._stdin_capable: bool | CodexStdinProbeAmbiguousError | None = None
        # Guards the first-probe race: concurrent spawns on the same runtime
        # instance both seeing _stdin_capable is None would otherwise both
        # enter the uncached branch, and whichever call's result lands last
        # (True/False/exc) wins, silently overwriting an ambiguous outcome
        # with a plain False. The lock serializes resolution so only one
        # subprocess probe ever runs, and every concurrent caller waits for
        # and shares that single outcome.
        self._stdin_capable_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "codex"

    @property
    def enforces_read_only(self) -> bool:
        return True

    @property
    def honours_model_override(self) -> bool:
        return False

    def transform_prompt(self, prompt: str) -> str:
        """Prepend the provider-neutral headless guardrail preamble.

        See ``_HEADLESS_PREAMBLE_CODEX`` and the class docstring above for
        why this is a subset of Claude Code's ``_HEADLESS_PREAMBLE`` rather
        than the same text.
        """
        return _HEADLESS_PREAMBLE_CODEX + prompt

    async def _resolve_stdin_capable(self) -> bool:
        """Resolve, and cache on this instance, whether the installed Codex
        CLI supports a stdin-delivered prompt (see
        ``_probe_codex_stdin_prompt_support()``).

        An ambiguous outcome (``CodexStdinProbeAmbiguousError``) is cached
        too, and re-raised on every subsequent call, so the probe subprocess
        runs at most once per instance even when the installed CLI's
        ``--help`` is unrecognizable: without this, every ``spawn()`` would
        re-probe and re-raise instead of failing once and staying failed.

        The first resolution is serialized by ``self._stdin_capable_lock``:
        without it, two concurrent spawns racing past the ``is None`` check
        both probe, and whichever result is assigned last wins, able to
        silently overwrite a cached ``CodexStdinProbeAmbiguousError`` with a
        plain ``False``. Only the probe itself needs the lock; once
        ``self._stdin_capable`` is set, every caller returns (or re-raises)
        from cache without reacquiring it.
        """
        if self._stdin_capable is None:
            async with self._stdin_capable_lock:
                if self._stdin_capable is None:
                    try:
                        self._stdin_capable = await _probe_codex_stdin_prompt_support()
                    except CodexStdinProbeAmbiguousError as exc:
                        self._stdin_capable = exc
        if isinstance(self._stdin_capable, CodexStdinProbeAmbiguousError):
            raise self._stdin_capable
        return self._stdin_capable

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        max_budget_usd: Decimal | None = None,
        output_dir: Path | None = None,
        run_label: str | None = None,
        read_only: bool = False,
        network_access: bool = False,
    ) -> AgentProcess | FileAgentProcess:
        if fallback_model is not None:
            log.warning(
                "codex.fallback_model_not_supported",
                fallback_model=fallback_model,
                hint="Codex CLI does not support a fallback model; the input is ignored",
            )

        if max_budget_usd is not None:
            if not self._config.allow_unenforced_budget:
                # This is a static configuration fault, not a billing or
                # rate-limit condition: no retry or fallback-model hop changes
                # whether Codex can enforce a budget cap, so it must not be
                # classified as billing (workflow.py's is_billing_failure
                # would otherwise walk the entire fallback chain re-raising
                # this identical error once per candidate, and
                # reliability_service would log an operator misconfiguration
                # as a billing incident). "sova_configuration_error" is one of
                # sova.llm.errors._CONFIGURATION_PATTERNS, scanned before the
                # billing patterns; keep that token in this message if it is
                # ever reworded.
                raise ConfigurationError(
                    f"sova_configuration_error: Codex CLI cannot enforce a budget cap "
                    f"(requested ${max_budget_usd}); set codex.allow_unenforced_budget = true "
                    "to spawn anyway with cost unbounded"
                )
            log.warning(
                "codex.budget_not_enforced",
                budget=str(max_budget_usd),
                hint="Codex CLI does not support budget caps; cost is not limited despite "
                "codex.allow_unenforced_budget being set",
            )

        sandbox = "read-only" if read_only else self._config.sandbox
        codex_model = self._config.model
        transformed_prompt = self.transform_prompt(prompt)

        stdin_capable = await self._resolve_stdin_capable()
        stdin_payload: str | None = None
        if stdin_capable:
            # No PROMPT positional at all: the probe confirmed this CLI reads
            # it from stdin when omitted, so the prompt never touches argv.
            args = _codex_exec_args(None, model=codex_model, sandbox=sandbox, network_access=network_access)
            stdin_payload = transformed_prompt
        else:
            args = _codex_exec_args(
                transformed_prompt, model=codex_model, sandbox=sandbox, network_access=network_access
            )

        log.info(
            "codex.spawn",
            cwd=str(cwd),
            model=codex_model,
            prompt_len=len(transformed_prompt),
            read_only=read_only,
            sandbox=sandbox,
            network_access=network_access,
            prompt_delivery="stdin" if stdin_capable else "argv",
        )

        return await _spawn_agent_process(
            args,
            cwd,
            env,
            output_dir,
            run_label,
            extra_scrub=ANTHROPIC_CREDENTIAL_VARS | ORCHESTRATOR_ONLY_VARS,
            extra_env=_codex_extra_env(env),
            stdin_payload=stdin_payload,
        )

    async def spawn_conversation_turn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        mcp_command: list[str],
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
        resume_session: str | None = None,
    ) -> AgentProcess | FileAgentProcess:
        """A conversation turn in Codex's read-only sandbox, with the read-only SOVA MCP server as its tool source.

        ``codex exec resume`` takes no ``--sandbox`` flag, so a resumed turn could not be pinned to the read-only
        sandbox on the command line; Codex turns therefore never resume (``continues_sessions`` is False) and the
        caller replays the transcript. Codex cannot enforce a budget cap, so *max_budget_usd* is not forwarded,
        and its model comes from ``codex.model`` as for every Codex spawn.

        An MCP server runs outside the sandbox, so none from the operator's own Codex config may load:
        ``--ignore-user-config`` skips ``$CODEX_HOME/config.toml`` (a ``--config mcp_servers=...`` override alone is
        merged into that table, not a replacement for it, so the operator's servers would survive it), and
        ``--ignore-rules`` skips the execpolicy rules. Auth still comes from ``CODEX_HOME``. The system layers
        (``/etc/codex``) and the project layers (``.codex/config.toml`` from the checkout up) merge in regardless, so a
        turn is refused while any of them defines an MCP server.

        The prompt (prior messages, tracker text) is never put on argv: a CLI whose stdin prompt delivery is not
        confirmed fails the turn instead of falling back the way a pipeline spawn does.
        """
        # The system layers and the project layers (.codex/config.toml in the checkout and its parents) merge in
        # whatever --ignore-user-config says, so a turn is refused while any of them defines an MCP server.
        layer_files = [*CODEX_SYSTEM_CONFIG_FILES, *codex_project_config_files(cwd)]
        configured_mcp = await asyncio.to_thread(codex_system_mcp_files, layer_files)
        if configured_mcp:
            raise ConversationTurnUnsupportedError(
                f"A Codex configuration layer defines MCP servers ({', '.join(configured_mcp)}), which a read-only "
                "conversation turn cannot exclude"
            )
        if not await self._resolve_stdin_capable():
            # The probe cannot tell a CLI without stdin support from one whose --help timed out, so the verdict is
            # not kept: the next turn probes again instead of being refused for as long as this instance lives.
            self._stdin_capable = None
            raise ConversationTurnUnsupportedError(
                "This Codex CLI does not document reading the prompt from stdin, and a conversation turn never puts "
                "its prompt on the command line; upgrade the Codex CLI (npm install -g @openai/codex)"
            )
        args = _codex_exec_args(None, model=self._config.model, sandbox="read-only")
        # Right after the ``exec`` subcommand, wherever _codex_exec_args() puts it.
        exec_at = args.index("exec") + 1
        args[exec_at:exec_at] = ["--ignore-user-config", "--ignore-rules"]
        command = ", ".join(_toml_string(part) for part in mcp_command[1:])
        server = f"{{command = {_toml_string(mcp_command[0])}, args = [{command}]}}"
        args.extend(["--config", f"mcp_servers={{{CONVERSATION_MCP_SERVER} = {server}}}"])
        log.info("codex.spawn_conversation", cwd=str(cwd), model=self._config.model, sandbox="read-only")
        return await _spawn_agent_process(
            args,
            cwd,
            env,
            None,
            None,
            extra_scrub=ANTHROPIC_CREDENTIAL_VARS | ORCHESTRATOR_ONLY_VARS,
            extra_env=_codex_extra_env(env),
            stdin_payload=prompt,
        )

    def parse_output(self, line: str) -> StreamEvent | None:
        return self._parser.parse_line(line)

    def create_stream_parser(self) -> StreamParser:
        return CodexStreamParser(
            cost_source=_resolve_codex_cost_source(self._config.billing_mode),
            model=self._config.model,
        )

    def stream_reader_drain_timeout(self) -> float:
        return self._config.reader_drain_timeout

    async def check_available(self) -> tuple[bool, str]:
        available, version_detail = await _check_cli_available("codex", "npm install -g @openai/codex")
        if not available:
            return False, version_detail

        probe = await _probe_codex_auth()
        authenticated, auth_detail = _interpret_codex_auth_probe(probe)
        if _resolve_codex_api_key() is not None:
            auth_detail = f"{auth_detail}, CODEX_API_KEY set"

        if authenticated is False:
            return False, f"{version_detail} but {auth_detail}"
        return True, f"{version_detail} ({auth_detail})"

    async def _probe_models(self) -> list[str] | None:
        """Report the one configured Codex model, if there is one.

        The Codex CLI exposes no model-listing endpoint, and
        ``honours_model_override`` is False, so an authenticated Codex
        runtime drives exactly one model: ``codex.model``. Reporting it is
        strictly more informative than the base ``[]`` for a capability view
        whose question is "what can this layer actually run". An empty
        ``codex.model`` means "whatever the Codex CLI's own config defaults
        to", a value SOVA cannot name, so that case keeps the base answer:
        reachable, with no model ID to report.
        """
        model = self._config.model.strip()
        return [model] if model else []
