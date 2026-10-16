"""Representative role/step task fixtures for the model-evaluation harness.

Each fixture approximates one pipeline step's real workload at a scale cheap
enough to run repeatedly against several candidate models. The set covers the
workloads issue #1129 names (triage, extraction, planning, research, spec
drafting, development, review, validation and CI fixes, PR-body drafting, and
oversight), not every step in the pipeline. The researcher role contributes
two fixtures (research, spec); CI-fix coverage is approximated by the
``validate`` fixture alone, with no separate ``monitor_ci`` fixture.

Most fixtures check more than the marker heading: ``required_content`` names
substrings a genuinely correct answer must contain (not merely a
correctly-formatted one), and ``accept`` lets a fixture supply a stricter
custom check in place of the default substring test. Three fixtures
(``generate_tasks``, ``extract_memory``, ``oversight/analysis``) stay
heading-only, because the prompt has no single correct answer to check a
response against; for those three, the harness measures format compliance
(did the model follow the heading instruction), not answer quality, and
``cost_per_accepted_result()`` for those rows should be read accordingly, not
as a capability signal.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sova.llm.models import LLMResult


@dataclass(frozen=True)
class EvalFixture:
    """One task to run against a candidate model.

    ``role``/``step`` match the pipeline's own role and step names (see
    ``.claude/rules/architecture.md``'s pipeline step lists), so a result can
    be mapped straight back to the ``roles.*_model``/``agent.model`` setting
    it stands in for. ``oversight`` is the one exception: it is a background
    daemon rather than a pipeline role, and its model comes from
    ``oversight.analysis_model``.

    ``required_content`` is an additional, case-insensitive set of substrings
    that must all appear in the response for it to be accepted, on top of
    ``accept_marker``; an empty tuple (the default) means the marker alone is
    checked, for a prompt with no single correct answer to verify against.
    ``accept``, when set, replaces the default marker/required_content check
    entirely for this fixture (see ``harness.accept_on_marker`` for the
    default it overrides).
    """

    role: str
    step: str
    prompt: str
    accept_marker: str
    required_content: tuple[str, ...] = ()
    accept: Callable[["EvalFixture", LLMResult], bool] | None = None


def _accept_research(fixture: EvalFixture, result: LLMResult) -> bool:
    """The marker, plus the exact timeout value as a whole number.

    A plain substring check for "45" also accepts "450", which the prompt
    never states; matching on a word boundary distinguishes a genuinely
    correct extraction from one that merely happens to contain the right
    digits.
    """
    return fixture.accept_marker in result.text and re.search(r"\b45\b", result.text) is not None


def _accept_triage(fixture: EvalFixture, result: LLMResult) -> bool:
    """The marker, plus an unambiguous "bug" classification.

    The prompt itself lists all three classification options ("bug, feature,
    or task"), so a plain substring check for "bug" also passes a response
    that merely restates the prompt. Requiring "bug" while ruling out the
    other two options' names distinguishes an actual classification from an
    echo of the question.
    """
    if fixture.accept_marker not in result.text:
        return False
    text_lower = result.text.lower()
    return "bug" in text_lower and "feature" not in text_lower and "task" not in text_lower


def _accept_develop(fixture: EvalFixture, result: LLMResult) -> bool:
    """`def clamp` plus evidence of tests, not just the function definition.

    The prompt asks for unit tests covering three branches (in-range,
    below-low, above-high); a response with the function but no tests, or a
    single smoke-test assertion, should not pass as "tests covering three
    cases". Counting ``assert`` occurrences is a coarse proxy for that
    without hardcoding the exact values a correct test would use.
    """
    return fixture.accept_marker in result.text and result.text.count("assert") >= 3


FIXTURES: tuple[EvalFixture, ...] = (
    EvalFixture(
        role="researcher",
        step="research",
        prompt=(
            "Using only the snippet below (no other context), answer under a "
            "'## Research' heading: what is the default per-request timeout, "
            "in seconds?\n\n"
            "```python\n"
            "# sova/llm/client.py\n"
            "DEFAULT_REQUEST_TIMEOUT_S = 45\n"
            "```"
        ),
        accept_marker="## Research",
        accept=_accept_research,
    ),
    EvalFixture(
        role="researcher",
        step="spec",
        prompt=(
            "Write a structured specification for adding a per-request timeout "
            "override, with '## Solution', '## Edge Cases', and '## Design "
            "Decisions' sections."
        ),
        accept_marker="## Solution",
        required_content=("## Edge Cases", "## Design Decisions"),
    ),
    EvalFixture(
        role="developer",
        step="develop",
        prompt=(
            "Implement a pure function `clamp(value, low, high)` with unit "
            "tests covering the in-range, below-low, and above-high cases."
        ),
        accept_marker="def clamp",
        accept=_accept_develop,
    ),
    EvalFixture(
        role="reviewer",
        step="self_review",
        prompt=(
            "Review this diff for correctness bugs only. List each finding under "
            "a '## Findings' heading; if there are none, write '## Findings' "
            "followed by 'none':\n"
            "+def divide(a, b):\n+    return a / b\n"
        ),
        accept_marker="## Findings",
        required_content=("ZeroDivisionError",),
    ),
    EvalFixture(
        role="triage",
        step="triage",
        prompt=(
            "Classify this issue report as bug, feature, or task, and state "
            "the priority under a '## Triage' heading: 'the dashboard 500s "
            "when a project has no open issues'."
        ),
        accept_marker="## Triage",
        accept=_accept_triage,
    ),
    EvalFixture(
        role="planner",
        step="generate_tasks",
        prompt=(
            "Break 'add CSV export to the costs page' into 2-4 discrete "
            "implementation tasks, one per line under a '## Tasks' heading."
        ),
        accept_marker="## Tasks",
    ),
    EvalFixture(
        role="developer",
        step="extract_memory",
        prompt=(
            "Extract the reusable lesson from this fix and state it under a "
            "'## Lesson' heading: a helper returned 0 on failure, which the "
            "caller could not tell apart from a real count of zero."
        ),
        accept_marker="## Lesson",
    ),
    EvalFixture(
        role="developer",
        step="validate",
        prompt=(
            "This test now fails after a refactor. Diagnose the cause and "
            "give the corrected code under a '## Fix' heading:\n"
            "def test_total():\n    assert total([]) == 0\n"
            "# total() now fails on an empty list\n"
        ),
        accept_marker="## Fix",
        required_content=("StopIteration",),
    ),
    EvalFixture(
        role="developer",
        step="create_pr",
        prompt=(
            "Draft a pull request body for a change that adds a per-request "
            "timeout override, with '## Summary' and '## Test Plan' sections."
        ),
        accept_marker="## Test Plan",
        required_content=("## Summary",),
    ),
    EvalFixture(
        role="oversight",
        step="analysis",
        prompt=(
            "Three of the last five runs for one issue failed at the same "
            "step. State the single most likely cause and the one action to "
            "take, under a '## Assessment' heading."
        ),
        accept_marker="## Assessment",
    ),
)
