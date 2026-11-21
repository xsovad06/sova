"""Shared conventional-commit line validation.

Used by ``CommitStep`` (commit subject) and ``CreatePRStep`` (PR title) to
validate an LLM-proposed ``type(scope): subject`` line against a project's
configured ``commit.types``/``commit.scopes`` (``sova.config.models.CommitConfig``),
so the type/scope-membership, lowercase-first-letter, and line-length rules
exist in one place rather than being re-implemented in both steps.
"""

from __future__ import annotations

import re

MAX_LINE_LENGTH = 72

# "ci" is scopeless (invariants/commit-format.sh: "ci: is allowed without
# scope, repo-wide infrastructure"); every other type requires one.
_SCOPELESS_TYPE = "ci"
# Public name for callers that describe the scope-optional type (an error hint), without reaching for a private.
SCOPELESS_TYPE = _SCOPELESS_TYPE

_LINE_RE = re.compile(r"^(?P<type>[a-z][a-z0-9]*)(?:\((?P<scope>[^)]+)\))?:\s*(?P<subject>\S.*)$")


def commit_line_problem(
    line: str,
    *,
    types: list[str] | frozenset[str],
    scopes: list[str] | frozenset[str],
    require_scope: bool = True,
) -> str | None:
    """Name what is wrong with *line* as a ``type(scope): subject`` line, or None when it is valid.

    Checks against the configured *types*/*scopes* (not this repo's own
    hardcoded lists), a lowercase-first-letter subject, and a 72-character
    line length. The returned text names the failing part (the length, an
    unknown type, an unknown or missing scope, a capitalized subject) so a
    caller can report it; :func:`is_valid_commit_line` is the boolean form.
    With *require_scope* False a missing scope is accepted on every type, as
    on ``"ci"``; a scope that is present must still be configured. An empty
    *scopes* places no restriction on the scope.
    """
    candidate = line.strip()
    if not candidate:
        return "the line is empty"
    if len(candidate) > MAX_LINE_LENGTH:
        return f"the line is {len(candidate)} characters, over the {MAX_LINE_LENGTH} character limit"

    match = _LINE_RE.match(candidate)
    if not match:
        return "the line is not in 'type(scope): subject' form (lowercase type, then a subject)"

    commit_type = match.group("type")
    scope = match.group("scope")
    subject = match.group("subject")

    if commit_type not in types:
        return f"unknown type '{commit_type}'"

    if scopes:
        if commit_type == _SCOPELESS_TYPE or not require_scope:
            # Scope-optional, not scope-forbidden: invariants/commit-format.sh
            # accepts both "ci: subject" and "ci(scope): subject" (the latter via
            # its general type(scope) pattern, which "ci" also matches), so a
            # scope is only rejected here when present but not configured.
            if scope is not None and scope not in scopes:
                return f"unknown scope '{scope}'"
        elif not scope:
            return "missing scope"
        elif scope not in scopes:
            return f"unknown scope '{scope}'"
    # An empty *scopes* is no restriction: any scope, or none, is accepted.

    # An all-caps opening word (e.g. "LLM provider abstraction") is an
    # acronym lowercase_first_letter() deliberately leaves alone; reject
    # only a subject starting with an isolated capitalized word.
    if subject[0].isupper() and not (len(subject) > 1 and subject[1].isupper()):
        return "the subject starts with a capitalized word (use lowercase)"
    return None


def is_valid_commit_line(line: str, *, types: list[str] | frozenset[str], scopes: list[str] | frozenset[str]) -> bool:
    """Return True if *line* is a valid ``type(scope): subject`` line (see :func:`commit_line_problem`)."""
    return commit_line_problem(line, types=types, scopes=scopes) is None


# A Jira PR title carries its ticket key in front of the conventional line ("[PROJ-123] type(scope): subject").
# The key charset matches the project keys setup_service accepts ("MY_PROJ-12" is valid).
_JIRA_PREFIX_RE = re.compile(r"^\[[A-Z][A-Z0-9_]*-\d+\]\s+")


def build_merge_subject(
    title: str,
    pr_number: int,
    *,
    types: list[str] | frozenset[str],
    scopes: list[str] | frozenset[str],
    conventional: bool = True,
    jira: bool = False,
) -> str:
    """Validate a PR *title* and return the squash-merge subject ``"<title> (#<pr_number>)"``.

    The rules are :func:`commit_line_problem`'s, applied to the title alone: the 72-character limit
    does not count the appended suffix (it does count a Jira ticket key, as ``create_pr`` does), and a
    ``(#<pr_number>)`` already ending the title is not appended twice. With *jira* True (a Jira
    project) a leading ticket key is kept in the subject but not validated as a type, and the line after it
    may omit the scope (``create_pr`` builds ``"[KEY-N] type: subject"``); for any other project a bracketed
    prefix is not a ticket key and fails the convention like any malformed title. With
    *conventional* False (``commit.pr_title_format = "freeform"``) the title is not checked against
    the convention. Raises ``ValueError`` naming the failing part when the title is not valid or spans
    several lines.
    """
    cleaned = title.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("the title spans several lines")
    suffix = f" (#{pr_number})"
    # Tolerate a hand-typed suffix ("(#42)" with no space, or extra spaces) so it is neither counted toward the
    # limit nor appended a second time.
    cleaned = re.sub(rf"\s*\(#{pr_number}\)$", "", cleaned)
    if not cleaned:
        raise ValueError("the title is empty")
    if not conventional:
        return f"{cleaned}{suffix}"
    key = _JIRA_PREFIX_RE.match(cleaned) if jira else None
    line = cleaned[key.end() :] if key else cleaned
    if key and len(cleaned) > MAX_LINE_LENGTH:
        # create_pr budgets a Jira title with its ticket key, so the limit counts the key too.
        raise ValueError(f"the line is {len(cleaned)} characters, over the {MAX_LINE_LENGTH} character limit")
    problem = commit_line_problem(line, types=types, scopes=scopes, require_scope=not key)
    if problem:
        raise ValueError(problem)
    return f"{cleaned}{suffix}"


def resolve_default_scope(scopes: list[str] | frozenset[str] | tuple[str, ...], *, preferred: str = "core") -> str:
    """Pick a safe fallback scope from *scopes* for a deterministic builder.

    Prefers *preferred* ("core") when it is itself an accepted scope, since
    that is SOVA's own convention and the common case. Falls back to the
    first configured scope otherwise, so a project whose list omits "core"
    entirely (e.g. "accounts"/"billing") still gets a scope its own
    commit-format check accepts, rather than always "core", which it may
    well reject: literally emitting *preferred* regardless of *scopes* is
    the exact drift this configuration exists to stop. An empty *scopes*
    (a project with no scope convention) returns "", which
    :func:`format_type_scope` renders as no scope at all.
    """
    if not scopes:
        return ""
    if preferred in scopes:
        return preferred
    return next(iter(scopes))


def resolve_default_type(types: list[str] | frozenset[str] | tuple[str, ...], *, preferred: str = "feat") -> str:
    """Pick a safe fallback commit type from *types* for a deterministic builder.

    Mirrors :func:`resolve_default_scope`: prefers *preferred* ("feat") when it
    is itself a configured type, since that is SOVA's own convention and the
    common case. Falls back to the first configured *non-scopeless* type
    otherwise (skipping "ci"), so a project whose list omits *preferred*
    entirely (e.g. ``["feature", "bugfix"]``) still gets a type its own
    commit-format check accepts, rather than a hardcoded "feat"/"fix" it may
    well reject. Scopeless types are skipped because both callers of this
    function unconditionally append a ``(scope)`` segment, which
    ``is_valid_commit_line()`` rejects for "ci": picking it here would hand
    both builders a type they cannot legally use. An empty *types* (a
    misconfigured project) returns *preferred* as a last resort, and a
    *types* list containing only scopeless types returns the first one
    anyway, since there is no better option left.
    """
    if not types:
        return preferred
    if preferred in types:
        return preferred
    for candidate in types:
        if candidate != _SCOPELESS_TYPE:
            return candidate
    return next(iter(types))


def format_type_scope(commit_type: str, scope: str) -> str:
    """Return ``"type(scope)"``, or bare ``"type"`` when *commit_type* is scopeless or *scope* is empty.

    "ci" is the one configured type :func:`is_valid_commit_line` requires to
    carry no scope at all; every other caller building a ``type(scope):
    subject`` line delegates the scope-or-not decision here instead of
    appending ``(scope)`` unconditionally, which would hand "ci" a scope its
    own convention rejects.
    """
    if commit_type == _SCOPELESS_TYPE or not scope:
        return commit_type
    return f"{commit_type}({scope})"


def match_type_prefix(subject: str, types: list[str] | frozenset[str] | tuple[str, ...]) -> re.Match[str] | None:
    """Match a configured ``type:``/``type(scope):`` prefix at the start of *subject*.

    Built from the configured *types* at call time (longest type names
    first, so one type that is a prefix of another can never shadow it)
    rather than a hardcoded alternation, so a project whose convention omits
    or adds types (e.g. ``ci``, or a custom ``feature``/``bugfix`` pair)
    still has its own prefix recognized instead of getting a second prefix
    stapled on top of its own.
    """
    if not types:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(types, key=len, reverse=True))
    pattern = re.compile(rf"^(?P<type>{alternation})(?:\([^)]*\))?:\s*")
    return pattern.match(subject)


def strip_type_prefix(subject: str, types: list[str] | frozenset[str] | tuple[str, ...]) -> str:
    """Remove one leading configured ``type:``/``type(scope):`` prefix from *subject*."""
    match = match_type_prefix(subject, types)
    return subject[match.end() :] if match else subject


def _path_segments(path: str) -> list[str]:
    return [p for p in path.strip().split("/") if p]


def _match_scope_segment(path: str, scopes: list[str] | frozenset[str] | tuple[str, ...]) -> str | None:
    """Return the configured scope *path* belongs to, or None when it names none.

    Checks every directory segment before the filename (so
    ``apps/accounts/models.py`` matches "accounts", not some coincidental
    match on "models"), then, only for a path with a single directory segment
    (e.g. ``app/billing.py``), falls back to the filename itself, with and
    without its extension, so a single-file change still matches a configured
    "billing" scope even though "billing" never appears as a directory. A
    deeper path (``sova/utils/db.py``) never reaches this fallback: its
    filename is incidental at that depth, and matching it against scope "db"
    (which names ``sova/db/``) would mis-scope the change.
    """
    parts = _path_segments(path)
    if not parts:
        return None
    for segment in parts[:-1]:
        if segment in scopes:
            return segment
    if len(parts) > 2:
        return None
    last = parts[-1]
    stem = last.rsplit(".", 1)[0]
    if stem in scopes:
        return stem
    if last in scopes:
        return last
    return None


def infer_scope_from_paths(
    changed_files: list[str] | None,
    scopes: list[str] | frozenset[str] | tuple[str, ...],
    *,
    preferred: str = "core",
) -> str:
    """Infer a conventional-commit scope from *changed_files* against *scopes*.

    Returns the most-touched configured scope among the changed paths (see
    :func:`_match_scope_segment`), falling back to
    :func:`resolve_default_scope` for an empty, mixed, or entirely unmatched
    change: a single path that names no configured scope makes the whole
    change "mixed" and falls back too, even when every other path is validly
    scoped, so a PR that is mostly one scope but touches one README is never
    mislabeled as that scope. Ties resolve to the first scope seen, which
    keeps the result deterministic. Shared by ``CommitStep`` (commit
    subject) and ``CreatePRStep`` (PR title) so both derive a path's scope
    the same way, project-agnostically: no path needs a particular
    top-level directory name to be recognized.
    """
    default_scope = resolve_default_scope(scopes, preferred=preferred)
    if not changed_files:
        return default_scope

    counts: dict[str, int] = {}
    order: list[str] = []
    for path in changed_files:
        match = _match_scope_segment(path, scopes)
        if match is None:
            return default_scope
        if match not in counts:
            order.append(match)
        counts[match] = counts.get(match, 0) + 1

    if not counts:
        return default_scope
    return max(order, key=lambda s: (counts[s], -order.index(s)))


# Project-agnostic top-level directory names recognized as docs/test-only,
# since a project's documentation or tests need not live under SOVA's own
# "docs"/"tests" layout.
_DOCS_DIR_NAMES = frozenset({"docs", "documentation", "doc"})
_TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "specs", "__tests__"})


def _is_docs_only_path(path: str) -> bool:
    parts = _path_segments(path)
    return bool(parts) and parts[0] in _DOCS_DIR_NAMES


def _is_test_only_path(path: str) -> bool:
    """Return True for a path a non-Python project would still recognize as test-only.

    Covers a top-level test directory under any common name, a nested
    ``__tests__`` segment (common in JS/TS projects), and the filename
    suffixes a Python (``test_*``/``*_test.py``), Go (``*_test.go``), or
    JS/TS (``*.test.ts``, ``*.spec.ts``) project each use for a test file.
    """
    parts = _path_segments(path)
    if not parts:
        return False
    if parts[0] in _TEST_DIR_NAMES or "__tests__" in parts:
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
    )


def infer_type_from_paths(
    changed_files: list[str] | None,
    types: list[str] | frozenset[str] | tuple[str, ...],
    *,
    preferred: str = "feat",
) -> str:
    """Infer "docs" or "test" from *changed_files* when every path confidently
    belongs to one of those types, before falling back to
    :func:`resolve_default_type`.

    A change confined to ``docs/`` is "docs"; one confined to ``tests/`` (or
    whose files are all named ``test_*``/``*_test.py``) is "test". Either
    only applies when the project actually configures that type, so a
    project whose ``commit.types`` omits "docs"/"test" is unaffected. Shared
    by ``CommitStep`` and ``CreatePRStep`` so both derive a path's type the
    same way.
    """
    if changed_files:
        if "docs" in types and all(_is_docs_only_path(p) for p in changed_files):
            return "docs"
        if "test" in types and all(_is_test_only_path(p) for p in changed_files):
            return "test"
    return resolve_default_type(types, preferred=preferred)


def describe_line_form(scopes: list[str] | frozenset[str] | tuple[str, ...]) -> str:
    """The line form for an LLM prompt: scope required (bar "ci") when *scopes* is set, optional when empty."""
    if scopes:
        return '"type(scope): subject" (or "type: subject" only when type is "ci")'
    return '"type: subject" or "type(scope): subject"'


def describe_scope_choice(scopes: list[str] | frozenset[str] | tuple[str, ...]) -> str:
    """The scope rule for an LLM prompt: the configured list, or a free choice when the project has none."""
    if scopes:
        return f"one of: {', '.join(scopes)}"
    return "a short lowercase name for the area the paths belong to, or omitted when none fits"


def lowercase_first_letter(text: str) -> str:
    """Lowercase only the first character of *text*, leaving the rest untouched.

    An all-caps opening word is left alone: "LLM provider abstraction" is an
    acronym, and lowercasing just its first character would yield the
    nonsense "lLM provider abstraction" rather than a lowercase subject.
    """
    if not text:
        return text
    if len(text) > 1 and text[0].isupper() and text[1].isupper():
        return text
    return text[0].lower() + text[1:]


def truncate_commit_line(line: str, limit: int = MAX_LINE_LENGTH) -> str:
    """Trim a ``type(scope): subject`` line to *limit* characters.

    Cuts the subject (never the ``type(scope):`` prefix, which the project's
    commit-format invariant parses) at a word boundary where one fits, so a
    verbatim issue title far over the limit still produces a valid line
    rather than a 118-character subject. A line already within *limit* is
    returned unchanged.
    """
    if len(line) <= limit:
        return line

    head, sep, subject = line.partition(": ")
    room = limit - len(head) - len(sep)
    if not sep or room < 1:
        # No parseable prefix, or no room for a subject: a hard cut is all
        # that is left, and a caller's own format check will reject it if it
        # is no longer valid, rather than an over-long line reaching git.
        return line[:limit].rstrip()

    clipped = subject[:room].rstrip()
    # Only trim back to a word boundary when the cut actually lands mid-word
    # (subject[room] continues the word clipped was cut from): a cut that
    # already lands exactly on a word boundary (subject[room] is whitespace,
    # or room reaches the end of subject) must keep that whole last word,
    # not drop it for no reason.
    cuts_mid_word = room < len(subject) and not subject[room].isspace()
    if cuts_mid_word and " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return f"{head}{sep}{clipped or subject[:room]}"
