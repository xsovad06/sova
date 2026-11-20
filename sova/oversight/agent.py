"""OversightAgent: background daemon that wakes on a configurable interval.

Records each wake cycle to the OversightRun DB table. The persona is loaded
fresh each cycle and available via ``get_system_prompt()`` for LLM context
injection. Each cycle runs the observation phase (#445) to collect a
cross-project health snapshot, the analysis phase (#446) sends the snapshot
to the LLM to produce structured findings, and the action phase (#447)
creates GitHub Issues from confirmed findings.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from sova.config.loader import load_config
from sova.config.models import OversightConfig, ProjectConfig
from sova.db.models import OversightFinding, OversightRun, OversightRunStatus
from sova.db.session import get_session
from sova.oversight.persona import load_persona
from sova.utils.logging import get_logger

log = get_logger(component="oversight.agent")


class OversightAgent:
    """Server-global background daemon for autonomous project monitoring."""

    def __init__(self, config: OversightConfig, *, project_dir: str | None = None) -> None:
        self._config = config
        self._project_dir = project_dir
        self._task: asyncio.Task | None = None
        self._cycle_number: int = 0
        self._persona: str = ""
        self._cycle_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

    def _reload_config(self) -> OversightConfig:
        """Re-read config so runtime SOVA config changes take effect."""
        from pathlib import Path

        try:
            project_dir = Path(self._project_dir) if self._project_dir else None
            cfg = load_config(project_dir)
            self._config = cfg.oversight
        except Exception:  # noqa: BLE001 (config reload keeps the previous config on any failure)
            log.warning("oversight.config_reload_failed", exc_info=True)
        return self._config

    def reload_config(self, cfg: ProjectConfig | None = None) -> OversightConfig:
        """Hot-reload config (called by settings router after TOML update).

        When *cfg* is provided, uses it directly instead of re-reading from
        disk. Falls back to disk re-read when called without arguments
        (backward compatibility).
        """
        if cfg is not None:
            self._config = cfg.oversight
        else:
            self._reload_config()
        self._wake_event.set()
        return self._config

    async def _interruptible_sleep(self, delay: float) -> None:
        """Sleep for *delay* seconds, but wake early if config is reloaded."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            self._wake_event.clear()

    def start(self) -> asyncio.Task:
        """Start the oversight background loop. Returns the task for cancellation."""
        self._task = asyncio.create_task(self._run_loop())
        log.info(
            "oversight.started",
            interval_minutes=self._config.wake_interval_minutes,
        )
        return self._task

    def get_system_prompt(self) -> str:
        """Return the current system prompt with persona context for LLM calls.

        The persona is loaded fresh each wake cycle (no caching) so edits to the
        persona file take effect immediately. Hooks (#445, #446, #447) call this
        method to inject persona guidance into their LLM requests.
        """
        if not self._persona:
            return ""
        return f"# Operations Persona (user-defined oversight guidance)\n\n{self._persona}\n\n---\n\n"

    async def stop(self) -> None:
        """Cancel the oversight background task."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            log.info("oversight.stopped")

    @staticmethod
    def _determine_outcome(
        snapshot: dict | None,
        analysis_error: str | None,
    ) -> tuple[OversightRunStatus, str | None]:
        """Determine the run status and error message from cycle results."""
        if snapshot is None:
            return OversightRunStatus.ERROR, "observation_failed"
        if analysis_error is not None:
            return OversightRunStatus.ERROR, analysis_error
        return OversightRunStatus.DONE, None

    async def _record_error_safe(
        self,
        run_id: str,
        cycle: int,
        duration_ms: int,
        started_at: datetime,
        error: str,
        snapshot: dict | None = None,
    ) -> None:
        """Record an error run, swallowing DB failures. *snapshot* is kept when observation completed."""
        try:
            await self._record_run(
                run_id,
                cycle,
                OversightRunStatus.ERROR,
                duration_ms,
                started_at=started_at,
                snapshot=snapshot,
                error=error,
            )
        except (SQLAlchemyError, OSError):
            log.warning("oversight.error_record_failed", exc_info=True)

    @property
    def enabled(self) -> bool:
        """Return True if the agent's current config has oversight enabled."""
        return self._config.enabled

    @property
    def running(self) -> bool:
        """Return True if the background loop task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def _db_scope(self) -> Path | None:
        """The project database every write of a cycle goes to: the agent's own, never the caller's contextvar.

        ``None`` (an agent built without ``project_dir``) keeps ``get_session()``'s default resolution.
        """
        return Path(self._project_dir) if self._project_dir else None

    @staticmethod
    async def _shielded(awaitable: Awaitable[None]) -> None:
        """Run a DB write to completion even when the cycle task is cancelled meanwhile.

        The write is retained as its own task and shielded, so a cancellation (or a second one) during it
        neither abandons the transaction (SQLite would keep its write lock) nor leaves the run row RUNNING.
        The cancellation propagates once the write has finished.
        """
        write = asyncio.ensure_future(awaitable)
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            while not write.done():
                # Another cancellation must not abandon the write: keep waiting, then re-raise below.
                with contextlib.suppress(asyncio.CancelledError):  # NOSONAR(S7497)
                    await asyncio.shield(write)
            raise

    async def run_cycle_once(self) -> None:
        """Execute a single wake cycle (open the run row, observe, analyze, propose, finish the row).

        Extracted from the loop body so the dashboard "Run Now" button can
        trigger an immediate cycle without waiting for the sleep interval.
        Serialized via ``_cycle_lock`` so a "Run Now" call waits for any
        in-progress scheduled cycle (and vice versa).
        """
        self._reload_config()
        if not self._config.enabled:
            log.debug("oversight.cycle_skip_disabled")
            return

        async with self._cycle_lock:
            run_id = str(uuid.uuid4())
            self._cycle_number += 1
            cycle = self._cycle_number
            started_at = datetime.now(timezone.utc)
            t0 = time.monotonic()
            snapshot: dict | None = None
            try:
                log.debug("oversight.cycle_start", cycle=cycle, run_id=run_id)
                await self._shielded(self._start_run(run_id, cycle, started_at))
                self._persona = load_persona(self._config.persona_path)
                snapshot = await self._observe()
                analysis_error: str | None = None
                findings: list[OversightFinding] = []
                if snapshot is not None:
                    findings, analysis_error = await self._analyze(snapshot, run_id)
                if findings and analysis_error is None:
                    await self._propose_issues(findings)
                duration_ms = int((time.monotonic() - t0) * 1000)
                status, error = self._determine_outcome(snapshot, analysis_error)
                await self._shielded(
                    self._record_run(
                        run_id,
                        cycle,
                        status,
                        duration_ms,
                        started_at=started_at,
                        snapshot=snapshot,
                        error=error,
                    )
                )
                log.debug("oversight.cycle_done", cycle=cycle, run_id=run_id, duration_ms=duration_ms)
            except asyncio.CancelledError:
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._shielded(
                    self._record_error_safe(run_id, cycle, duration_ms, started_at, "cancelled", snapshot)
                )
                raise
            except Exception as exc:  # noqa: BLE001 (daemon loop must survive any single-cycle error)
                duration_ms = int((time.monotonic() - t0) * 1000)
                log.warning("oversight.cycle_error", cycle=cycle, run_id=run_id, exc_info=True)
                await self._shielded(
                    self._record_error_safe(run_id, cycle, duration_ms, started_at, str(exc), snapshot)
                )

    async def _run_loop(self) -> None:
        """Main loop: execute a wake cycle, then sleep for the configured interval.

        Exits when ``enabled`` becomes False so ``running`` returns False and
        the settings router can re-start the agent if re-enabled.
        """
        while True:
            self._reload_config()
            if not self._config.enabled:
                log.info("oversight.loop_exit_disabled")
                self._task = None
                return
            await self.run_cycle_once()
            interval_seconds = self._config.wake_interval_minutes * 60
            await self._interruptible_sleep(interval_seconds)

    async def _analyze(self, snapshot: dict, run_id: str) -> tuple[list[OversightFinding], str | None]:
        """Run the analysis phase: send snapshot to LLM, persist findings.

        Returns:
            Tuple of (list of findings, error message or None).
        """
        from sova.llm.client import get_provider
        from sova.oversight.analysis import analyze_snapshot

        try:
            provider = get_provider()
            findings, error = await analyze_snapshot(
                snapshot,
                run_id,
                self._persona,
                provider,
                model=self._config.analysis_model,
                dedup_window_days=self._config.dedup_window_days,
                analysis_timeout=self._config.analysis_timeout_seconds,
                project_dir=self._db_scope,
            )
            return findings, error
        except Exception as exc:  # noqa: BLE001 (LLM analysis failure is reported as a cycle error, not a crash)
            log.warning("oversight.analyze_failed", run_id=run_id, exc_info=True)
            return [], f"analyze_failed: {exc}"

    async def _propose_issues(self, findings: list[OversightFinding]) -> None:
        """Create GitHub Issues from high-confidence findings (action phase).

        Non-fatal: failures are logged but do not abort the cycle.
        """
        from pathlib import Path

        from sova.adapters import create_adapter
        from sova.config.registry import list_projects
        from sova.oversight.actions import propose_issues

        try:
            sova_cfg = load_config()
            if not sova_cfg.github_repo or sova_cfg.task_source.type != "github":
                log.debug("oversight.actions.skip", reason="no github_repo or non-github task source")
                return

            sova_adapter = create_adapter(sova_cfg)

            project_adapters = {}
            for slug, path_str in list_projects().items():
                try:
                    proj_cfg = load_config(Path(path_str))
                    if proj_cfg.github_repo and proj_cfg.task_source.type == "github":
                        project_adapters[slug] = create_adapter(proj_cfg)
                except Exception:  # noqa: BLE001 (one misconfigured project must not abort adapter discovery)
                    log.debug("oversight.actions.project_adapter_failed", slug=slug, exc_info=True)

            created = await propose_issues(
                findings,
                self._config,
                sova_adapter,
                project_adapters,
                confidence_threshold=self._config.confidence_threshold,
                project_dir=self._db_scope,
            )

            if created and self._config.auto_triage:
                await self._auto_triage(created)

        except Exception:  # noqa: BLE001 (action stage spans config, tracker and subprocess failures)
            log.warning("oversight.actions.failed", exc_info=True)

    async def _auto_triage(self, findings: list[OversightFinding]) -> None:
        """Run sova triage on newly created global issues."""
        for finding in findings:
            if finding.scope != "global" or finding.github_issue_number is None:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sova",
                    "triage",
                    str(finding.github_issue_number),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=300)
                log.info(
                    "oversight.actions.auto_triage",
                    issue=finding.github_issue_number,
                    exit_code=proc.returncode,
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                log.warning(
                    "oversight.actions.triage_timeout",
                    issue=finding.github_issue_number,
                    exc_info=True,
                )
            except (RuntimeError, OSError):
                log.warning(
                    "oversight.actions.triage_failed",
                    issue=finding.github_issue_number,
                    exc_info=True,
                )

    async def _observe(self) -> dict | None:
        """Run the observation phase and return the snapshot as a dict."""
        from sova.oversight.observation import build_snapshot

        try:
            snapshot = await build_snapshot()
            return snapshot.to_dict()
        except Exception:  # noqa: BLE001 (observation aggregates many providers; any failure yields no snapshot)
            log.warning("oversight.observe_failed", exc_info=True)
            return None

    async def _start_run(self, run_id: str, cycle: int, started_at: datetime) -> None:
        """Insert the run row as ``running`` so findings written during the cycle satisfy their foreign key.

        Non-fatal: on a DB failure ``_record_run`` inserts the row at the end of the cycle instead.
        """
        try:
            async with await get_session(project_dir=self._db_scope) as session, session.begin():
                session.add(
                    OversightRun(
                        id=run_id,
                        status=OversightRunStatus.RUNNING,
                        cycle_number=cycle,
                        started_at=started_at,
                    )
                )
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("oversight.start_run_failed", run_id=run_id, cycle=cycle, exc_info=True)

    async def _record_run(
        self,
        run_id: str,
        cycle: int,
        status: OversightRunStatus,
        duration_ms: int,
        *,
        started_at: datetime | None = None,
        error: str | None = None,
        snapshot: dict | None = None,
    ) -> None:
        """Finish the wake cycle's record: update the row inserted at cycle start, or insert it if missing."""
        try:
            async with await get_session(project_dir=self._db_scope) as session, session.begin():
                now = datetime.now(timezone.utc)
                record = await session.get(OversightRun, run_id)
                if record is None:
                    record = OversightRun(id=run_id, cycle_number=cycle, started_at=started_at or now)
                    session.add(record)
                record.status = status
                record.duration_ms = duration_ms
                record.error = error
                record.snapshot_json = snapshot
                record.ended_at = now
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("oversight.record_failed", run_id=run_id, cycle=cycle, exc_info=True)
