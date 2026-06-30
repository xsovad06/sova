"""Egress filter -- scans outgoing text for sensitive data before external posts.

Validates all text destined for external systems (GitHub, Jira) to prevent
accidental credential leakage in LLM-generated content. Pure regex scanning,
no LLM calls. Configurable via ``[egress]`` in sova.toml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from sova.utils.logging import get_logger

log = get_logger(component="llm.egress")

EgressMode = Literal["off", "warn", "block"]


@dataclass(frozen=True)
class EgressScanResult:
    """Result of scanning outgoing text for sensitive data."""

    clean: bool
    flags: list[str] = field(default_factory=list)
    redacted_text: str = ""


# ---------------------------------------------------------------------------
# Sensitive data patterns
# ---------------------------------------------------------------------------
# Each tuple: (compiled regex, category name, replacement function or string)

_PatternEntry = tuple[re.Pattern[str], str, str]

_SENSITIVE_PATTERNS: list[_PatternEntry] = [
    # AWS access keys (AKIA...)
    (
        re.compile(r"(?:^|[^A-Z0-9])(AKIA[0-9A-Z]{16})(?:[^A-Z0-9]|$)"),
        "aws_access_key",
        "[REDACTED:aws_key]",
    ),
    # AWS secret keys (40 char base64-ish after a separator)
    (
        re.compile(r"(?i)(?:aws_secret_access_key|aws_secret)\s*[=:]\s*([A-Za-z0-9/+=]{40})"),
        "aws_secret_key",
        "[REDACTED:aws_secret]",
    ),
    # Generic API keys / secrets with explicit label
    (
        re.compile(
            r"(?i)(?:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?key"
            r"|private[_-]?key|auth[_-]?token|authorization|bearer)"
            r"\s*[=:]\s*['\"]?([A-Za-z0-9_\-/.+=]{8,})['\"]?"
        ),
        "api_key",
        "[REDACTED:api_key]",
    ),
    # OpenAI / Anthropic sk- prefixed keys
    (
        re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b"),
        "sk_api_key",
        "[REDACTED:sk_key]",
    ),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    (
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{36,})\b"),
        "github_token",
        "[REDACTED:github_token]",
    ),
    # Slack tokens (xoxb-, xoxp-, xoxo-, xoxa-, xapp-)
    (
        re.compile(r"\b(xox[bpoa]-[A-Za-z0-9\-]+)\b"),
        "slack_token",
        "[REDACTED:slack_token]",
    ),
    (
        re.compile(r"\b(xapp-[A-Za-z0-9\-]+)\b"),
        "slack_app_token",
        "[REDACTED:slack_token]",
    ),
    # Generic "token" / "secret" with separator -- requires = or : followed by a value
    # Avoids matching "tokenizer", "token count", "secret sauce", etc.
    (
        re.compile(r"(?i)\btoken\s*[=:]\s*['\"]?([A-Za-z0-9_\-/.+=]{8,})['\"]?"),
        "generic_token",
        "[REDACTED:token]",
    ),
    (
        re.compile(r"(?i)\bsecret\s*[=:]\s*['\"]?([A-Za-z0-9_\-/.+=]{8,})['\"]?"),
        "generic_secret",
        "[REDACTED:secret]",
    ),
    # Password in key=value pairs
    (
        re.compile(r"(?i)(?:password|passwd|pwd)\s*[=:]\s*['\"]?(\S{4,})['\"]?"),
        "password",
        "[REDACTED:password]",
    ),
    # Connection string credentials (postgresql://user:pass@host)
    # Replacement string unused -- _redact_connection_string callback handles this category
    (
        re.compile(r"((?:postgresql|mysql|mongodb|redis|amqp|mssql)://)[^\s:]+:([^\s@]+)@"),
        "connection_string",
        "",
    ),
    # Private key blocks
    (
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "private_key",
        "[REDACTED:private_key]",
    ),
    # JWT tokens (three base64url segments separated by dots)
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-+/=]{10,}\b"),
        "jwt",
        "[REDACTED:jwt]",
    ),
]


def _redact_connection_string(match: re.Match[str]) -> str:
    """Redact credentials from a database connection string, preserving scheme."""
    scheme = match.group(1)
    return f"{scheme}[REDACTED]@"


def scan_and_redact(text: str) -> EgressScanResult:
    """Scan text for sensitive patterns and return a redacted version.

    Returns an EgressScanResult with:
    - clean: True if no sensitive patterns were found
    - flags: list of detected pattern categories
    - redacted_text: text with sensitive values replaced by [REDACTED:...] markers
    """
    if not text:
        return EgressScanResult(clean=True, redacted_text=text)

    flags: list[str] = []
    redacted = text

    for pattern, category, replacement in _SENSITIVE_PATTERNS:
        if category == "connection_string":
            new_text = pattern.sub(_redact_connection_string, redacted)
        else:
            new_text = pattern.sub(replacement, redacted)
        if new_text != redacted:
            if category not in flags:
                flags.append(category)
            redacted = new_text

    return EgressScanResult(
        clean=len(flags) == 0,
        flags=flags,
        redacted_text=redacted,
    )


def filter_egress(text: str, *, mode: EgressMode = "warn", destination: str = "") -> str | None:
    """Filter outgoing text based on the configured egress mode.

    Args:
        text: The text to filter.
        mode: "off" (passthrough), "warn" (redact and return), "block" (return None).
        destination: Label for log messages (e.g. "post_comment", "create_pr").

    Returns:
        The (possibly redacted) text, or None if mode is "block" and sensitive data was found.
    """
    if mode == "off" or not text:
        return text

    result = scan_and_redact(text)

    if result.clean:
        return text

    log.warning(
        "egress.sensitive_data_detected",
        mode=mode,
        destination=destination,
        flags=result.flags,
        flag_count=len(result.flags),
    )

    if mode == "block":
        return None

    # mode == "warn": return redacted text
    return result.redacted_text
