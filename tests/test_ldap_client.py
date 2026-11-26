"""Tests for sova.adapters.ldap_client (LDAP directory client)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters import ldap_client as lc
from sova.config.models import LdapConfig

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(**kwargs: object) -> LdapConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "server": "ldaps://ldap.example.com",
        "base_dn": "ou=users,dc=example,dc=com",
        "group_base_dn": "ou=groups,dc=example,dc=com",
    }
    defaults.update(kwargs)
    return LdapConfig(**defaults)


def _entry(dn: str, **attrs: object) -> dict:
    return {"type": "searchResEntry", "dn": dn, "attributes": attrs}


def _raw(dn: str, **attrs: object) -> dict:
    """Flattened result shape returned by ``LdapClient._search`` (post conn.response parsing)."""
    return {"dn": dn, **attrs}


@pytest.fixture(autouse=True)
def _reset_warned_once() -> Generator[None, None, None]:
    """Each test sees configuration warnings as if logged for the first time."""
    lc._warned_once.clear()
    yield
    lc._warned_once.clear()


@pytest.fixture
def mock_ldap3() -> Generator[MagicMock, None, None]:
    """Patch the module's guarded ldap3 import with a mock, as if installed."""
    mock_module = MagicMock()
    mock_module.SUBTREE = "SUBTREE"
    mock_module.BASE = "BASE"
    with patch.object(lc, "_LDAP3_AVAILABLE", True), patch.object(lc, "ldap3", mock_module):
        yield mock_module


# ---------------------------------------------------------------------------
# create_ldap_client
# ---------------------------------------------------------------------------


class TestCreateLdapClient:
    def test_disabled_returns_none(self) -> None:
        assert lc.create_ldap_client(_config(enabled=False)) is None

    def test_package_unavailable_returns_none(self) -> None:
        with patch.object(lc, "_LDAP3_AVAILABLE", False):
            assert lc.create_ldap_client(_config(enabled=True)) is None

    def test_enabled_and_available_returns_client(self, mock_ldap3: MagicMock) -> None:
        client = lc.create_ldap_client(_config(enabled=True))
        assert isinstance(client, lc.LdapClient)

    @pytest.mark.parametrize("missing", ["server", "base_dn"])
    def test_enabled_without_required_field_returns_none(self, mock_ldap3: MagicMock, missing: str) -> None:
        """Enabling LDAP with no server or base DN set never builds a client for a directory nobody chose."""
        assert lc.create_ldap_client(_config(enabled=True, **{missing: ""})) is None

    def test_enabled_with_stock_defaults_returns_none(self, mock_ldap3: MagicMock) -> None:
        assert lc.create_ldap_client(LdapConfig(enabled=True)) is None

    def test_not_configured_warns_once_per_project_scope(self, mock_ldap3: MagicMock) -> None:
        """On the multi-project hub a second misconfigured project still gets its own warning."""
        with patch.object(lc, "log") as log:
            lc.create_ldap_client(LdapConfig(enabled=True), "/projects/a")
            lc.create_ldap_client(LdapConfig(enabled=True), "/projects/b")
            lc.create_ldap_client(LdapConfig(enabled=True), "/projects/a")
        assert [c.args[0] for c in log.warning.call_args_list] == ["ldap.not_configured"] * 2
        assert [c.args[0] for c in log.debug.call_args_list] == ["ldap.not_configured"]

    def test_not_configured_warns_once_then_logs_at_debug(self, mock_ldap3: MagicMock) -> None:
        """A client is built per lookup, so the same misconfiguration must not warn on every call."""
        with patch.object(lc, "log") as log:
            lc.create_ldap_client(LdapConfig(enabled=True))
            lc.create_ldap_client(LdapConfig(enabled=True))
        assert [c.args[0] for c in log.warning.call_args_list] == ["ldap.not_configured"]
        assert [c.args[0] for c in log.debug.call_args_list] == ["ldap.not_configured"]


class TestLdapConfigDefaults:
    def test_no_directory_is_preconfigured(self) -> None:
        config = LdapConfig()
        assert config.enabled is False
        assert (config.server, config.base_dn, config.group_base_dn) == ("", "", "")

    def test_attribute_map_defaults_to_standard_attributes(self) -> None:
        assert LdapConfig().attribute_map == {
            "job_title": "title",
            "cost_center": "departmentNumber",
            "organization": "o",
            "location": "l",
        }

    def test_partial_attribute_map_keeps_the_other_defaults(self) -> None:
        """Overriding one field must not silently empty the others."""
        config = LdapConfig(attribute_map={"job_title": "corpJobTitle", "team_lead": "corpLead"})
        assert config.attribute_map == {
            "job_title": "corpJobTitle",
            "cost_center": "departmentNumber",
            "organization": "o",
            "location": "l",
            "team_lead": "corpLead",
        }

    def test_empty_attribute_name_turns_a_field_off(self) -> None:
        raw = {"dn": "uid=jdoe", "title": ["Engineer"], "o": ["Example Org"]}
        config = LdapConfig(attribute_map={"organization": ""})
        person = lc._parse_person(raw, attribute_map=config.attribute_map)
        assert (person.job_title, person.organization) == ("Engineer", "")

    def test_profile_fields_are_all_person_fields(self) -> None:
        """The validator's field list and the Person dataclass must not drift apart."""
        import dataclasses

        from sova.config.models import LDAP_PROFILE_FIELDS

        person_fields = {f.name for f in dataclasses.fields(lc.Person)}
        assert set(LDAP_PROFILE_FIELDS) <= person_fields

    def test_attribute_map_rejects_unknown_field(self) -> None:
        with pytest.raises(ValueError, match="unknown field"):
            LdapConfig(attribute_map={"shoe_size": "shoeSize"})


# ---------------------------------------------------------------------------
# LdapClient construction
# ---------------------------------------------------------------------------


class TestLdapClientInit:
    def test_raises_when_package_unavailable(self) -> None:
        config = _config()
        with patch.object(lc, "_LDAP3_AVAILABLE", False), pytest.raises(lc.LdapUnavailableError):
            lc.LdapClient(config)


# ---------------------------------------------------------------------------
# _escape_filter_value
# ---------------------------------------------------------------------------


class TestEscapeFilterValue:
    def test_escapes_special_characters(self) -> None:
        assert lc._escape_filter_value("a*b(c)d\\e") == r"a\2ab\28c\29d\5ce"

    def test_passthrough_ordinary_text(self) -> None:
        assert lc._escape_filter_value("jdoe") == "jdoe"


# ---------------------------------------------------------------------------
# _parse_person / _uid_from_dn
# ---------------------------------------------------------------------------


class TestParsePerson:
    def test_flattens_list_attributes(self) -> None:
        raw = {
            "dn": "uid=jdoe,ou=users,dc=example,dc=com",
            "uid": ["jdoe"],
            "displayName": ["Jane Doe"],
            "mail": ["jdoe@example.com"],
            "manager": ["uid=mgr,ou=users,dc=example,dc=com"],
        }
        person = lc._parse_person(raw)
        assert person.uid == "jdoe"
        assert person.display_name == "Jane Doe"
        assert person.mail == "jdoe@example.com"
        assert person.manager_dn == "uid=mgr,ou=users,dc=example,dc=com"

    def test_handles_missing_attributes(self) -> None:
        person = lc._parse_person({"dn": "uid=x,ou=users,dc=example,dc=com"})
        assert person.uid == ""
        assert person.mail == ""
        assert person.manager_dn == ""

    def test_reads_profile_fields_from_standard_attributes_by_default(self) -> None:
        raw = {"dn": "uid=jdoe", "title": ["Engineer"], "o": ["Example Org"], "l": ["Brno"], "departmentNumber": "42"}
        person = lc._parse_person(raw)
        assert (person.job_title, person.organization, person.location, person.cost_center) == (
            "Engineer",
            "Example Org",
            "Brno",
            "42",
        )
        assert person.team_lead == ""

    def test_unmapped_profile_fields_default_to_empty_strings(self) -> None:
        person = lc._parse_person({"dn": "uid=jdoe", "title": ["Engineer"]})
        assert (person.cost_center_desc, person.team_lead, person.geo) == ("", "", "")

    def test_reads_profile_fields_through_a_custom_attribute_map(self) -> None:
        """A directory with its own schema maps a profile field to a vendor-specific attribute."""
        raw = {"dn": "uid=jdoe", "title": ["Ignored"], "corpJobTitle": ["Staff Engineer"], "corpLead": ["mgr"]}
        person = lc._parse_person(raw, attribute_map={"job_title": "corpJobTitle", "team_lead": "corpLead"})
        assert (person.job_title, person.team_lead, person.organization) == ("Staff Engineer", "mgr", "")


class TestUidFromDn:
    def test_extracts_uid(self) -> None:
        assert lc._uid_from_dn("uid=jdoe,ou=users,dc=example,dc=com") == "jdoe"

    def test_returns_empty_for_non_uid_dn(self) -> None:
        assert lc._uid_from_dn("cn=team,ou=groups,dc=example,dc=com") == ""


# ---------------------------------------------------------------------------
# _parse_server
# ---------------------------------------------------------------------------


class TestParseServer:
    def test_parses_host_and_port(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config(server="ldap://ldap.example.com:1389", allow_insecure=True))
        assert client._parse_server() == ("ldap.example.com", 1389)

    def test_defaults_port_389(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config(server="ldap://ldap.example.com", allow_insecure=True))
        assert client._parse_server() == ("ldap.example.com", 389)

    def test_ldaps_defaults_port_636(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config(server="ldaps://ldap.example.com"))
        assert client._parse_server() == ("ldap.example.com", 636)


# ---------------------------------------------------------------------------
# LdapConfig scheme validation
# ---------------------------------------------------------------------------


class TestRequireSecureScheme:
    def test_rejects_cleartext_server_by_default(self) -> None:
        with pytest.raises(ValueError, match="ldaps://"):
            _config(server="ldap://ldap.example.com")

    def test_allows_cleartext_when_explicitly_opted_in(self) -> None:
        config = _config(server="ldap://ldap.example.com", allow_insecure=True)
        assert config.server == "ldap://ldap.example.com"


# ---------------------------------------------------------------------------
# check_connectivity
# ---------------------------------------------------------------------------


class TestCheckConnectivity:
    async def test_success(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch(
            "sova.adapters.ldap_client.asyncio.open_connection",
            new=AsyncMock(return_value=(MagicMock(), writer)),
        ):
            assert await client.check_connectivity() is True
        writer.close.assert_called_once()

    async def test_failure_on_connection_error(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        with patch(
            "sova.adapters.ldap_client.asyncio.open_connection",
            new=AsyncMock(side_effect=OSError("no route to host")),
        ):
            assert await client.check_connectivity() is False

    async def test_failure_on_timeout(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config(timeout_seconds=1))
        with patch(
            "sova.adapters.ldap_client.asyncio.open_connection",
            new=AsyncMock(side_effect=TimeoutError()),
        ):
            assert await client.check_connectivity() is False


# ---------------------------------------------------------------------------
# search_people / get_person: exercise the real ldap3 protocol path
# ---------------------------------------------------------------------------


class TestSearchPeople:
    async def test_returns_parsed_people(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = [
            _entry(
                "uid=jdoe,ou=users,dc=example,dc=com",
                uid=["jdoe"],
                displayName=["Jane Doe"],
                mail=["jdoe@example.com"],
            )
        ]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        people = await client.search_people("jdoe")

        assert len(people) == 1
        assert people[0].uid == "jdoe"
        assert people[0].display_name == "Jane Doe"
        conn.unbind.assert_called_once()

    async def test_repeat_query_uses_cache(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=example,dc=com", uid=["jdoe"])]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        await client.search_people("jdoe")
        await client.search_people("jdoe")

        assert mock_ldap3.Connection.call_count == 1

    async def test_requests_uid_base_and_mapped_profile_attributes(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config(uid_attribute="sAMAccountName", attribute_map={"job_title": "corpJobTitle"}))
        await client.search_people("jdoe")

        assert conn.search.call_args.kwargs["attributes"] == [
            "sAMAccountName",
            "cn",
            "displayName",
            "mail",
            "manager",
            "corpJobTitle",
            "departmentNumber",
            "o",
            "l",
        ]

    async def test_empty_attribute_name_is_not_requested(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config(attribute_map={"cost_center": "", "organization": "", "location": ""}))
        await client.search_people("jdoe")

        assert conn.search.call_args.kwargs["attributes"] == ["uid", "cn", "displayName", "mail", "manager", "title"]


class TestGetPerson:
    async def test_found(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=example,dc=com", uid=["jdoe"])]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        person = await client.get_person("jdoe")

        assert person is not None
        assert person.uid == "jdoe"

    async def test_not_found_returns_none(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        assert await client.get_person("ghost") is None

    async def test_negative_result_is_cached(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        await client.get_person("ghost")
        await client.get_person("ghost")

        assert mock_ldap3.Connection.call_count == 1


class TestCacheTTL:
    async def test_entry_expires_after_ttl(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=example,dc=com", uid=["jdoe"])]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config(cache_ttl_seconds=1))

        with patch("sova.adapters.ldap_client.time.monotonic", return_value=1000.0):
            await client.get_person("jdoe")
        with patch("sova.adapters.ldap_client.time.monotonic", return_value=1002.0):
            await client.get_person("jdoe")

        assert mock_ldap3.Connection.call_count == 2


# ---------------------------------------------------------------------------
# _search error handling
# ---------------------------------------------------------------------------


class TestSearchErrorHandling:
    async def test_returns_empty_on_exception(self, mock_ldap3: MagicMock) -> None:
        mock_ldap3.Connection.side_effect = RuntimeError("connection refused")

        client = lc.LdapClient(_config())
        result = await client._search("base", "(uid=*)", ["uid"])

        assert result == []


# ---------------------------------------------------------------------------
# find_manager_chain
# ---------------------------------------------------------------------------


class TestFindManagerChain:
    async def test_walks_chain_to_root(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        alice = lc.Person(uid="alice", manager_dn="uid=bob,ou=users,dc=example,dc=com")
        bob = lc.Person(
            uid="bob",
            dn="uid=bob,ou=users,dc=example,dc=com",
            manager_dn="uid=carol,ou=users,dc=example,dc=com",
        )
        carol = lc.Person(uid="carol", dn="uid=carol,ou=users,dc=example,dc=com")

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=alice)),
            patch.object(client, "_get_person_by_dn", new=AsyncMock(side_effect=[bob, carol])),
        ):
            chain = await client.find_manager_chain("alice")

        assert chain == [bob, carol]

    async def test_stops_when_person_not_found(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        with patch.object(client, "get_person", new=AsyncMock(return_value=None)):
            assert await client.find_manager_chain("ghost") == []

    async def test_detects_cycle(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        alice = lc.Person(uid="alice", manager_dn="dn-bob")
        bob = lc.Person(uid="bob", dn="dn-bob", manager_dn="dn-alice")
        alice_again = lc.Person(uid="alice", dn="dn-alice", manager_dn="dn-bob")

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=alice)),
            patch.object(client, "_get_person_by_dn", new=AsyncMock(side_effect=[bob, alice_again])),
        ):
            chain = await client.find_manager_chain("alice", max_depth=10)

        assert chain == [bob, alice_again]


# ---------------------------------------------------------------------------
# get_org_chart
# ---------------------------------------------------------------------------


class TestGetOrgChart:
    async def test_returns_empty_when_manager_not_found(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        with patch.object(client, "get_person", new=AsyncMock(return_value=None)):
            assert await client.get_org_chart("ghost") == []

    async def test_depth_one_returns_direct_reports(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        manager = lc.Person(uid="mgr", dn="uid=mgr,ou=users,dc=example,dc=com")
        raw_reports = [_raw("uid=rep1,ou=users,dc=example,dc=com", uid=["rep1"])]

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=manager)),
            patch.object(client, "_search", new=AsyncMock(return_value=raw_reports)) as mock_search,
        ):
            reports = await client.get_org_chart("mgr", depth=1)

        assert [r.uid for r in reports] == ["rep1"]
        mock_search.assert_awaited_once()

    async def test_depth_two_recurses_into_reports(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config())
        manager = lc.Person(uid="mgr", dn="dn-mgr")
        rep1_raw = [_raw("dn-rep1", uid=["rep1"])]
        rep2_raw = [_raw("dn-rep2", uid=["rep2"])]

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=manager)),
            patch.object(client, "_search", new=AsyncMock(side_effect=[rep1_raw, rep2_raw])),
        ):
            reports = await client.get_org_chart("mgr", depth=2)

        assert {r.uid for r in reports} == {"rep1", "rep2"}


# ---------------------------------------------------------------------------
# get_group_members
# ---------------------------------------------------------------------------


class TestGetGroupMembers:
    async def test_returns_uids_from_member_dns(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = [
            _entry(
                "cn=team-x,ou=adhoc,ou=managedGroups,dc=example,dc=com",
                member=[
                    "uid=jdoe,ou=users,dc=example,dc=com",
                    "uid=asmith,ou=users,dc=example,dc=com",
                ],
            )
        ]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        uids = await client.get_group_members("team-x")

        assert uids == ["jdoe", "asmith"]

    async def test_returns_empty_when_group_not_found(self, mock_ldap3: MagicMock) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        assert await client.get_group_members("ghost-team") == []

    async def test_returns_empty_without_searching_when_group_base_dn_is_unset(self, mock_ldap3: MagicMock) -> None:
        """An empty group base DN must not turn into a search from the directory root."""
        conn = MagicMock()
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config(group_base_dn=""))
        assert await client.get_group_members("team-x") == []
        conn.search.assert_not_called()

    async def test_unset_group_base_dn_warns_once(self, mock_ldap3: MagicMock) -> None:
        client = lc.LdapClient(_config(group_base_dn=""))
        with patch.object(lc, "log") as log:
            assert await client.get_group_members("team-x") == []
            assert await client.get_group_members("team-y") == []
        assert [c.args[0] for c in log.warning.call_args_list] == ["ldap.group_base_dn_not_set"]
        assert [c.args[0] for c in log.debug.call_args_list] == ["ldap.group_base_dn_not_set"]


def _import_with_stub_ldap3(tmp_path: Path, stub_source: str) -> subprocess.CompletedProcess[str]:
    """Import ldap_client in a fresh interpreter where DeprecationWarning is an error.

    A stub ``ldap3`` package stands in for the real one: CI installs only the
    ``dev`` extra, never ``ldap``, so without the stub the filter around the
    import would never run there.
    """
    stub = tmp_path / "ldap3"
    stub.mkdir()
    (stub / "__init__.py").write_text(stub_source, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), str(_REPO_ROOT), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import sova.adapters.ldap_client"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


class TestLdap3ImportWarnings:
    def test_pyasn1_tagmap_deprecation_is_silenced(self, tmp_path: Path) -> None:
        """ldap3 2.9.1 triggers pyasn1's tagMap deprecation on import; that one warning is not ours."""
        stub = 'import warnings\nwarnings.warn("tagMap is deprecated. Use TAG_MAP instead.", DeprecationWarning)\n'
        result = _import_with_stub_ldap3(tmp_path, stub)
        assert result.returncode == 0, result.stderr

    def test_other_ldap3_deprecations_still_surface(self, tmp_path: Path) -> None:
        """The filter matches the message, so any other deprecation raised during the import still fails."""
        stub = 'import warnings\nwarnings.warn("connect_timeout is deprecated", DeprecationWarning)\n'
        result = _import_with_stub_ldap3(tmp_path, stub)
        assert result.returncode != 0
        assert "connect_timeout is deprecated" in result.stderr
