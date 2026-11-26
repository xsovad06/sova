"""The dashboard's one navigation definition.

The desktop sidebar, the phone tab bar and the phone More sheet (`templates/base.html`) all render from
`NAV_PAGES`, so a page added here shows in every one of them and none can drift from the others. The sidebar shows a
page where its `sidebar` field says (the always-visible list, the collapsed More group or the bottom), the tab bar
shows the `tab` pages in order with the owl in the middle, and the More sheet shows every other page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SidebarPlace = Literal["main", "more", "bottom"]


@dataclass(frozen=True)
class NavPage:
    """One page in the navigation.

    `id` is the `page` value its route passes the template, which marks the current page. `path` is under the project
    prefix (`/p/<slug>`) unless `global_path` says it is the same for every project. `sidebar` is None for a page the
    desktop sidebar does not list (the phone Home page, reached from the tab bar and by its URL).
    """

    id: str
    path: str
    label: str
    icon: str
    sidebar: SidebarPlace | None
    tab: bool = False
    global_path: bool = False
    needs_awareness: bool = False


NAV_PAGES: tuple[NavPage, ...] = (
    NavPage("phone", "/phone", "Home", "home", sidebar=None, tab=True),
    NavPage("work", "/work", "Work", "work", sidebar="main", tab=True),
    NavPage("supervisor", "/supervisor", "Supervisor", "supervisor", sidebar="main", tab=True),
    NavPage("oversight", "/oversight", "Oversight", "oversight", sidebar="main"),
    NavPage("costs", "/costs", "Costs", "costs", sidebar="main"),
    NavPage("briefing", "/briefing", "Briefing", "bell", sidebar="more", needs_awareness=True),
    NavPage("queue", "/queue", "Queue", "queue", sidebar="more"),
    NavPage("specs", "/specs", "Specs", "document-text", sidebar="more"),
    NavPage("pr-metrics", "/pr-metrics", "PR Metrics", "chart-bar", sidebar="more"),
    NavPage("reliability", "/reliability", "Reliability", "chart-bar", sidebar="more"),
    NavPage("roles", "/roles", "Roles", "roles", sidebar="more"),
    NavPage("commands", "/commands", "Commands", "commands", sidebar="more"),
    NavPage("dependency-health", "/dependency-health", "Dependencies", "shield", sidebar="more"),
    NavPage("fleet", "/fleet", "Fleet", "fleet", sidebar="more"),
    NavPage("connections", "/connections", "Connections", "link", sidebar="more"),
    NavPage("setup", "/setup", "Add Project", "plus-circle", sidebar="more", global_path=True),
    NavPage("settings", "/settings", "Settings", "settings", sidebar="bottom", tab=True),
)


@dataclass(frozen=True)
class NavLink:
    """A `NavPage` resolved for one request: its href carries the project prefix."""

    id: str
    href: str
    label: str
    icon: str
    sidebar: SidebarPlace | None
    tab: bool


def nav_links(prefix: str, awareness_enabled: bool) -> list[NavLink]:
    """Every page the navigation shows for a project at `prefix` ('' in single-project mode), in order.

    The Briefing page is listed only when the project has awareness enabled, since it renders empty otherwise.
    """
    return [
        NavLink(
            id=page.id,
            href=page.path if page.global_path else prefix + page.path,
            label=page.label,
            icon=page.icon,
            sidebar=page.sidebar,
            tab=page.tab,
        )
        for page in NAV_PAGES
        if awareness_enabled or not page.needs_awareness
    ]
