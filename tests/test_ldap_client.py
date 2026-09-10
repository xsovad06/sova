"""Tests for sova.adapters.ldap_client (LDAP directory client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters import ldap_client as lc
from sova.config.models import LdapConfig


def _config(**kwargs) -> LdapConfig:
    defaults = {"enabled": True}
    defaults.update(kwargs)
    return LdapConfig(**defaults)


def _entry(dn: str, **attrs: object) -> dict:
    return {"type": "searchResEntry", "dn": dn, "attributes": attrs}


def _raw(dn: str, **attrs: object) -> dict:
    """Flattened result shape returned by ``LdapClient._search`` (post conn.response parsing)."""
    return {"dn": dn, **attrs}


@pytest.fixture
def mock_ldap3():
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

    def test_enabled_and_available_returns_client(self, mock_ldap3) -> None:
        client = lc.create_ldap_client(_config(enabled=True))
        assert isinstance(client, lc.LdapClient)


# ---------------------------------------------------------------------------
# LdapClient construction
# ---------------------------------------------------------------------------


class TestLdapClientInit:
    def test_raises_when_package_unavailable(self) -> None:
        with patch.object(lc, "_LDAP3_AVAILABLE", False), pytest.raises(lc.LdapUnavailableError):
            lc.LdapClient(_config())


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
            "dn": "uid=jdoe,ou=users,dc=redhat,dc=com",
            "uid": ["jdoe"],
            "displayName": ["Jane Doe"],
            "mail": ["jdoe@redhat.com"],
            "manager": ["uid=mgr,ou=users,dc=redhat,dc=com"],
        }
        person = lc._parse_person(raw)
        assert person.uid == "jdoe"
        assert person.display_name == "Jane Doe"
        assert person.mail == "jdoe@redhat.com"
        assert person.manager_dn == "uid=mgr,ou=users,dc=redhat,dc=com"

    def test_handles_missing_attributes(self) -> None:
        person = lc._parse_person({"dn": "uid=x,ou=users,dc=redhat,dc=com"})
        assert person.uid == ""
        assert person.mail == ""
        assert person.manager_dn == ""


class TestUidFromDn:
    def test_extracts_uid(self) -> None:
        assert lc._uid_from_dn("uid=jdoe,ou=users,dc=redhat,dc=com") == "jdoe"

    def test_returns_empty_for_non_uid_dn(self) -> None:
        assert lc._uid_from_dn("cn=team,ou=groups,dc=redhat,dc=com") == ""


# ---------------------------------------------------------------------------
# _parse_server
# ---------------------------------------------------------------------------


class TestParseServer:
    def test_parses_host_and_port(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config(server="ldap://ldap.corp.redhat.com:1389"))
        assert client._parse_server() == ("ldap.corp.redhat.com", 1389)

    def test_defaults_port_389(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config(server="ldap://ldap.corp.redhat.com"))
        assert client._parse_server() == ("ldap.corp.redhat.com", 389)

    def test_ldaps_defaults_port_636(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config(server="ldaps://ldap.corp.redhat.com"))
        assert client._parse_server() == ("ldap.corp.redhat.com", 636)


# ---------------------------------------------------------------------------
# check_connectivity
# ---------------------------------------------------------------------------


class TestCheckConnectivity:
    async def test_success(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch(
            "sova.adapters.ldap_client.asyncio.open_connection",
            new=AsyncMock(return_value=(MagicMock(), writer)),
        ):
            assert await client.check_connectivity() is True
        writer.close.assert_called_once()

    async def test_failure_on_connection_error(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        with patch(
            "sova.adapters.ldap_client.asyncio.open_connection",
            new=AsyncMock(side_effect=OSError("no route to host")),
        ):
            assert await client.check_connectivity() is False

    async def test_failure_on_timeout(self, mock_ldap3) -> None:
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
    async def test_returns_parsed_people(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = [
            _entry(
                "uid=jdoe,ou=users,dc=redhat,dc=com",
                uid=["jdoe"],
                displayName=["Jane Doe"],
                mail=["jdoe@redhat.com"],
            )
        ]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        people = await client.search_people("jdoe")

        assert len(people) == 1
        assert people[0].uid == "jdoe"
        assert people[0].display_name == "Jane Doe"
        conn.unbind.assert_called_once()

    async def test_repeat_query_uses_cache(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=redhat,dc=com", uid=["jdoe"])]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        await client.search_people("jdoe")
        await client.search_people("jdoe")

        assert mock_ldap3.Connection.call_count == 1


class TestGetPerson:
    async def test_found(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=redhat,dc=com", uid=["jdoe"])]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        person = await client.get_person("jdoe")

        assert person is not None
        assert person.uid == "jdoe"

    async def test_not_found_returns_none(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        assert await client.get_person("ghost") is None

    async def test_negative_result_is_cached(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        await client.get_person("ghost")
        await client.get_person("ghost")

        assert mock_ldap3.Connection.call_count == 1


class TestCacheTTL:
    async def test_entry_expires_after_ttl(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = [_entry("uid=jdoe,ou=users,dc=redhat,dc=com", uid=["jdoe"])]
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
    async def test_returns_empty_on_exception(self, mock_ldap3) -> None:
        mock_ldap3.Connection.side_effect = RuntimeError("connection refused")

        client = lc.LdapClient(_config())
        result = await client._search("base", "(uid=*)", ["uid"])

        assert result == []


# ---------------------------------------------------------------------------
# find_manager_chain
# ---------------------------------------------------------------------------


class TestFindManagerChain:
    async def test_walks_chain_to_root(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        alice = lc.Person(uid="alice", manager_dn="uid=bob,ou=users,dc=redhat,dc=com")
        bob = lc.Person(
            uid="bob",
            dn="uid=bob,ou=users,dc=redhat,dc=com",
            manager_dn="uid=carol,ou=users,dc=redhat,dc=com",
        )
        carol = lc.Person(uid="carol", dn="uid=carol,ou=users,dc=redhat,dc=com")

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=alice)),
            patch.object(client, "_get_person_by_dn", new=AsyncMock(side_effect=[bob, carol])),
        ):
            chain = await client.find_manager_chain("alice")

        assert chain == [bob, carol]

    async def test_stops_when_person_not_found(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        with patch.object(client, "get_person", new=AsyncMock(return_value=None)):
            assert await client.find_manager_chain("ghost") == []

    async def test_detects_cycle(self, mock_ldap3) -> None:
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
    async def test_returns_empty_when_manager_not_found(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        with patch.object(client, "get_person", new=AsyncMock(return_value=None)):
            assert await client.get_org_chart("ghost") == []

    async def test_depth_one_returns_direct_reports(self, mock_ldap3) -> None:
        client = lc.LdapClient(_config())
        manager = lc.Person(uid="mgr", dn="uid=mgr,ou=users,dc=redhat,dc=com")
        raw_reports = [_raw("uid=rep1,ou=users,dc=redhat,dc=com", uid=["rep1"])]

        with (
            patch.object(client, "get_person", new=AsyncMock(return_value=manager)),
            patch.object(client, "_search", new=AsyncMock(return_value=raw_reports)) as mock_search,
        ):
            reports = await client.get_org_chart("mgr", depth=1)

        assert [r.uid for r in reports] == ["rep1"]
        mock_search.assert_awaited_once()

    async def test_depth_two_recurses_into_reports(self, mock_ldap3) -> None:
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
    async def test_returns_uids_from_member_dns(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = [
            _entry(
                "cn=team-x,ou=adhoc,ou=managedGroups,dc=redhat,dc=com",
                member=[
                    "uid=jdoe,ou=users,dc=redhat,dc=com",
                    "uid=asmith,ou=users,dc=redhat,dc=com",
                ],
            )
        ]
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        uids = await client.get_group_members("team-x")

        assert uids == ["jdoe", "asmith"]

    async def test_returns_empty_when_group_not_found(self, mock_ldap3) -> None:
        conn = MagicMock()
        conn.response = []
        mock_ldap3.Connection.return_value = conn

        client = lc.LdapClient(_config())
        assert await client.get_group_members("ghost-team") == []
