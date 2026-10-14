#!/usr/bin/env bash
# Invariant: No double-dash separators in prose
# Flags ` -- ` (space-dash-dash-space) in .md, .py, .html, .sh, .js, .yml, .toml and .css files
# and in .githooks/ hooks. Excludes fenced code blocks (markdown) and inline code spans, and in
# non-markdown files POSIX end-of-options markers such as `git checkout -- <path>`.
#
# Two modes share every exemption rule, so they cannot disagree:
#   <worktree_dir> [base_branch]   only lines added against origin/<base_branch> (pre-push and CI)
#   --all <worktree_dir>           every tracked file (whole-tree guard, see tests/test_no_double_dash.py)
set -euo pipefail

# The flagged literal is assembled from two quoted parts so that this script does not
# contain it as prose and trip its own whole-tree check.
DD=' -''- '
# Also matches the marker at end of line: markdown soft-wrap drops the trailing space
# the interior form above requires, so a line ending in the marker (trailing
# whitespace allowed) must be flagged too, not just an interior occurrence.
DD_RE=' -''-([[:space:]]|$)'

PATHSPECS=('*.md' '*.py' '*.html' '*.sh' '*.js' '*.yml' '*.toml' '*.css' '.githooks/*')

usage() {
  echo "Usage: $0 <worktree_dir> [base_branch]"
  echo "       $0 --all <worktree_dir>"
  echo "Flags '$DD' (space-dash-dash-space) in prose text (.md, .py, .html, .sh, .js, .yml, .toml, .css, .githooks/)."
  echo "Default mode checks lines added against origin/<base_branch>; --all checks every tracked file."
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

mode=changed
if [[ "${1:-}" == "--all" ]]; then
  mode=all
  shift
fi

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

# lines_in_code_blocks: outputs line numbers that are inside fenced code blocks.
# If an opening fence has no closing fence, returns empty (no exclusions)
# so violations in actual prose are not suppressed by malformed markup.
lines_in_code_blocks() {
  local output
  output=$(awk '
    /^[[:space:]]*```/ || /^[[:space:]]*~~~/ {
      if (in_block) { in_block = 0; lines = lines NR "\n"; next }
      else          { in_block = 1; lines = lines NR "\n"; next }
    }
    in_block { lines = lines NR "\n" }
    END { if (!in_block) printf "%s", lines; else exit 1 }
  ' "$1") || output=""
  printf '%s' "$output"
}

# strip_inline_code: removes backtick-delimited code spans from a line.
# Handles double-backtick spans (which may contain single backticks) first.
strip_inline_code() {
  # shellcheck disable=SC2016
  sed -E 's/``(([^`]|`[^`])*)``//g; s/`[^`]*`//g'
}

# is_posix_end_of_opts: returns 0 if the dashes look like a POSIX end-of-options
# marker rather than a prose em-dash separator.
is_posix_end_of_opts() {
  local line="$1"
  # Common commands (and git wrapper functions such as git_in_worktree) followed by the marker
  grep -qE '\b(git(_[a-z_]+)?|grep|sed|awk|find|rm|cp|mv|ls|cat|docker|kubectl|npm|yarn|pip|cargo|make|bash|sh|zsh|xargs|ssh|rsync|curl|wget|tar|diff|patch)\b.*('"$DD"'|'"$DD_RE"')' <<< "$line"
}

# extract_added_line_numbers: parses unified diff, outputs "line_number<TAB>content"
# for each added line. Tracks line numbers via @@ hunk headers.
extract_added_line_numbers() {
  awk '
    /^@@ / {
      s = $0
      sub(/^@@ -[0-9,]* \+/, "", s)
      sub(/[^0-9].*/, "", s)
      cur = s + 0
      if (cur < 1) cur = 1
      next
    }
    /^\+\+\+/ { next }
    /^---/    { next }
    /^\+/     { print cur "\t" substr($0, 2); cur++ ; next }
    /^-/      { next }
    { cur++ }
  '
}

# extract_marked_lines: outputs "line_number<TAB>content" for every line of a file holding the marker.
extract_marked_lines() {
  awk -v pat="$DD" -v re="$DD_RE" 'index($0, pat) || $0 ~ re { printf "%d\t%s\n", NR, $0 }' "$1"
}

violations=""

# check_candidates: reads "line_number<TAB>content" records for one file on stdin and appends
# a report line to $violations for each one that is prose. The single place the exemption
# rules live; both modes feed it.
check_candidates() {
  local f="$1" is_md=false blocks_loaded=false code_block_lines="" line_num content cleaned

  if [[ "$f" == *.md ]]; then
    is_md=true
  fi

  while IFS=$'\t' read -r line_num content; do
    [[ -z "$line_num" ]] && continue

    # Cheap prefilter: skip lines without the marker before forking anything.
    if [[ "$content" != *"$DD"* ]] && [[ ! "$content" =~ $DD_RE ]]; then
      continue
    fi

    if $is_md; then
      if ! $blocks_loaded; then
        code_block_lines=$(lines_in_code_blocks "$WORKTREE_DIR/$f")
        blocks_loaded=true
      fi
      if grep -qxF "$line_num" <<< "$code_block_lines"; then
        continue
      fi
    fi

    cleaned=$(strip_inline_code <<< "$content")

    if [[ "$cleaned" == *"$DD"* ]] || [[ "$cleaned" =~ $DD_RE ]]; then
      if ! $is_md && is_posix_end_of_opts "$cleaned"; then
        continue
      fi
      violations+="  $f:$line_num: $content"$'\n'
    fi
  done
}

if [[ "$mode" == "all" ]]; then
  while IFS= read -r -d '' f; do
    [[ -f "$WORKTREE_DIR/$f" ]] || continue
    check_candidates "$f" < <(extract_marked_lines "$WORKTREE_DIR/$f")
  done < <(git -C "$WORKTREE_DIR" ls-files -z -- "${PATHSPECS[@]}")
else
  changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- "${PATHSPECS[@]}" 2>/dev/null || true)
  [[ -z "$changed_files" ]] && exit 0

  while IFS= read -r f; do
    [[ -f "$WORKTREE_DIR/$f" ]] || continue

    diff_output=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" || true)
    [[ -z "$diff_output" ]] && continue

    check_candidates "$f" < <(extract_added_line_numbers <<< "$diff_output")
  done <<< "$changed_files"
fi

if [[ -n "$violations" ]]; then
  echo "FAIL: Double-dash separator '$DD' found in prose (use colon, period, or parentheses):"
  echo "$violations"
  exit 1
fi
exit 0
