"""LDAP directory client for issue routing and reviewer suggestions.

Standalone query service, not a task adapter (LDAP is not a task source).
The directory is opt-in and has no default server: ``ldap.server`` and
``ldap.base_dn`` must be set before an enabled config produces a client
(anonymous bind). ``ldap3`` is an optional dependency (``pip install sova[ldap]``);
all imports are guarded so the rest of SOVA works without it installed.

All ``ldap3`` calls are synchronous and wrapped in ``asyncio.to_thread()``
for use from async pipeline code (triage, PR creation).
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
import warnings
from dataclasses import dataclass
from urllib.parse import urlparse

from sova.config.models import DEFAULT_LDAP_ATTRIBUTE_MAP, LDAP_PROFILE_FIELDS, LdapConfig
from sova.utils.logging import get_logger

try:
    # ldap3 2.9.1 (its latest release) imports pyasn1's ``tagMap``/``typeMap``,
    # which pyasn1 0.6.4 deprecated in favor of ``TAG_MAP``/``TYPE_MAP``. The
    # warning is ldap3's to fix, not ours, so silence exactly that one here.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"(tagMap|typeMap) is deprecated", category=DeprecationWarning)
        import ldap3

    _LDAP3_AVAILABLE = True
except ImportError:
    ldap3 = None  # type: ignore[assignment]
    _LDAP3_AVAILABLE = False

log = get_logger(component="adapters.ldap")

_DEFAULT_LDAP_PORT = 389
_DEFAULT_LDAPS_PORT = 636

# Attributes every person search requests. The uid attribute name is directory-specific
# (LdapConfig.uid_attribute) and the profile attributes come from LdapConfig.attribute_map;
# LdapClient._person_attributes() adds both. A directory without an attribute simply
# returns no value for it.
_PERSON_ATTRIBUTES_BASE = [
    "cn",
    "displayName",
    "mail",
    "manager",
]

_CACHE_MISS = object()

# Configuration warnings already logged at warning level in this process, keyed by event and
# the config values behind it. A pipeline builds a client per lookup, so repeating the same
# warning on every call would bury other log lines; the repeats go to debug instead.
_warned_once: set[tuple[str, ...]] = set()


def _warn_once(event: str, key: tuple[str, ...], **fields: object) -> None:
    """Log ``event`` at warning level the first time ``key`` is seen, then at debug level."""
    marker = (event, *key)
    if marker in _warned_once:
        log.debug(event, **fields)
        return
    _warned_once.add(marker)
    log.warning(event, **fields)


# RFC 4515 filter-special characters that must be escaped in LDAP filter values.
_FILTER_ESCAPE_MAP = {
    "\\": r"\5c",
    "*": r"\2a",
    "(": r"\28",
    ")": r"\29",
    "\x00": r"\00",
}


def _escape_filter_value(value: str) -> str:
    """Escape RFC 4515 special characters for safe inclusion in an LDAP filter."""
    return "".join(_FILTER_ESCAPE_MAP.get(ch, ch) for ch in value)


class LdapUnavailableError(RuntimeError):
    """Raised when the optional ``ldap3`` package is not installed."""


@dataclass(frozen=True, slots=True)
class Person:
    """A person entry read from the LDAP directory."""

    uid: str
    dn: str = ""
    display_name: str = ""
    mail: str = ""
    job_title: str = ""
    cost_center: str = ""
    cost_center_desc: str = ""
    organization: str = ""
    team_lead: str = ""
    geo: str = ""
    location: str = ""
    manager_dn: str = ""


def _require_ldap3() -> None:
    if not _LDAP3_AVAILABLE:
        raise LdapUnavailableError("ldap3 is not installed. Install with: pip install sova[ldap]")


def _first(value: object) -> str:
    """Flatten an ldap3 attribute value (list or scalar) to a single string."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _parse_person(raw: dict, uid_attribute: str = "uid", attribute_map: dict[str, str] | None = None) -> Person:
    """Build a Person, reading each profile field from the attribute ``attribute_map`` names for it.

    ``attribute_map`` defaults to the standard attributes (``DEFAULT_LDAP_ATTRIBUTE_MAP``); a
    profile field with no entry, or an empty attribute name, stays empty.
    """
    mapping = attribute_map if attribute_map is not None else DEFAULT_LDAP_ATTRIBUTE_MAP
    profile = {field: _first(raw.get(attr)) for field, attr in mapping.items() if attr and field in LDAP_PROFILE_FIELDS}
    return Person(
        uid=_first(raw.get(uid_attribute)),
        dn=str(raw.get("dn", "")),
        display_name=_first(raw.get("displayName")),
        mail=_first(raw.get("mail")),
        manager_dn=_first(raw.get("manager")),
        **profile,
    )


def _uid_from_dn(dn: str, uid_attribute: str = "uid") -> str:
    match = re.match(rf"^{re.escape(uid_attribute)}=([^,]+),", dn, re.IGNORECASE)
    return match.group(1) if match else ""


class LdapClient:
    """Query a corporate LDAP directory for people and org data.

    Anonymous bind, read-only. Every query method degrades to an empty
    result (never raises) on connection or protocol failure, since LDAP
    is a best-effort enrichment source, not a hard dependency.
    """

    def __init__(self, config: LdapConfig) -> None:
        _require_ldap3()
        self._config = config
        self._cache: dict[str, tuple[float, object]] = {}

    async def check_connectivity(self) -> bool:
        """TCP connect test to the LDAP server (VPN check) before querying."""
        try:
            host, port = self._parse_server()
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._config.timeout_seconds
            )
        except (OSError, ValueError):
            log.warning("ldap.connectivity_check_failed", server=self._config.server, exc_info=True)
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def search_people(self, query: str) -> list[Person]:
        """Fuzzy search people by uid, name, or email."""
        cache_key = f"search:{query.lower()}"
        cached = self._cache_get(cache_key)
        if cached is not _CACHE_MISS:
            return cached  # type: ignore[return-value]

        uid_attr = self._config.uid_attribute
        safe = _escape_filter_value(query)
        filter_str = f"(|({uid_attr}=*{safe}*)(cn=*{safe}*)(mail=*{safe}*))"
        raw = await self._search(self._config.base_dn, filter_str, self._person_attributes())
        people = [self._parse(r) for r in raw]
        self._cache_set(cache_key, people)
        return people

    async def get_person(self, uid: str) -> Person | None:
        """Fetch a single person's full profile by uid."""
        cache_key = f"person:{uid.lower()}"
        cached = self._cache_get(cache_key)
        if cached is not _CACHE_MISS:
            return cached  # type: ignore[return-value]

        uid_attr = self._config.uid_attribute
        safe = _escape_filter_value(uid)
        raw = await self._search(self._config.base_dn, f"({uid_attr}={safe})", self._person_attributes(), size_limit=1)
        person = self._parse(raw[0]) if raw else None
        self._cache_set(cache_key, person)
        return person

    async def find_manager_chain(self, uid: str, max_depth: int = 10) -> list[Person]:
        """Walk the manager DN chain from ``uid`` up toward the root."""
        chain: list[Person] = []
        current = await self.get_person(uid)
        seen_dns: set[str] = set()
        for _ in range(max_depth):
            if current is None or not current.manager_dn:
                break
            if current.manager_dn in seen_dns:
                log.warning("ldap.manager_chain_cycle", uid=uid)
                break
            seen_dns.add(current.manager_dn)
            manager = await self._get_person_by_dn(current.manager_dn)
            if manager is None:
                break
            chain.append(manager)
            current = manager
        return chain

    async def get_org_chart(self, manager_uid: str, depth: int = 1) -> list[Person]:
        """Return the recursive team structure reporting to ``manager_uid``."""
        manager = await self.get_person(manager_uid)
        if manager is None or not manager.dn:
            return []
        return await self._direct_reports(manager.dn, depth)

    async def get_group_members(self, group_cn: str) -> list[str]:
        """Enumerate uids belonging to a group entry.

        Returns ``[]`` (and logs ``ldap.group_base_dn_not_set``, at warning level once per
        directory) when ``group_base_dn`` is empty:
        searching from an empty base would span the whole directory. The caller (triage's
        team-mapping lookup) treats ``[]`` as no unambiguous owner and moves on to its other
        resolution paths.
        """
        if not self._config.group_base_dn:
            _warn_once(
                "ldap.group_base_dn_not_set",
                (self._config.server, self._config.base_dn),
                hint="set ldap.group_base_dn to use ldap.team_mapping",
            )
            return []
        cache_key = f"group:{group_cn.lower()}"
        cached = self._cache_get(cache_key)
        if cached is not _CACHE_MISS:
            return cached  # type: ignore[return-value]

        safe = _escape_filter_value(group_cn)
        raw = await self._search(self._config.group_base_dn, f"(cn={safe})", ["member"], size_limit=1)
        if not raw:
            self._cache_set(cache_key, [])
            return []
        members = raw[0].get("member", [])
        if not isinstance(members, list):
            members = [members] if members else []
        uids = [_uid_from_dn(str(dn), self._config.uid_attribute) for dn in members]
        uids = [u for u in uids if u]
        self._cache_set(cache_key, uids)
        return uids

    # internals ---------------------------------------------------------------

    def _person_attributes(self) -> list[str]:
        """Requested attributes for a person search: the uid attribute, the base set and the mapped profile ones."""
        attributes = [self._config.uid_attribute, *_PERSON_ATTRIBUTES_BASE]
        for attr in self._config.attribute_map.values():
            if attr and attr not in attributes:
                attributes.append(attr)
        return attributes

    def _parse(self, raw: dict) -> Person:
        return _parse_person(raw, self._config.uid_attribute, self._config.attribute_map)

    async def _direct_reports(self, manager_dn: str, depth: int) -> list[Person]:
        if depth <= 0:
            return []
        safe_dn = _escape_filter_value(manager_dn)
        raw = await self._search(self._config.base_dn, f"(manager={safe_dn})", self._person_attributes())
        reports = [self._parse(r) for r in raw]
        if depth > 1:
            for report in list(reports):
                if report.dn:
                    reports.extend(await self._direct_reports(report.dn, depth - 1))
        return reports

    async def _get_person_by_dn(self, dn: str) -> Person | None:
        raw = await self._search(dn, "(objectClass=*)", self._person_attributes(), scope="BASE", size_limit=1)
        return self._parse(raw[0]) if raw else None

    async def _search(
        self,
        base_dn: str,
        filter_str: str,
        attributes: list[str],
        *,
        scope: str = "SUBTREE",
        size_limit: int = 0,
    ) -> list[dict]:
        try:
            return await asyncio.to_thread(self._search_sync, base_dn, filter_str, attributes, scope, size_limit)
        except Exception:  # noqa: BLE001 (ldap3 raises varied protocol/socket errors; LDAP is best-effort)
            log.warning("ldap.search_failed", base_dn=base_dn, exc_info=True)
            return []

    def _search_sync(
        self,
        base_dn: str,
        filter_str: str,
        attributes: list[str],
        scope: str,
        size_limit: int,
    ) -> list[dict]:
        host, port = self._parse_server()
        use_ssl = urlparse(self._config.server).scheme == "ldaps"
        tls = None
        if use_ssl:
            tls = ldap3.Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=self._config.ca_cert_file or None,
            )
        server = ldap3.Server(host, port=port, use_ssl=use_ssl, tls=tls, connect_timeout=self._config.timeout_seconds)
        conn = ldap3.Connection(server, auto_bind=True, receive_timeout=self._config.timeout_seconds)
        try:
            conn.search(
                search_base=base_dn,
                search_filter=filter_str,
                search_scope=getattr(ldap3, scope),
                attributes=attributes,
                size_limit=size_limit,
            )
            results = []
            for entry in conn.response or []:
                if entry.get("type") != "searchResEntry":
                    continue
                attrs = dict(entry.get("attributes", {}))
                attrs["dn"] = entry.get("dn", "")
                results.append(attrs)
            return results
        finally:
            conn.unbind()

    def _parse_server(self) -> tuple[str, int]:
        parsed = urlparse(self._config.server)
        host = parsed.hostname or self._config.server
        default_port = _DEFAULT_LDAPS_PORT if parsed.scheme == "ldaps" else _DEFAULT_LDAP_PORT
        port = parsed.port or default_port
        return host, port

    def _cache_get(self, key: str) -> object:
        entry = self._cache.get(key)
        if entry is None:
            return _CACHE_MISS
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._cache[key]
            return _CACHE_MISS
        return value

    def _cache_set(self, key: str, value: object) -> None:
        self._cache[key] = (time.monotonic() + self._config.cache_ttl_seconds, value)


def create_ldap_client(config: LdapConfig, scope: str = "") -> LdapClient | None:
    """Return an ``LdapClient`` if LDAP routing is enabled and available.

    Returns ``None`` (with a log entry) on any unmet precondition so callers
    can fall back to ``github_user`` without special-casing failure modes.
    ``scope`` names the project (its directory) so the ``ldap.not_configured`` warning
    is logged once per project, not once per process: the multi-project hub serves
    several projects from one process.
    """
    if not config.enabled:
        return None
    if not config.server or not config.base_dn:
        _warn_once(
            "ldap.not_configured",
            (scope, config.server, config.base_dn),
            hint="set ldap.server and ldap.base_dn",
        )
        return None
    if not _LDAP3_AVAILABLE:
        log.warning("ldap.package_unavailable", hint="pip install sova[ldap]")
        return None
    return LdapClient(config)
