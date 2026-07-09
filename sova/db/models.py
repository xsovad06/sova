"""SQLAlchemy ORM models for SOVA."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


class Base(DeclarativeBase):
    """Base class for all ORM models."""


_FK_TASK_RUNS_ID = "task_runs.id"
_CASCADE_ALL_DELETE_ORPHAN = "all, delete-orphan"


class TaskRun(Base):
    """Audit trail for every task execution."""

    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)
    run_label: Mapped[str] = mapped_column(String(200), default="")
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

    step_executions: Mapped[list["StepExecution"]] = relationship(back_populates="task_run")
    failure_records: Mapped[list["FailureRecord"]] = relationship(back_populates="task_run")
    cost_records: Mapped[list["CostRecord"]] = relationship(back_populates="task_run")
    output_lines: Mapped[list["OutputLine"]] = relationship(
        back_populates="task_run", cascade=_CASCADE_ALL_DELETE_ORPHAN
    )
    resource_samples: Mapped[list["ResourceSampleRecord"]] = relationship(
        back_populates="task_run", cascade=_CASCADE_ALL_DELETE_ORPHAN
    )
    resource_summary: Mapped["ResourceSummaryRecord | None"] = relationship(
        back_populates="task_run", cascade=_CASCADE_ALL_DELETE_ORPHAN, uselist=False
    )

    @validates("issue_number")
    def _normalize_issue_number(self, _key: str, value: str | None) -> str | None:
        """Strip '#' prefix so '#67' and '67' are stored consistently."""
        if value is None:
            return None
        return value.lstrip("#").strip() if value else value

    lifecycle_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("issue_lifecycles.id"), index=True)
    workflow_definition_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workflow_definitions.id"), index=True
    )

    __table_args__ = (
        Index("ix_task_runs_issue", "issue_number"),
        Index("ix_task_runs_status", "status"),
        Index("ix_task_runs_project", "project_slug"),
    )


class OutputLine(Base):
    """Individual output line persisted from an agent run."""

    __tablename__ = "output_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    task_run: Mapped["TaskRun"] = relationship(back_populates="output_lines")

    __table_args__ = (Index("ix_output_lines_run_lineno", "task_run_id", "line_number"),)


class StepExecution(Base):
    """Record of each step execution within a task run."""

    __tablename__ = "step_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    output_summary: Mapped[str | None] = mapped_column(Text)
    gate_check_result: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task_run: Mapped["TaskRun"] = relationship(back_populates="step_executions")


class FailureRecord(Base):
    """Every failure with full context for observability."""

    __tablename__ = "failure_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(50), nullable=False)
    failure_type: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    task_run: Mapped["TaskRun"] = relationship(back_populates="failure_records")


class CostRecord(Base):
    """Individual LLM invocation cost."""

    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), index=True)
    phase: Mapped[str] = mapped_column(String(50), nullable=False)
    issue: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    model_selection_reason: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    task_run: Mapped["TaskRun | None"] = relationship(back_populates="cost_records")


_MEMORIES_ID_FK = "memories.id"


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
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    superseded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey(_MEMORIES_ID_FK))
    retrieval_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    health_score: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_memories_category", "category"),
        Index("ix_memories_tags", "tags"),
        Index("ix_memories_superseded_by", "superseded_by"),
        Index("ix_memories_archived", "archived"),
    )


class MemoryEdge(Base):
    """Directed relationship between two memories."""

    __tablename__ = "memory_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey(_MEMORIES_ID_FK, ondelete="CASCADE"), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, ForeignKey(_MEMORIES_ID_FK, ondelete="CASCADE"), nullable=False)
    relation: Mapped[str] = mapped_column(String(30), nullable=False, default="relates_to")
    weight: Mapped[float] = mapped_column(Numeric(5, 4), default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation", name="uq_memory_edge"),
        CheckConstraint(
            "relation IN ('relates_to', 'refines', 'depends_on', 'supersedes', 'contradicts')",
            name="ck_memory_edge_relation",
        ),
        CheckConstraint("weight >= 0.0 AND weight <= 1.0", name="ck_memory_edge_weight"),
        Index("ix_memory_edges_source", "source_id"),
        Index("ix_memory_edges_target", "target_id"),
    )


class TaskAssessmentRecord(Base):
    """Stored assessment for backlog issues."""

    __tablename__ = "task_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[str] = mapped_column(String(50), nullable=False)
    project_slug: Mapped[str] = mapped_column(String(100), default="")
    suitability: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    missing_context: Mapped[list] = mapped_column(JSON, default=list)
    estimated_complexity: Mapped[str] = mapped_column(String(20), default="moderate")
    suggested_role: Mapped[str] = mapped_column(String(50), default="developer")
    sub_tasks: Mapped[list] = mapped_column(JSON, default=list)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_assessments_issue", "issue_number"),
        Index("ix_assessments_suitability", "suitability"),
        Index("ix_assessments_project_slug", "project_slug"),
    )


class IssueLifecycle(Base):
    """Spine connecting all TaskRuns for a single issue journey."""

    __tablename__ = "issue_lifecycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[str] = mapped_column(String(50), nullable=False)
    project_slug: Mapped[str] = mapped_column(String(100), default="")
    current_phase: Mapped[str] = mapped_column(String(30), nullable=False, default="development")
    phase_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    pr_number: Mapped[int | None] = mapped_column(Integer)
    branch_name: Mapped[str] = mapped_column(String(200), default="")
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    phases: Mapped[list["LifecyclePhaseRecord"]] = relationship(
        back_populates="lifecycle", order_by="LifecyclePhaseRecord.id"
    )

    @validates("issue_number")
    def _normalize_issue_number(self, _key: str, value: str) -> str:
        return value.lstrip("#").strip() if value else value

    __table_args__ = (
        Index("ix_issue_lifecycles_issue", "issue_number"),
        Index("ix_issue_lifecycles_project", "project_slug"),
    )


class LifecyclePhaseRecord(Base):
    """Tracks each phase execution within a lifecycle."""

    __tablename__ = "lifecycle_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lifecycle_id: Mapped[int] = mapped_column(Integer, ForeignKey("issue_lifecycles.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    task_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lifecycle: Mapped["IssueLifecycle"] = relationship(back_populates="phases")

    __table_args__ = (Index("ix_lifecycle_phases_lifecycle_phase", "lifecycle_id", "phase"),)


class WorkflowDefinition(Base):
    """A saved command DAG defining a custom role."""

    __tablename__ = "workflow_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    graph_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_states: Mapped[list] = mapped_column(JSON, default=list)
    output_state: Mapped[str] = mapped_column(String(50), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (Index("ix_workflow_definitions_name", "name"),)


class CommandContract(Base):
    """Input/output schema for a command (enables DAG validation)."""

    __tablename__ = "command_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0.0)
    estimated_duration_s: Mapped[int] = mapped_column(Integer, default=60)
    idempotent: Mapped[bool] = mapped_column(Boolean, default=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_command_contracts_name", "command_name"),)


class CodeRabbitEvent(Base):
    """Tracks CodeRabbit review events for rate-limit quota tracking."""

    __tablename__ = "coderabbit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    review_id: Mapped[str] = mapped_column(String(100), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    project_slug: Mapped[str] = mapped_column(String(100), default="")

    __table_args__ = (
        UniqueConstraint("review_id", "project_slug", name="uq_coderabbit_event_review"),
        Index("ix_coderabbit_events_recorded", "recorded_at"),
        Index("ix_coderabbit_events_project", "project_slug"),
    )


class ResourceSampleRecord(Base):
    """Time-series resource measurement for an agent run."""

    __tablename__ = "resource_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID, ondelete="CASCADE"), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_percent: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    memory_rss_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_vms_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    io_read_bytes: Mapped[int | None] = mapped_column(Integer)
    io_write_bytes: Mapped[int | None] = mapped_column(Integer)
    num_children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    num_threads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task_run: Mapped["TaskRun"] = relationship(back_populates="resource_samples")

    __table_args__ = (Index("ix_resource_samples_run_time", "task_run_id", "sampled_at"),)


class ResourceSummaryRecord(Base):
    """Aggregated resource summary for an agent run (one per TaskRun)."""

    __tablename__ = "resource_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_TASK_RUNS_ID, ondelete="CASCADE"), nullable=False, unique=True
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peak_cpu_percent: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0.0)
    avg_cpu_percent: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0.0)
    peak_memory_rss_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peak_memory_vms_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_io_read_bytes: Mapped[int | None] = mapped_column(Integer)
    total_io_write_bytes: Mapped[int | None] = mapped_column(Integer)
    peak_num_threads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    task_run: Mapped["TaskRun"] = relationship(back_populates="resource_summary")


class PRQueueStatus(StrEnum):
    """Status values for PR creation queue entries."""

    PENDING = "pending"
    CREATING = "creating"
    CREATED = "created"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PRCreationQueue(Base):
    """Queue for throttled PR creation behind CodeRabbit quota."""

    __tablename__ = "pr_creation_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_TASK_RUNS_ID), nullable=False)
    issue_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    base_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    head_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    repo: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    github_user: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    error_message: Mapped[str | None] = mapped_column(Text)
    project_slug: Mapped[str] = mapped_column(String(100), default="")
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_pr_queue_status", "status"),
        Index("ix_pr_queue_task_run", "task_run_id"),
        Index("ix_pr_queue_enqueued", "enqueued_at"),
        Index("ix_pr_queue_project_status_enqueued", "project_slug", "status", "enqueued_at"),
    )
