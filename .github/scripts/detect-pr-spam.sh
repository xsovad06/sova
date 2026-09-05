#!/usr/bin/env bash
# Detect high-confidence spam signals on a pull request. Run --help for the
# input and output contract.
#
# The pull request body arrives in the PR_BODY environment variable, never as
# an argument, so a crafted description cannot be interpolated into a shell
# command. Reason tokens come from a fixed set so untrusted input is never
# echoed into GITHUB_OUTPUT; details about a match go to stderr.
#
# The file-based rules fail open: when the changed-file list is empty or could
# not be fetched, rules (b) and (c) are skipped rather than guessed at. Rule (a)
# reads only the pull request body, so it is complete on its own and still
# applies. Incomplete data must never close a pull request on a partial signal.
set -euo pipefail

# A bounty slash command at the start of a body line. A bare mention of the
# word "bounty" in prose is not a signal.
readonly BOUNTY_COMMAND_RE='^[[:space:]]*/(claim|bounty|reward)([^[:alnum:]]|$)'

# A bounty token appearing as a whole word in a file name. Matches BOUNTY.md,
# bounty-fix.md and BOUNTY_FIX.md; does not match boundary.md or reclaim.md.
readonly BOUNTY_FILE_RE='(^|[^[:alnum:]])(bounty|claim|reward)([^[:alnum:]]|$)'

# A GitHub issue-closing keyword. The leading boundary keeps "prefixes #12"
# from matching on "fixes".
readonly CLOSING_KEYWORD_RE='(^|[^[:alnum:]])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]+#[0-9]+'

usage() {
  cat <<'EOF'
Usage: detect-pr-spam.sh < changed-files.txt

Reads the pull request body from PR_BODY and the changed file paths from
stdin (one per line; stdin is always consumed in full). Prints:

  spam=true|false
  reason=bounty-command|bounty-file|community-files-only|no-files|none

Detection rules:
  a) a bounty slash command (/claim, /bounty, /reward) starting a body line
  b) a changed file whose name contains bounty, claim or reward as a word
  c) an issue-closing keyword in the body while the diff touches community
     meta files only (CODE_OF_CONDUCT.md, CONTRIBUTING.md, CODEOWNERS, LICENSE,
     in the repository root, .github/ or docs/)

Rules (b) and (c) are skipped when the file list is empty (reason=no-files);
rule (a) needs only the body and is evaluated first.

Exits 0 on every successful evaluation, including a clean verdict.
EOF
}

# verdict SPAM REASON: emit the machine-readable result and stop.
verdict() {
  echo "spam=$1"
  echo "reason=$2"
  exit 0
}

# is_community_file PATH: true for the meta files a drive-by spam PR rewrites.
# The full path is matched, not the bare basename, so an unrelated nested file
# such as templates/CONTRIBUTING.md is never treated as a community file (it
# would otherwise keep community_only true and put a genuine PR in scope of the
# auto-close rule). Only GitHub's own community health file locations count:
# the repository root, .github/ and docs/. README.md and ordinary docs/ content
# stay excluded so a genuine first-time typo fix is never auto-closed.
is_community_file() {
  local path
  path=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')
  case "$path" in
    .GITHUB/*|DOCS/*) path="${path#*/}" ;;
  esac
  case "$path" in
    CODE_OF_CONDUCT.MD|CONTRIBUTING.MD|CODEOWNERS|LICENSE) return 0 ;;
    *) return 1 ;;
  esac
}

main() {
  if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  local body files file base community_only=true
  body="${PR_BODY:-}"

  # Drain stdin before evaluating any rule. Callers pipe the file list in
  # under `set -o pipefail`, so exiting early would kill the producer with
  # SIGPIPE and fail the whole pipeline on a genuine spam match.
  files=$(cat)

  if grep -qEi "$BOUNTY_COMMAND_RE" <<< "$body"; then
    echo "signal: bounty slash command in the pull request body" >&2
    verdict true bounty-command
  fi

  if [[ -z "${files//[[:space:]]/}" ]]; then
    echo "no changed files reported: failing open" >&2
    verdict false no-files
  fi

  while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    base="${file##*/}"
    if grep -qEi "$BOUNTY_FILE_RE" <<< "$base"; then
      echo "signal: bounty-named file: $file" >&2
      verdict true bounty-file
    fi
    is_community_file "$file" || community_only=false
  done <<< "$files"

  if [[ "$community_only" == true ]] && grep -qEi "$CLOSING_KEYWORD_RE" <<< "$body"; then
    echo "signal: claims to close an issue but only touches community meta files" >&2
    verdict true community-files-only
  fi

  verdict false none
}

main "$@"
