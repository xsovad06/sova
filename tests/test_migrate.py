"""Tests for SOVA migration commands."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sova.cli.commands.migrate import convert_conf_to_toml, parse_cost_jsonl


class TestConvertConfToToml:
    """Tests for legacy .conf -> sova.toml conversion."""

    def test_basic_conversion(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text(
            '# Comment\nGITHUB_REPO="owner/repo"\nGITHUB_USER="myuser"\nBASE_BRANCH="main"\nTEST_CMD="make test"\n'
        )

        result = convert_conf_to_toml(conf)

        assert 'github_repo = "owner/repo"' in result
        assert 'github_user = "myuser"' in result
        assert 'base_branch = "main"' in result
        assert 'test_cmd = "make test"' in result

    def test_boolean_conversion(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text(
            'REVIEW_ENABLED="true"\nNO_AI_COAUTHOR="false"\nSKIP_MANUAL_TEST="1"\nAUTO_APPROVE_FIXES="yes"\n'
        )

        result = convert_conf_to_toml(conf)

        assert "enabled = true" in result
        assert "no_ai_coauthor = false" in result
        assert "skip_manual_test = true" in result
        assert "auto_approve_fixes = true" in result

    def test_list_conversion(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text('WORKTREE_COPY_FILES=".env,.env.local,.secrets"\n')

        result = convert_conf_to_toml(conf)

        assert 'copy_files = [".env", ".env.local", ".secrets"]' in result

    def test_integer_conversion(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text("CI_POLL_INTERVAL=30\nCI_MAX_WAIT=600\nREVIEW_MAX_ROUNDS=3\n")

        result = convert_conf_to_toml(conf)

        assert "poll_interval = 30" in result
        assert "max_wait = 600" in result
        assert "max_rounds = 3" in result

    def test_decimal_quoted(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text('MAX_BUDGET="15.00"\n')

        result = convert_conf_to_toml(conf)

        assert 'max_budget = "15.00"' in result

    def test_sections_grouped(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text('AGENT_MODEL="sonnet"\nMAX_BUDGET="5.00"\nREVIEW_ENABLED="true"\nREVIEW_MAX_ROUNDS=2\n')

        result = convert_conf_to_toml(conf)

        assert "[agent]" in result
        assert "[review]" in result

    def test_comments_and_blanks_skipped(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text('# Full line comment\n\n  GITHUB_REPO="test/repo"\n\n# Another comment\n')

        result = convert_conf_to_toml(conf)

        assert 'github_repo = "test/repo"' in result
        assert "# Full line comment" not in result

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        conf = tmp_path / "pak-agent.conf"
        conf.write_text('GITHUB_REPO="test/repo"\nUNKNOWN_KEY="value"\n')

        result = convert_conf_to_toml(conf)

        assert "UNKNOWN_KEY" not in result
        assert 'github_repo = "test/repo"' in result

    def test_migrated_toml_is_loadable(self, tmp_path: Path) -> None:
        """The migrated TOML file should be loadable by sova config."""
        conf = tmp_path / "pak-agent.conf"
        conf.write_text(
            'GITHUB_REPO="owner/repo"\n'
            'AGENT_MODEL="sonnet"\n'
            'MAX_BUDGET="5.00"\n'
            'REVIEW_ENABLED="false"\n'
            "REVIEW_MAX_ROUNDS=3\n"
            "CI_POLL_INTERVAL=30\n"
            'WORKTREE_COPY_FILES=".env,.secrets"\n'
            'NO_AI_COAUTHOR="true"\n'
        )

        toml_content = convert_conf_to_toml(conf)

        # Write as sova.toml and load with sova config loader
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text(toml_content)

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == "owner/repo"
        assert cfg.agent.model == "sonnet"
        assert cfg.agent.max_budget == Decimal("5")
        assert cfg.review.enabled is False
        assert cfg.review.max_rounds == 3
        assert cfg.ci.poll_interval == 30
        assert cfg.worktree.copy_files == [".env", ".secrets"]
        assert cfg.commit.no_ai_coauthor is True

    def test_full_conf_default(self) -> None:
        """Convert the actual pak-agent.conf.default file if it exists."""
        conf_path = Path(__file__).parent.parent / "agent" / "pak-agent.conf.default"
        if not conf_path.exists():
            pytest.skip("pak-agent.conf.default not present (already removed)")

        result = convert_conf_to_toml(conf_path)

        # Should produce valid-looking TOML
        assert "[agent]" in result
        assert "[review]" in result
        assert "[ci]" in result


class TestParseCostJsonl:
    """Tests for JSONL cost data parsing."""

    def test_basic_parsing(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "costs.jsonl"
        jsonl.write_text(
            '{"timestamp":"2026-04-20T10:00:04Z","issue":"32","phase":"harden",'
            '"model":"claude-opus-4-6","input_tokens":3,"output_tokens":1247,'
            '"cache_tokens":16421,"cost_usd":0.13382125,"duration_ms":37484}\n'
        )

        records = parse_cost_jsonl(jsonl)

        assert len(records) == 1
        r = records[0]
        assert r["issue"] == "32"
        assert r["phase"] == "harden"
        assert r["model"] == "claude-opus-4-6"
        assert r["input_tokens"] == 3
        assert r["output_tokens"] == 1247
        assert r["cache_tokens"] == 16421
        assert r["cost_usd"] == Decimal("0.13382125")
        assert r["duration_ms"] == 37484
        assert r["recorded_at"] == datetime(2026, 4, 20, 10, 0, 4, tzinfo=timezone.utc)

    def test_multiple_records(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "costs.jsonl"
        lines = []
        for i in range(5):
            lines.append(
                json.dumps(
                    {
                        "timestamp": f"2026-04-20T10:0{i}:00Z",
                        "issue": str(30 + i),
                        "phase": "develop",
                        "model": "claude-opus-4-6",
                        "input_tokens": 100 * i,
                        "output_tokens": 200 * i,
                        "cache_tokens": 300 * i,
                        "cost_usd": 0.1 * i,
                        "duration_ms": 1000 * i,
                    }
                )
            )
        jsonl.write_text("\n".join(lines) + "\n")

        records = parse_cost_jsonl(jsonl)

        assert len(records) == 5
        assert records[0]["issue"] == "30"
        assert records[4]["issue"] == "34"

    def test_empty_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "costs.jsonl"
        jsonl.write_text("")

        records = parse_cost_jsonl(jsonl)

        assert records == []

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "costs.jsonl"
        jsonl.write_text(
            "\n"
            '{"timestamp":"2026-04-20T10:00:00Z","issue":"1","phase":"dev",'
            '"model":"opus","input_tokens":0,"output_tokens":0,'
            '"cache_tokens":0,"cost_usd":0,"duration_ms":0}\n'
            "\n"
        )

        records = parse_cost_jsonl(jsonl)

        assert len(records) == 1


class TestImportCosts:
    """Tests for importing cost records into the database."""

    @pytest.mark.asyncio
    async def test_import_to_db(self, tmp_path: Path) -> None:
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session, init_db

        await init_db(tmp_path)

        records = [
            {
                "issue": "42",
                "phase": "develop",
                "model": "claude-opus-4-6",
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_tokens": 300,
                "cost_usd": Decimal("0.5"),
                "duration_ms": 5000,
                "recorded_at": datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc),
            },
            {
                "issue": "43",
                "phase": "review",
                "model": "claude-sonnet-4-6",
                "input_tokens": 50,
                "output_tokens": 100,
                "cache_tokens": 150,
                "cost_usd": Decimal("0.1"),
                "duration_ms": 2000,
                "recorded_at": datetime(2026, 4, 20, 11, 0, 0, tzinfo=timezone.utc),
            },
        ]

        from sova.cli.commands.migrate import _import_costs

        imported = await _import_costs(records, tmp_path)
        assert imported == 2

        session = await get_session(tmp_path)
        async with session:
            result = await session.execute(select(CostRecord).order_by(CostRecord.id))
            rows = result.scalars().all()
            assert len(rows) == 2
            assert rows[0].issue == "42"
            assert rows[0].phase == "develop"
            assert rows[1].issue == "43"
            assert rows[1].model == "claude-sonnet-4-6"
