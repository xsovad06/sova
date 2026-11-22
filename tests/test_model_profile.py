"""``sova config set/get llm.routing`` and the default model profile for new projects (#1323).

Runs the real settings service against a file database under ``tmp_path``; the home directory is redirected by the
autouse ``_isolate_home_config`` fixture, so the profile file lands in a throwaway ``~/.config/sova``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.usefixtures("_dispose_project_engines")

runner = CliRunner()


async def _store_rows(project_dir: Path, rows: dict[str, object]) -> None:
    from sova.config.db_loader import save_setting
    from sova.config.loader import invalidate_config_cache
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session, session.begin():
        for key, value in rows.items():
            await save_setting(session, key, value)
    invalidate_config_cache(project_dir)


async def _stored(project_dir: Path) -> dict[str, object]:
    from sqlalchemy import select

    from sova.db.models import ProjectSetting
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        rows = (await session.execute(select(ProjectSetting))).scalars().all()
    return {row.key: json.loads(row.value) for row in rows}


def _routes(stored: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in stored.items() if key.startswith("llm.routing")}


def _write_profile(content: str) -> Path:
    from sova.dashboard.services.model_profile_service import profile_path

    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestSetRoutingObject:
    async def test_the_object_replaces_every_route(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing.review": "old-model", "llm.routing.develop": "old-model"})

        result = await update_config(
            tmp_path, key="llm.routing", value='{"develop": "claude-sonnet-5-5", "triage": "claude-haiku-5-5"}'
        )

        assert result["status"] == "ok"
        assert _routes(await _stored(tmp_path)) == {
            "llm.routing.develop": "claude-sonnet-5-5",
            "llm.routing.triage": "claude-haiku-5-5",
        }
        assert load_config(tmp_path).llm.routing == {"develop": "claude-sonnet-5-5", "triage": "claude-haiku-5-5"}

    async def test_an_empty_object_clears_every_route_and_a_stray_whole_table_row(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing": {"develop": "a-model"}, "llm.routing.review": "b-model"})

        result = await update_config(tmp_path, key="llm.routing", value="{}")

        assert result["status"] == "ok"
        assert _routes(await _stored(tmp_path)) == {}
        assert load_config(tmp_path).llm.routing == {}

    async def test_the_same_table_is_unchanged_and_writes_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        await _store_rows(tmp_path, {"llm.routing.develop": "claude-sonnet-5-5"})

        with patch.object(settings_service, "_replace_routing_rows") as write:
            result = await settings_service.update_config(
                tmp_path, key="llm.routing", value='{"develop": "claude-sonnet-5-5"}'
            )

        assert result["unchanged"] is True
        write.assert_not_called()

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("not json", "JSON object"),
            ('["develop"]', "got list"),
            ('"develop"', "got str"),
            ("null", "got NoneType"),
            ('{"develop": "claude-sonnet-5-5", "deploy": "x-model"}', "deploy"),
            ('{"develop": 5}', "non-empty model ID"),
            ('{"develop": ""}', "non-empty model ID"),
            ('{"develop": "two words"}', "whitespace"),
        ],
    )
    async def test_a_bad_object_is_rejected_whole_and_writes_nothing(
        self, tmp_path: Path, value: str, message: str
    ) -> None:
        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5"})

        result = await update_config(tmp_path, key="llm.routing", value=value)

        assert message in result["error"]
        assert _routes(await _stored(tmp_path)) == {"llm.routing.review": "claude-opus-5-5"}

    async def test_an_unknown_key_names_the_valid_task_types_and_sizes(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.routing", value='{"deploy": "x-model"}')

        assert "'deploy'" in result["error"]
        assert "self_review" in result["error"]
        assert "moderate" in result["error"]

    async def test_a_complexity_tier_is_a_valid_key(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.routing", value='{"epic": "claude-opus-5-5"}')

        assert result["status"] == "ok"
        assert _routes(await _stored(tmp_path)) == {"llm.routing.epic": "claude-opus-5-5"}

    async def test_a_short_model_name_is_stored_with_a_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.routing", value='{"develop": "sonnet"}')

        assert result["status"] == "ok"
        assert "pin an explicit model ID" in result["warning"]

    async def test_a_routing_table_set_by_the_environment_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services.settings_service import update_config

        monkeypatch.setenv("SOVA_LLM_ROUTING", '{"develop": "x"}')

        result = await update_config(tmp_path, key="llm.routing", value='{"develop": "y-model"}')

        assert result["env_var"] == "SOVA_LLM_ROUTING"
        assert _routes(await _stored(tmp_path)) == {}

    async def test_a_failed_write_leaves_the_old_table(self, tmp_path: Path) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5"})

        with patch("sova.config.db_loader.save_setting", side_effect=SQLAlchemyError("down")):
            result = await update_config(tmp_path, key="llm.routing", value='{"develop": "claude-sonnet-5-5"}')

        assert result["unavailable"] is True
        assert _routes(await _stored(tmp_path)) == {"llm.routing.review": "claude-opus-5-5"}

    async def test_a_table_masked_by_a_whole_table_row_is_normalized_not_unchanged(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config

        # The whole-table row masks the review row: the table in effect is already {"develop": "a-model"}.
        await _store_rows(tmp_path, {"llm.routing": {"develop": "a-model"}, "llm.routing.review": "b-model"})

        result = await update_config(tmp_path, key="llm.routing", value='{"develop": "a-model"}')

        assert "unchanged" not in result
        assert _routes(await _stored(tmp_path)) == {"llm.routing.develop": "a-model"}
        assert load_config(tmp_path).llm.routing == {"develop": "a-model"}

    async def test_a_route_left_in_sova_toml_is_named_in_a_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        (tmp_path / "sova.toml").write_text('[llm.routing]\nreview = "x-model"\n', encoding="utf-8")
        await _store_rows(tmp_path, {"github_repo": "owner/repo"})

        with patch.object(settings_service, "_remove_setting_from_toml", return_value=False):
            result = await settings_service.update_config(tmp_path, key="llm.routing", value="{}")

        assert result["status"] == "ok"
        assert "'llm.routing.review' still applies" in result["warning"]
        assert "sova.toml" in result["warning"]

    async def test_a_failed_toml_cleanup_with_nothing_left_behind_has_no_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5"})

        with patch.object(settings_service, "_remove_setting_from_toml", return_value=False):
            result = await settings_service.update_config(
                tmp_path, key="llm.routing", value='{"develop": "claude-sonnet-5-5"}'
            )

        assert result["status"] == "ok"
        assert "warning" not in result

    def test_routes_left_behind_warning_when_the_table_cannot_be_read_back(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        with patch.object(settings_service, "_current_routing", return_value=None):
            warning = settings_service._routes_left_behind_warning(tmp_path, {})

        assert warning is not None
        assert "could not be read back" in warning


class TestSetRoutingStep:
    async def test_one_step_is_set_and_the_others_are_left_alone(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5"})

        result = await update_config(tmp_path, key="llm.routing.develop", value="claude-sonnet-5-5")

        assert result == {"status": "ok", "key": "llm.routing.develop", "value": "claude-sonnet-5-5", "cleared": False}
        assert _routes(await _stored(tmp_path)) == {
            "llm.routing.review": "claude-opus-5-5",
            "llm.routing.develop": "claude-sonnet-5-5",
        }

    async def test_an_empty_value_deletes_that_steps_row(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5", "llm.routing.develop": "x-model"})

        result = await update_config(tmp_path, key="llm.routing.develop", value="")

        assert result["cleared"] is True
        assert _routes(await _stored(tmp_path)) == {"llm.routing.review": "claude-opus-5-5"}

    async def test_a_stored_row_for_a_retired_task_can_still_be_cleared_but_not_set(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import reset_config, update_config

        await _store_rows(tmp_path, {"llm.routing.retired_task": "x-model", "llm.routing.retired_two": "y-model"})

        assert "unknown" in await update_config(tmp_path, key="llm.routing.other_task", value="x-model")
        cleared = await update_config(tmp_path, key="llm.routing.retired_task", value="")
        reset = await reset_config(tmp_path, key="llm.routing.retired_two")

        assert (cleared["cleared"], reset["status"], reset["deleted"]) == (True, "ok", 1)
        assert _routes(await _stored(tmp_path)) == {}
        # A step that is neither named nor stored is still refused.
        assert "unknown" in await reset_config(tmp_path, key="llm.routing.retired_task")

    async def test_the_value_it_already_has_is_unchanged_and_writes_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        await _store_rows(tmp_path, {"llm.routing.develop": "claude-sonnet-5-5"})

        with patch.object(settings_service, "_write_routing_rows") as write:
            result = await settings_service.update_config(
                tmp_path, key="llm.routing.develop", value="claude-sonnet-5-5"
            )
            cleared = await settings_service.update_config(tmp_path, key="llm.routing.review", value="")

        assert result["unchanged"] is True
        assert cleared["unchanged"] is True
        write.assert_not_called()

    async def test_clearing_a_step_masked_by_a_whole_table_row_deletes_its_row(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        # The review row is masked: the table in effect has no review route, but the row would come back later.
        await _store_rows(tmp_path, {"llm.routing": {"develop": "a-model"}, "llm.routing.review": "b-model"})

        result = await update_config(tmp_path, key="llm.routing.review", value="")

        assert "unchanged" not in result
        assert _routes(await _stored(tmp_path)) == {"llm.routing.develop": "a-model"}

    async def test_an_unknown_step_is_refused_with_the_valid_names(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.routing.deploy", value="x-model")

        assert result["unknown"] is True
        assert "self_review" in result["error"]
        assert _routes(await _stored(tmp_path)) == {}

    async def test_a_model_id_with_whitespace_is_refused(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.routing.develop", value="two words")

        assert "whitespace" in result["error"]

    async def test_reset_deletes_one_route_and_nothing_else(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import reset_config

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5", "llm.routing.develop": "x-model"})

        result = await reset_config(tmp_path, key="llm.routing.develop")

        assert (result["status"], result["key"], result["value"], result["deleted"]) == (
            "ok",
            "llm.routing.develop",
            None,
            1,
        )
        assert _routes(await _stored(tmp_path)) == {"llm.routing.review": "claude-opus-5-5"}


@pytest.mark.usefixtures("no_cli_provider_init", "quiet_logs")
class TestConfigCli:
    @pytest.fixture(autouse=True)
    def _file_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `set` and `get` each run their own event loop: an in-memory database would be a different one in each.
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

    def test_a_route_set_from_the_terminal_reads_back(self, tmp_path: Path) -> None:
        from sova.cli.app import app

        project = str(tmp_path)
        set_result = runner.invoke(
            app, ["config", "set", "llm.routing", '{"develop": "claude-sonnet-5-5"}', "--project", project]
        )
        get_task = runner.invoke(app, ["config", "get", "llm.routing.develop", "--project", project])
        get_all = runner.invoke(app, ["config", "get", "llm.routing", "--project", project])

        assert set_result.exit_code == 0, set_result.output
        assert get_task.exit_code == 0
        assert get_task.stdout.strip() == "claude-sonnet-5-5"
        assert json.loads(get_all.stdout) == {"develop": "claude-sonnet-5-5"}

    def test_a_project_with_no_routes_prints_an_empty_object(self, tmp_path: Path) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["config", "get", "llm.routing", "--project", str(tmp_path)])

        assert (result.exit_code, result.stdout.strip()) == (0, "{}")

    @pytest.mark.parametrize(
        ("key", "message"), [("llm.routing.develop", "not set"), ("llm.routing.nope", "not a routing key")]
    )
    def test_an_unset_or_unknown_route_exits_one(self, tmp_path: Path, key: str, message: str) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["config", "get", key, "--project", str(tmp_path)])

        assert result.exit_code == 1
        assert result.stdout.strip() == ""
        assert message in result.output

    def test_a_cleared_route_says_so_and_a_bad_object_exits_one(self, tmp_path: Path) -> None:
        from sova.cli.app import app

        project = str(tmp_path)
        runner.invoke(app, ["config", "set", "llm.routing.develop", "claude-sonnet-5-5", "--project", project])
        cleared = runner.invoke(app, ["config", "set", "llm.routing.develop", "", "--project", project])
        bad = runner.invoke(app, ["config", "set", "llm.routing", "[1]", "--project", project])

        assert "cleared" in cleared.output
        assert bad.exit_code == 1


class TestRoutingWrites:
    def test_several_short_names_are_one_plural_warning(self) -> None:
        from sova.dashboard.services.settings_service import _short_name_warning

        assert "are short model names" in _short_name_warning(["opus", "sonnet"])
        assert "is a short model name" in _short_name_warning(["opus"])
        assert _short_name_warning(["claude-opus-5-5"]) is None

    async def test_a_sova_toml_failure_after_the_commit_is_not_reported_as_a_database_failure(
        self, tmp_path: Path
    ) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services import settings_service

        with patch.object(settings_service, "_remove_setting_from_toml", side_effect=OSError("read-only")):
            result = await settings_service.replace_routing(tmp_path, routes={"develop": "claude-sonnet-5-5"})

        assert result["status"] == "ok"
        assert "unavailable" not in result
        assert load_config(tmp_path).llm.routing == {"develop": "claude-sonnet-5-5"}

    async def test_a_step_already_at_the_value_answers_unchanged_under_the_lock(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        await settings_service.set_step_model(tmp_path, step="develop", model="claude-sonnet-5-5")

        again = await settings_service.set_step_model(
            tmp_path, step="develop", model="claude-sonnet-5-5", skip_if_unchanged=True
        )
        unset = await settings_service.clear_step_model(tmp_path, step="review", skip_if_unchanged=True)

        assert again["unchanged"] is True
        assert unset["unchanged"] is True

    async def test_a_per_step_environment_variable_is_not_an_override_so_a_write_is_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config

        monkeypatch.setenv("SOVA_LLM_ROUTING_DEVELOP", "env-model")

        result = await update_config(tmp_path, key="llm.routing.develop", value="claude-sonnet-5-5")

        # load_config() ignores a per-step variable (only SOVA_LLM_ROUTING, a JSON object, overrides), so the
        # stored route is the one in effect and the write reports no shadowing.
        assert result["status"] == "ok"
        assert load_config(tmp_path).llm.routing == {"develop": "claude-sonnet-5-5"}


class TestSaveModelProfile:
    async def test_it_saves_only_the_model_settings_the_project_stores(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import profile_path, save_model_profile

        await _store_rows(
            tmp_path,
            {
                "agent.model": "claude-sonnet-5-5",
                "agent.fallback_models": ["claude-haiku-5-5"],
                "roles.reviewer_model": "claude-opus-5-5",
                "llm.routing.review": "claude-opus-5-5",
                "llm.provider": "claude_code",
                "github_repo": "owner/repo",
                "llm.api_key": "secret",
            },
        )

        result = save_model_profile(tmp_path)

        assert result["status"] == "ok"
        assert json.loads(profile_path().read_text(encoding="utf-8")) == {
            "agent.fallback_models": ["claude-haiku-5-5"],
            "agent.model": "claude-sonnet-5-5",
            "llm.routing.review": "claude-opus-5-5",
            "roles.reviewer_model": "claude-opus-5-5",
        }

    async def test_a_whole_table_routing_row_is_saved_as_per_step_keys(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import save_model_profile

        await _store_rows(tmp_path, {"llm.routing": {"develop": "claude-sonnet-5-5"}})

        assert save_model_profile(tmp_path)["keys"] == ["llm.routing.develop"]

    async def test_a_project_storing_none_of_them_is_a_visible_error_and_writes_no_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import profile_path, save_model_profile

        await _store_rows(tmp_path, {"github_repo": "owner/repo"})

        result = save_model_profile(tmp_path)

        assert "stores none of the model settings" in result["error"]
        assert not profile_path().exists()

    async def test_a_seeded_short_alias_is_left_out_and_named_in_a_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import profile_path, save_model_profile

        await _store_rows(tmp_path, {"agent.model": "opus", "roles.reviewer_model": "claude-opus-5-5"})

        result = save_model_profile(tmp_path)

        assert result["status"] == "ok"
        assert result["keys"] == ["roles.reviewer_model"]
        assert result["skipped"] == ["agent.model"]
        assert "agent.model" in result["warning"]
        assert json.loads(profile_path().read_text(encoding="utf-8")) == {"roles.reviewer_model": "claude-opus-5-5"}

    async def test_a_project_storing_only_short_names_saves_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import profile_path, save_model_profile

        await _store_rows(tmp_path, {"agent.model": "opus", "agent.fallback_models": ["sonnet"]})

        result = save_model_profile(tmp_path)

        assert "pin an explicit model ID first" in result["error"]
        assert not profile_path().exists()

    def test_the_role_keys_follow_the_roles_config_fields(self) -> None:
        from sova.config.models import RolesConfig
        from sova.dashboard.services import model_profile_service as service

        fallback_roles = {n.removesuffix("_fallback_models") for n in RolesConfig.model_fields if "_fallback_" in n}
        assert fallback_roles <= set(service._ROLES)
        assert {f"roles.{role}_fallback_models" for role in fallback_roles} <= service._MODEL_LIST_KEYS
        assert "developer_runtime" not in service._ROLES

    async def test_an_unreadable_profile_is_not_replaced(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import save_model_profile

        await _store_rows(tmp_path, {"agent.model": "claude-sonnet-5-5"})
        path = _write_profile("{not json")

        result = save_model_profile(tmp_path)

        assert result["unreadable_profile"] is True
        # The message names the file as ~/.config/sova/..., never the absolute path under the home directory.
        assert "~/.config/sova/model-profile.json" in result["error"]
        assert str(path.parent) not in result["error"]
        assert path.read_text(encoding="utf-8") == "{not json"

    async def test_a_profile_that_cannot_be_written_is_an_error_not_a_success(self, tmp_path: Path) -> None:
        from sova.dashboard.services import model_profile_service

        await _store_rows(tmp_path, {"agent.model": "claude-sonnet-5-5"})

        with patch.object(model_profile_service, "write_text_atomic", side_effect=PermissionError("read-only")):
            result = model_profile_service.save_model_profile(tmp_path)

        assert result["unavailable"] is True
        assert "could not be written" in result["error"]
        # The exception text goes to the server log only: it never reaches an HTTP response.
        assert "read-only" not in result["error"]

    async def test_an_unknown_home_directory_is_an_error_not_a_success(self, tmp_path: Path) -> None:
        from sova.dashboard.services import model_profile_service

        await _store_rows(tmp_path, {"agent.model": "claude-sonnet-5-5"})

        with patch.object(Path, "home", side_effect=RuntimeError("no home")):
            result = model_profile_service.save_model_profile(tmp_path)

        assert result["unavailable"] is True


class TestApplyModelProfile:
    async def test_only_the_missing_keys_are_inserted_and_the_config_sees_them(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(
            json.dumps(
                {
                    "agent.model": "claude-sonnet-5-5",
                    "roles.reviewer_model": "claude-opus-5-5",
                    "llm.routing.review": "claude-opus-5-5",
                }
            )
        )
        # A row holding the built-in default is still a row the user set: never overwritten.
        await _store_rows(tmp_path, {"agent.model": "opus"})

        result = await apply_model_profile(tmp_path)

        assert result == {"applied": ["llm.routing.review", "roles.reviewer_model"]}
        stored = await _stored(tmp_path)
        assert stored["agent.model"] == "opus"
        cfg = load_config(tmp_path)
        assert (cfg.roles.reviewer_model, cfg.llm.routing) == ("claude-opus-5-5", {"review": "claude-opus-5-5"})

    async def test_a_short_model_name_in_the_profile_is_skipped_and_named_in_the_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(
            json.dumps(
                {
                    "agent.model": "opus",
                    "agent.fallback_models": ["claude-sonnet-5-5", "haiku"],
                    "roles.reviewer_model": "claude-opus-5-5",
                }
            )
        )

        result = await apply_model_profile(tmp_path)

        assert result["applied"] == ["roles.reviewer_model"]
        assert "agent.model" in result["warning"]
        assert "agent.fallback_models" in result["warning"]
        assert set(await _stored(tmp_path)) == {"roles.reviewer_model"}

    async def test_an_entry_outside_the_allowed_set_or_a_stale_task_is_skipped_and_the_rest_applied(
        self, tmp_path: Path
    ) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(
            json.dumps(
                {
                    "agent.model": "claude-sonnet-5-5",
                    "llm.provider": "ollama",
                    "llm.api_key": "secret",
                    "llm.routing.retired_task": "x-model",
                    "roles.reviewer_model": 5,
                    "agent.fallback_models": "not-a-list",
                }
            )
        )

        result = await apply_model_profile(tmp_path)

        assert result["applied"] == ["agent.model"]
        for skipped in ("llm.provider", "llm.api_key", "llm.routing.retired_task", "roles.reviewer_model"):
            assert skipped in result["warning"]
        assert set(await _stored(tmp_path)) == {"agent.model"}

    @pytest.mark.parametrize("content", [None, "{not json", "[1, 2]", "null"])
    async def test_a_missing_or_malformed_profile_applies_nothing_and_does_not_raise(
        self, tmp_path: Path, content: str | None
    ) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        if content is not None:
            _write_profile(content)

        result = await apply_model_profile(tmp_path)

        assert result["applied"] == []
        # A missing profile is the normal case; a malformed one is named, never passed off as an empty one.
        assert ("warning" in result) is (content is not None)
        assert await _stored(tmp_path) == {}

    async def test_a_whole_table_routing_row_keeps_the_profile_routes_out(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(json.dumps({"llm.routing.review": "claude-opus-5-5", "agent.model": "claude-sonnet-5-5"}))
        await _store_rows(tmp_path, {"llm.routing": {"develop": "x-model"}})

        assert await apply_model_profile(tmp_path) == {"applied": ["agent.model"]}

    async def test_a_row_inserted_by_a_concurrent_install_is_not_overwritten(self, tmp_path: Path) -> None:
        from sova.dashboard.services import model_profile_service

        _write_profile(json.dumps({"agent.model": "claude-sonnet-5-5"}))

        from sqlalchemy.ext.asyncio import AsyncSession

        original_execute = AsyncSession.execute
        reads = 0

        async def race(self: AsyncSession, *args: object, **kwargs: object) -> object:
            nonlocal reads
            reads += 1
            if reads > 1:
                return await original_execute(self, *args, **kwargs)
            # The install's own read of which keys exist: it sees none, then the other install commits its row.
            await _store_rows(tmp_path, {"agent.model": "chosen-by-the-other-install"})

            class _NoRows:
                def scalars(self) -> list[str]:
                    return []

            return _NoRows()

        with patch.object(AsyncSession, "execute", race):
            result = await model_profile_service.apply_model_profile(tmp_path)

        assert result == {"applied": []}
        assert (await _stored(tmp_path))["agent.model"] == "chosen-by-the-other-install"

    async def test_an_unwritable_database_is_reported_not_taken_for_nothing_to_apply(self, tmp_path: Path) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from sova.dashboard.services import model_profile_service

        _write_profile(json.dumps({"agent.model": "claude-sonnet-5-5"}))

        with patch.object(model_profile_service, "get_session", side_effect=SQLAlchemyError("/srv/db down")):
            result = await model_profile_service.apply_model_profile(tmp_path)

        assert "could not be applied" in result["error"]
        assert "/srv/db" not in result["error"]

    async def test_a_key_sova_toml_sets_keeps_its_toml_value(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(
            json.dumps(
                {
                    "agent.model": "profile-model",
                    "llm.routing.review": "profile-model",
                    "llm.routing.develop": "profile-model",
                    "roles.reviewer_model": "profile-model",
                }
            )
        )
        (tmp_path / "sova.toml").write_text(
            '[agent]\nmodel = "project-model"\n\n[llm.routing]\nreview = "project-model"\n', encoding="utf-8"
        )
        # The install's default rows: the database is not empty, so sova.toml is never migrated into it.
        await _store_rows(tmp_path, {"github_repo": "owner/repo"})

        result = await apply_model_profile(tmp_path)

        assert result == {"applied": ["llm.routing.develop", "roles.reviewer_model"]}
        cfg = load_config(tmp_path)
        assert cfg.agent.model == "project-model"
        assert cfg.llm.routing == {"review": "project-model", "develop": "profile-model"}

    async def test_an_unreadable_sova_toml_applies_nothing(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(json.dumps({"agent.model": "profile-model"}))
        (tmp_path / "sova.toml").write_text("[agent\n", encoding="utf-8")

        result = await apply_model_profile(tmp_path)

        assert "sova.toml could not be read" in result["error"]
        assert await _stored(tmp_path) == {}


class TestInstallAppliesTheProfile:
    async def test_a_failed_or_skipped_profile_is_a_warning_and_a_success_line_names_what_was_applied(
        self, tmp_path: Path
    ) -> None:
        from sova.cli.commands import project

        with (
            patch(
                "sova.dashboard.services.model_profile_service.apply_model_profile",
                side_effect=[
                    {"error": "could not be applied"},
                    {"applied": ["agent.model"], "warning": "skipped llm.provider"},
                ],
            ),
            patch.object(project.console, "print") as out,
        ):
            await project._apply_model_profile(tmp_path)
            await project._apply_model_profile(tmp_path)

        printed = [call.args[0] for call in out.call_args_list]
        assert "Warning: could not be applied" in printed[0]
        assert "Warning: skipped llm.provider" in printed[1]
        assert "agent.model" in printed[2]


class TestApplyInsertFailures:
    async def test_a_row_taken_in_between_is_left_alone_and_not_a_warning(self, tmp_path: Path) -> None:
        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(json.dumps({"roles.reviewer_model": "profile-model"}))
        await _store_rows(tmp_path, {"github_repo": "owner/repo"})

        async def lost_race(session, key, value):  # noqa: ANN001 (stand-in for the helper)
            from sova.config.db_loader import save_setting

            await save_setting(session, key, "other-install")
            return False

        with patch("sova.dashboard.services.model_profile_service.insert_setting_if_absent", lost_race):
            result = await apply_model_profile(tmp_path)

        assert result == {"applied": []}
        assert (await _stored(tmp_path))["roles.reviewer_model"] == "other-install"

    async def test_an_integrity_failure_that_leaves_no_row_is_a_warning_naming_the_key(self, tmp_path: Path) -> None:
        from sqlalchemy.exc import IntegrityError

        from sova.dashboard.services.model_profile_service import apply_model_profile

        _write_profile(json.dumps({"roles.reviewer_model": "profile-model", "agent.model": "profile-model"}))
        await _store_rows(tmp_path, {"github_repo": "owner/repo"})

        async def broken(session, key, value):  # noqa: ANN001 (stand-in for the helper)
            if key == "agent.model":
                raise IntegrityError("insert", {}, Exception("NOT NULL constraint failed"))
            from sova.config.db_loader import insert_setting_if_absent

            return await insert_setting_if_absent(session, key, value)

        with patch("sova.dashboard.services.model_profile_service.insert_setting_if_absent", broken):
            result = await apply_model_profile(tmp_path)

        assert result["applied"] == ["roles.reviewer_model"]
        assert "agent.model" in result["warning"]
        assert "NOT NULL" not in result["warning"]

    async def test_insert_setting_if_absent_never_updates_and_reraises_a_failure_with_no_row(
        self, tmp_path: Path
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        from sova.config.db_loader import insert_setting_if_absent
        from sova.db.session import get_session

        await _store_rows(tmp_path, {"agent.model": "mine"})
        async with await get_session(project_dir=tmp_path) as session, session.begin():
            assert await insert_setting_if_absent(session, "agent.model", "theirs") is False
            assert await insert_setting_if_absent(session, "roles.reviewer_model", "theirs") is True
        assert await _stored(tmp_path) == {"agent.model": "mine", "roles.reviewer_model": "theirs"}

        async with await get_session(project_dir=tmp_path) as session, session.begin():
            with patch.object(session, "flush", side_effect=IntegrityError("insert", {}, Exception("CHECK"))):
                with pytest.raises(IntegrityError):
                    await insert_setting_if_absent(session, "roles.triage_model", "x")


@pytest.mark.usefixtures("no_cli_provider_init", "quiet_logs")
class TestInstallCommand:
    """``_install()`` itself: the profile fills what a fresh install leaves unset, never a stored row."""

    @pytest.fixture(autouse=True)
    async def _install_environment(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
        from sova.cli.commands import project

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        monkeypatch.setattr(project, "_configure_git_hooks", _noop_async)
        monkeypatch.setattr(project, "_exclude_runtime_files", _noop_async)
        monkeypatch.setattr(project, "_verify_install", lambda *_a, **_k: ([], []))
        yield
        # _install() opens the project database more than once; close every engine it left behind.
        await _close_engines()

    async def test_a_fresh_install_fills_agent_model_and_routes_and_keeps_an_existing_row(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _write_profile(
            json.dumps(
                {
                    "agent.model": "profile-model",
                    "llm.routing.develop": "profile-model",
                    "roles.reviewer_model": "profile-model",
                }
            )
        )
        await _store_rows(tmp_path, {"roles.reviewer_model": "project-model", "github_repo": "owner/repo"})
        await _close_engines()

        await _install(path=tmp_path, no_dashboard=True, update=False)

        stored = await _stored(tmp_path)
        assert stored["agent.model"] == "profile-model"
        assert stored["llm.routing.develop"] == "profile-model"
        assert stored["roles.reviewer_model"] == "project-model"

    async def test_a_fresh_install_seeds_no_agent_model_row_of_its_own(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        await _install(path=tmp_path, no_dashboard=True, update=False)

        assert "agent.model" not in await _stored(tmp_path)

    async def test_update_applies_nothing(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _write_profile(json.dumps({"agent.model": "profile-model"}))
        await _store_rows(tmp_path, {"github_repo": "owner/repo"})
        await _close_engines()

        await _install(path=tmp_path, no_dashboard=True, update=True)

        assert "agent.model" not in await _stored(tmp_path)

    async def test_a_failed_database_stage_skips_the_profile(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.cli.commands import project

        with (
            patch("sova.db.session.init_db", side_effect=OSError("disk full")),
            patch.object(project, "_apply_model_profile", AsyncMock()) as apply,
        ):
            await project._install(path=tmp_path, no_dashboard=True, update=False)

        apply.assert_not_awaited()


async def _close_engines() -> None:
    """Dispose every database engine, so _install() starts on a fresh one and none leaks into the next test."""
    from sova.db.session import close_db

    await close_db()


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None
