# SOVA Scripts

Utility scripts for project maintenance, diagnostics, and analysis.

## Failure Analysis

### `audit_sova_failures.py`

Comprehensive failure rate audit for the SOVA project.

**Purpose**: Diagnose failure patterns, track improvement over time, and identify systemic issues.

**Usage**:
```bash
# Human-readable report
python scripts/audit_sova_failures.py

# Detailed report with error messages
python scripts/audit_sova_failures.py --detailed

# JSON output for programmatic use
python scripts/audit_sova_failures.py --json
```

**Output**:
- Overall statistics (total runs, success/failure rates, cost)
- Timeout analysis (before/after fix comparison)
- Failure categorization (budget, gate checks, timeouts)
- Top failure-prone steps
- Error message clusters (with `--detailed`)

**When to Run**:
- After major infrastructure changes (LLM provider, timeout adjustments, pipeline refactors)
- Quarterly for trend tracking
- When investigating reported high failure rates

**Requirements**:
- Must be run from the SOVA project directory (works in main repo or worktrees)
- Requires read access to `.claude/sova.db`

**Example Output**:
```
OVERALL STATISTICS
Total Runs:       332
  Done:           207 (62.3%)
  Failed:         114 (34.3%)
  
TIMEOUT ANALYSIS
Before Fix: 319 runs, 112 failed (35.1%)
After Fix:  13 runs, 2 failed (15.4%)
  Improvement: 19.7 percentage points
```

**Related**:
- Issue #710: SOVA project failure rate audit
- Issues #687, #699: Timeout fix that resolved the systemic issue

## Releases

### `changelog.py`

Generates `CHANGELOG.md` and the next release version from conventional commits (#1388). Run it through the Makefile:

```bash
make next-version              # e.g. 0.2.0: feat bumps minor, anything else patch, a breaking change major (minor before 1.0)
make changelog                 # Unreleased commits under "Unreleased"
make changelog RELEASE=0.2.0   # The same commits titled as release 0.2.0, when cutting it
```

Only `feat`, `fix` and breaking commits are listed. The release procedure is in CONTRIBUTING.md, "Releases".
