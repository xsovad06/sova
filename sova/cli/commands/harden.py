"""CLI command: sova harden -- enrich issues with context, criteria, and re-triage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.adapters import create_adapter
from sova.adapters.base import Task, TaskFilters, TaskState
from sova.config.loader import load_config
from sova.roles.triage import TriageRole

console = Console(stderr=True)

# Max lines to read from each project doc file.
_MAX_DOC_LINES = 500


def harden(
    issue: Annotated[Optional[str], typer.Argument(help="Issue number to harden (all open if omitted).")] = None,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without posting to GitHub.")] = False,
    skip_triage: Annotated[bool, typer.Option("--skip-triage", help="Skip re-triage after hardening.")] = False,
) -> None:
    """Enrich issues with context, acceptance criteria, and technical approach."""
    asyncio.run(_harden(issue=issue, project_dir=project, dry_run=dry_run, skip_triage=skip_triage))


async def _harden(
    *,
    issue: str | None,
    project_dir: Path | None,
    dry_run: bool,
    skip_triage: bool,
) -> None:
    from sova.db.session import init_db
    from sova.llm.client import invoke

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo)

    # Fetch all open issues once (used for both target selection and conflict analysis).
    all_open = await adapter.list_tasks(TaskFilters(state="open"))

    if issue:
        tasks = [await adapter.get_task(issue)]
    else:
        tasks = [t for t in all_open if t.state in (TaskState.BACKLOG, TaskState.TRIAGED, TaskState.NEEDS_SPEC)]

    if not tasks:
        console.print("[yellow]No issues found to harden.[/yellow]")
        return

    console.print(f"[bold]Hardening {len(tasks)} issue(s)...[/bold]\n")

    # Load shared context once for the batch.
    project_docs = _load_project_docs(resolved_dir)
    all_issues_summary = _format_issues_summary(all_open)

    results: list[tuple[Task, str, str | None]] = []  # (task, status, triage_verdict)

    for task in tasks:
        console.print(f"[cyan]Hardening #{task.id}: {task.title}[/cyan]")

        issue_type = _detect_issue_type(task.labels)
        template_content = _load_issue_template(resolved_dir, issue_type)
        prompt = _build_harden_prompt(task, project_docs, all_issues_summary, template_content, issue_type)

        try:
            result = await invoke(prompt, cwd=resolved_dir, timeout=300)
        except RuntimeError as exc:
            console.print(f"[red]  Failed: {exc}[/red]")
            results.append((task, "failed", None))
            continue

        enriched_body = _strip_code_fences(result.text)

        if not enriched_body.strip():
            console.print("[red]  Claude returned empty analysis.[/red]")
            results.append((task, "empty", None))
            continue

        if dry_run:
            console.print(f"\n[bold]--- DRY RUN: #{task.id} ---[/bold]\n")
            console.print(enriched_body)
            console.print("\n[bold]--- END DRY RUN ---[/bold]\n")
            results.append((task, "dry-run", None))
            continue

        # Update issue body
        await adapter.edit_body(task.id, enriched_body)

        # Post a brief comment noting the hardening
        await adapter.post_comment(
            task.id,
            "Issue hardened by SOVA (body updated with enriched requirements, "
            "acceptance criteria, and technical approach).",
        )
        console.print(f"[green]  Updated issue #{task.id} body.[/green]")

        # Re-triage with enriched content
        triage_verdict = None
        if not skip_triage:
            try:
                enriched_task = replace(task, body=enriched_body)
                role = TriageRole()
                assessment = await role.assess_task(enriched_task)
                triage_verdict = assessment.suitability

                label = role.SUITABILITY_LABELS[assessment.suitability]
                await adapter.add_label(task.id, label)
                console.print(f"[green]  Re-triaged: {triage_verdict}[/green]")
            except Exception as exc:
                console.print(f"[yellow]  Re-triage failed: {exc}[/yellow]")

        results.append((task, "hardened", triage_verdict))

    # Summary table
    table = Table(title="Harden Results", show_header=True)
    table.add_column("Issue", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Status", style="green")
    table.add_column("Triage", style="yellow")

    for task, status, verdict in results:
        status_style = {"hardened": "green", "dry-run": "blue", "failed": "red", "empty": "red"}.get(status, "white")
        table.add_row(
            f"#{task.id}",
            task.title[:50],
            f"[{status_style}]{status}[/{status_style}]",
            verdict or "-",
        )

    console.print(table)
    console.print(f"\n[bold]Processed {len(results)} issue(s).[/bold]")


def _load_project_docs(project_dir: Path) -> str:
    """Load project vision/strategy docs and issue templates for context."""
    docs: list[str] = []

    doc_patterns = [
        "docs/vision*.md",
        "docs/VISION*.md",
        "docs/strategy*.md",
        "docs/STRATEGY*.md",
        "docs/roadmap*.md",
        "docs/ROADMAP*.md",
        "docs/plan*.md",
        "docs/PLAN*.md",
        "VISION.md",
        "ROADMAP.md",
        "PLAN.md",
        "STRATEGY.md",
        "docs/architecture*.md",
    ]

    for pattern in doc_patterns:
        for f in sorted(project_dir.glob(pattern)):
            if not f.is_file():
                continue
            lines = f.read_text(errors="replace").splitlines()[:_MAX_DOC_LINES]
            docs.append(f"--- {f.name} ---\n" + "\n".join(lines))

    # Issue templates
    template_dir = project_dir / ".github" / "ISSUE_TEMPLATE"
    if template_dir.is_dir():
        for f in sorted(template_dir.iterdir()):
            if f.suffix in (".md", ".yml"):
                docs.append(f"--- Issue Template: {f.name} ---\n" + f.read_text(errors="replace"))

    return "\n\n".join(docs)


def _detect_issue_type(labels: list[str]) -> str:
    """Detect issue type from labels. Returns 'feature', 'bug', 'task', or 'feature' as default."""
    for label in labels:
        normalized = label.lower().replace(" ", "").replace(":", "")
        if "bug" in normalized and "type" in normalized:
            return "bug"
        if "task" in normalized and "type" in normalized:
            return "task"
        if "feature" in normalized and "type" in normalized:
            return "feature"
    return "feature"


def _load_issue_template(project_dir: Path, issue_type: str) -> str:
    """Load the issue template for the given type, stripping YAML frontmatter."""
    template_path = project_dir / ".github" / "ISSUE_TEMPLATE" / f"{issue_type}.md"
    if not template_path.is_file():
        return ""

    content = template_path.read_text(errors="replace")

    # Strip YAML frontmatter (--- ... ---)
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()

    return content


def _format_issues_summary(tasks: list[Task]) -> str:
    """Format a list of tasks into a compact summary for the prompt."""
    lines = []
    for t in tasks:
        labels_str = ", ".join(t.labels) if t.labels else "none"
        lines.append(f"- #{t.id}: {t.title} [labels: {labels_str}]")
    return "\n".join(lines) if lines else "(no open issues)"


def _build_harden_prompt(
    task: Task,
    project_docs: str,
    all_issues_summary: str,
    template_content: str,
    issue_type: str,
) -> str:
    """Build the Claude prompt for issue hardening."""
    labels_str = ", ".join(task.labels) if task.labels else "none"

    template_instruction = ""
    if template_content:
        template_instruction = f"""
The issue is of type "{issue_type}". Structure your output to match the following issue template.
Use the template's sections as your output skeleton, filling each section with enriched content.
Strip the HTML comments from the template -- replace them with actual content.

--- Target Template ---
{template_content}
--- End Template ---
"""
    else:
        template_instruction = """
Use the following section structure for the output:

## Objective
## Detailed Description
## Technical Approach
## Acceptance Criteria
## Files / Modules to Change
## Out of Scope / Constraints
## References
"""

    return f"""You are analyzing GitHub issue #{task.id} to prepare it for autonomous agent development.

Issue #{task.id}: {task.title}
Labels: {labels_str}
Current body:
{task.body}

Project vision, strategy documents, and issue templates:
{project_docs or "No project vision/strategy documents found. Use your best judgment."}

All open issues in this project (for dependency/overlap analysis):
{all_issues_summary}

Your task: Produce an UPDATED issue body that an autonomous agent can pick up and implement
without ambiguity. The output replaces the current issue body entirely.
{template_instruction}
Follow these steps internally (do NOT include the step numbers in the output):

1. CONTEXT ENRICHMENT: Search the project vision/strategy docs for any details relevant to this issue.
   Extract specific requirements, design decisions, or constraints that should be reflected in the issue.

2. TECHNICAL APPROACH: Describe the concrete architecture and data flow for this feature/fix.
   What is the high-level design? How do the components interact? This eliminates ambiguity that
   would cause an agent to make wrong design decisions.

3. MODELS & SCHEMAS: If this issue involves new models, list concrete field names, types, and
   relationships. If it involves new API endpoints, specify URL patterns, HTTP methods, and
   request/response shapes. If it involves new services, specify function signatures.

4. ACCEPTANCE CRITERIA: Write clear, testable acceptance criteria as checkboxes.
   Each criterion should be independently verifiable. Be specific, not generic.
   Include "All existing tests pass (`make check`)" and "No new lint warnings (`make lint`)".
   If the issue already has good criteria, keep and improve them.

5. SCOPE ANALYSIS: Identify which files/components are affected. Estimate complexity (S/M/L/XL).
   List explicit dependencies on other issues.

6. CONFLICT CHECK: Review the list of all open issues. Flag any that overlap in scope with this
   issue (e.g., two issues that would create the same model or service). Note how to avoid
   duplicate work.

7. IMPLEMENTATION HINTS: Brief technical guidance for the developer/agent.
   Key files to look at, patterns to follow, gotchas to watch for.

Output the COMPLETE updated issue body in markdown. Do NOT wrap the output in a code block.
Do NOT include YAML frontmatter. Start directly with the first section heading."""


def _strip_code_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Remove first line (```markdown or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()
