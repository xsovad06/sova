"""Dependency health scanning: manifest detection, registry lookups, urgency scoring.

Scans a target project's package manifests (npm, Python), queries the
relevant registries for the latest version/deprecation/publish data, and
scores each package's urgency (0-100) from version lag, deprecation, and
staleness. Results are cached in-memory per project with a configurable TTL
since registry calls are slow and this data changes infrequently.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import tomllib
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.dependency_health")

_STALE_DAYS = 365
_MAX_CONCURRENT_REQUESTS = 10
# An npm lockfile enumerates the whole transitive tree, so cap the fan-out: an
# uncapped scan issues one registry request per package inside a single HTTP request.
_MAX_PACKAGES_PER_ECOSYSTEM = 400
# OSV.dev querybatch takes a bounded list; a whole-tree payload risks rejection,
# which would drop vulnerability data for every package at once.
_OSV_BATCH_SIZE = 200
# Our ecosystem keys mapped to OSV.dev's. Packages outside this map are not queried.
_OSV_ECOSYSTEMS = {"npm": "npm", "pypi": "PyPI"}
_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_NPM_REGISTRY_URL = "https://registry.npmjs.org/{name}"
_PYPI_REGISTRY_URL = "https://pypi.org/pypi/{name}/json"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_NPM_RANGE_PREFIX_RE = re.compile(r"^[\^~>=<\s]+")
_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(==|>=|<=|~=)\s*([A-Za-z0-9_.\-]+)")
_PEP508_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")

# Bounded LRU: caps memory growth from projects that are renamed or unregistered
# over the process lifetime, since there is no explicit eviction hook for those events.
_MAX_CACHED_PROJECTS = 100
_cache: OrderedDict[Path, tuple[float, DependencyHealthSnapshot]] = OrderedDict()
_cache_lock = asyncio.Lock()
# One scan lock per project so concurrent callers (page load, refresh, awareness
# briefing) share a single scan instead of each fanning out to the registries.
_scan_locks: OrderedDict[Path, asyncio.Lock] = OrderedDict()


def _lru_set(store: OrderedDict[Path, Any], key: Path, value: Any) -> None:
    """Insert/update `key` as most-recently-used, evicting the oldest entry past the cap."""
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MAX_CACHED_PROJECTS:
        store.popitem(last=False)


@dataclass
class PackageHealth:
    """Health data for a single dependency."""

    name: str
    ecosystem: str
    current_version: str | None
    latest_version: str | None
    lag_category: str  # "up_to_date" | "patch" | "minor" | "major" | "unknown"
    deprecated: bool
    vulnerable: bool
    last_published: str | None
    license: str | None
    urgency: int


@dataclass
class DependencyHealthSnapshot:
    """Aggregated dependency health data for a project."""

    enabled: bool
    generated_at: str
    ecosystems: list[str]
    total: int
    up_to_date: int
    major_behind: int
    deprecated: int
    vulnerable: int
    stale: int
    health_percent: float
    truncated: bool = False
    truncated_count: int = 0
    packages: list[PackageHealth] = field(default_factory=list)


def _parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """Extract a (major, minor, patch) tuple from the leading numeric part of a version string."""
    if not raw:
        return None
    match = _VERSION_RE.search(raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _lag_category(current: str | None, latest: str | None) -> str:
    current_v = _parse_version(current)
    latest_v = _parse_version(latest)
    if current_v is None or latest_v is None:
        return "unknown"
    if current_v >= latest_v:
        return "up_to_date"
    if current_v[0] < latest_v[0]:
        return "major"
    if current_v[1] < latest_v[1]:
        return "minor"
    return "patch"


def _is_stale(last_published: str | None) -> bool:
    if not last_published:
        return False
    try:
        published = datetime.fromisoformat(last_published)
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - published).days
    return age_days >= _STALE_DAYS


def _compute_urgency(lag_category: str, deprecated: bool, vulnerable: bool, stale: bool) -> int:
    score = 0
    if lag_category == "major":
        score += 40
    elif lag_category == "minor":
        score += 15
    elif lag_category == "patch":
        score += 5
    if deprecated:
        score += 40
    if vulnerable:
        score += 40
    if stale:
        score += 15
    return min(score, 100)


# ---------------------------------------------------------------------------
# Manifest scanning
# ---------------------------------------------------------------------------


def _exact_version(spec: str) -> str | None:
    """Return `spec` unchanged only if it is an exact pin, or None if it is a range.

    A range like `^1.2.3`, `~1.2.3`, or `>=1.2.3` names an allowed range, not the
    installed version: reporting the stripped boundary (e.g. "1.2.3" for "^1.2.3")
    would misreport what is actually installed. Only a manifest entry with no
    range-operator prefix is treated as identifying a specific version.
    """
    cleaned = spec.strip()
    if not cleaned or _NPM_RANGE_PREFIX_RE.match(cleaned):
        return None
    return cleaned


def _parse_requirement(line: str) -> tuple[str, str | None] | None:
    """Parse a PEP 508 requirement line into (name, pinned_version), or None if it has no name.

    Only `==` identifies an exact installed version; `>=`, `<=`, and `~=` define a
    range, so the version boundary is discarded (kept as None) for those operators
    to avoid misreporting a range bound as the installed version.
    """
    match = _REQUIREMENT_RE.match(line)
    if match:
        name, operator, version = match.group(1), match.group(2), match.group(3)
        return name, version if operator == "==" else None
    name_match = _PEP508_NAME_RE.match(line)
    if name_match:
        return name_match.group(1), None
    return None


def _scan_npm_lockfile(lockfile_path: Path, direct_names: set[str]) -> dict[str, str | None]:
    """Parse npm lockfileVersion 2/3 'packages' map for resolved versions of direct dependencies only.

    The lockfile enumerates the whole transitive tree, so entries are cross-referenced
    against `direct_names` (from package.json's dependencies/devDependencies) to exclude
    nested/transitive packages.
    """
    try:
        data = json.loads(lockfile_path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("dependency_health.npm_lockfile_parse_failed", path=str(lockfile_path), exc_info=True)
        return {}
    packages = data.get("packages")
    if not isinstance(packages, dict):
        return {}
    result: dict[str, str | None] = {}
    for path, meta in packages.items():
        if not path or "node_modules/" not in path or not isinstance(meta, dict):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if name not in direct_names:
            continue
        version = meta.get("version")
        if name and name not in result:
            result[name] = version
    return result


def _scan_npm_manifest(manifest_path: Path) -> dict[str, str | None]:
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("dependency_health.npm_manifest_parse_failed", path=str(manifest_path), exc_info=True)
        return {}
    if not isinstance(data, dict):
        # Valid JSON can still be the wrong shape (e.g. a top-level array).
        log.warning("dependency_health.npm_manifest_invalid_shape", path=str(manifest_path))
        return {}
    result: dict[str, str | None] = {}
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            if isinstance(spec, str):
                result[name] = _exact_version(spec)
    return result


def _scan_npm(project_dir: Path) -> dict[str, str | None]:
    lockfile = project_dir / "package-lock.json"
    manifest = project_dir / "package.json"
    if not manifest.exists():
        return {}
    manifest_deps = _scan_npm_manifest(manifest)
    if lockfile.exists():
        resolved = _scan_npm_lockfile(lockfile, set(manifest_deps))
        if resolved:
            # Direct deps present in the manifest but missing a lockfile entry
            # (e.g. lockfile out of sync) still fall back to the manifest version.
            for name, version in manifest_deps.items():
                resolved.setdefault(name, version)
            return resolved
    return manifest_deps


def _scan_requirements_txt(path: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        log.warning("dependency_health.requirements_read_failed", path=str(path), exc_info=True)
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        parsed = _parse_requirement(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


def _scan_pyproject(path: Path) -> dict[str, str | None]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        log.warning("dependency_health.pyproject_parse_failed", path=str(path), exc_info=True)
        return {}
    result: dict[str, str | None] = {}

    # A valid TOML document can still declare `project` or `tool.poetry.*` as a
    # non-table value (e.g. `project = []`); validate each container before
    # chaining `.get()`/iterating so a malformed section degrades to "nothing
    # found here" instead of raising and failing the whole scan.
    project = data.get("project")
    deps_list = project.get("dependencies") if isinstance(project, dict) else None
    if isinstance(deps_list, list):
        for dep in deps_list:
            if not isinstance(dep, str):
                continue
            parsed = _parse_requirement(dep.strip())
            if parsed:
                result[parsed[0]] = parsed[1]

    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(poetry_deps, dict):
        for name, spec in poetry_deps.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, str):
                result.setdefault(name, _exact_version(spec))
            else:
                result.setdefault(name, None)
    return result


def _scan_python(project_dir: Path) -> dict[str, str | None]:
    requirements = project_dir / "requirements.txt"
    pyproject = project_dir / "pyproject.toml"
    result: dict[str, str | None] = {}
    if requirements.exists():
        result.update(_scan_requirements_txt(requirements))
    if pyproject.exists():
        for name, version in _scan_pyproject(pyproject).items():
            result.setdefault(name, version)
    return result


def scan_manifests(project_dir: Path) -> dict[str, dict[str, str | None]]:
    """Detect and parse package manifests. Returns {ecosystem: {name: current_version}}."""
    ecosystems: dict[str, dict[str, str | None]] = {}
    npm = _scan_npm(project_dir)
    if npm:
        ecosystems["npm"] = npm
    python = _scan_python(project_dir)
    if python:
        ecosystems["pypi"] = python
    return ecosystems


# ---------------------------------------------------------------------------
# Registry lookups
# ---------------------------------------------------------------------------


async def _fetch_registry_json(client: httpx.AsyncClient, url_template: str, name: str) -> dict[str, Any] | None:
    """GET a registry's JSON metadata for a package, or None if the lookup fails."""
    try:
        # Package names come from a manifest; "/" is meaningful for npm scopes,
        # everything else is escaped so a name cannot reshape the request URL.
        resp = await client.get(url_template.format(name=quote(name, safe="@/")))
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        log.debug("dependency_health.registry_fetch_failed", package=name, url=url_template, exc_info=True)
        return None


def _build_package_health(
    name: str,
    ecosystem: str,
    current: str | None,
    latest: str | None,
    *,
    deprecated: bool = False,
    last_published: str | None = None,
    license_id: str | None = None,
) -> PackageHealth:
    """Assemble a PackageHealth, deriving lag, staleness, and urgency from the registry facts."""
    lag = _lag_category(current, latest)
    urgency = _compute_urgency(lag, deprecated, False, _is_stale(last_published))
    return PackageHealth(name, ecosystem, current, latest, lag, deprecated, False, last_published, license_id, urgency)


def _npm_package_health(name: str, current: str | None, data: dict[str, Any] | None) -> PackageHealth:
    if data is None:
        return _build_package_health(name, "npm", current, None)
    latest = data.get("dist-tags", {}).get("latest")
    versions = data.get("versions", {})
    latest_meta = versions.get(latest, {}) if isinstance(versions, dict) else {}
    last_published = data.get("time", {}).get(latest) if isinstance(data.get("time"), dict) else None
    license_id = latest_meta.get("license")
    if isinstance(license_id, dict):
        license_id = license_id.get("type")
    return _build_package_health(
        name,
        "npm",
        current,
        latest,
        deprecated=bool(latest_meta.get("deprecated")),
        last_published=last_published,
        license_id=license_id,
    )


def _has_inactive_classifier(classifiers: Any) -> bool:
    """True if PyPI marks the project inactive. Tolerates a non-list/non-str classifier payload."""
    if not isinstance(classifiers, list):
        return False
    return any(isinstance(c, str) and "Inactive" in c for c in classifiers)


def _pypi_package_health(name: str, current: str | None, data: dict[str, Any] | None) -> PackageHealth:
    if data is None:
        return _build_package_health(name, "pypi", current, None)
    info = data.get("info", {})
    latest = info.get("version")
    classifiers = info.get("classifiers", [])
    releases = data.get("releases", {}).get(latest, [])
    latest_release = releases[0] if isinstance(releases, list) and releases else {}
    last_published = latest_release.get("upload_time_iso_8601") or None
    return _build_package_health(
        name,
        "pypi",
        current,
        latest,
        deprecated=_has_inactive_classifier(classifiers),
        last_published=last_published,
        license_id=info.get("license") or None,
    )


def _capped(ecosystem: str, deps: dict[str, str | None]) -> tuple[list[tuple[str, str | None]], int]:
    """Limit an ecosystem to _MAX_PACKAGES_PER_ECOSYSTEM entries, keeping a stable order.

    Returns (kept_entries, dropped_count) so callers can surface truncation to the API/UI.
    """
    entries = sorted(deps.items())
    dropped = 0
    if len(entries) > _MAX_PACKAGES_PER_ECOSYSTEM:
        dropped = len(entries) - _MAX_PACKAGES_PER_ECOSYSTEM
        log.warning(
            "dependency_health.package_limit_reached",
            ecosystem=ecosystem,
            total=len(entries),
            limit=_MAX_PACKAGES_PER_ECOSYSTEM,
        )
        entries = entries[:_MAX_PACKAGES_PER_ECOSYSTEM]
    return entries, dropped


async def _fetch_all_packages(
    ecosystems: dict[str, dict[str, str | None]],
    *,
    timeout_seconds: float,
) -> tuple[list[PackageHealth], int]:
    sem = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
    packages: list[PackageHealth] = []

    async def _fetch_one(ecosystem: str, name: str, current: str | None, client: httpx.AsyncClient) -> PackageHealth:
        async with sem:
            url = _NPM_REGISTRY_URL if ecosystem == "npm" else _PYPI_REGISTRY_URL
            data = await _fetch_registry_json(client, url, name)
            try:
                if ecosystem == "npm":
                    return _npm_package_health(name, current, data)
                return _pypi_package_health(name, current, data)
            except (AttributeError, TypeError, ValueError):
                # An unexpected registry payload shape must degrade this one package
                # to "unknown", not abort the whole scan and 500 the request.
                log.warning("dependency_health.package_parse_failed", package=name, ecosystem=ecosystem, exc_info=True)
                return _build_package_health(name, ecosystem, current, None)

    truncated_count = 0
    capped_by_ecosystem: dict[str, list[tuple[str, str | None]]] = {}
    for ecosystem, deps in ecosystems.items():
        entries, dropped = _capped(ecosystem, deps)
        capped_by_ecosystem[ecosystem] = entries
        truncated_count += dropped

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        tasks = [
            _fetch_one(ecosystem, name, current, client)
            for ecosystem, entries in capped_by_ecosystem.items()
            for name, current in entries
        ]
        if tasks:
            packages = await asyncio.gather(*tasks)

    await _annotate_vulnerabilities(packages, timeout_seconds=timeout_seconds)
    return packages, truncated_count


async def _annotate_vulnerabilities(packages: list[PackageHealth], *, timeout_seconds: float) -> None:
    """Batch-query OSV.dev for known vulnerabilities and update urgency in place.

    Best-effort: any failure leaves `vulnerable` as False rather than raising,
    since vulnerability data is a supplement to (not a prerequisite for) the
    version-lag scoring already computed.
    """
    queryable = [p for p in packages if p.ecosystem in _OSV_ECOSYSTEMS and p.current_version]
    if not queryable:
        return
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for start in range(0, len(queryable), _OSV_BATCH_SIZE):
            chunk = queryable[start : start + _OSV_BATCH_SIZE]
            await _annotate_chunk(client, chunk)


async def _annotate_chunk(client: httpx.AsyncClient, chunk: list[PackageHealth]) -> None:
    """Query OSV for one bounded chunk. A failed chunk leaves only that chunk unannotated."""
    queries = [
        {"package": {"name": p.name, "ecosystem": _OSV_ECOSYSTEMS[p.ecosystem]}, "version": p.current_version}
        for p in chunk
    ]
    try:
        resp = await client.post(_OSV_BATCH_URL, json={"queries": queries})
        if resp.status_code != 200:
            log.debug("dependency_health.osv_query_rejected", status=resp.status_code, count=len(queries))
            return
        results = resp.json().get("results", [])
    except (httpx.HTTPError, json.JSONDecodeError):
        log.debug("dependency_health.osv_query_failed", count=len(queries), exc_info=True)
        return
    if len(results) != len(chunk):
        log.warning(
            "dependency_health.osv_result_count_mismatch",
            expected=len(chunk),
            received=len(results),
        )
        return
    for pkg, result in zip(chunk, results, strict=True):
        if isinstance(result, dict) and result.get("vulns"):
            pkg.vulnerable = True
            pkg.urgency = _compute_urgency(pkg.lag_category, pkg.deprecated, True, _is_stale(pkg.last_published))


def _build_snapshot(
    ecosystems: dict[str, dict[str, str | None]],
    packages: list[PackageHealth],
    *,
    enabled: bool = True,
    truncated_count: int = 0,
) -> DependencyHealthSnapshot:
    total = len(packages)
    up_to_date = sum(1 for p in packages if p.lag_category == "up_to_date")
    major_behind = sum(1 for p in packages if p.lag_category == "major")
    deprecated = sum(1 for p in packages if p.deprecated)
    vulnerable = sum(1 for p in packages if p.vulnerable)
    stale = sum(1 for p in packages if _is_stale(p.last_published))
    # `urgency == 0` means no finding at all (current, not deprecated/vulnerable/stale),
    # not just "on the latest version": an up-to-date-but-vulnerable package must not
    # count as healthy.
    healthy = sum(1 for p in packages if p.urgency == 0)
    health_percent = round((healthy / total) * 100, 1) if total else 100.0
    return DependencyHealthSnapshot(
        enabled=enabled,
        generated_at=datetime.now(timezone.utc).isoformat(),
        ecosystems=sorted(ecosystems.keys()),
        total=total,
        up_to_date=up_to_date,
        major_behind=major_behind,
        deprecated=deprecated,
        vulnerable=vulnerable,
        stale=stale,
        health_percent=health_percent,
        truncated=truncated_count > 0,
        truncated_count=truncated_count,
        packages=sorted(packages, key=lambda p: p.urgency, reverse=True),
    )


async def _read_cache(project_dir: Path, ttl_seconds: int) -> DependencyHealthSnapshot | None:
    async with _cache_lock:
        cached = _cache.get(project_dir)
        if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
            _cache.move_to_end(project_dir)
            return cached[1]
    return None


async def _scan_lock_for(project_dir: Path) -> asyncio.Lock:
    async with _cache_lock:
        if project_dir in _scan_locks:
            _scan_locks.move_to_end(project_dir)
            return _scan_locks[project_dir]
        lock = asyncio.Lock()
        _scan_locks[project_dir] = lock
        _scan_locks.move_to_end(project_dir)
        # Evict the oldest *idle* lock past the cap. A lock a scan is currently
        # holding or waiting on must never be evicted: a later caller for that
        # same project would then create a second lock, and both would permit a
        # scan for the same project concurrently. If every cached lock besides
        # the one just inserted is busy, exceed the cap temporarily instead.
        while len(_scan_locks) > _MAX_CACHED_PROJECTS:
            for key in _scan_locks:
                if key != project_dir and not _scan_locks[key].locked():
                    del _scan_locks[key]
                    break
            else:
                break
        return lock


async def get_snapshot(
    project_dir: Path,
    *,
    enabled: bool,
    cache_ttl_minutes: int,
    registry_timeout_seconds: int,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> DependencyHealthSnapshot:
    """Return the cached dependency health snapshot, scanning if stale or absent.

    `cache_only` returns an empty snapshot rather than scanning on a cache miss, for
    callers (the awareness briefing) that must not fan out to package registries.
    `cache_only` always wins over `force_refresh`: a caller that must never trigger a
    scan is never allowed to, even if `force_refresh` is also set.
    """
    if not enabled:
        return _build_snapshot({}, [], enabled=False)

    ttl_seconds = cache_ttl_minutes * 60
    if cache_only:
        cached = await _read_cache(project_dir, ttl_seconds)
        return cached if cached is not None else _build_snapshot({}, [])

    if not force_refresh:
        cached = await _read_cache(project_dir, ttl_seconds)
        if cached is not None:
            return cached

    # Single-flight: a scan issues one registry request per package, so concurrent
    # callers wait for the in-flight scan instead of each starting their own.
    async with await _scan_lock_for(project_dir):
        if not force_refresh:
            cached = await _read_cache(project_dir, ttl_seconds)
            if cached is not None:
                return cached

        ecosystems = scan_manifests(project_dir)
        if ecosystems:
            packages, truncated_count = await _fetch_all_packages(
                ecosystems, timeout_seconds=float(registry_timeout_seconds)
            )
        else:
            packages, truncated_count = [], 0
        snapshot = _build_snapshot(ecosystems, packages, truncated_count=truncated_count)

        async with _cache_lock:
            _lru_set(_cache, project_dir, (time.monotonic(), snapshot))
        return snapshot


def snapshot_to_dict(snapshot: DependencyHealthSnapshot) -> dict:
    """Serialize the summary fields only; packages are served by their own endpoint."""
    return {f.name: getattr(snapshot, f.name) for f in fields(snapshot) if f.name != "packages"}


def package_to_dict(pkg: PackageHealth) -> dict:
    return asdict(pkg)
