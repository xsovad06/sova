"""Tests for sova/knowledge/retro.py and the `sova retro` command group (issue #744)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import typer
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from sova.cli.commands.retro import _backfill, _report
from sova.cli.commands.retro import app as retro_app
from sova.db.models import PrRetro, TaskRun
from sova.db.session import get_session
from sova.knowledge.retro import (
    COMMON_MISTAKES_PATH,
    RetroClassificationError,
    RetroError,
    RetroFinding,
    RuleBullet,
    RuleCatalog,
    build_report,
    collect_pr_review_data,
    load_rule_bullets,
    load_rule_catalog,
    parse_classification,
    parse_first_seen,
    parse_rule_bullets,
    record_pr_retro,
    with_rule_text,
)
from sova.llm.models import LLMResult

runner = CliRunner()

_T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _rule(file: str, label: str, text: str = "") -> RuleBullet:
    return RuleBullet(file=file, label=label, raw_label=label, text=text or f"- **{label}**: rule text")


def _finding(fid: str, round_: int | None, *, severity: str = "medium", source: str = "reviewer") -> RetroFinding:
    return RetroFinding(id=fid, round=round_, source=source, severity=severity, file="a.py", text=f"text {fid}")


def _catalog(*rules: RuleBullet) -> RuleCatalog:
    return RuleCatalog(bullets=list(rules), first_seen={(r.file, r.raw_label): _T0 - timedelta(days=30) for r in rules})


def _llm(payload: object, cost: str = "0.01") -> LLMResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return LLMResult(text=text, model="claude-haiku-5-5", cost_usd=Decimal(cost))


class TestParseRuleBullets:
    def test_reads_top_level_bold_bullets_with_continuations_and_disambiguates_repeats(self) -> None:
        content = (
            "# Title\n"
            "## Python\n"
            "- **No emojis**: not in code\n"
            "- **Line length**: 120 max\n"
            "  continued on an indented line\n"
            "  - **nested bullet** is not a rule of its own\n"
            "## Markdown\n"
            "- **No emojis**: not in docs\n"
            "```bash\n"
            "- **inside a fence** is not a rule\n"
            "```\n"
            "* plain bullet\n"
        )

        bullets = parse_rule_bullets("AGENTS.md", content)

        assert [b.label for b in bullets] == ["No emojis [Python]", "Line length", "No emojis [Markdown]"]
        assert [b.raw_label for b in bullets] == ["No emojis", "Line length", "No emojis"]
        assert bullets[1].text == "- **Line length**: 120 max\n  continued on an indented line"
        assert {b.file for b in bullets} == {"AGENTS.md"}


class TestRuleCatalog:
    def test_parse_first_seen_keeps_the_earliest_added_line_per_file_and_label(self) -> None:
        log = (
            "COMMIT 2026-05-01T10:00:00+02:00\n"
            "diff --git a/AGENTS.md b/AGENTS.md\n"
            "+++ b/AGENTS.md\n"
            "+- **Old rule**: first wording\n"
            "COMMIT 2026-06-01T10:00:00+00:00\n"
            "+++ b/AGENTS.md\n"
            "+- **Old rule**: reworded later\n"
            "+- **New rule**: added later\n"
            "+++ b/.claude/rules/workflow.md\n"
            "+- **Old rule**: same label, other file\n"
        )

        seen = parse_first_seen(log)

        assert seen[("AGENTS.md", "Old rule")] == datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
        assert seen[("AGENTS.md", "New rule")] == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert seen[(".claude/rules/workflow.md", "Old rule")] == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)

    def test_in_force_hides_rules_written_after_the_review_and_mistakes_citing_this_or_a_later_pr(self) -> None:
        early = _rule("AGENTS.md", "Early")
        late = _rule("AGENTS.md", "Late")
        uncommitted = _rule("CLAUDE.md", "Uncommitted")
        uncited = _rule(COMMON_MISTAKES_PATH, "Uncited mistake", "- **Uncited mistake** (occurrences: 2): x")
        cited = _rule(COMMON_MISTAKES_PATH, "Cited mistake", "- **Cited mistake**: seen in PR #100 and PR #120")
        catalog = RuleCatalog(
            bullets=[early, late, uncommitted, uncited, cited],
            first_seen={(r.file, r.raw_label): _T0 + timedelta(days=d) for r, d in ((early, -1), (late, 1))},
        )

        assert [r.label for r in catalog.in_force(_T0, pr_number=120)] == ["Early", "Uncited mistake"]
        assert [r.label for r in catalog.in_force(_T0, pr_number=121)] == ["Early", "Uncited mistake", "Cited mistake"]


async def test_load_rule_catalog_dates_tracked_bullets_from_git_and_reads_untracked_mistakes(tmp_path: Path) -> None:
    import os
    import subprocess

    def git(*args: str, date: str = "") -> None:
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={**os.environ, **env},
        )

    (tmp_path / ".claude" / "rules").mkdir(parents=True)
    (tmp_path / ".claude" / "agent-memory").mkdir()
    git("init", "-q")
    (tmp_path / "AGENTS.md").write_text("- **First rule**: a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "one", date="2026-05-01T10:00:00+00:00")
    (tmp_path / ".claude" / "rules" / "x.md").write_text("- **Second rule**: b\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "two", date="2026-06-01T10:00:00+00:00")
    (tmp_path / COMMON_MISTAKES_PATH).write_text("- **Untracked mistake**: c\n", encoding="utf-8")

    catalog = await load_rule_catalog(tmp_path)

    assert [(b.file, b.label) for b in catalog.bullets] == [
        ("AGENTS.md", "First rule"),
        (".claude/rules/x.md", "Second rule"),
        (COMMON_MISTAKES_PATH, "Untracked mistake"),
    ]
    assert catalog.first_seen == {
        ("AGENTS.md", "First rule"): datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        (".claude/rules/x.md", "Second rule"): datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
    }


async def test_load_rule_bullets_reads_the_bullets_without_calling_git(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("- **First rule**: a\n", encoding="utf-8")

    with patch("sova.knowledge.retro.run", AsyncMock(side_effect=AssertionError("git must not run"))):
        bullets = await load_rule_bullets(tmp_path)

    assert [(b.file, b.label) for b in bullets] == [("AGENTS.md", "First rule")]


async def _seed_runs(pr: int = 7) -> None:
    def run(role: str, minute: int, handoff: dict | None, cost: str = "1") -> TaskRun:
        return TaskRun(
            role=role,
            status="completed",
            pr_number=pr,
            handoff_json=handoff,
            total_cost_usd=Decimal(cost),
            started_at=_T0 + timedelta(minutes=minute),
        )

    review1 = {
        "pending_findings": [
            {"file": "a.py", "severity": 6, "category": "bug", "description": "first   finding"},
            {"file": "", "severity": 3, "category": "incomplete-review", "description": "chunk 2 unreviewed"},
        ]
    }
    review2 = {"pending_findings": [{"file": "a.py", "severity": 2, "category": "style", "description": "second"}]}
    address = {
        "phase": "address_review",
        "addressed_findings": [
            {"file": "a.py", "severity": 6, "category": "bug", "description": "reviewer copy"},
            {"file": "a.py", "severity": 6, "source": "github-review", "description": "re-read review"},
            {"source": "sonarcloud", "severity": "MAJOR", "file_path": "b.py", "tool_id": "S1", "message": "sonar"},
            {"source": "sonarcloud", "severity": "coverage", "file_path": "project-wide", "message": "Coverage"},
            {
                "source": "coderabbit",
                "severity": "MAJOR",
                "file_path": "c.py",
                "tool_id": "T1",
                "message": "rabbit <details>tool output</details> says",
            },
        ],
    }
    address_again = {"phase": "address_review", "addressed_findings": [address["addressed_findings"][2]]}
    legacy_rabbit = {
        "phase": "develop",
        "addressed_findings": [{"file": "d.py", "severity": 6, "source": "coderabbit", "description": "old shape"}],
    }
    async with await get_session() as session:
        session.add_all(
            [
                run("developer", 0, legacy_rabbit),
                run("reviewer", 10, review1),
                run("reviewer", 15, None),
                run("developer", 20, address),
                run("command:address-pr", 25, None),
                run("reviewer", 30, review2),
                run("developer", 40, address_again),
                TaskRun(role="reviewer", status="completed", pr_number=8, handoff_json=review2),
            ]
        )
        await session.commit()


@pytest.mark.usefixtures("reset_db_rows")
class TestCollectPrReviewData:
    async def test_collects_reviewer_rounds_and_external_findings_once(self) -> None:
        await _seed_runs()

        async with await get_session() as session:
            data = await collect_pr_review_data(session, 7)

        assert data.review_rounds == 2
        assert data.address_cycles == 3
        assert data.cost_usd == Decimal("7")
        assert data.first_review_at == _T0 + timedelta(minutes=10)
        assert [(f.id, f.round, f.source, f.severity, f.file, f.text) for f in data.findings] == [
            ("F1", 1, "reviewer", "high", "a.py", "first finding"),
            ("F2", 2, "reviewer", "low", "a.py", "second"),
            ("F3", None, "coderabbit", "high", "d.py", "old shape"),
            ("F4", None, "sonarcloud", "medium", "b.py", "sonar"),
            ("F5", None, "coderabbit", "medium", "c.py", "rabbit says"),
        ]


class TestParseClassification:
    _RULES = [_rule("AGENTS.md", "A"), _rule("AGENTS.md", "B")]
    _FINDINGS = [_finding("F1", 1), _finding("F2", 2), _finding("F3", None, source="sonarcloud")]

    def test_accepts_a_fenced_reply_and_maps_rule_ids(self) -> None:
        reply = (
            "```json\n"
            + json.dumps(
                {
                    "findings": [
                        {"id": "F1", "rule": "K2", "same_as": None, "why": "matches B"},
                        {"id": "F2", "rule": None, "same_as": "F1", "why": ""},
                        {"id": "F3", "rule": None, "same_as": None, "why": ""},
                    ]
                }
            )
            + "\n```"
        )

        verdicts = parse_classification(reply, self._RULES, self._FINDINGS)

        assert verdicts["F1"].rule == self._RULES[1]
        assert verdicts["F1"].why == "matches B"
        assert verdicts["F2"].same_as == "F1" and verdicts["F2"].rule is None
        assert verdicts["F3"].rule is None

    @pytest.mark.parametrize(
        "items",
        [
            "not json at all",
            {"verdicts": []},
            [{"id": "F1", "rule": None, "same_as": None, "why": ""}, {"id": "F2", "rule": None, "same_as": None}],
            [
                {"id": "F1", "rule": None, "same_as": None, "why": ""},
                {"id": "F1", "rule": None, "same_as": None, "why": ""},
                {"id": "F2", "rule": None, "same_as": None, "why": ""},
                {"id": "F3", "rule": None, "same_as": None, "why": ""},
            ],
            [
                {"id": "F1", "rule": "K9", "same_as": None, "why": "x"},
                {"id": "F2", "rule": None, "same_as": None, "why": ""},
                {"id": "F3", "rule": None, "same_as": None, "why": ""},
            ],
            [
                {"id": "F1", "rule": "K1", "same_as": None, "why": " "},
                {"id": "F2", "rule": None, "same_as": None, "why": ""},
                {"id": "F3", "rule": None, "same_as": None, "why": ""},
            ],
            [
                {"id": "F1", "rule": None, "same_as": "F2", "why": ""},
                {"id": "F2", "rule": None, "same_as": None, "why": ""},
                {"id": "F3", "rule": None, "same_as": None, "why": ""},
            ],
            [
                {"id": "F1", "rule": None, "same_as": None, "why": ""},
                {"id": "F2", "rule": None, "same_as": None, "why": ""},
                {"id": "F3", "rule": None, "same_as": "F1", "why": ""},
            ],
        ],
        ids=[
            "no-json",
            "no-findings-key",
            "missing-key-in-entry-and-missing-id",
            "duplicate-id",
            "unknown-rule",
            "empty-why",
            "same-as-later-round",
            "same-as-on-external",
        ],
    )
    def test_rejects_anything_short_of_one_valid_verdict_per_finding(self, items: object) -> None:
        reply = (
            items if isinstance(items, str) else json.dumps(items if isinstance(items, dict) else {"findings": items})
        )

        with pytest.raises(RetroClassificationError):
            parse_classification(reply, self._RULES, self._FINDINGS)


@pytest.mark.usefixtures("reset_db_rows")
class TestRecordPrRetro:
    _CATALOG = _catalog(_rule("AGENTS.md", "Fan-out audit"), _rule(COMMON_MISTAKES_PATH, "x or default"))
    _REPLY = {
        "findings": [
            {"id": "F1", "rule": "K1", "same_as": None, "why": "missed a consumer"},
            {"id": "F2", "rule": "K2", "same_as": "F1", "why": "same"},
            {"id": "F3", "rule": None, "same_as": None, "why": ""},
            {"id": "F4", "rule": "K2", "same_as": None, "why": "falsy default"},
            {"id": "F5", "rule": None, "same_as": None, "why": ""},
        ]
    }

    async def _record(self, invoke: AsyncMock, *, force: bool = False, pr: int = 7):
        with patch("sova.knowledge.retro.invoke", invoke):
            return await record_pr_retro(
                Path.cwd(),
                pr,
                merged_at=_T0 + timedelta(days=2),
                catalog=self._CATALOG,
                model="claude-haiku-5-5",
                force=force,
            )

    async def _rows(self) -> list[PrRetro]:
        async with await get_session() as session:
            return list((await session.execute(select(PrRetro))).scalars())

    async def test_stores_one_row_with_counts_repeats_and_digest(self) -> None:
        await _seed_runs()
        invoke = AsyncMock(return_value=_llm(self._REPLY, cost="0.02"))

        outcome = await self._record(invoke)

        assert outcome.status == "recorded"
        [row] = await self._rows()
        assert (row.pr_number, row.review_rounds, row.address_cycles) == (7, 2, 3)
        assert row.cost_usd == Decimal("7") and row.classify_cost_usd == Decimal("0.02")
        assert row.classify_model == "claude-haiku-5-5"
        # F2 re-raises F1, so it is not counted again.
        assert row.findings_by_severity == {"critical": 0, "high": 2, "medium": 2, "low": 0, "unknown": 0}
        assert row.kinds_count == 2 and len(row.kinds_digest) == 64
        repeats = [(f["id"], f["rule"]) for f in row.findings if f["rule"]]
        assert repeats == [
            ("F1", {"file": "AGENTS.md", "label": "Fan-out audit"}),
            ("F2", {"file": COMMON_MISTAKES_PATH, "label": "x or default"}),
            ("F4", {"file": COMMON_MISTAKES_PATH, "label": "x or default"}),
        ]
        [segment] = invoke.call_args.kwargs["untrusted_segments"]
        assert invoke.call_args.kwargs["isolated"] is True
        assert "first finding" in segment and segment in invoke.call_args.args[0]

    async def test_skips_a_recorded_pr_unless_forced(self) -> None:
        await _seed_runs()
        await self._record(AsyncMock(return_value=_llm(self._REPLY)))
        invoke = AsyncMock(return_value=_llm(self._REPLY, cost="0.05"))

        assert (await self._record(invoke)).status == "skipped"
        invoke.assert_not_called()

        assert (await self._record(invoke, force=True)).status == "recorded"
        [row] = await self._rows()
        assert row.classify_cost_usd == Decimal("0.05")

    async def test_unparseable_classification_stores_no_row(self) -> None:
        await _seed_runs()

        with pytest.raises(RetroClassificationError) as exc_info:
            await self._record(AsyncMock(return_value=_llm("I could not decide.", cost="0.03")))

        assert exc_info.value.cost_usd == Decimal("0.03")
        assert await self._rows() == []

    async def test_reviewed_pr_without_findings_is_recorded_without_an_llm_call(self) -> None:
        async with await get_session() as session:
            session.add(
                TaskRun(role="reviewer", status="completed", pr_number=9, handoff_json={"pending_findings": []})
            )
            await session.commit()
        invoke = AsyncMock()

        outcome = await self._record(invoke, pr=9)

        invoke.assert_not_called()
        assert outcome.status == "recorded"
        [row] = await self._rows()
        assert (row.review_rounds, row.findings, row.classify_cost_usd) == (1, [], Decimal("0"))

    async def test_pr_without_review_data_is_not_recorded(self) -> None:
        outcome = await self._record(AsyncMock(), pr=404)

        assert outcome.status == "no_data"
        assert await self._rows() == []


def _row(pr: int, month: int, findings: list[dict], **kw: object) -> PrRetro:
    return PrRetro(
        pr_number=pr,
        merged_at=datetime(2026, month, 15, tzinfo=timezone.utc),
        review_rounds=kw.get("rounds", 1),
        address_cycles=kw.get("cycles", 1),
        cost_usd=Decimal(str(kw.get("cost", "10"))),
        classify_cost_usd=Decimal("0.01"),
        classify_model="m",
        findings_by_severity={},
        kinds_digest="d" * 64,
        kinds_count=2,
        findings=findings,
    )


def _f(fid: str, severity: str, rule: tuple[str, str] | None = None, same_as: str | None = None) -> dict:
    return {
        "id": fid,
        "severity": severity,
        "rule": {"file": rule[0], "label": rule[1]} if rule else None,
        "same_as": same_as,
    }


class TestBuildReport:
    def test_monthly_share_counts_medium_or_worse_first_occurrences_and_ranks_rules(self) -> None:
        fan = ("AGENTS.md", "Fan-out audit")
        falsy = (COMMON_MISTAKES_PATH, "x or default")
        rows = [
            _row(1, 9, [_f("F1", "high", fan), _f("F2", "high", fan, same_as="F1"), _f("F3", "low", falsy)]),
            _row(2, 9, [_f("F1", "medium"), _f("F2", "critical", falsy)], rounds=3, cycles=2, cost="20"),
            _row(3, 10, [_f("F1", "medium", fan), _f("F2", "medium")]),
            _row(4, 8, [_f("F1", "high", fan)]),
        ]

        report = build_report(rows, since="2026-09")

        sept, octo = report.months
        assert (sept.month, sept.prs, sept.medium_plus, sept.repeats) == ("2026-09", 2, 3, 2)
        assert (sept.mean_rounds, sept.mean_cycles, sept.mean_cost) == (2.0, 1.5, Decimal("15"))
        assert (octo.month, octo.prs, octo.medium_plus, octo.repeats) == ("2026-10", 1, 2, 1)
        assert (report.medium_plus, report.repeats, report.tracked_repeats) == (5, 3, 2)
        assert [(r.file, r.label, r.count, r.prs) for r in report.top_rules] == [
            ("AGENTS.md", "Fan-out audit", 2, [1, 3]),
            (COMMON_MISTAKES_PATH, "x or default", 1, [2]),
        ]


class TestWithRuleText:
    def test_pairs_ranked_rules_with_bullet_text_and_notes_a_vanished_bullet(self) -> None:
        fan = ("AGENTS.md", "Fan-out audit")
        gone = ("AGENTS.md", "Removed rule")
        rows = [_row(1, 9, [_f("F1", "high", fan), _f("F2", "high", gone)]), _row(2, 9, [_f("F1", "high", fan)])]

        ranked = build_report(rows).top_rules
        rules = with_rule_text(ranked, [_rule("AGENTS.md", "Fan-out audit", "- **Fan-out audit**: grep")])

        assert [(r.label, r.count, r.prs, r.text) for r in rules] == [
            ("Fan-out audit", 2, [1, 2], "- **Fan-out audit**: grep"),
            ("Removed rule", 1, [1], None),
        ]
        assert rules[0].note is None
        assert "no longer in the catalog" in (rules[1].note or "")

    def test_returns_only_what_exists_without_padding(self) -> None:
        rows = [_row(1, 9, [_f("F1", "high", ("AGENTS.md", "A"))])]

        ranked = build_report(rows, top=3).top_rules
        assert len(with_rule_text(ranked, [_rule("AGENTS.md", "A")])) == 1
        assert with_rule_text([], []) == []


class TestRetroTopRulesCli:
    @pytest.fixture(autouse=True)
    def _environment(self, tmp_path: Path):
        self.rows = [_row(1, 9, [_f("F1", "high", ("AGENTS.md", "A")), _f("F2", "high", ("AGENTS.md", "B"))])]
        self.bullets = AsyncMock(return_value=[_rule("AGENTS.md", "A", "- **A**: text a")])
        self.dated_catalog = AsyncMock()
        with (
            patch("sova.cli.commands.retro._project_dir", AsyncMock(return_value=tmp_path)),
            patch("sova.cli.commands.retro.load_retro_rows", AsyncMock(side_effect=lambda _s: self.rows)),
            patch("sova.cli.commands.retro.load_rule_bullets", self.bullets),
            patch("sova.cli.commands.retro.load_rule_catalog", self.dated_catalog),
            patch("sova.db.session.get_session", AsyncMock(return_value=AsyncMock())),
        ):
            yield

    def test_prints_the_ranked_rules_as_json_honouring_limit(self) -> None:
        result = runner.invoke(retro_app, ["top-rules", "--limit", "1"])

        assert result.exit_code == 0
        assert [r["label"] for r in json.loads(result.stdout)] == ["A"]
        assert json.loads(result.stdout)[0]["text"] == "- **A**: text a"
        self.dated_catalog.assert_not_awaited()

    def test_prints_the_note_for_a_rule_missing_from_the_catalog(self) -> None:
        result = runner.invoke(retro_app, ["top-rules"])

        missing = json.loads(result.stdout)[1]
        assert (missing["label"], missing["text"]) == ("B", None)
        assert "unavailable" in missing["note"]

    def test_prints_an_empty_list_only_when_nothing_is_recorded(self) -> None:
        self.rows = []

        result = runner.invoke(retro_app, ["top-rules"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        self.bullets.assert_not_awaited()

    @pytest.mark.parametrize(
        "error",
        [OSError("disk gone"), OperationalError("select", {}, Exception("refused"))],
    )
    def test_an_unreadable_database_exits_1_instead_of_printing_an_empty_list(self, error: Exception) -> None:
        with patch("sova.cli.commands.retro.load_retro_rows", AsyncMock(side_effect=error)):
            result = runner.invoke(retro_app, ["top-rules"])

        assert result.exit_code == 1
        assert "could not read the repeated rules" in result.output
        assert "[]" not in result.output

    def test_an_unreadable_rule_catalog_exits_1_instead_of_printing_rules_without_text(self) -> None:
        self.bullets.side_effect = RetroError("no rules")

        result = runner.invoke(retro_app, ["top-rules"])

        assert result.exit_code == 1
        assert "could not read the repeated rules" in result.output

    @pytest.mark.parametrize(
        "args",
        [
            ["--since", "2026-9"],
            ["--since", "2026/09"],
            ["--since", "2026-00"],
            ["--since", "2026-13"],
            ["--since", "2026-1\u00b2"],
            ["--limit", "0"],
        ],
    )
    def test_a_malformed_since_or_a_limit_below_one_is_a_usage_error(self, args: list[str]) -> None:
        assert runner.invoke(retro_app, ["top-rules", *args]).exit_code == 2

    @pytest.mark.parametrize("since", ["2026-9", "2026/09", "2026-00", "2026-13", "2026-1\u00b2"])
    def test_report_gives_the_same_usage_error_for_a_malformed_since(self, since: str) -> None:
        assert runner.invoke(retro_app, ["report", "--since", since]).exit_code == 2


class TestRetroCli:
    @pytest.fixture(autouse=True)
    def _environment(self, tmp_path: Path):
        with (
            patch("sova.cli.commands.retro._project_context", AsyncMock(return_value=(tmp_path, "o/r", None, "m"))),
            patch("sova.cli.commands.retro.load_rule_catalog", AsyncMock(return_value=TestRecordPrRetro._CATALOG)),
        ):
            yield

    def test_record_refuses_an_unmerged_pr_without_classifying(self) -> None:
        record = AsyncMock()
        with (
            patch("sova.cli.commands.retro._merged_at", AsyncMock(return_value=None)),
            patch("sova.cli.commands.retro.record_pr_retro", record),
        ):
            result = runner.invoke(retro_app, ["record", "7"])

        assert result.exit_code == 1
        assert "not merged" in result.output
        record.assert_not_called()

    def test_record_exits_non_zero_when_classification_fails(self) -> None:
        with (
            patch("sova.cli.commands.retro._merged_at", AsyncMock(return_value=_T0)),
            patch(
                "sova.cli.commands.retro.record_pr_retro",
                AsyncMock(side_effect=RetroClassificationError("bad reply", cost_usd=Decimal("0.01"))),
            ),
        ):
            result = runner.invoke(retro_app, ["record", "7"])

        assert result.exit_code == 1
        assert "bad reply" in result.output

    @pytest.mark.usefixtures("reset_db_rows")
    async def test_backfill_records_the_rest_after_a_failure_and_exits_non_zero(self, capsys) -> None:
        await _seed_runs(pr=7)
        await _seed_runs(pr=11)
        invoke = AsyncMock(side_effect=[_llm("garbage", cost="0.01"), _llm(TestRecordPrRetro._REPLY, cost="0.02")])
        with (
            patch("sova.cli.commands.retro._merged_prs", AsyncMock(return_value={7: _T0, 11: _T0, 12: _T0})),
            patch("sova.knowledge.retro.invoke", invoke),
            pytest.raises(typer.Exit) as exc_info,
        ):
            await _backfill(project=None, limit=None, dry_run=False)

        output = capsys.readouterr()
        assert exc_info.value.exit_code == 1
        assert "recorded 1, skipped 0, failed 1" in output.out
        assert "$0.0300" in output.out
        async with await get_session() as session:
            recorded = list((await session.execute(select(PrRetro.pr_number))).scalars())
        assert recorded == [11]

    @pytest.mark.usefixtures("reset_db_rows")
    async def test_report_prints_the_monthly_row_the_share_and_the_top_rule(self, tmp_path: Path, capsys) -> None:
        fan = ("AGENTS.md", "Fan-out audit")
        async with await get_session() as session:
            session.add_all(
                [_row(1, 9, [_f("F1", "high", fan), _f("F2", "medium")]), _row(2, 9, [_f("F1", "low", fan)])]
            )
            await session.commit()

        with patch("sova.cli.commands.retro._project_dir", AsyncMock(return_value=tmp_path)):
            await _report(since=None, project=None)

        out = capsys.readouterr().out
        assert "2026-09     2     2        1   50.0%" in out
        assert "repeating a recorded rule: 1 of 2 (50.0%)" in out
        assert "1  AGENTS.md: Fan-out audit (PRs #1)" in out
