#!/usr/bin/env bash
# Invariant: every remote script or stylesheet the dashboard loads is pinned and
# integrity-checked.
#
# A <script> or stylesheet <link> pointing at another origin must carry:
#   - integrity="sha256-/sha384-/sha512-...": the browser refuses the file if a
#     CDN or package account is compromised. DOMPurify, the dashboard's XSS
#     sanitizer, is one of these files.
#   - crossorigin: without it the browser fetches in no-cors mode and an
#     integrity check can never pass.
#   - an exact version for jsDelivr, unpkg and cdnjs URLs. A range such as
#     `marked@15` serves new bytes on every upstream release, and the pinned
#     hash then blocks the script.
#
# Unlike the diff-based invariants this checks the whole tree: the usual
# regression is a stale branch bringing an old, unpinned tag back.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: $0 <worktree-dir> [base-branch]"
  echo "Checks that remote scripts and stylesheets in dashboard templates are version-pinned"
  echo "and carry integrity and crossorigin attributes."
  exit 0
fi

WORKTREE_DIR="${1:-.}"
TEMPLATE_DIR="sova/dashboard/templates"

templates=$(git -C "$WORKTREE_DIR" ls-files -- "$TEMPLATE_DIR/*.html")
[[ -z "$templates" ]] && exit 0

# Perl reads each file whole, so a tag split across lines is still one tag.
violations=$(
  cd "$WORKTREE_DIR"
  # shellcheck disable=SC2016  # the Perl program is single-quoted on purpose
  printf '%s\n' "$templates" | tr '\n' '\0' | xargs -0 perl -0777 -ne '
    sub unpinned {
      my ($url) = @_;
      if ($url =~ m{//(?:cdn\.jsdelivr\.net/npm|unpkg\.com)/(?:\@[^/]+/)?[^/@]+(?:\@([^/]+))?}) {
        return 1 unless defined $1;
        return $1 !~ /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
      }
      if ($url =~ m{//cdnjs\.cloudflare\.com/ajax/libs/[^/]+/([^/]+)/}) {
        return $1 !~ /^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?$/;
      }
      return 0;
    }
    while (/(<(script|link)\b[^>]*>)/gis) {
      my ($tag, $kind, $start) = ($1, lc $2, $-[0]);
      my $url;
      if ($kind eq "script") {
        next unless $tag =~ /\bsrc\s*=\s*["\x27](https?:\/\/[^"\x27]+)/i;
        $url = $1;
      } else {
        next unless $tag =~ /\brel\s*=\s*["\x27][^"\x27]*\b(?:stylesheet|modulepreload)\b/i;
        next unless $tag =~ /\bhref\s*=\s*["\x27](https?:\/\/[^"\x27]+)/i;
        $url = $1;
      }
      my @problems;
      push @problems, "no integrity hash" unless $tag =~ /\bintegrity\s*=\s*["\x27]\s*sha(?:256|384|512)-/i;
      push @problems, "no crossorigin" unless $tag =~ /\bcrossorigin\b/i;
      push @problems, "version not pinned" if unpinned($url);
      next unless @problems;
      my $line = 1 + (substr($_, 0, $start) =~ tr/\n//);
      print "  $ARGV:$line: ", join(", ", @problems), ": $url\n";
    }
  '
)

if [[ -n "$violations" ]]; then
  echo "FAIL: remote dashboard assets must be version-pinned and integrity-checked:"
  echo "$violations"
  echo "Pin an exact version, then add integrity=\"sha384-...\" crossorigin=\"anonymous\". Hash the pinned file:"
  echo "  curl -fsS <url> | openssl dgst -sha384 -binary | openssl base64 -A"
  exit 1
fi
exit 0
