#!/usr/bin/env bash
# Invariant: Detect commits that silently undo other work ("silent revert").
#
# A silent revert is a commit that drops code another PR already landed, usually
# because a rebase, squash, or conflict resolution was done from a stale tree.
# Nothing conflicts and CI stays green (the reverted PR's tests vanish with its
# code), so only a content check can see it. Two independent checks run on every
# commit in the range:
#
# 1. Out-of-scope deletion. A commit whose conventional-commit scope names a
#    narrow area (db, ipc, adapters, ...) must not delete SILENT_REVERT_THRESHOLD
#    or more lines from a file outside that area. The wide scopes (core,
#    dashboard) are exempt from that line count: in this repo they legitimately
#    touch most of the tree, and the commits that reverted PRs #1151 and #1152
#    were both feat(core). Deleting a protected file outright (an invariant, a
#    hook, a workflow, a rules or docs page) is flagged for every scope except
#    the one that owns it. A subject with no recognised scope has nothing in
#    scope.
#
# 2. Undone recent work. Scope independent. If a commit removes most of what a
#    recent base-branch commit added to a file (one that landed within
#    SILENT_REVERT_RECENT_HOURS of the commit being checked), it is reverting
#    that commit, whatever the commit calls itself. The report names the commit
#    (and so the PR) that was undone.
#
# Usage: silent-revert-guard.sh <worktree-dir> [base-branch]
#
# The commit range checked is `origin/<base-branch>..HEAD`: exactly the commits
# unique to this branch. (An `@{u}..HEAD` range would also sweep in the base
# branch's own commits after a rebase and force-push, since the old remote tip
# is no longer an ancestor.) Set SILENT_REVERT_RANGE to an explicit `<a>..<b>`
# range to check that instead, for example the squash commit that just landed
# on the base branch. An explicit range that git cannot resolve is a hard
# failure, not a silent pass.
#
# Escape hatch: add "silent-revert-ok: <reason>" on its own line in the commit
# message body to exempt that commit. The reason must be a real justification
# (SILENT_REVERT_MIN_REASON characters or more): a bare acknowledgment of the
# deletion does not exempt anything.
set -euo pipefail

# Byte-wise collation keeps sort, comm and grep consistent whatever the caller's locale.
export LC_ALL=C

usage() {
  echo "Usage: $0 <worktree-dir> [base-branch]"
  echo "Flags commits that silently revert other work: large deletions outside the commit's"
  echo "declared scope, and commits that remove most of what a recent base-branch commit added."
  echo "Env: SILENT_REVERT_RANGE=<a>..<b>      check an explicit range instead of origin/<base>..HEAD."
  echo "     SILENT_REVERT_THRESHOLD=<n>       out-of-scope deleted lines per file (default 50)."
  echo "     SILENT_REVERT_PROSE_THRESHOLD=<n> same, for docs/*.md files (default 400)."
  echo "     SILENT_REVERT_RECENT=<n>          recent base commits per file to compare against (default 12)."
  echo "     SILENT_REVERT_RECENT_HOURS=<n>    only base commits at most this old when the commit was made (default 72)."
  echo "     SILENT_REVERT_MIN_LINES=<n>       minimum undone lines to report (default 15)."
  echo "     SILENT_REVERT_MIN_PERCENT=<n>     minimum share of a commit's additions undone (default 50)."
  echo "     SILENT_REVERT_MIN_REASON=<n>      minimum length of a silent-revert-ok reason (default 12)."
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

WORKTREE_DIR="${1:-.}"
BASE_BRANCH="${2:-main}"

DELETION_THRESHOLD="${SILENT_REVERT_THRESHOLD:-50}"
PROSE_DELETION_THRESHOLD="${SILENT_REVERT_PROSE_THRESHOLD:-400}"
RECENT_COMMITS="${SILENT_REVERT_RECENT:-12}"
RECENT_HOURS="${SILENT_REVERT_RECENT_HOURS:-72}"
MIN_UNDONE_LINES="${SILENT_REVERT_MIN_LINES:-15}"
MIN_UNDONE_PERCENT="${SILENT_REVERT_MIN_PERCENT:-50}"
MIN_REASON_LENGTH="${SILENT_REVERT_MIN_REASON:-12}"

# Lines shorter than this carry no identity (braces, `else:`, `pass`, `)`), so
# they are ignored when comparing what a commit added with what a later one removed.
MIN_LINE_LENGTH=8

# ---------------------------------------------------------------------------
# Scope-to-path mapping
# ---------------------------------------------------------------------------
# Prints the space-separated path prefixes a scope may delete from, or `*` when
# the scope is too broad to judge by path. Scopes mirror commit-format.sh.
# Measured over the last 600 commits: core (199 commits) and dashboard (130)
# each delete lines across tests/, sova/dashboard, sova/supervisor, sova/roles
# and more, so a path map for them would either flag routine commits or be so
# wide it constrains nothing.

scope_paths() {
  case "$1" in
    core | dashboard) echo "*" ;;
    adapters) echo "sova/adapters/" ;;
    awareness) echo "sova/awareness/" ;;
    cli) echo "sova/cli/ commands/ .github/" ;;
    commands) echo "commands/ .claude/commands/ .agents/ plugins/ skills/ sova/commands/ .claude-plugin/" ;;
    config) echo "sova/config/ sova/dashboard/ sova/llm/ sova.toml sova.toml.default" ;;
    db) echo "sova/db/" ;;
    docs) echo "docs/" ;;
    invariants) echo "invariants/ .githooks/" ;;
    ipc) echo "sova/ipc/ sova/dashboard/" ;;
    knowledge) echo ".claude/ knowledge/ docs/ personas/" ;;
    mcp) echo "sova/mcp/ sova/dashboard/" ;;
    monitoring) echo "sova/monitoring/" ;;
    oversight) echo "sova/oversight/ sova/dashboard/" ;;
    personas) echo "personas/" ;;
    roles) echo "sova/roles/ sova/dashboard/" ;;
    scheduler) echo "sova/scheduler/" ;;
    supervisor) echo "sova/supervisor/ sova/dashboard/" ;;
    *) echo "" ;;
  esac
}

# Paths any commit may touch without being out of scope: tests ride along with
# every code change. A revert hiding in tests is the job of the second check.
COMPANION_PATHS="tests/"

# The ci type is the one scopeless type (repo-wide infrastructure).
CI_PATHS=".github/ .githooks/ invariants/ Makefile pyproject.toml deploy/"

# Files exempt from scope checking (generated/compiled artifacts).
EXEMPT_PATTERNS=(
  "sova/dashboard/static/tailwind.min.css"
  "package-lock.json"
  "poetry.lock"
  "uv.lock"
  "requirements*.txt"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

git_in_worktree() {
  git -C "$WORKTREE_DIR" "$@"
}

is_exempt_file() {
  local file="$1" pattern
  for pattern in "${EXEMPT_PATTERNS[@]}"; do
    # shellcheck disable=SC2254
    case "$file" in
      $pattern) return 0 ;;
    esac
  done
  return 1
}

is_in_scope() {
  local file="$1" allowed_paths="$2" path_prefix
  for path_prefix in $allowed_paths; do
    case "$file" in
      "$path_prefix"*) return 0 ;;
    esac
  done
  return 1
}

# Prose gets a larger line budget: legitimate documentation rewrites routinely
# exceed the code threshold. The match is case sensitive on purpose.
is_prose_file() {
  case "$1" in
    docs/*.md) return 0 ;;
  esac
  return 1
}

# Files whose whole-file deletion is flagged at any size. A small invariant or
# workflow file sits under the line threshold, so deleting it outright would
# otherwise be invisible.
is_protected_file() {
  case "$1" in
    docs/*.md | invariants/* | .githooks/* | .github/workflows/* | .github/scripts/* | .claude/rules/*.md) return 0 ;;
  esac
  return 1
}

normalize_rename_path() {
  local file="$1"
  # Git numstat renders renames in two forms:
  # 1. Brace-abbreviated: docs/{old.md => new.md} (shared prefix/suffix)
  # 2. Plain arrow: NOTES.md => docs/guide.md (no common path component)
  # The `t` branch skips the second substitution when braces matched.
  # The caller must verify that the row is actually a rename (via
  # --diff-filter=R) before using this result; the plain-arrow fallback
  # is a textual heuristic that would corrupt a literal " => " filename.
  echo "$file" | sed -E -e 's#\{[^}]* => ([^}]*)\}#\1#' -e t -e 's#^.* => (.*)$#\1#'
}

# Scopes come only from the conventional-commit prefix, so a trailing "(#1153)"
# or "(v2)" in the description is never read as one.
parse_scopes() {
  local subject="$1" match
  if [[ "$subject" =~ ^[a-z]+\(([a-z0-9,]+)\): ]]; then
    match="${BASH_REMATCH[1]}"
    echo "${match//,/ }"
  fi
}

commit_type() {
  local subject="$1"
  if [[ "$subject" =~ ^([a-z]+)[\(:] ]]; then
    echo "${BASH_REMATCH[1]}"
  fi
}

# The reason given on a "silent-revert-ok:" line, or nothing if there is none.
exemption_reason() {
  local body="$1"
  # awk, not sed: BSD sed (macOS) lacks GNU's `T` command. No early exit, so the
  # writer never takes a SIGPIPE under pipefail.
  printf '%s\n' "$body" | awk '
    !found && tolower($0) ~ /^[[:space:]]*silent-revert-ok:/ {
      sub(/^[[:space:]]*[Ss][Ii][Ll][Ee][Nn][Tt]-[Rr][Ee][Vv][Ee][Rr][Tt]-[Oo][Kk]:[[:space:]]*/, "")
      sub(/[[:space:]]+$/, "")
      print
      found = 1
    }'
}

# ---------------------------------------------------------------------------
# Check 2 helpers: undone recent work
# ---------------------------------------------------------------------------

# Distinct, whitespace-trimmed, non-trivial lines read from stdin, sorted.
normalize_lines() {
  sed -E -e 's/^[[:space:]]+//' -e 's/[[:space:]]+$//' \
    | awk -v min="$MIN_LINE_LENGTH" 'length($0) >= min' \
    | sort -u
}

# The normalized lines of a file at a revision (nothing if the file is absent there).
lines_at() {
  git_in_worktree cat-file -p "${1}:${2}" 2>/dev/null | normalize_lines || true
}

# The normalized lines a commit added, limited to the given paths if any.
lines_added_by() {
  git_in_worktree show --format= -U0 "$1" -- "${@:2}" 2>/dev/null \
    | sed -n -e '/^+++/d' -e 's/^+//p' \
    | normalize_lines || true
}

count_lines() {
  if [[ -z "$1" ]]; then
    echo 0
  else
    printf '%s\n' "$1" | wc -l | tr -d ' '
  fi
}

# Reports each recent base-branch commit whose additions to $file the given
# commit mostly removes. Prints one violation line per undone commit. The fourth
# argument is every line the commit added anywhere: a line removed from this file
# but added to another in the same commit was moved, not reverted.
undone_recent_work() {
  local commit="$1" file="$2" short_hash="$3" relocated="$4"
  local parent="${commit}^"
  git_in_worktree rev-parse --verify --quiet "$parent" > /dev/null || return 0

  local before after
  before=$(lines_at "$parent" "$file")
  [[ -z "$before" ]] && return 0
  after=$(lines_at "$commit" "$file")

  # Content the commit still carries: what remains in this file, plus anything
  # it added elsewhere.
  local retained
  retained=$(printf '%s\n%s\n' "$after" "$relocated" | sort -u)

  local commit_time window_start
  commit_time=$(git_in_worktree log -1 --format="%ct" "$commit")
  window_start=$((commit_time - RECENT_HOURS * 3600))

  local considered=0 recent_hash recent_time added total survivors undone percent recent_subject
  while read -r recent_hash recent_time; do
    [[ -z "$recent_hash" ]] && continue
    # A commit inside the checked range belongs to this branch, not to the base.
    if printf '%s\n' "$RANGE_COMMITS" | grep -qFx "$recent_hash"; then
      continue
    fi
    considered=$((considered + 1))
    [[ "$considered" -gt "$RECENT_COMMITS" ]] && break
    [[ "$recent_time" -lt "$window_start" ]] && continue

    added=$(lines_added_by "$recent_hash" "$file")
    total=$(count_lines "$added")
    [[ "$total" -lt "$MIN_UNDONE_LINES" ]] && continue

    # Lines that commit added, that were still present just before this commit,
    # and that this commit no longer carries anywhere.
    survivors=$(comm -12 <(printf '%s\n' "$added") <(printf '%s\n' "$before"))
    [[ -z "$survivors" ]] && continue
    undone=$(count_lines "$(comm -23 <(printf '%s\n' "$survivors") <(printf '%s\n' "$retained"))")
    percent=$((undone * 100 / total))

    if [[ "$undone" -ge "$MIN_UNDONE_LINES" && "$percent" -ge "$MIN_UNDONE_PERCENT" ]]; then
      recent_subject=$(git_in_worktree log -1 --format="%s" "$recent_hash")
      echo "  $short_hash removes $undone of $total lines (${percent}%) that ${recent_hash:0:8} ($recent_subject) added to $file"
    fi
  done < <(git_in_worktree log --no-merges -n "$((RECENT_COMMITS * 2))" --format="%H %ct" "$parent" -- "$file")
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# An explicit SILENT_REVERT_RANGE must resolve; unlike the default range, a bad
# explicit range is a script error, not an empty (and therefore passing) commit
# list. The default fails open, so an offline push is never blocked, but says
# that nothing was checked.
if [[ -n "${SILENT_REVERT_RANGE:-}" ]]; then
  commit_range="$SILENT_REVERT_RANGE"
  if ! commits=$(git_in_worktree rev-list --no-merges "$commit_range" 2>&1); then
    echo "FAIL: could not resolve SILENT_REVERT_RANGE '$commit_range':"
    echo "$commits"
    exit 1
  fi
else
  commit_range="origin/$BASE_BRANCH..HEAD"
  if ! commits=$(git_in_worktree rev-list --no-merges "$commit_range" 2> /dev/null); then
    echo "WARNING: could not resolve $commit_range; nothing was checked (has origin/$BASE_BRANCH been fetched?)" >&2
    exit 0
  fi
fi

[[ -z "$commits" ]] && exit 0
RANGE_COMMITS="$commits"

scope_violations=""
undone_violations=""
weak_exemptions=""

while IFS= read -r commit_hash; do
  [[ -z "$commit_hash" ]] && continue

  body=$(git_in_worktree log -1 --format="%B" "$commit_hash")
  subject=$(git_in_worktree log -1 --format="%s" "$commit_hash")
  short_hash="${commit_hash:0:8}"

  reason=$(exemption_reason "$body")
  if [[ -n "$reason" ]]; then
    if [[ "${#reason}" -ge "$MIN_REASON_LENGTH" ]]; then
      continue
    fi
    weak_exemptions+="  $short_hash silent-revert-ok reason '$reason' is too short to justify a deletion"$'\n'
  fi

  scopes=$(parse_scopes "$subject")
  allowed_paths="$COMPANION_PATHS"
  wide_scope=false
  for scope in $scopes; do
    scope_path=$(scope_paths "$scope")
    if [[ "$scope_path" == "*" ]]; then
      wide_scope=true
    else
      allowed_paths="$allowed_paths $scope_path"
    fi
  done
  if [[ "$(commit_type "$subject")" == "ci" ]]; then
    allowed_paths="$allowed_paths $CI_PATHS"
  fi

  deleted_files=""
  deleted_files_computed=false
  renamed_files=""
  renamed_files_computed=false
  relocated_lines=""
  relocated_lines_computed=false

  while IFS=$'\t' read -r added deleted file; do
    [[ -z "$file" ]] && continue

    # Skip binary files (git reports "-" for added/deleted)
    [[ "$deleted" == "-" ]] && continue

    # Normalize rename syntax to the destination path, but only for confirmed
    # renames. A literal " => " in a real filename would otherwise be mishandled.
    case "$file" in
      *' => '*)
        if ! $renamed_files_computed; then
          if ! renamed_files=$(git_in_worktree show --diff-filter=R --name-only --format="" "$commit_hash"); then
            echo "WARNING: could not resolve renames for $short_hash (rename normalization degraded)" >&2
            renamed_files=""
          fi
          renamed_files_computed=true
        fi
        normalized=$(normalize_rename_path "$file")
        if [[ -n "$renamed_files" ]] && echo "$renamed_files" | grep -qFx "$normalized"; then
          file="$normalized"
        fi
        # When rename lookup fails or the file is not confirmed as a rename,
        # $file keeps the raw numstat syntax (e.g. docs/{a => b}). That form
        # matches no prose or protected pattern, so the plain code threshold
        # applies: strictly stricter, never a false pass.
        ;;
    esac

    if is_exempt_file "$file"; then
      continue
    fi

    # Check 2 does not depend on scope. A renamed file has no content at its
    # old path, so there is nothing to compare for it.
    if [[ "$deleted" -ge "$MIN_UNDONE_LINES" && "$file" != *' => '* ]]; then
      if ! $relocated_lines_computed; then
        relocated_lines=$(lines_added_by "$commit_hash")
        relocated_lines_computed=true
      fi
      found=$(undone_recent_work "$commit_hash" "$file" "$short_hash" "$relocated_lines")
      if [[ -n "$found" ]]; then
        undone_violations+="$found"$'\n'
      fi
    fi

    # Check 1. A wide scope skips the line count below, but not the protected-file
    # rule: otherwise feat(core), the most common scope, could delete an invariant.
    if is_in_scope "$file" "$allowed_paths"; then
      continue
    fi

    if is_protected_file "$file"; then
      if ! $deleted_files_computed; then
        if ! deleted_files=$(git_in_worktree show --diff-filter=D --name-only --format="" "$commit_hash"); then
          echo "WARNING: could not list deleted files for $short_hash (whole-file deletion check degraded)" >&2
          deleted_files=""
        fi
        deleted_files_computed=true
      fi
      if [[ -n "$deleted_files" ]] && echo "$deleted_files" | grep -qFx "$file"; then
        scope_violations+="  $short_hash deleted $file entirely (scope: ${scopes:-none})"$'\n'
        continue
      fi
    fi

    if $wide_scope; then
      continue
    fi

    if is_prose_file "$file"; then
      effective_threshold="$PROSE_DELETION_THRESHOLD"
    else
      effective_threshold="$DELETION_THRESHOLD"
    fi

    if [[ "$deleted" -ge "$effective_threshold" ]]; then
      scope_violations+="  $short_hash deleted $deleted lines from $file (scope: ${scopes:-none})"$'\n'
    fi
  done < <(git_in_worktree show --numstat --format="" "$commit_hash")

done <<< "$commits"

failed=false

if [[ -n "$undone_violations" ]]; then
  failed=true
  echo "FAIL: Commits undo recent work already on $BASE_BRANCH:"
  echo "$undone_violations"
fi

if [[ -n "$scope_violations" ]]; then
  failed=true
  echo "FAIL: Commits delete significant code outside their declared scope:"
  echo "$scope_violations"
fi

if $failed; then
  if [[ -n "$weak_exemptions" ]]; then
    echo "$weak_exemptions"
  fi
  echo "This may indicate a silent revert caused by rebase, squash, or conflict resolution from a stale tree."
  echo "If a commit is intentional, add 'silent-revert-ok: <reason>' to its message body."
  echo "The reason must justify the deletion, not just acknowledge it."
  exit 1
fi
exit 0
