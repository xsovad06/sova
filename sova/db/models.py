"""SQLAlchemy ORM models for SOVA."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class TaskRun(Base):
    """Audit trail for every task execution."""

    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[str] = mapped_column(String(50), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="developer")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    current_step: Mapped[str | None] = mapped_column(String(50))
    branch_name: Mapped[str] = mapped_column(String(200), default="")
    worktree_path: Mapped[str] = mapped_column(String(500), default="")
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pid: Mapped[int | None] = mapped_column(Integer)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    project_slug: Mapped[str] = mapped_column(String(100), default="")
    assessment_json: Mapped[dict | None] = mapped_column(JSON)
    handoff_json: Mapped[dict | None] = mapped_column(JSON)
    resumed_from_id: Mapped[int | None] = mapped_column(Integer)
    output_file_path: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("issue_number")
    def _normalize_issue_number(self, _key: str, value: str) -> str:
        """Strip '#' prefix so '#67' and '67' are stored consistently."""
        return value.lstrip("#").strip() if value else value

    __table_args__ = (
        Index("ix_task_runs_issue", "issue_number"),
        Index("ix_task_runs_status", "status"),
        Index("ix_task_runs_project", "project_slug"),
    )


class StepExecution(Base):
    """Record of each step execution within a task run."""

    __tablename__ = "step_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    output_summary: Mapped[str | None] = mapped_column(Text)
    gate_check_result: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FailureRecord(Base):
    """Every failure with full context for observability."""

    __tablename__ = "failure_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)
    failure_type: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CostRecord(Base):
    """Individual LLM invocation cost."""

    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    phase: Mapped[str] = mapped_column(String(50), nullable=False)
    issue: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Memory(Base):
    """Agent memory entries."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="learning")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(String(500), default="")
    repo: Mapped[str] = mapped_column(String(200), default="")
    issue_number: Mapped[str] = mapped_column(String(50), default="")
    tier: Mapped[str] = mapped_column(String(20), default="project")
    superseded_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_memories_category", "category"),
        Index("ix_memories_tags", "tags"),
    )


class TaskAssessmentRecord(Base):
    """Stored assessment for backlog issues."""

    __tablename__ = "task_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[str] = mapped_column(String(50), nullable=False)
    project_slug: Mapped[str] = mapped_column(String(100), default="")
    suitability: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    missing_context: Mapped[list] = mapped_column(JSON, default=list)
    estimated_complexity: Mapped[str] = mapped_column(String(20), default="moderate")
    suggested_role: Mapped[str] = mapped_column(String(50), default="developer")
    sub_tasks: Mapped[list] = mapped_column(JSON, default=list)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_assessments_issue", "issue_number"),
        Index("ix_assessments_suitability", "suitability"),
    )
