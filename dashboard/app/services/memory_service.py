"""Query memory.db (SQLite FTS5) and read markdown memory files."""

import sqlite3

import markdown

from app import config


def search(query: str, limit: int = 20) -> list[dict]:
    """Full-text search on memory.db using FTS5 MATCH."""
    db = config.MEMORY_DB
    if not db.exists() or db.stat().st_size == 0:
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            safe_q = query.replace('"', " ").replace("*", " ").replace("(", " ").replace(")", " ")
            safe_q = safe_q.replace("^", " ").replace(":", " ").replace("-", " ")
            safe_q = f'"{safe_q.strip()}"'
            rows = conn.execute(
                "SELECT id, content, tags, created_at, updated_at FROM memories WHERE memories_fts MATCH ? LIMIT ?",
                (safe_q, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return []


def get_tags() -> list[str]:
    """Get distinct tags from memory.db."""
    db = config.MEMORY_DB
    if not db.exists() or db.stat().st_size == 0:
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT DISTINCT tags FROM memories WHERE tags != ''").fetchall()
            tags = set()
            for row in rows:
                for tag in row[0].split(","):
                    tag = tag.strip()
                    if tag:
                        tags.add(tag)
            return sorted(tags)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return []


def get_memory_count() -> int:
    """Count entries in memory.db."""
    db = config.MEMORY_DB
    if not db.exists() or db.stat().st_size == 0:
        return 0
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return 0


def get_markdown_file(name: str) -> dict | None:
    """Read and render a markdown memory file."""
    path = config.MARKDOWN_FILES.get(name)
    if not path or not path.exists():
        return None
    raw = path.read_text()
    html = markdown.markdown(raw, extensions=["tables", "fenced_code"])
    return {"name": name, "raw": raw, "html": html, "lines": len(raw.splitlines())}


def list_markdown_files() -> list[dict]:
    """List available markdown memory files with line counts."""
    result = []
    for name, path in config.MARKDOWN_FILES.items():
        if path.exists():
            lines = len(path.read_text().splitlines())
            result.append({"name": name, "lines": lines})
    return result
