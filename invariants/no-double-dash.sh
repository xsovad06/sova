#!/usr/bin/env bash
# Invariant: No double-dash separators in prose
# Flags ` -- ` (space-dash-dash-space) in .md, .py, and .html files.
# Excludes fenced code blocks and inline code spans in markdown files.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: $0 <worktree_dir> [base_branch]"
  echo "Flags ' -- ' (space-dash-dash-space) in prose text (.md, .py, .html)."
  exit 0
fi

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- '*.md' '*.py' '*.html' 2>/dev/null || true)
[[ -z "$changed_files" ]] && exit 0

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

# is_posix_end_of_opts: returns 0 if ` -- ` looks like a POSIX end-of-options
# marker rather than a prose em-dash separator.
is_posix_end_of_opts() {
  local line="$1"
  # Common commands followed by ` -- ` (end-of-options marker)
  echo "$line" | grep -qE '\b(git|grep|sed|awk|find|rm|cp|mv|ls|cat|docker|kubectl|npm|yarn|pip|cargo|make|bash|sh|zsh|xargs|ssh|rsync|curl|wget|tar|diff|patch)\b.* -- '
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

violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue

  diff_output=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" || true)
  [[ -z "$diff_output" ]] && continue

  is_md=false
  [[ "$f" == *.md ]] && is_md=true

  if $is_md; then
    code_block_lines=$(lines_in_code_blocks "$WORKTREE_DIR/$f")
  fi

  while IFS=$'\t' read -r line_num content; do
    [[ -z "$line_num" ]] && continue

    if $is_md; then
      if echo "$code_block_lines" | grep -qxF "$line_num"; then
        continue
      fi
    fi

    cleaned=$(echo "$content" | strip_inline_code)

    if echo "$cleaned" | grep -qF ' -- '; then
      if ! $is_md && is_posix_end_of_opts "$cleaned"; then
        continue
      fi
      violations+="  $f:$line_num: $content"$'\n'
    fi
  done <<< "$(echo "$diff_output" | extract_added_line_numbers)"
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: Double-dash separator ' -- ' found in prose (use colon, period, or parentheses):"
  echo "$violations"
  exit 1
fi
exit 0
