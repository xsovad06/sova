"""Pure unit tests for ``sova.git.artifact_exclusions``' pathspec builders (no git subprocess).

The end-to-end behaviour (git accepting the pathspecs, a sidecar staying out of a stash) is covered against a
real repository in ``tests/test_untracked_collisions.py``; these pin the pathspec shapes themselves (#1522).
"""

from __future__ import annotations

from sova.config.db_loader import SETTINGS_STAMP_SUFFIX
from sova.git.artifact_exclusions import (
    INFO_EXCLUDE_ENTRIES,
    RUNTIME_STATE_PATHS,
    _covered_by_sidecar_prefix,
    _needs_sidecar_exclude,
    exclude_pathspecs,
)

DB = ".claude/sova.db"
STAMP = ".claude/sova.db.settings-stamp"


class TestSettingsStampIsRuntimeState:
    def test_runtime_state_paths_name_the_settings_stamp(self) -> None:
        assert STAMP in RUNTIME_STATE_PATHS

    def test_info_exclude_entries_anchor_the_settings_stamp(self) -> None:
        assert f"/{STAMP}*" in INFO_EXCLUDE_ENTRIES

    def test_the_stamp_literal_matches_the_config_cache_suffix(self) -> None:
        """The module hard-codes the stamp to stay a leaf; a rename of the suffix must fail here, not drift."""
        assert f"{DB}{SETTINGS_STAMP_SUFFIX}" == STAMP
        assert f"{DB}{SETTINGS_STAMP_SUFFIX}" in RUNTIME_STATE_PATHS


class TestExcludePathspecsSidecarGlobs:
    def test_exclude_pathspecs_adds_dash_and_dot_globs_when_only_the_db_is_ignored(self) -> None:
        specs = exclude_pathspecs((DB,), ignored=frozenset({DB}))

        assert f":(exclude,glob){DB}-*" in specs
        assert f":(exclude,glob){DB}.*" in specs
        assert f":(exclude){DB}*" not in specs

    def test_exclude_pathspecs_keeps_the_prefix_exclude_when_nothing_is_ignored(self) -> None:
        specs = exclude_pathspecs((DB, STAMP))

        assert specs[0] == ":/"
        assert f":(exclude){DB}*" in specs
        # The `sova.db` prefix exclude already covers the stamp, so it gets no literal exclude of its own.
        assert not any(STAMP in spec for spec in specs)
        assert not any(spec.startswith(":(exclude,glob)") for spec in specs)

    def test_exclude_pathspecs_emits_nothing_for_an_ignored_stamp_the_db_globs_cover(self) -> None:
        """With the install rules both are ignored: only the `sova.db` sidecar globs remain, none for the stamp."""
        specs = exclude_pathspecs((DB, STAMP), ignored=frozenset({DB, STAMP}))

        assert specs == (":/", f":(exclude,glob){DB}-*", f":(exclude,glob){DB}.*")

    def test_exclude_pathspecs_orders_sidecar_globs_before_keep_and_extra(self) -> None:
        specs = exclude_pathspecs(
            (DB,),
            ignored=frozenset({DB}),
            keep=(f"{DB}-wal",),
            extra=(":(exclude,glob)**/.env",),
        )

        assert specs == (
            ":/",
            f":(exclude,glob){DB}-*",
            f":(exclude,glob){DB}.*",
            f":(exclude,literal){DB}-wal",
            ":(exclude,glob)**/.env",
        )

    def test_exclude_pathspecs_adds_no_sidecar_glob_when_the_parent_dir_is_ignored(self) -> None:
        specs = exclude_pathspecs((DB, STAMP), ignored=frozenset({".claude"}))

        assert specs == (":/",)


class TestNeedsSidecarExclude:
    def test_needs_sidecar_exclude_false_for_a_directory_prefix(self) -> None:
        assert _needs_sidecar_exclude(".claude/agent-control/", frozenset({".claude/agent-control"})) is False

    def test_needs_sidecar_exclude_false_when_the_prefix_is_not_ignored(self) -> None:
        assert _needs_sidecar_exclude(DB, frozenset()) is False

    def test_needs_sidecar_exclude_true_when_only_the_file_is_ignored(self) -> None:
        assert _needs_sidecar_exclude(DB, frozenset({DB})) is True

    def test_needs_sidecar_exclude_false_when_the_parent_dir_is_ignored(self) -> None:
        assert _needs_sidecar_exclude(DB, frozenset({".claude"})) is False

    def test_needs_sidecar_exclude_true_for_a_top_level_ignored_name_prefix(self) -> None:
        assert _needs_sidecar_exclude("sova.db", frozenset({"sova.db"})) is True


class TestCoveredBySidecarPrefix:
    def test_a_dot_sidecar_of_another_name_prefix_is_covered(self) -> None:
        assert _covered_by_sidecar_prefix(STAMP, (DB, STAMP)) is True

    def test_a_dash_sidecar_of_another_name_prefix_is_covered(self) -> None:
        assert _covered_by_sidecar_prefix(f"{DB}-wal", (DB, f"{DB}-wal")) is True

    def test_a_prefix_does_not_cover_itself(self) -> None:
        assert _covered_by_sidecar_prefix(DB, (DB,)) is False

    def test_a_longer_name_without_a_separator_is_not_covered(self) -> None:
        """`sova.dbx` is not a sidecar: the `sova.db` globs (`-*`, `.*`) would not keep it out once dropped."""
        assert _covered_by_sidecar_prefix(".claude/sova.dbx", (DB, ".claude/sova.dbx")) is False

    def test_a_directory_prefix_covers_nothing(self) -> None:
        assert _covered_by_sidecar_prefix(".claude/x.y", (".claude/x/", ".claude/x.y")) is False
