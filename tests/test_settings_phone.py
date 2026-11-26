"""The Settings page on the phone (#1566): search and the settings checked most, then the section list; a section
opens as its own view with a way back.

There is no browser here, so the markup is asserted on the rendered page and the layout contract on the shipped
stylesheet; which view shows is `state.layoutView()` (tests/test_settings_page_js.py). The measured layout at 390,
768 and 1440 px is checked by driving a real browser before merge.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from tests.conftest import DASHBOARD_DIR
from tests.dashboard_helpers import css_rule as _rule
from tests.dashboard_helpers import phone_css_block as _phone_block

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_STATIC = DASHBOARD_DIR / "static"
_PAGE_JS = (_STATIC / "js" / "pages" / "settings" / "page.js").read_text(encoding="utf-8")
_HOME = '.sova-settings-layout[data-view="home"]'


def _rule_listing(block: str, selector: str) -> str:
    """The body of the rule whose selector list includes `selector` among others."""
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    for match in re.finditer(r"(?m)^\s*([^{}]+?)\s*\{(.*?)\}", block, re.DOTALL):
        if selector in [part.strip() for part in match.group(1).split(",")]:
            return match.group(2)
    raise AssertionError(f"no rule lists {selector}")


class TestMarkup:
    async def test_the_page_first_paints_the_home_view(self, client: AsyncClient) -> None:
        html = (await client.get("/settings")).text
        assert '<div class="sova-settings-layout" data-view="home">' in html

    async def test_a_section_view_has_a_back_control_hidden_from_768_px(self, client: AsyncClient) -> None:
        html = (await client.get("/settings")).text
        main = html[html.index('<div class="sova-settings-main">') :]
        assert main.index("data-settings-back") < main.index('id="section-overview"')
        assert re.search(r'<a href="#" class="sova-settings-back md:hidden" data-settings-back>', main)

    async def test_every_section_is_in_the_list_with_its_chevron(self, client: AsyncClient) -> None:
        from sova.dashboard.settings_meta import SECTIONS

        html = (await client.get("/settings")).text
        nav = html[
            html.index('<nav class="sova-settings-nav"') : html.index(
                "</nav>", html.index('<nav class="sova-settings-nav"')
            )
        ]
        for section_id in (s[0] if isinstance(s, tuple) else s for s in SECTIONS):
            assert f'data-section-link="{section_id}"' in nav
        assert nav.count("sova-settings-nav-chevron md:hidden") == nav.count("data-section-link=")

    async def test_the_command_sync_card_is_named(self, client: AsyncClient) -> None:
        html = (await client.get("/settings")).text
        card = html[html.index('id="overview-commands"') :]
        assert card.index('data-widget="installation"') < card.index("</div>\n          </div>")


class TestHomeView:
    def test_home_leads_with_the_overview_controls_then_the_section_list(self) -> None:
        block = _phone_block()
        assert "flex-direction: column" in _rule(block, ".sova-settings-layout")
        assert "order: 1" in _rule(block, f"{_HOME} .sova-settings-nav")
        for part in (
            "#section-overview > .sova-settings-section-head",
            ".sova-overview-health",
            ".sova-overview-switches",
        ):
            assert "display: none" in _rule_listing(block, f"{_HOME} {part}")

    def test_the_four_controls_are_one_card_of_rows_with_the_control_on_the_right(self) -> None:
        block = _phone_block()
        card = _rule(block, f"{_HOME} .sova-overview-grid .sova-card")
        assert "grid-template-columns: minmax(0, 1fr) auto" in card
        assert "min-height: 60px" in card
        control = _rule(
            block,
            f"{_HOME} .sova-overview-grid :is([data-setting-switch], [data-setting-stepper], .sova-setting-readonly)",
        )
        assert "grid-column: 2" in control

    def test_sync_all_and_review_changes_take_their_own_line(self) -> None:
        block = _phone_block()
        assert "grid-column: 1 / -1" in _rule(
            block, f'{_HOME} #overview-commands .sova-settings-actions:has(> [data-act="install-sync"])'
        )

    def test_search_and_changed_share_a_row(self) -> None:
        block = _phone_block()
        assert "flex-wrap: nowrap" in _rule(block, ".sova-settings-tools")
        assert "min-width: 0" in _rule(block, ".sova-settings-search")


class TestSectionView:
    def test_only_home_shows_the_section_list(self) -> None:
        assert "display: none" in _rule(
            _phone_block(), '.sova-settings-layout:not([data-view="home"]) .sova-settings-nav'
        )

    def test_the_back_control_shows_only_in_a_section_and_is_a_tap_target(self) -> None:
        block = _phone_block()
        assert "min-height: 44px" in _rule(block, ".sova-settings-back")
        assert "display: none" in _rule(
            block, f'{_HOME} .sova-settings-back,\n  .sova-settings-layout[data-view="results"] .sova-settings-back'
        )

    def test_text_links_are_tap_targets_and_long_keys_wrap(self) -> None:
        block = _phone_block()
        assert "min-height: 44px" in _rule_listing(block, ".sova-setting-link")
        assert "min-height: 44px" in _rule_listing(block, ".sova-settings-anchor")
        assert "overflow-wrap: anywhere" in _rule(block, ".sova-setting-desc")

    def test_a_group_scrolls_into_view_below_the_top_bar(self) -> None:
        assert "var(--phone-topbar-height)" in _rule(_phone_block(), ".sova-settings-group")

    def test_the_back_control_and_every_section_link_go_through_the_history_module(self) -> None:
        """The history behavior is run in tests/test_settings_back_nav.py; this checks the page uses that module."""
        assert "import { createBackNav } from './back-nav.js';" in _PAGE_JS
        assert "history.pushState" not in _PAGE_JS
        assert "nav.goHome();" in _PAGE_JS
        assert _PAGE_JS.count("nav.go(target);") == 2
        assert "layout.dataset.view = state.layoutView(" in _PAGE_JS
