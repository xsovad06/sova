"""Memory extraction infrastructure for agent runs.

Automatic LLM-based extraction is disabled (no-op). The infrastructure
functions (_build_extraction_prompt, _parse_extraction_response,
_deduplicate_and_store) are retained for future rule-based extraction.
Use ``/extract-knowledge`` for human-reviewed knowledge capture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sova.db.models import Memory
from sova.knowledge import memory
from sova.knowledge.embeddings import embed_text
from sova.knowledge.similarity import parse_confirmation_counter, set_confirmation_counter, titles_match
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.extraction")

_VALID_CATEGORIES = frozenset({"learning", "review_pattern", "common_mistake", "task_insight"})
_PROMOTION_THRESHOLD = 3


@dataclass
class ExtractedMemory:
    """A single learning extracted by the LLM."""

    category: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """Summary of an extraction run."""

    memories_stored: int = 0
    memories_skipped: int = 0
    memories_confirmed: int = 0
    cost_usd: Decimal = Decimal("0")
    error: str | None = None


async def extract_memories(  # noqa: RUF029 -- async retained for caller compatibility
    *,
    role: str,
    issue_number: str,
    repo: str,
    task_title: str,
    files_changed: list[str],
    step_summaries: list[str],
    review_findings: list[dict] | None = None,
    spec_content: str | None = None,
    cwd: Path | str,
) -> ExtractionResult:
    """No-op: automatic memory extraction is disabled.

    LLM-based extraction had low signal-to-noise. Use the human-reviewed
    ``/extract-knowledge`` command instead. The step slot is kept in
    pipelines so future rule-based extraction is a single-file change.

    All parameters are retained so re-enabling extraction is a single-file change.
    """
    # Reference params to satisfy static analysis; they're retained for future use
    _ = (repo, task_title, files_changed, step_summaries, review_findings, spec_content, cwd)
    log.info("extraction.skipped_noop", role=role, issue=issue_number)
    return ExtractionResult()


def _build_extraction_prompt(
    *,
    role: str,
    task_title: str,
    files_changed: list[str],
    step_summaries: list[str],
    review_findings: list[dict] | None = None,
    spec_content: str | None = None,
) -> str:
    """Build the LLM prompt for knowledge extraction."""
    files_section = "\n".join(f"- {f}" for f in files_changed[:30]) if files_changed else "- (none)"
    steps_section = "\n".join(f"- {s}" for s in step_summaries) if step_summaries else "- (none)"

    spec_section = ""
    if spec_content:
        spec_section = f"\n\n## Spec Decision Chain\n{spec_content}"

    findings_section = ""
    if review_findings:
        findings_lines = []
        for f in review_findings[:20]:
            loc = f"{f.get('file', '?')}:{f.get('line', '?')}"
            findings_lines.append(
                f"- [{f.get('severity', '?')}/10] [{f.get('category', '?')}] {loc}: {f.get('description', '')}"
            )
        findings_section = f"\n\n## Review Findings\n{chr(10).join(findings_lines)}"

    return f"""You are a knowledge extraction assistant for an autonomous software development agent. \
Analyze the following completed agent run and extract 0-5 reusable learnings.

## Run Context
- Role: {role}
- Task: {task_title}
- Files changed:
{files_section}
- Pipeline steps:
{steps_section}{spec_section}{findings_section}

## Categories
- learning: Framework, ORM, library, or domain patterns discovered during development
- review_pattern: Code quality patterns worth applying to future PRs
- common_mistake: Errors encountered and corrected that should be checked pre-emptively
- task_insight: Codebase structure, complexity, or approach insights for future similar tasks

## Rules
- Return ONLY learnings that are reusable across future tasks in this project
- Skip routine operations (commit, push, CI pass) unless something novel happened
- Each learning must be actionable -- a future agent reading it should know exactly what to do
- Include the WHY, not just the WHAT
- Do NOT include task-specific details (issue numbers, branch names, PR numbers)
- Return an empty array if nothing novel was discovered -- most routine runs produce zero learnings

## Output Format
Return ONLY a JSON array (no markdown fences, no extra text):
[
  {{
    "category": "learning|review_pattern|common_mistake|task_insight",
    "title": "Short descriptive title (max 100 chars)",
    "content": "Actionable description of the pattern and why it matters",
    "tags": ["relevant", "tags"]
  }}
]

Return [] if nothing worth remembering."""


def _parse_extraction_response(text: str) -> list[ExtractedMemory]:
    """Parse LLM JSON response into ExtractedMemory objects."""
    text = text.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                log.warning("extraction.parse_failed", text_preview=text[:200])
                return []
        else:
            log.warning("extraction.parse_failed", text_preview=text[:200])
            return []

    if not isinstance(data, list):
        log.warning("extraction.not_array", type=type(data).__name__)
        return []

    memories = []
    for item in data:
        if not isinstance(item, dict):
            continue

        category = item.get("category", "learning")
        if category not in _VALID_CATEGORIES:
            category = "learning"

        title = item.get("title", "").strip()
        content = item.get("content", "").strip()
        if not title or not content:
            continue

        memories.append(
            ExtractedMemory(
                category=category,
                title=title[:200],
                content=content,
                tags=item.get("tags", []),
            )
        )

    return memories[:5]


async def _deduplicate_and_store(
    mem: ExtractedMemory,
    *,
    repo: str,
    issue_number: str,
) -> str:
    """Check for duplicates, bump confirmation counters, or store new.

    Uses embedding-based similarity when available, falls back to title substring matching.
    Returns "stored", "confirmed", or "skipped".
    """
    # Compute embedding once, reuse for both dedup search and storage
    match_text = f"{mem.title} {mem.content}"
    embedding = embed_text(match_text)
    similar = await memory.find_similar(match_text, category=mem.category, query_embedding=embedding)

    if similar:
        existing_mem, _score = similar[0]
        return await _confirm_existing(existing_mem)

    # Lexical fallback: title substring match catches duplicates that
    # semantic search misses (different content but identical titles)
    existing = await memory.search(category=mem.category, query=mem.title[:50], limit=20)
    for existing_mem in existing:
        if titles_match(existing_mem.title, mem.title):
            return await _confirm_existing(existing_mem)

    content_with_counter = f"{mem.content}\n\n[confirmed: 0]"
    await memory.store(
        category=mem.category,
        title=mem.title,
        content=content_with_counter,
        tags=mem.tags,
        repo=repo,
        issue_number=issue_number,
        embedding=embedding,
    )
    return "stored"


async def _confirm_existing(existing_mem: "Memory") -> str:
    """Bump the confirmation counter on an existing memory."""
    counter = parse_confirmation_counter(existing_mem.content)
    new_counter = counter + 1

    new_content = set_confirmation_counter(existing_mem.content, new_counter)
    await memory.update(existing_mem.id, content=new_content)

    if new_counter >= _PROMOTION_THRESHOLD:
        await memory.promote(existing_mem.id, "shared")
        log.info("extraction.promoted", memory_id=existing_mem.id, counter=new_counter)

    return "confirmed"
