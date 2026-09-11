"""Tests for sova.dashboard.services.dependency_health_service."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sova.dashboard.services import dependency_health_service as svc


class TestParseVersion:
    def test_parses_semver(self) -> None:
        assert svc._parse_version("1.2.3") == (1, 2, 3)

    def test_parses_with_prefix(self) -> None:
        assert svc._parse_version("v1.2.3") == (1, 2, 3)

    def test_parses_with_prerelease_suffix(self) -> None:
        assert svc._parse_version("1.2.3-beta.1") == (1, 2, 3)

    def test_none_input(self) -> None:
        assert svc._parse_version(None) is None

    def test_unparseable(self) -> None:
        assert svc._parse_version("not-a-version") is None

    def test_empty_string(self) -> None:
        assert svc._parse_version("") is None


class TestLagCategory:
    def test_up_to_date(self) -> None:
        assert svc._lag_category("1.2.3", "1.2.3") == "up_to_date"

    def test_current_ahead_of_latest(self) -> None:
        assert svc._lag_category("2.0.0", "1.2.3") == "up_to_date"

    def test_major_behind(self) -> None:
        assert svc._lag_category("1.0.0", "2.0.0") == "major"

    def test_minor_behind(self) -> None:
        assert svc._lag_category("1.1.0", "1.2.0") == "minor"

    def test_patch_behind(self) -> None:
        assert svc._lag_category("1.2.1", "1.2.3") == "patch"

    def test_unknown_when_current_missing(self) -> None:
        assert svc._lag_category(None, "1.2.3") == "unknown"

    def test_unknown_when_latest_missing(self) -> None:
        assert svc._lag_category("1.2.3", None) == "unknown"


class TestIsStale:
    def test_recent_not_stale(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert svc._is_stale(recent) is False

    def test_old_is_stale(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        assert svc._is_stale(old) is True

    def test_none_not_stale(self) -> None:
        assert svc._is_stale(None) is False

    def test_malformed_not_stale(self) -> None:
        assert svc._is_stale("not-a-date") is False


class TestComputeUrgency:
    def test_up_to_date_no_flags(self) -> None:
        assert svc._compute_urgency("up_to_date", False, False, False) == 0

    def test_major_behind(self) -> None:
        assert svc._compute_urgency("major", False, False, False) == 40

    def test_minor_behind(self) -> None:
        assert svc._compute_urgency("minor", False, False, False) == 15

    def test_patch_behind(self) -> None:
        assert svc._compute_urgency("patch", False, False, False) == 5

    def test_deprecated_adds(self) -> None:
        assert svc._compute_urgency("up_to_date", True, False, False) == 40

    def test_vulnerable_adds(self) -> None:
        assert svc._compute_urgency("up_to_date", False, True, False) == 40

    def test_stale_adds(self) -> None:
        assert svc._compute_urgency("up_to_date", False, False, True) == 15

    def test_clamped_to_100(self) -> None:
        assert svc._compute_urgency("major", True, True, True) == 100


class TestScanNpm:
    def test_manifest_only(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"react": "18.2.0"}, "devDependencies": {"jest": "~29.0.0"}})
        )
        result = svc._scan_npm(tmp_path)
        # A bare version with no range prefix is an exact pin; a range (`~29.0.0`)
        # is not the installed version, so it is reported as unresolved (None).
        assert result == {"react": "18.2.0", "jest": None}

    def test_lockfile_preferred_over_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))
        (tmp_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        "": {},
                        "node_modules/react": {"version": "18.2.0"},
                        "node_modules/@scope/pkg": {"version": "1.0.0"},
                    }
                }
            )
        )
        result = svc._scan_npm(tmp_path)
        assert result["react"] == "18.2.0"

    def test_transitive_lockfile_entries_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))
        (tmp_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        "": {},
                        "node_modules/react": {"version": "18.2.0"},
                        "node_modules/@scope/pkg": {"version": "1.0.0"},
                    }
                }
            )
        )
        result = svc._scan_npm(tmp_path)
        assert "@scope/pkg" not in result

    def test_direct_dep_missing_from_lockfile_falls_back_to_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0", "lodash": "4.17.0"}}))
        (tmp_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "packages": {
                        "": {},
                        "node_modules/react": {"version": "18.2.0"},
                    }
                }
            )
        )
        result = svc._scan_npm(tmp_path)
        assert result["react"] == "18.2.0"
        # lodash falls back to the manifest, which pins an exact version.
        assert result["lodash"] == "4.17.0"

    def test_no_manifest_returns_empty(self, tmp_path: Path) -> None:
        assert svc._scan_npm(tmp_path) == {}

    def test_malformed_manifest_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{not valid json")
        assert svc._scan_npm(tmp_path) == {}

    def test_malformed_lockfile_falls_back_to_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "18.0.0"}}))
        (tmp_path / "package-lock.json").write_text("{not valid json")
        result = svc._scan_npm(tmp_path)
        assert result == {"react": "18.0.0"}


class TestScanPython:
    def test_requirements_txt_pinned(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n# a comment\n-e .\nflask>=2.0.0\n")
        result = svc._scan_python(tmp_path)
        assert result["requests"] == "2.31.0"
        # `>=` is a range, not an exact pin, so the boundary is not reported as installed.
        assert result["flask"] is None

    def test_requirements_txt_no_version(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        result = svc._scan_python(tmp_path)
        assert result["requests"] is None

    def test_pyproject_project_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["requests==2.31.0", "flask"]\n')
        result = svc._scan_python(tmp_path)
        assert result["requests"] == "2.31.0"
        assert result["flask"] is None

    def test_pyproject_poetry_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "2.31.0"\n')
        result = svc._scan_python(tmp_path)
        assert "python" not in result
        assert result["requests"] == "2.31.0"

    def test_pyproject_poetry_range_dependency_is_unresolved(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\nrequests = "^2.31.0"\n')
        result = svc._scan_python(tmp_path)
        # `^2.31.0` is a range, not an exact pin, so it is reported as unresolved.
        assert result["requests"] is None

    def test_no_files_returns_empty(self, tmp_path: Path) -> None:
        assert svc._scan_python(tmp_path) == {}

    def test_requirements_takes_precedence_over_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["requests==1.0.0"]\n')
        result = svc._scan_python(tmp_path)
        assert result["requests"] == "2.31.0"


class TestScanManifests:
    def test_empty_project(self, tmp_path: Path) -> None:
        assert svc.scan_manifests(tmp_path) == {}

    def test_mixed_ecosystems(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "18.2.0"}}))
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        result = svc.scan_manifests(tmp_path)
        assert set(result.keys()) == {"npm", "pypi"}


class TestPackageHealthBuilders:
    def test_npm_health_up_to_date(self) -> None:
        data = {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"license": "MIT"}},
            "time": {"1.0.0": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()},
        }
        pkg = svc._npm_package_health("foo", "1.0.0", data)
        assert pkg.lag_category == "up_to_date"
        assert pkg.deprecated is False
        assert pkg.license == "MIT"
        assert pkg.urgency == 0

    def test_npm_health_deprecated(self) -> None:
        data = {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"deprecated": "use bar instead"}},
            "time": {},
        }
        pkg = svc._npm_package_health("foo", "1.0.0", data)
        assert pkg.deprecated is True
        assert pkg.urgency == 40

    def test_npm_health_fetch_failed(self) -> None:
        pkg = svc._npm_package_health("foo", "1.0.0", None)
        assert pkg.lag_category == "unknown"
        assert pkg.latest_version is None
        assert pkg.urgency == 0

    def test_pypi_health_major_behind(self) -> None:
        data = {
            "info": {"version": "2.0.0", "classifiers": [], "license": "Apache-2.0"},
            "releases": {"2.0.0": [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}]},
        }
        pkg = svc._pypi_package_health("foo", "1.0.0", data)
        assert pkg.lag_category == "major"
        assert pkg.license == "Apache-2.0"
        assert pkg.urgency == 40

    def test_pypi_health_inactive_classifier_is_deprecated(self) -> None:
        data = {
            "info": {"version": "1.0.0", "classifiers": ["Development Status :: 7 - Inactive"]},
            "releases": {"1.0.0": []},
        }
        pkg = svc._pypi_package_health("foo", "1.0.0", data)
        assert pkg.deprecated is True

    def test_pypi_health_fetch_failed(self) -> None:
        pkg = svc._pypi_package_health("foo", "1.0.0", None)
        assert pkg.lag_category == "unknown"


class TestGetSnapshot:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        svc._cache.clear()
        yield
        svc._cache.clear()

    async def test_disabled_returns_empty(self, tmp_path: Path) -> None:
        result = await svc.get_snapshot(tmp_path, enabled=False, cache_ttl_minutes=30, registry_timeout_seconds=10)
        assert result.enabled is False
        assert result.total == 0

    async def test_no_manifests_returns_empty_enabled(self, tmp_path: Path) -> None:
        result = await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
        assert result.enabled is True
        assert result.total == 0
        assert result.ecosystems == []

    async def test_scans_and_caches(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        fake_pkg = svc.PackageHealth("requests", "pypi", "2.31.0", "2.31.0", "up_to_date", False, False, None, None, 0)
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(return_value=([fake_pkg], 0))) as mock_fetch:
            result1 = await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
            result2 = await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
        assert result1.total == 1
        assert result2.total == 1
        mock_fetch.assert_called_once()

    async def test_force_refresh_bypasses_cache(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        fake_pkg = svc.PackageHealth("requests", "pypi", "2.31.0", "2.31.0", "up_to_date", False, False, None, None, 0)
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(return_value=([fake_pkg], 0))) as mock_fetch:
            await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
            await svc.get_snapshot(
                tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10, force_refresh=True
            )
        assert mock_fetch.call_count == 2

    async def test_health_percent_calculation(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("a==1.0.0\nb==1.0.0\n")
        packages = [
            svc.PackageHealth("a", "pypi", "1.0.0", "1.0.0", "up_to_date", False, False, None, None, 0),
            svc.PackageHealth("b", "pypi", "1.0.0", "2.0.0", "major", False, False, None, None, 40),
        ]
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(return_value=(packages, 0))):
            result = await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
        assert result.total == 2
        assert result.up_to_date == 1
        assert result.major_behind == 1
        assert result.health_percent == 50.0
        # Packages sorted by urgency descending
        assert result.packages[0].name == "b"

    async def test_health_percent_excludes_up_to_date_but_vulnerable(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("a==1.0.0\n")
        # up-to-date lag category but flagged vulnerable -> must not count as healthy.
        packages = [svc.PackageHealth("a", "pypi", "1.0.0", "1.0.0", "up_to_date", False, True, None, None, 40)]
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(return_value=(packages, 0))):
            result = await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
        assert result.up_to_date == 1
        assert result.health_percent == 0.0


class TestCappedFanOut:
    def test_under_limit_returns_all(self) -> None:
        deps = {f"p{i}": "1.0.0" for i in range(5)}
        entries, dropped = svc._capped("npm", deps)
        assert len(entries) == 5
        assert dropped == 0

    def test_over_limit_truncates(self) -> None:
        deps = {f"p{i:04d}": "1.0.0" for i in range(svc._MAX_PACKAGES_PER_ECOSYSTEM + 50)}
        capped, dropped = svc._capped("npm", deps)
        assert len(capped) == svc._MAX_PACKAGES_PER_ECOSYSTEM
        assert dropped == 50
        # Stable (sorted) order, so the same subset is scanned every cycle.
        assert capped[0][0] == "p0000"


class TestHasInactiveClassifier:
    def test_inactive_present(self) -> None:
        assert svc._has_inactive_classifier(["Development Status :: 7 - Inactive"]) is True

    def test_absent(self) -> None:
        assert svc._has_inactive_classifier(["Programming Language :: Python"]) is False

    def test_non_list_payload(self) -> None:
        assert svc._has_inactive_classifier("Inactive") is False

    def test_non_string_element_does_not_raise(self) -> None:
        assert svc._has_inactive_classifier([None, 42, {"a": 1}]) is False


class TestAnnotateVulnerabilities:
    @staticmethod
    def _pkg(name: str) -> svc.PackageHealth:
        return svc.PackageHealth(name, "pypi", "1.0.0", "1.0.0", "up_to_date", False, False, None, None, 0)

    async def test_marks_vulnerable_and_bumps_urgency(self) -> None:
        packages = [self._pkg("a"), self._pkg("b")]
        with patch.object(svc, "_annotate_chunk", new=AsyncMock()) as mock_chunk:
            mock_chunk.side_effect = lambda _client, chunk: chunk[0].__setattr__("vulnerable", True)
            await svc._annotate_vulnerabilities(packages, timeout_seconds=1.0)
        assert packages[0].vulnerable is True

    async def test_skips_packages_without_current_version(self) -> None:
        pkg = svc.PackageHealth("a", "pypi", None, "1.0.0", "unknown", False, False, None, None, 0)
        with patch.object(svc, "_annotate_chunk", new=AsyncMock()) as mock_chunk:
            await svc._annotate_vulnerabilities([pkg], timeout_seconds=1.0)
        mock_chunk.assert_not_called()

    async def test_chunks_oversized_query_list(self) -> None:
        packages = [self._pkg(f"p{i}") for i in range(svc._OSV_BATCH_SIZE + 1)]
        with patch.object(svc, "_annotate_chunk", new=AsyncMock()) as mock_chunk:
            await svc._annotate_vulnerabilities(packages, timeout_seconds=1.0)
        assert mock_chunk.call_count == 2
        assert len(mock_chunk.call_args_list[0].args[1]) == svc._OSV_BATCH_SIZE
        assert len(mock_chunk.call_args_list[1].args[1]) == 1

    async def test_chunk_failure_leaves_packages_unannotated(self) -> None:
        pkg = self._pkg("a")

        class _FailingClient:
            async def post(self, *_args, **_kwargs):
                raise httpx.ConnectError("boom")

        await svc._annotate_chunk(_FailingClient(), [pkg])
        assert pkg.vulnerable is False

    async def test_chunk_marks_vulnerable_from_osv_results(self) -> None:
        pkg = self._pkg("a")

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"results": [{"vulns": [{"id": "GHSA-x"}]}]}

        class _Client:
            async def post(self, *_args, **_kwargs):
                return _Resp()

        await svc._annotate_chunk(_Client(), [pkg])
        assert pkg.vulnerable is True
        assert pkg.urgency == 40


class TestSingleFlightScan:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        svc._cache.clear()
        svc._scan_locks.clear()
        yield
        svc._cache.clear()
        svc._scan_locks.clear()

    async def test_concurrent_callers_share_one_scan(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        fake = svc.PackageHealth("requests", "pypi", "2.31.0", "2.31.0", "up_to_date", False, False, None, None, 0)

        async def _slow_fetch(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return [fake], 0

        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(side_effect=_slow_fetch)) as mock_fetch:
            results = await asyncio.gather(
                *[
                    svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
                    for _ in range(4)
                ]
            )
        mock_fetch.assert_called_once()
        assert all(r.total == 1 for r in results)

    async def test_cache_only_does_not_scan_on_miss(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock()) as mock_fetch:
            result = await svc.get_snapshot(
                tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10, cache_only=True
            )
        mock_fetch.assert_not_called()
        assert result.total == 0
        assert result.enabled is True

    async def test_cache_only_returns_warm_cache(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
        fake = svc.PackageHealth("requests", "pypi", "2.31.0", "2.31.0", "up_to_date", False, False, None, None, 0)
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock(return_value=([fake], 0))):
            await svc.get_snapshot(tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10)
        with patch.object(svc, "_fetch_all_packages", new=AsyncMock()) as mock_fetch:
            result = await svc.get_snapshot(
                tmp_path, enabled=True, cache_ttl_minutes=30, registry_timeout_seconds=10, cache_only=True
            )
        mock_fetch.assert_not_called()
        assert result.total == 1


class TestRegistryFetchUrl:
    async def test_scoped_npm_name_keeps_slash_and_at(self) -> None:
        captured: list[str] = []

        class _Resp:
            status_code = 404

        class _Client:
            async def get(self, url: str):
                captured.append(url)
                return _Resp()

        await svc._fetch_registry_json(_Client(), svc._NPM_REGISTRY_URL, "@scope/pkg")
        assert captured == ["https://registry.npmjs.org/@scope/pkg"]

    async def test_hostile_name_cannot_reshape_url(self) -> None:
        captured: list[str] = []

        class _Resp:
            status_code = 404

        class _Client:
            async def get(self, url: str):
                captured.append(url)
                return _Resp()

        await svc._fetch_registry_json(_Client(), svc._PYPI_REGISTRY_URL, "evil?x=1#frag")
        assert "?" not in captured[0].removeprefix("https://pypi.org/pypi/")
        assert "#" not in captured[0]
