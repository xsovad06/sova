"""Researcher role -- investigate issues and prepare them for development.

Reads TRIAGED issues, explores the codebase via LLM analysis, appends
a structured research assessment to the issue body, and moves them to
RESEARCHED. Falls back to a minimal stub when LLM is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.llm.client import invoke, resolve_model
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="role.researcher")

_MAX_SOURCE_TREE_LINES = 500
_MAX_FILE_LINES = 200

_EXCLUDED_PATTERNS = (
    "__pycache__/",
    ".pyc",
    "node_modules/",
    ".venv/",
    "venv/",
    ".egg-info/",
    ".git/",
)

_RESEARCH_PROMPT = """\
You are a senior software architect performing codebase research for a development task.
Your goal is to investigate the codebase and produce a structured research assessment
that a developer agent can use to implement the solution without making architectural
decisions or wasting time on discovery.

## Issue #{issue_id}: {title}

{body}

## Labels
{labels}

## Project Architecture and Conventions
{project_context}

## Source Tree
{source_tree}

## Research Objectives
1. Identify all files that need to be modified, created, or deleted
2. Determine if data model changes are required (DB models, Pydantic schemas, migrations)
3. Identify API changes (new endpoints, modified signatures, schema changes)
4. Find existing code to reuse (utilities, patterns, base classes) -- reference specific functions/classes
5. Anticipate edge cases and failure modes not obvious from the issue description
6. Design a step-by-step implementation approach (3-5 steps)
7. Note any UI implications (templates, components, user-facing behavior)
8. Estimate implementation complexity

## Critical Rules
- Reference SPECIFIC file paths from the source tree above, not hypothetical paths
- For each affected file, explain WHAT changes are needed and WHY
- Identify existing patterns in the codebase that the implementation should follow
- If this is an infrastructure/config-only issue with no codebase changes, say so explicitly
- Be concrete: reference actual function names, class names, module names from the source tree

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text):
{{
    "affected_files": [
        {{
            "path": "path/to/file.py",
            "action": "modify",
            "reason": "What needs to change and why"
        }}
    ],
    "data_model_changes": "Description of schema/model changes, or null if none needed",
    "api_changes": "Description of endpoint/API changes, or null if none needed",
    "dependencies": [
        {{
            "path": "path/to/existing/code.py",
            "what": "Function or class to reuse",
            "how": "How to apply it in this implementation"
        }}
    ],
    "edge_cases": ["edge case 1", "edge case 2"],
    "suggested_approach": ["Step 1: ...", "Step 2: ...", "Step 3: ..."],
    "ui_notes": "UI implications and changes, or null if none",
    "estimated_complexity": "trivial" | "simple" | "moderate" | "complex" | "epic",
    "assessment": {{
        "suitability": "ready" | "needs_spec" | "needs_research" | "human_only",
        "confidence": 0.0-1.0,
        "reasoning": "One paragraph summary of findings"
    }}
}}
"""


def _gather_project_context(project_dir: Path) -> str:
    """Read project documentation files for research context."""
    files = [
        ("CLAUDE.md", project_dir / "CLAUDE.md"),
        ("AGENTS.md", project_dir / "AGENTS.md"),
        ("Architecture Rules", project_dir / ".claude" / "rules" / "architecture.md"),
        ("Agent Memory Cookbook", project_dir / ".claude" / "agent-memory" / "cookbook.md"),
    ]

    sections: list[str] = []
    for label, path in files:
        try:
            if path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
                content = "\n".join(lines[:_MAX_FILE_LINES])
                if len(lines) > _MAX_FILE_LINES:
                    content += f"\n\n(... truncated, {len(lines) - _MAX_FILE_LINES} lines omitted)"
                sections.append(f"### {label}\n\n{content}")
        except OSError:
            log.warning("researcher.read_error", path=str(path), exc_info=True)

    return "\n\n".join(sections)


async def _get_source_tree(project_dir: Path) -> str:
    """Get a filtered source tree listing from git."""
    try:
        result = await run("git", "ls-files", cwd=project_dir, timeout=30)
        if not result.success:
            return ""
    except Exception:
        return ""

    lines = [
        line
        for line in result.stdout.strip().splitlines()
        if line and not any(pat in line for pat in _EXCLUDED_PATTERNS)
    ]

    if len(lines) > _MAX_SOURCE_TREE_LINES:
        truncated = lines[:_MAX_SOURCE_TREE_LINES]
        truncated.append(f"(... {len(lines) - _MAX_SOURCE_TREE_LINES} more files)")
        return "\n".join(truncated)

    return "\n".join(lines)


def _parse_research_response(text: str) -> dict | None:
    """Parse Claude's JSON research response."""
    cleaned = text.strip()

    # Strip markdown fencing if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        end_idx = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end_idx = i
                break
        cleaned = "\n".join(lines[1:end_idx])

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try extracting JSON from surrounding text
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                log.warning("researcher.parse_failed", reason="no valid JSON found")
                return None
        else:
            log.warning("researcher.parse_failed", reason="no JSON boundaries found")
            return None

    required_keys = {"affected_files", "suggested_approach", "estimated_complexity"}
    if not required_keys.issubset(data.keys()):
        missing = required_keys - data.keys()
        log.warning("researcher.parse_failed", reason=f"missing keys: {missing}")
        return None

    return data


def _build_research_section(task: Task, data: dict) -> str:
    """Build the ## Research markdown section from parsed LLM response."""
    assessment = data.get("assessment", {})
    confidence = assessment.get("confidence", 0.0)
    reasoning = assessment.get("reasoning", "")
    complexity = data.get("estimated_complexity", "moderate")

    parts: list[str] = [
        "## Research\n",
        f"**Issue**: {task.title}",
        f"**Complexity**: {complexity}",
        f"**Confidence**: {confidence:.0%}",
    ]

    if reasoning:
        parts.append(f"\n{reasoning}")

    # Affected Files
    affected = data.get("affected_files", [])
    if affected:
        parts.append("\n### Affected Files\n")
        for f in affected:
            action = f.get("action", "modify")
            path = f.get("path", "unknown")
            reason = f.get("reason", "")
            parts.append(f"- `{path}` ({action}): {reason}")

    # Data Model Changes
    dm = data.get("data_model_changes")
    if dm:
        parts.append(f"\n### Data Model Changes\n\n{dm}")

    # API Changes
    api = data.get("api_changes")
    if api:
        parts.append(f"\n### API Changes\n\n{api}")

    # Dependencies
    deps = data.get("dependencies", [])
    if deps:
        parts.append("\n### Dependencies\n")
        for d in deps:
            path = d.get("path", "")
            what = d.get("what", "")
            how = d.get("how", "")
            parts.append(f"- `{path}` -- {what}: {how}")

    # Edge Cases
    edges = data.get("edge_cases", [])
    if edges:
        parts.append("\n### Edge Cases\n")
        for e in edges:
            parts.append(f"- {e}")

    # Suggested Approach
    approach = data.get("suggested_approach", [])
    if approach:
        parts.append("\n### Suggested Approach\n")
        for i, step in enumerate(approach, 1):
            # Strip leading "Step N:" if present to avoid duplication
            step_text = step
            if step.lower().startswith("step ") and ":" in step:
                step_text = step.split(":", 1)[1].strip()
            parts.append(f"{i}. {step_text}")

    # UI Notes
    ui = data.get("ui_notes")
    if ui:
        parts.append(f"\n### UI Notes\n\n{ui}")

    return "\n".join(parts)


class ResearcherRole(AgentRole):
    """Investigate triaged issues via LLM codebase analysis.

    Gathers project context (docs, architecture rules, source tree),
    sends a structured prompt to Claude, and appends a research assessment
    to the issue body. Falls back to a minimal stub when the LLM is
    unavailable or returns unparseable output.
    """

    name = "researcher"
    description = "Investigate triaged issues and prepare them for development"
    allowed_input_states = frozenset({TaskState.TRIAGED})
    output_state = TaskState.RESEARCHED

    async def assess_task(self, task: Task) -> TaskAssessment:
        """Assess research suitability using heuristics."""
        if not task.body or not task.body.strip():
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.8,
                reasoning="Issue has no description; needs specification before research.",
                missing_context=["description", "acceptance criteria"],
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        if task.labels and "agent:human-only" in task.labels:
            return TaskAssessment(
                suitability="human_only",
                confidence=0.85,
                reasoning="Issue is marked as human-only.",
                estimated_complexity="complex",
                suggested_role="researcher",
            )

        if "## Research" in task.body:
            return TaskAssessment(
                suitability="ready",
                confidence=0.9,
                reasoning="Issue already has a research section; ready for development.",
                estimated_complexity="moderate",
                suggested_role="developer",
            )

        return TaskAssessment(
            suitability="needs_research",
            confidence=0.6,
            reasoning="Task needs codebase exploration before development can begin.",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for research",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        log.info("researcher.start", issue=ctx.issue_number)

        research_section = await self._research_with_llm(task, ctx)
        if research_section is None:
            log.warning("researcher.llm_unavailable", issue=ctx.issue_number, msg="Falling back to stub research")
            research_section = self._build_stub_research(task)

        updated_body = (task.body or "").rstrip() + "\n\n" + research_section
        await ctx.adapter.edit_body(ctx.issue_number, updated_body)
        await ctx.adapter.transition_state(ctx.issue_number, TaskState.RESEARCHED)

        log.info("researcher.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} researched",
            output_state=TaskState.RESEARCHED,
        )

    async def _research_with_llm(self, task: Task, ctx: ExecutionContext) -> str | None:
        """Perform LLM-powered codebase research.

        Returns the formatted research section on success, None on failure.
        """
        try:
            project_context = _gather_project_context(ctx.project_dir)
            source_tree = await _get_source_tree(ctx.project_dir)

            model = resolve_model("researcher", ctx.config.roles)
            prompt = _RESEARCH_PROMPT.format(
                issue_id=task.id,
                title=task.title,
                body=task.body or "(no description)",
                labels=", ".join(task.labels) if task.labels else "none",
                project_context=project_context or "(no project documentation found)",
                source_tree=source_tree or "(source tree unavailable)",
            )

            result = await invoke(
                prompt,
                model=model,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 5,
                timeout=300,
            )
            ctx.add_cost(result.cost_usd)

            data = _parse_research_response(result.text)
            if data is None:
                return None

            return _build_research_section(task, data)

        except Exception as exc:
            log.warning("researcher.llm_failed", error=str(exc), exc_info=True)
            return None

    def _build_stub_research(self, task: Task) -> str:
        """Build a minimal research section when LLM is unavailable."""
        return (
            f"## Research\n\n"
            f"**Issue**: {task.title}\n"
            f"**Note**: Automated research unavailable. Manual review recommended before development.\n\n"
            f"Issue has been researched and is ready for development."
        )
