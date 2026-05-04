"""Git and GitHub CLI operations -- backward-compatible re-exports.

Split into focused modules:
- branch.py -- branch management, commit, push
- pr.py -- pull request operations via gh CLI
- rebase.py -- rebase with LLM-assisted conflict resolution
"""

from sova.git.branch import (
    _SUSPICIOUS_PATHS as _SUSPICIOUS_PATHS,
)
from sova.git.branch import (
    commit as commit,
)
from sova.git.branch import (
    create_branch as create_branch,
)
from sova.git.branch import (
    get_current_branch as get_current_branch,
)
from sova.git.branch import (
    push as push,
)
from sova.git.branch import (
    rebase as rebase,
)
from sova.git.branch import (
    sync_branch as sync_branch,
)
from sova.git.pr import (
    _GH_STATE_MAP as _GH_STATE_MAP,
)
from sova.git.pr import (
    CheckConclusion as CheckConclusion,
)
from sova.git.pr import (
    CheckStatus as CheckStatus,
)
from sova.git.pr import (
    CICheck as CICheck,
)
from sova.git.pr import (
    PRInfo as PRInfo,
)
from sova.git.pr import (
    PRStatus as PRStatus,
)
from sova.git.pr import (
    assign_pr as assign_pr,
)
from sova.git.pr import (
    create_pr as create_pr,
)
from sova.git.pr import (
    find_pr_for_issue as find_pr_for_issue,
)
from sova.git.pr import (
    get_ci_checks as get_ci_checks,
)
from sova.git.pr import (
    get_pr_diff as get_pr_diff,
)
from sova.git.pr import (
    get_pr_files as get_pr_files,
)
from sova.git.pr import (
    get_pr_status as get_pr_status,
)
from sova.git.rebase import (
    RebaseResult as RebaseResult,
)
from sova.git.rebase import (
    rebase_with_conflict_resolution as rebase_with_conflict_resolution,
)
