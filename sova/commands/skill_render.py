"""Render SOVA's canonical commands/*.md into Codex-native SKILL.md packages.

SOVA's ``commands/`` directory is the single source of truth for its
distributable workflow commands; Claude Code reaches them as flat
``.claude/commands/*.md`` files (see ``sova.commands.self_render``). Codex has
no slash-command concept (``CodexAdapter.commands_dir()`` returns ``None``),
but it does discover reusable ``SKILL.md`` packages on disk. This module
mechanically derives one such package per canonical command, so the workflow
text is authored exactly once.

Mirrors the pattern in ``sova.commands.marketplace_export``: a small, reviewed
substitution list rewrites Claude-specific idioms (``$ARGUMENTS``, SOVA
template variables, cross-command ``/foo`` references) into Codex-appropriate
prose, rather than a templating engine or a duplicated copy of the workflow
text. Unlike the marketplace export (which drops commands outside its
curated subset), every canonical command ships as a skill here, so a ``/foo``
cross-reference is rewritten to point at that other skill rather than to
inline prose describing it.
"""

from __future__ import annotations

import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Callable, Union

from sova.commands.catalog import CommandEntry, discover, get_canonical_dir, parse_frontmatter
from sova.commands.distribution import InstallResult, install_skills
from sova.commands.self_render import repo_root, self_config
from sova.commands.templates import build_variables, dedash_prose, split_fenced_lines
from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

# A substitution's replacement, either a plain string (passed straight to
# re.Pattern.sub()) or a callable taking the Match (for a replacement that
# depends on what was actually matched, e.g. preserving an already-consumed
# article's capitalization).
_Replacement = Union[str, Callable[["re.Match[str]"], str]]

log = get_logger(component="commands.skill_render")

# Every SOVA-managed entry under a runtime's shared ``skills/`` style
# directory is written as ``sova-<name>``, so it can never collide with
# pre-existing, independently-maintained content that already occupies a
# plain name there (confirmed on disk: this repo's own ``.agents/skills/``
# holds hand-authored ``testing-patterns``/``database-patterns`` directories
# that predate this renderer).
SKILL_NAME_PREFIX = "sova-"


class SkillRenderError(ValueError):
    """A canonical command contains an idiom _CODEX_SUBSTITUTIONS doesn't cover."""


# Mechanical text substitutions applied to every canonical command body
# before it is packaged as a Codex skill. Reviewed by hand, not generated,
# so a new Claude-specific idiom must be added here deliberately; one that
# isn't is caught by _assert_fully_rendered() below instead of shipping
# unexplained text into a Codex skill.
#
# SOVA template variables (`{{ check_cmd }}` and friends) are deliberately
# NOT substituted here: they are left in the rendered body for
# `install_skills()` -> `render_command()` to fill at install time, exactly
# as it already does for the Claude Code path, so a Codex skill gets the
# project's real configured command (`make check`) rather than a generic
# "see its Makefile" placeholder or an unrunnable `<check-command>` sitting
# inside a ```bash fence. `_assert_fully_rendered()` below treats these as
# known, deferred placeholders rather than leftover idioms.
_CODEX_SUBSTITUTIONS: list[tuple[re.Pattern[str], _Replacement]] = [
    (re.compile(r"\$ARGUMENTS"), "the arguments provided when this skill is invoked"),
    # Non-canonical Claude slash commands with no Codex-skill counterpart:
    # rewritten to neutral prose rather than left as a dangling `/name`
    # reference (which _assert_fully_rendered()'s residual-reference check
    # would otherwise reject).
    (
        re.compile(r"a `/verify-local` command"),
        "a local verification procedure",
    ),
    (
        re.compile(r"the `/verify-local` procedure"),
        "that procedure",
    ),
    (
        re.compile(r"no `/verify-local` command exists"),
        "no such procedure exists",
    ),
    (
        re.compile(r"or `/approve-merge`"),
        "or by merging it directly once the user asks",
    ),
    # Claude-only tool/product idioms with no Codex equivalent. Scoped to the
    # exact phrases known to occur in canonical commands today, rather than a
    # blanket word substitution, since e.g. "Claude Code" also appears in
    # legitimate meta-discussion (commands/agent-readiness.md describing
    # CLAUDE.md itself) that must survive untouched.
    (
        re.compile(r"a TodoWrite list"),
        "a task checklist",
    ),
    (
        re.compile(r'Use `subagent_type="general-purpose"` for all three\.'),
        "Run each as a parallel helper agent.",
    ),
    (
        re.compile(r"the entire Claude Code knowledge system"),
        "the entire project knowledge system",
    ),
]

_CODEX_FENCE_SUBSTITUTIONS: list[tuple[re.Pattern[str], _Replacement]] = [
    (re.compile(r"\$ARGUMENTS"), "<arguments>"),
]


# Template placeholders install_skills() -> render_command() substitutes at
# install time (see build_variables()); left unrendered by _apply_substitutions
# above on purpose, so _assert_fully_rendered() must not treat them as leftover
# Claude-specific idioms. Derived from a default ProjectConfig rather than
# hand-listed, so a new template variable can't silently reopen the "ships
# unrendered" failure mode this whole module exists to prevent.
#
# Computed lazily via ProjectConfig.model_construct() rather than at import
# time via ProjectConfig(): the latter is a pydantic BaseSettings subclass
# that reads SOVA_* environment overrides and validates them, so a malformed
# override (e.g. SOVA_MAX_PARALLEL_AGENTS=abc) would raise at import of this
# module, which sits on the import chain of sova.core.steps.develop. The
# value only depends on build_variables()'s fixed key set, never on an actual
# config value, so skipping validation/env-reading entirely costs nothing.
@lru_cache(maxsize=1)
def _known_deferred_placeholders() -> frozenset[str]:
    return frozenset(build_variables(ProjectConfig.model_construct()))


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# A backticked slash-command reference that survived every substitution pass
# above (including the per-command cross-reference rewriting below): either a
# new canonical command the cross-reference list doesn't know about yet, or a
# Claude-only command (like `/approve-merge`) with no reviewed rewrite. Either
# way it must fail loudly rather than ship a reference Codex has no concept of.
#
# The leading negative lookbehind requires the opening backtick to sit at a
# word boundary (preceded by whitespace, punctuation, or start of text), which
# every genuine reference does ("Run `/develop`", "or `/approve-merge`").
# Without it, prose like "the `MERGEABLE`/not-`BEHIND` case" (commands/
# integrate-pr.md) false-positives: the `/not-` substring there is two
# adjacent code spans joined by a literal "/", not a command reference, and
# its opening backtick is glued directly onto the preceding word.
# Claude Code Agent-tool-specific tokens with no legitimate alternate meaning
# in any canonical command (unlike "Claude Code" itself, which commands/
# agent-readiness.md legitimately discusses as a repo file; that phrase is
# deliberately NOT denylisted here for that reason). A survivor means a new
# canonical command used one of these with no matching _CODEX_SUBSTITUTIONS
# entry, same failure mode as an unrewritten $ARGUMENTS or /command.
_CLAUDE_ONLY_TOOL_TOKEN_RE = re.compile(r"\b(?:TodoWrite|subagent_type|Task tool)\b")

_RESIDUAL_SLASH_COMMAND_RE = re.compile(r"(?<![`\w])`/[a-z][a-z0-9-]*`")

# Same idea as above, for a bare (unbackticked) reference: canonical commands
# sometimes write "Run before /pr" rather than "Run before `/pr`"
# (commands/review.md, commands/review-full.md both do), and that bare form
# is rewritten for every *canonical* name by _cross_reference_substitutions()
# just like the backticked one is. A bare reference that survives to here
# names a command with no Codex counterpart (and no reviewed rewrite in
# _CODEX_SUBSTITUTIONS), so it must fail loudly rather than ship a dangling
# `/name` with no backticks to even signal it's a command reference.
#
# Lookbehind/lookahead mirror _cross_reference_re()'s path-segment and
# longer-command-name exclusions: a path like `build/test/lint` or
# `/tmp/pr-commits.txt` must not match. Every hyphen must be followed by an
# alphanumeric (no trailing or doubled hyphen), so "the `MERGEABLE`/not-
# `BEHIND` case" (commands/integrate-pr.md), which, once its two inline
# code spans are stripped, leaves a bare "/not-", isn't mistaken for a
# command reference: no canonical command name ends in a hyphen.
_RESIDUAL_BARE_SLASH_COMMAND_RE = re.compile(r"(?<![`\w/.\-])/[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?![\w\-/])")

# Stripped out of a prose line before the bare-reference check runs, so a
# non-command path inside an inline code span (`` `gh api .../pulls/<N>/comments` ``
# in commands/integrate-pr.md) is never mistaken for an unrendered command
# reference. The backticked check above intentionally keeps spans intact: it
# only matches a span that is *exactly* one `/command`, so a longer span like
# this one never matches it either way.
_INLINE_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")


# A determiner, plus at most one intervening modifier word, directly
# preceding a backticked cross-reference ("a previous `/integrate-pr` run",
# "its own `/develop` step"). Consumed and re-emitted verbatim by
# _cross_reference_substitutions()'s repl(), rather than normalized, so a
# modifier between the determiner and the reference can't double up into "a
# previous the `sova-x` skill".
#
# Capped at one modifier word (not more): a higher cap lets the alternation's
# "that"/"this" entries (needed as genuine demonstratives, as in "that
# previous `/foo`") also match "that" used as a relative pronoun several
# words upstream of an unrelated verb phrase ("that happens via
# `/integrate-pr`" in commands/pr.md), consuming "happens via " as if it were
# a modifier and leaving the real, needed "the" un-injected. One modifier
# word is enough for every real case while keeping the match tight to the
# words immediately next to the reference.
_DETERMINER_RE_FRAGMENT = r"\b(?:[Tt]he|[Aa]n?|[Tt]his|[Tt]hat|[Ee]ach|[Ee]very|[Ii]ts|[Oo]ur|[Yy]our)\s+(?:[a-z]+\s+)?"


def _cross_reference_re(name: str) -> re.Pattern[str]:
    """Match a ``/name`` cross-reference, and nothing that merely contains that text.

    The lookbehind rejects a path segment (``build/test/lint`` is not a
    reference to ``/test``, ``/tmp/pr-commits.txt`` is not one to ``/pr``);
    the lookahead rejects a longer command name (``/review-pr`` must be
    rewritten by its own pattern, never by ``/review``'s, which would leave
    a mangled "the `sova-review` skill-pr" behind).
    """
    return re.compile(rf"(?<![\w/.\-])/{re.escape(name)}(?![\w\-])")


def _cross_reference_substitutions(skill_names: list[str]) -> list[tuple[re.Pattern[str], _Replacement]]:
    """Two regexes per canonical command name, rewriting a /foo cross-reference to its skill name.

    The backticked form is matched first and consumes the enclosing
    backticks, carrying any trailing argument hint back out with it
    (``` `/develop <description>` ``` -> ``the `sova-develop` skill
    <description>``): the replacement supplies its own backticks, so letting
    the bare pattern handle an already-backticked reference nests them
    (``` `the `sova-develop` skill <description>` ```), which renders as
    broken markdown.

    It also optionally consumes a directly preceding determiner ("the "/"a "/
    "its ", etc., any case) and up to two intervening modifier words ("a
    previous ", "its own "), plus a directly trailing " command" word, so the
    replacement doesn't double up into "a previous the `sova-integrate-pr`
    skill" or leave a dangling "... skill command workflow" behind. A
    callable replacement (rather than a template string) is used so a
    consumed determiner (and any modifiers) is re-emitted verbatim rather
    than normalized ("The `/develop` step" -> "The `sova-develop` skill
    step", "a previous `/integrate-pr` run" -> "a previous `sova-integrate-pr`
    skill run"); "the" is injected only when no determiner was consumed at
    all ("Run `/develop`" -> "Run the `sova-develop` skill").

    ``skill_names`` includes the command's own name: a self-reference
    ("re-run ``/integrate-pr``") names a Claude slash command that does not
    exist under Codex, so it needs rewriting exactly like a reference to any
    other skill.
    """
    substitutions: list[tuple[re.Pattern[str], _Replacement]] = []
    for name in skill_names:
        skill_label = f"{SKILL_NAME_PREFIX}{name}"
        escaped = re.escape(name)

        def repl(match: re.Match[str], skill_label: str = skill_label) -> str:
            article = match.group("article")
            arg = match.group("arg") or ""
            prefix = article if article else "the "
            return f"{prefix}`{skill_label}` skill{arg}"

        backticked = re.compile(
            rf"(?P<article>{_DETERMINER_RE_FRAGMENT})?`/{escaped}(?![\w\-])(?P<arg>[^`\n]*)`(?:\s+command\b)?"
        )
        substitutions.append((backticked, repl))
        substitutions.append((_cross_reference_re(name), f"the `{skill_label}` skill"))
    return substitutions


def _apply_substitutions(
    body: str,
    prose_substitutions: list[tuple[re.Pattern[str], _Replacement]],
    fence_substitutions: list[tuple[re.Pattern[str], _Replacement]],
) -> str:
    """Rewrite each line with the substitution set matching its prose/code context."""
    lines: list[str] = []
    for fenced, line in split_fenced_lines(body):
        for pattern, replacement in fence_substitutions if fenced else prose_substitutions:
            line = pattern.sub(replacement, line)
        lines.append(line)
    return "\n".join(lines)


def _assert_fully_rendered(command_name: str, text: str, *, label: str = "body") -> None:
    """Fail loudly on any idiom the substitution lists above didn't cover, rather than shipping it verbatim.

    Applied to both the rendered body and the rendered frontmatter
    description (``label`` only changes the error message), since the
    description is what Codex actually reads to decide whether to activate
    the skill: an idiom that leaks there is just as much a shipped defect.
    """
    if "$ARGUMENTS" in text:
        raise SkillRenderError(f"commands/{command_name}.md: unrendered $ARGUMENTS reached the Codex skill {label}")
    deferred = _known_deferred_placeholders()
    leftover = sorted({name for name in _PLACEHOLDER_RE.findall(text) if name not in deferred})
    if leftover:
        raise SkillRenderError(
            f"commands/{command_name}.md: unrendered placeholder(s) {leftover} reached the Codex skill {label}"
        )
    tool_tokens = sorted(set(_CLAUDE_ONLY_TOOL_TOKEN_RE.findall(text)))
    if tool_tokens:
        raise SkillRenderError(
            f"commands/{command_name}.md: Claude-only tool token(s) {tool_tokens} reached the Codex skill {label}"
        )
    # Cross-references are rewritten in prose only: a `/foo` inside a fence is a
    # shell command or path and is left verbatim by design, so checking the whole
    # body here would fire on text the renderer deliberately did not touch.
    prose = "\n".join(line for fenced, line in split_fenced_lines(text) if not fenced)
    prose_outside_code_spans = _INLINE_CODE_SPAN_RE.sub("", prose)
    residual = _RESIDUAL_SLASH_COMMAND_RE.findall(prose) + _RESIDUAL_BARE_SLASH_COMMAND_RE.findall(
        prose_outside_code_spans
    )
    if residual:
        raise SkillRenderError(
            f"commands/{command_name}.md: unrendered command reference(s) {residual} reached the Codex skill {label}"
        )


_QUOTED_SCALAR_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def _unwrap_quoted(value: str) -> str:
    """Strip a pre-existing double-quoted YAML scalar wrapper, if present.

    Canonical command frontmatter quotes ``description`` only when it needs
    to (e.g. a colon elsewhere in the text would otherwise end the YAML
    scalar early); ``_parse_yaml_simple`` does not strip that wrapper, so an
    already-quoted value would double-quote when passed through
    ``_yaml_escape`` unless unwrapped first.

    Only unwraps when the whole value is one well-formed quoted scalar (no
    unescaped interior ``"``): a description that merely starts and ends with
    ``"`` because it quotes a word in prose (``"review" mode for "pr"``) is
    not a YAML quoting wrapper, and stripping its outer characters would
    silently change the string the author wrote.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"') and _QUOTED_SCALAR_RE.fullmatch(value):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _yaml_escape(value: str) -> str:
    """Make a frontmatter value safe as a double-quoted YAML scalar."""
    escaped = _unwrap_quoted(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _rewrite_frontmatter_description(content: str, new_description: str) -> str:
    """Replace a single-line ``description:`` frontmatter value, leaving every other field untouched.

    Used so a standalone skill's rendered (substituted) description is what
    actually ships, rather than being computed only to validate against in
    ``_assert_fully_rendered()`` and then discarded, which would leave the
    unrendered original shipping regardless of whether validation passed.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            break
        if re.match(r"^description:\s*\S", lines[i]):
            lines[i] = f"description: {_yaml_escape(new_description)}"
            return "\n".join(lines)
    return content


def render_codex_skill(entry: CommandEntry, skill_names: list[str]) -> str:
    """Mechanically transform one canonical command into a Codex SKILL.md package body."""
    content = entry.path.read_text(encoding="utf-8")
    parsed = parse_frontmatter(content)
    if parsed is None:
        raise SkillRenderError(f"{entry.path} has no valid frontmatter")
    _fields, body = parsed

    cross_references = _cross_reference_substitutions(skill_names)
    prose_substitutions = _CODEX_SUBSTITUTIONS + cross_references
    body = dedash_prose(_apply_substitutions(body, prose_substitutions, _CODEX_FENCE_SUBSTITUTIONS)).strip("\n")
    _assert_fully_rendered(entry.name, body, label="body")

    # The description gets the same substitution pass as the body (not just
    # cross-references): several canonical descriptions name another command
    # ("Run before /pr to catch issues early"), use `{{ var }}` placeholders,
    # or could in principle use $ARGUMENTS, all of which are Claude-specific
    # idioms Codex has no concept of either. It's asserted exactly like the
    # body too: the description is the one field Codex reads to decide
    # whether to activate the skill at all, so a leaked idiom there is just
    # as much a shipped defect.
    description = dedash_prose(_apply_substitutions(entry.description, prose_substitutions, []))
    _assert_fully_rendered(entry.name, description, label="description")

    frontmatter = "\n".join(
        [
            "---",
            f"name: {SKILL_NAME_PREFIX}{entry.name}",
            f"description: {_yaml_escape(description)}",
            "---",
        ]
    )
    return f"{frontmatter}\n\n{body}\n"


def _default_codex_filter(cmd: CommandEntry) -> bool:
    """Fallback restriction used only when a caller doesn't pass ``supports=``.

    Mirrors ``RuntimeAdapter.supports_command()`` against a hardcoded
    "codex" literal, duplicating that one rule rather than importing
    ``CodexAdapter`` here, which would create a ``sova.agents`` <->
    ``sova.commands`` import cycle (``CodexAdapter`` already imports this
    module) if done at module scope. Every caller that produces a checked-in
    or installed artifact (``render_self_skills()`` below,
    ``CodexAdapter.extra_skill_sources()``) passes ``supports=`` explicitly
    instead, via a deferred import where needed, so ``RuntimeAdapter.
    supports_command()`` stays the single authoritative implementation for
    anything that actually ships; this fallback only serves a caller with no
    adapter context at all (a library user calling ``render_codex_skills()``
    directly with no adapter of its own).
    """
    return not cmd.runtimes or "codex" in cmd.runtimes


def render_codex_skills(
    canonical_dir: Path | None = None,
    *,
    supports: Callable[[CommandEntry], bool] | None = None,
) -> dict[str, str]:
    """Render every Codex-eligible canonical command into {name: SKILL.md content}.

    Keyed by the bare command name (``develop``, not ``sova-develop``): the
    ``sova-`` prefix is applied once, at installation time, by
    ``name_prefix`` on ``install_skills()``/``update_skills()``, not here.

    ``supports``, when given, restricts output to the commands it accepts;
    pass an adapter's own ``supports_command`` (e.g.
    ``CodexAdapter.extra_skill_sources()`` does) rather than relying on the
    ``_default_codex_filter()`` fallback, which exists only for a caller with
    no adapter instance to ask.
    """
    canonical = canonical_dir if canonical_dir is not None else get_canonical_dir()
    accept = supports if supports is not None else _default_codex_filter
    entries = [cmd for cmd in discover(canonical) if accept(cmd)]
    all_names = [cmd.name for cmd in entries]

    duplicates = sorted({name for name in all_names if all_names.count(name) > 1})
    if duplicates:
        raise SkillRenderError(f"duplicate canonical command name(s) {duplicates}: each must map to one Codex skill")

    rendered: dict[str, str] = {}
    for entry in entries:
        rendered[entry.name] = render_codex_skill(entry, all_names)

    return rendered


_FRONTMATTER_NAME_LINE_RE = re.compile(r'^(name:\s*)([\'"]?)(.+?)\2\s*$')


def _prefix_skill_frontmatter_name(content: str, skill_dir_name: str, prefix: str) -> str:
    """Rewrite a standalone skill's frontmatter ``name:`` field to carry *prefix*.

    Operates on the raw text rather than through ``parse_frontmatter()`` plus
    reassembly, so every other frontmatter field (``allowed_tools``, etc.)
    and its exact formatting survives untouched; only the ``name:`` line's
    value changes. Without this, the installed directory name carries the
    prefix (``_collect_skills()`` applies it) while the frontmatter inside
    still declares the plain name, so a runtime that keys skills by that
    declared name (not the directory) sees a collision between the prefixed
    package and whatever pre-existing, independently-maintained content
    already used the plain name (exactly what the prefix exists to
    prevent).

    A skill with no frontmatter, or frontmatter with no ``name:`` field, is
    left untouched rather than rejected: not every hand-authored SKILL.md
    (project-local content synced generically through this same function)
    declares a name a runtime could key on in the first place, so there is
    nothing to collide and nothing to rewrite.

    A declared name that disagrees with the directory it lives in is
    rejected outright, rather than silently prefixed to a mismatched value:
    the installed directory name is always derived from ``skill_dir_name``
    (``install_skills()``'s ``name_prefix`` is applied to the directory, not
    to whatever the frontmatter happens to say), so a mismatch here would
    reach the installed tree undetected otherwise, which is the exact
    dir/identity split the prefix exists to prevent.

    The ``name:`` value may be a bare scalar or a single-quoted/double-quoted
    one (``name: "foo"``); the quote style, if any, is preserved around the
    rewritten value.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            break
        match = _FRONTMATTER_NAME_LINE_RE.match(lines[i])
        if match:
            key_part, quote, value = match.group(1), match.group(2), match.group(3)
            expected_prefixed = f"{prefix}{skill_dir_name}" if prefix else skill_dir_name
            if value not in (skill_dir_name, expected_prefixed):
                raise SkillRenderError(
                    f"skills/{skill_dir_name}/SKILL.md declares name {value!r}, which does not match its "
                    f"directory name {skill_dir_name!r}"
                )
            if not prefix or value == expected_prefixed:
                return content
            lines[i] = f"{key_part}{quote}{expected_prefixed}{quote}"
            return "\n".join(lines)
    return content


# Frontmatter keys with no meaning outside Claude Code. Dropped from a
# standalone skill before it reaches a non-Claude target, rather than passed
# through verbatim: Codex has no tool-permission concept of its own, so
# `allowed_tools: Read, Grep, ...` is at best noise and at worst implies a
# restriction Codex doesn't actually enforce.
_CLAUDE_ONLY_FRONTMATTER_KEYS = frozenset({"allowed_tools"})


def _strip_claude_only_frontmatter_keys(content: str) -> str:
    """Drop any ``_CLAUDE_ONLY_FRONTMATTER_KEYS`` line from a SKILL.md's frontmatter block."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content
    out = [lines[0]]
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        key_match = re.match(r"^([a-zA-Z_][\w-]*):", lines[i])
        if key_match is None or key_match.group(1) not in _CLAUDE_ONLY_FRONTMATTER_KEYS:
            out.append(lines[i])
        i += 1
    out.extend(lines[i:])
    return "\n".join(out)


def materialize_combined_skill_sources(
    standalone_skills_dir: Path,
    extra: dict[str, str],
    scratch_dir: Path,
    *,
    name_prefix: str = "",
    existing_target_dir: Path | None = None,
) -> None:
    """Combine hand-authored and mechanically-rendered skill sources into one directory tree.

    ``install_skills()``/``update_skills()`` operate over a single source
    directory, so the two source kinds (hand-authored under
    ``standalone_skills_dir``, mechanically rendered in ``extra``) are
    merged here first: that lets a single call produce one coherent
    manifest, rather than two independent manifests each only seeing half
    the installed skills.

    ``name_prefix`` rewrites each standalone skill's frontmatter ``name:``
    to match the prefix the caller will later apply to the installed
    directory name (via ``install_skills(..., name_prefix=...)``), so the two
    stay consistent. Command-derived entries in ``extra`` are left alone:
    ``render_codex_skill()`` already bakes the matching prefix into their
    frontmatter directly.

    A standalone skill's body (and its ``description`` field, when present)
    goes through the same ``_CODEX_SUBSTITUTIONS`` + cross-reference +
    ``dedash_prose`` + ``_assert_fully_rendered`` pipeline that a
    command-derived skill gets in ``render_codex_skill()``, using
    ``extra``'s keys as the cross-reference name list (every command-derived
    skill's bare name): a hand-authored skill is exactly as likely to use a
    Claude-specific idiom (a ``/command`` reference, a `{{ var }}`
    placeholder) as a canonical command body is, and leaving it unchecked
    would let that idiom reach a non-Claude target unrendered with nothing
    to catch it. A skill with no frontmatter is left as plain prose with no
    pipeline applied, matching ``_prefix_skill_frontmatter_name()``'s own
    "nothing to rewrite" treatment of that case. Claude-only frontmatter
    keys (``allowed_tools``) are dropped via
    ``_strip_claude_only_frontmatter_keys()`` regardless.

    Only ``SKILL.md`` is ever read from a standalone skill directory, so a
    skill with sibling entries (a ``README.md``, a ``references/`` directory)
    must fail loudly here rather than silently shipping a ``SKILL.md`` that
    references content nothing ever copied.

    ``existing_target_dir``, when given, is the directory a prefixed copy of
    this skill would ultimately be installed alongside (e.g. ``.agents/skills/``
    for Codex). A standalone skill whose plain name already exists there as an
    unprefixed, hand-authored directory with its own ``SKILL.md`` is skipped
    entirely rather than also installed under the prefix: this repo's own
    ``.agents/skills/testing-patterns/`` is the SOVA-specific, hand-maintained
    version of what ``skills/testing-patterns/SKILL.md`` renders generically,
    and installing both left two skills declaring overlapping "writing or
    modifying test files" auto-activation triggers in the same discovery
    directory, with the thinner generic one just as likely to win. The
    hand-authored, pre-existing copy at the plain name always wins; this
    mirrors ``skill_name_prefix``'s own purpose (never clobber unmanaged
    content already using a plain name) one level up, at skill-selection time
    rather than file-write time.
    """
    combined = dict(extra)
    skill_names = list(extra)
    cross_references = _cross_reference_substitutions(skill_names)
    prose_substitutions = _CODEX_SUBSTITUTIONS + cross_references

    if standalone_skills_dir.is_dir():
        for skill_dir in sorted(standalone_skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            if (
                name_prefix
                and existing_target_dir is not None
                and (existing_target_dir / skill_dir.name / "SKILL.md").is_file()
            ):
                continue
            other_entries = sorted(p.name for p in skill_dir.iterdir() if p.name != "SKILL.md")
            if other_entries:
                raise SkillRenderError(
                    f"skills/{skill_dir.name}/ has sibling entr(ies) {other_entries} that materialize_combined_"
                    "skill_sources() would silently drop; copy them explicitly or fold their content into SKILL.md"
                )
            if skill_dir.name in combined:
                raise SkillRenderError(
                    f"skill name collision: {skill_dir.name!r} is both a command-derived and a standalone skill"
                )
            content = skill_file.read_text(encoding="utf-8")
            parsed = parse_frontmatter(content)
            if parsed is not None:
                fields, body = parsed
                rendered_body = dedash_prose(
                    _apply_substitutions(body, prose_substitutions, _CODEX_FENCE_SUBSTITUTIONS)
                ).rstrip("\n")
                _assert_fully_rendered(f"skills/{skill_dir.name}", rendered_body, label="body")
                frontmatter_raw = content[: len(content) - len(body)]
                description = fields.get("description")
                if isinstance(description, str):
                    rendered_description = dedash_prose(_apply_substitutions(description, prose_substitutions, []))
                    _assert_fully_rendered(f"skills/{skill_dir.name}", rendered_description, label="description")
                    frontmatter_raw = _rewrite_frontmatter_description(frontmatter_raw, rendered_description)
                content = f"{frontmatter_raw}{rendered_body}\n"
            content = _strip_claude_only_frontmatter_keys(content)
            combined[skill_dir.name] = _prefix_skill_frontmatter_name(content, skill_dir.name, name_prefix)

    scratch_dir.mkdir(parents=True, exist_ok=True)
    for name, content in combined.items():
        dest_dir = scratch_dir / name
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "SKILL.md").write_text(content, encoding="utf-8")


def render_self_skills(root: Path | None = None, target_dir: Path | None = None) -> InstallResult:
    """Render this repo's own canonical commands + standalone skills into .agents/skills/.

    Mirrors ``sova.commands.self_render.render_self()`` for
    ``.claude/commands/``: the rendered tree is a checked-in build artifact,
    regenerated by ``make skills-render`` and guarded by
    ``tests/test_skill_render_drift.py``, so it cannot drift silently.

    The restriction predicate and the default target directory are both
    taken from ``CodexAdapter`` (imported here, deferred, rather than at
    module scope: ``CodexAdapter`` already imports this module, so a
    module-level import the other way would close an import cycle) rather
    than duplicated as a local filter function and a hand-pinned path
    constant. That keeps this checked-in artifact produced by the same
    restriction rule and the same directory a real ``sova install`` would
    use, so the two can't silently diverge the next time either one moves.
    """
    from sova.agents.codex import CodexAdapter

    base = root if root is not None else repo_root()
    commands_dir = base / "commands"
    standalone_dir = base / "skills"
    adapter = CodexAdapter()
    # The real, checked-in skills directory, used only to detect a standalone
    # skill colliding with pre-existing hand-authored content there (see
    # materialize_combined_skill_sources()'s existing_target_dir). Resolved
    # independently of `target` below, which the drift test overrides to an
    # empty scratch directory: that override must not make this repo's own
    # already-committed `testing-patterns/` invisible to the collision check,
    # or a fresh render would disagree with the checked-in tree on whether
    # `sova-testing-patterns` should exist at all.
    real_target = adapter.skills_dir(base)
    target = target_dir if target_dir is not None else real_target
    if target is None:
        raise SkillRenderError("CodexAdapter.skills_dir() returned None; cannot render self skills")

    extra = render_codex_skills(commands_dir, supports=adapter.supports_command)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        materialize_combined_skill_sources(
            standalone_dir, extra, scratch, name_prefix=SKILL_NAME_PREFIX, existing_target_dir=real_target
        )
        result = install_skills(scratch, target, self_config(), name_prefix=SKILL_NAME_PREFIX)

    log.info("skills.self_rendered", target=str(target), installed=result.installed)
    return result


def main() -> None:
    """Entry point for ``make skills-render``."""
    from sova.agents.codex import CodexAdapter

    result = render_self_skills()
    target = CodexAdapter().skills_dir(repo_root())
    print(f"Rendered {result.installed} skills into {target}/")


if __name__ == "__main__":
    main()
