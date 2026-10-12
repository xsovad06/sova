"""Keep docs/codex-runtime-setup.md in sync with the implementation it documents.

The guide names runtime identifiers, sandbox literal values, install/auth
commands, and config keys that live in code. A rename or value change on
either side (CLI flag, config field, env var) should fail this test rather
than silently leaving the guide stale.
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path

from sova.agents.registry import ADAPTERS
from sova.config.models import CodexConfig
from sova.ipc import runtime as runtime_module
from sova.utils.env import CODEX_CREDENTIAL_VARS, OPENAI_CREDENTIAL_VARS

DOC_PATH = Path(__file__).parent.parent / "docs" / "codex-runtime-setup.md"


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_runtime_names_match_registry() -> None:
    text = _doc_text()
    # Both registries (sova.ipc.runtime's spawn-side factory and
    # sova.agents.registry's artifact-install-side factory) must agree on
    # the identifier the doc tells users to put in agent.runtime.
    assert "codex" in runtime_module._RUNTIMES
    assert "codex" in ADAPTERS
    assert "claude-code" in runtime_module._RUNTIMES
    assert '"codex"' in text
    assert '"claude-code"' in text


def test_sandbox_literal_values_match_config() -> None:
    text = _doc_text()
    allowed = typing.get_args(CodexConfig.model_fields["sandbox"].annotation)
    assert set(allowed) == {"read-only", "workspace-write"}
    for value in allowed:
        assert value in text
    # The doc's own point is that this value is deliberately excluded.
    assert "danger-full-access" in text


def test_default_sandbox_matches_doc() -> None:
    assert CodexConfig().sandbox == "workspace-write"
    assert 'sandbox = "workspace-write"' in _doc_text()


def test_codex_config_fields_match_doc() -> None:
    # Match the fully-qualified config key, not the bare field name: a bare
    # "model" is a substring of unrelated prose ("agent.model",
    # "model-selection"), so the assertion would pass even with the Codex
    # field undocumented.
    text = _doc_text()
    for field_name in CodexConfig.model_fields:
        key = f"codex.{field_name}"
        assert key in text, f"CodexConfig field {field_name!r} is not documented as {key!r}"


def test_env_prefix_matches_doc() -> None:
    env_prefix = CodexConfig.model_config.get("env_prefix")
    assert env_prefix == "SOVA_CODEX_"
    assert env_prefix in _doc_text()


def test_credential_vars_documented() -> None:
    text = _doc_text()
    for var in CODEX_CREDENTIAL_VARS | OPENAI_CREDENTIAL_VARS:
        assert var in text


def test_install_command_matches_check_available() -> None:
    source = inspect.getsource(runtime_module.CodexRuntime.check_available)
    assert "npm install -g @openai/codex" in source
    assert "npm install -g @openai/codex" in _doc_text()


def test_auth_probe_commands_documented() -> None:
    source = inspect.getsource(runtime_module._probe_codex_auth)
    assert '"login", "status"' in source or "'login', 'status'" in source
    assert "codex login status" in _doc_text()


def test_doctor_verify_command_documented() -> None:
    from sova.cli.app import app

    registered = {command.name for command in app.registered_commands}
    assert "doctor" in registered
    assert "sova doctor" in _doc_text()


def test_exec_flags_documented() -> None:
    """The drift section names the hardcoded `codex exec` flags; keep that list honest."""
    args = runtime_module._codex_exec_args("PROMPT", model="some-model", sandbox="read-only")
    flags = [arg for arg in args if arg.startswith("--") and arg != "--"]
    assert flags, "argv builder emitted no flags; the doc's drift section is now wrong"
    text = _doc_text()
    for flag in flags:
        assert f"`{flag}`" in text, f"{flag} is passed by _codex_exec_args() but not documented"


def test_named_helpers_exist() -> None:
    """Every implementation symbol the guide points a reader at must still exist."""
    for name in (
        "_codex_exec_args",
        "_probe_codex_auth",
        "_probe_codex_stdin_prompt_support",
        "CodexStdinProbeAmbiguousError",
        "CodexRuntime",
    ):
        assert hasattr(runtime_module, name), name
        assert name in _doc_text(), f"{name} is referenced by the guide's own prose checks"
