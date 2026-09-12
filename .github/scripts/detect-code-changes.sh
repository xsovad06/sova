#!/usr/bin/env bash
# Decide whether a pull request needs the expensive CI suite. Run --help for
# the input and output contract.
#
# Two layers of detection, either of which can grant a skip:
#
#   1. whole-pull-request: every file the PR touches is markdown
#   2. delta-since-last-green: the file tree at this commit is identical to the
#      tree at the last commit this workflow went green on, except for markdown
#
# Layer 2 is what makes a documentation push onto a code-carrying PR free.
# Layer 1 alone cannot do it: the pull request diff still contains the code
# that was pushed earlier, so it classifies as code forever.
#
# Layer 2 compares TREES, not commits. Documentation reaches a branch by
# `commit --amend` plus a force-push, which produces a SIBLING of the commit
# that was tested, not a descendant. Every commit-topology test therefore
# reports "diverged" on exactly the case this exists to catch, and every
# commit-range diff (including the compare API, which is always three-dot and
# walks back to the merge base) reports the whole amended commit rather than
# what the amend actually changed. Two recursive tree listings, diffed on
# (path, mode, blob sha), answer the real question directly: is any
# non-markdown byte, or file mode, different from the state that was proven
# green.
#
# A skip re-uses a green result that was produced against whatever the base
# branch held at the time it ran. That is only safe if any later base-branch
# change is guaranteed to reach this branch, with its own real diff, before
# merge is allowed. This repository's `main-protection` ruleset sets
# strict_required_status_checks_policy to true, so GitHub itself enforces
# that: a branch that falls behind cannot merge until it is updated, and that
# update (rebase or merge) pulls in whatever the base branch actually changed.
# If the base branch only gained markdown, the update's tree still classifies
# as docs-only here, correctly, since nothing but markdown is genuinely at
# stake. If the base branch gained code, the update's tree differs by more
# than markdown, this script returns code-changed, and the required suite
# runs against the up-to-date branch before merge is possible. The two layers
# above never need to know which case they are in; the enforcement lives in
# the ruleset, not in this script. If that policy is ever turned back to
# false, this layer becomes unsound (a stale green result could be reused
# indefinitely) and must be revisited.
#
# Every failure path fails OPEN (code=true, run the suite). A skip is only ever
# granted on positive evidence. Reason tokens come from a fixed set so
# untrusted input is never echoed into GITHUB_OUTPUT.
#
# All lookups go through the GitHub API rather than a local git checkout, so
# this script is safe to invoke from a pull_request_target workflow without
# ever reading or executing pull request head content.
set -euo pipefail

# How many recent successful runs to consider when looking for a baseline.
readonly BASELINE_SEARCH_LIMIT=1

usage() {
  cat <<'EOF'
Usage: detect-code-changes.sh detect
       detect-code-changes.sh classify < changed-files.txt

Modes:
  detect    Full detection. Reads configuration from the environment, queries
            the GitHub API, and prints the decision. Requires GH_TOKEN.
  classify  Pure classification with no network access. Reads changed file
            paths from stdin (one per line) and prints code=true|false.
            Used by the unit tests and for local debugging.

Environment (detect mode):
  GH_TOKEN        token with read access to the repository (required)
  REPO            owner/name (required)
  EVENT_NAME      github.event_name (required)
  PR_NUMBER       pull request number (required for pull request events)
  HEAD_SHA        pull request head commit sha (required for pull request events)
  HEAD_BRANCH     pull request head branch name (required for pull request events)
  WORKFLOW_FILE   workflow file name whose past runs form the baseline,
                  e.g. ci.yml (required for pull request events)

Output (both modes print to stdout, one key per line):
  code=true|false
  reason=<token>            (detect mode only)

Reason tokens:
  non-pr             not a pull request event, no diff base exists
  bad-input          a required environment variable was missing
  no-files           the pull request file list was empty or unavailable
  docs-only-pr       every file in the pull request is markdown
  no-baseline        no earlier successful run to compare against
  baseline-unusable  a file tree could not be read in full
  identical          the file tree is byte-identical to the baseline
  docs-only-delta    only markdown differs from the baseline tree
  code-changed       a non-markdown file differs

code=false is only ever printed for docs-only-pr, docs-only-delta and
identical. Every other outcome prints code=true.
EOF
}

log() {
  echo "$*" >&2
}

# Classify a newline-separated list of paths. Prints "true" when at least one
# path is not markdown, "false" when every path is markdown, "empty" when the
# list holds no paths at all.
#
# Only markdown counts as documentation. docs/ and .claude/ also hold code
# (docs/pipeline-determinism.html, the .claude/benchmark/*.sh hooks,
# .claude/commands/.sova-manifest.json), so excluding those trees wholesale
# would let a broken script merge with the suite skipped.
classify_paths() {
  local files="$1"
  local path
  local seen=0

  while IFS= read -r path; do
    [ -z "$path" ] && continue
    seen=1
    case "$path" in
      *.md) ;;
      *)
        echo "true"
        return 0
        ;;
    esac
  done <<<"$files"

  if [ "$seen" -eq 0 ]; then
    echo "empty"
    return 0
  fi
  echo "false"
}

# Fetch every file path in the pull request. Prints nothing on failure.
#
# A page fetched before pagination fails is still valid stdout from `gh`, so
# capturing output and status together and discarding the output on failure
# is required: a bare `|| true` on the pipeline would keep whatever partial
# page list had already been printed, letting a docs-only first page mask a
# code-carrying later page that the failed request never reached.
fetch_pr_files() {
  local output
  output="$(gh api --paginate "repos/${REPO}/pulls/${PR_NUMBER}/files" --jq '.[].filename' 2>/dev/null)" || return 0
  printf '%s\n' "$output"
}

# Find the head sha of the most recent successful run of this workflow on this
# branch. Prints nothing when there is none.
#
# A run that skipped its expensive steps still counts: its green result was
# inherited from a run that did not, and the tree comparison below is
# transitive, so the chain stays anchored to real evidence.
#
# Branch and query values are passed as -f/-F params, not interpolated into
# the URL string: `gh api` appends them as URL-encoded query parameters on a
# GET request, so a branch name containing `&`, `#`, or other characters that
# are valid in a git ref but meaningful in a URL cannot corrupt the query.
fetch_baseline_sha() {
  gh api \
    -f "branch=${HEAD_BRANCH}" \
    -f status=success \
    -f "event=${EVENT_NAME}" \
    -F "per_page=${BASELINE_SEARCH_LIMIT}" \
    "repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/runs" \
    --jq '.workflow_runs[0].head_sha' 2>/dev/null || true
}

# Print "<path>\t<mode>\t<sha>" for every file and submodule pointer (gitlink)
# in a commit's tree. Directory ("tree") entries are excluded: their sha
# already reflects every change beneath them, so including them would make
# every commit differ on every ancestor directory path, which is not itself a
# markdown-classifiable path and would defeat the whole detector.
#
# Mode is included, not just path and blob sha, so a permission-only change
# (e.g. `chmod +x` on a `.sh` file, with byte-identical content) still shows
# up as a real difference: two trees whose blob shas match at a path but whose
# modes differ ARE different, and a comparison keyed on (path, sha) alone
# would silently miss it. Gitlink (submodule pointer) entries are included for
# the same reason: a submodule bump changes the gitlink's sha with no blob of
# its own, so filtering to `type == "blob"` would make it invisible to the
# diff entirely.
#
# Returns non-zero when the tree could not be read, or when the API truncated
# it. A truncated listing cannot prove that nothing else changed.
fetch_tree() {
  local sha="$1" json truncated
  json="$(gh api "repos/${REPO}/git/trees/${sha}?recursive=1" 2>/dev/null)" || return 1
  truncated="$(printf '%s' "$json" | jq -r '.truncated // false' 2>/dev/null || echo true)"
  if [ "$truncated" != "false" ]; then
    log "tree listing for ${sha} was truncated by the API"
    return 1
  fi
  printf '%s' "$json" | jq -r '.tree[] | select(.type != "tree") | "\(.path)\t\(.mode)\t\(.sha)"' 2>/dev/null || return 1
}

emit() {
  echo "code=$1"
  echo "reason=$2"
  log "decision: code=$1 reason=$2"
}

detect() {
  if [ "${EVENT_NAME:-}" != "pull_request" ] && [ "${EVENT_NAME:-}" != "pull_request_target" ]; then
    # A push to main has no pull request diff base. Always run the suite.
    emit true non-pr
    return 0
  fi

  local var
  for var in REPO PR_NUMBER HEAD_SHA HEAD_BRANCH WORKFLOW_FILE; do
    if [ -z "${!var:-}" ]; then
      log "missing required environment variable: $var"
      emit true bad-input
      return 0
    fi
  done

  # Layer 1: is the whole pull request documentation?
  local pr_files
  pr_files="$(fetch_pr_files)"
  if [ -z "$pr_files" ]; then
    # Empty list: an API hiccup, a pagination failure, or a genuinely empty
    # diff. None of those prove the change is documentation.
    emit true no-files
    return 0
  fi

  if [ "$(classify_paths "$pr_files")" = "false" ]; then
    emit false docs-only-pr
    return 0
  fi

  # Layer 2: does anything but documentation differ from the last green tree?
  local baseline
  baseline="$(fetch_baseline_sha)"
  if [ -z "$baseline" ] || [ "$baseline" = "null" ]; then
    log "no earlier successful ${WORKFLOW_FILE} run on ${HEAD_BRANCH}"
    emit true no-baseline
    return 0
  fi
  if [ "$baseline" = "$HEAD_SHA" ]; then
    # This exact commit already went green. Nothing to re-run.
    emit false identical
    return 0
  fi
  log "baseline: ${baseline}"

  local baseline_tree head_tree
  if ! baseline_tree="$(fetch_tree "$baseline")" || [ -z "$baseline_tree" ]; then
    emit true baseline-unusable
    return 0
  fi
  if ! head_tree="$(fetch_tree "$HEAD_SHA")" || [ -z "$head_tree" ]; then
    emit true baseline-unusable
    return 0
  fi

  # A "<path>\t<mode>\t<sha>" triple present in exactly one listing marks a
  # path that was added, removed, or whose content or mode changed.
  local delta
  delta="$(printf '%s\n%s\n' "$baseline_tree" "$head_tree" | sort | uniq -u | cut -f1 | sort -u)"

  if [ -z "$delta" ]; then
    emit false identical
    return 0
  fi

  log "paths differing from the baseline tree:"
  log "$delta"

  if [ "$(classify_paths "$delta")" = "false" ]; then
    emit false docs-only-delta
    return 0
  fi

  emit true code-changed
}

main() {
  local mode="${1:-detect}"

  case "$mode" in
    -h | --help | help)
      usage
      exit 0
      ;;
    classify)
      local input verdict
      input="$(cat)"
      verdict="$(classify_paths "$input")"
      # An empty list is not evidence of documentation: fail open.
      [ "$verdict" = "empty" ] && verdict="true"
      echo "code=${verdict}"
      ;;
    detect)
      detect
      ;;
    *)
      log "unknown mode: $mode"
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
