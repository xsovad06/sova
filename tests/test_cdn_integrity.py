"""Tests for ``invariants/cdn-integrity.sh``.

Every remote script or stylesheet the dashboard loads must pin an exact version
and carry ``integrity`` and ``crossorigin``. Without a hash a compromised CDN or
package can run code in the dashboard (DOMPurify, its XSS sanitizer, is one of
these files); with a hash but a floating version (``marked@15``) the next
upstream release changes the bytes and the browser blocks the script.

The last test runs the invariant on this repository's own templates. The
Invariants CI job is not a required check, but this test suite is, so a
regression fails the build either way.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.conftest import INVARIANTS_DIR, InvariantRepo

SCRIPT = "cdn-integrity.sh"
TEMPLATE = "sova/dashboard/templates/page.html"

SRI = 'integrity="sha384-948ahk4ZmxYVYOc+rxN1H2gM1EJ2Duhp7uHtZ4WSLkV4Vtx5MUqnV+l7u9B+jFv+" crossorigin="anonymous"'
PINNED = f'<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js" {SRI}></script>'


def run_with_template(repo: InvariantRepo, html: str, path: str = TEMPLATE) -> subprocess.CompletedProcess[str]:
    repo.write(path, f"<!doctype html>\n<head>\n{html}\n</head>\n")
    repo.commit("feat(dashboard): page")
    return repo.run(SCRIPT)


def test_script_is_executable_and_handles_help() -> None:
    assert os.access(INVARIANTS_DIR / SCRIPT, os.X_OK)
    result = subprocess.run(["bash", str(INVARIANTS_DIR / SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0


def test_no_templates_passes(invariant_repo: InvariantRepo) -> None:
    assert invariant_repo.run(SCRIPT).returncode == 0


@pytest.mark.parametrize(
    "html",
    [
        pytest.param(PINNED, id="pinned jsdelivr script with integrity"),
        pytest.param(
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js" ' + SRI + "></script>",
            id="pinned cdnjs script",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/@scope/pkg@1.2.3-rc.1/x.js" ' + SRI + "></script>",
            id="pinned scoped package with a prerelease",
        ),
        pytest.param(
            '<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" '
            'integrity="sha512-abc" crossorigin="anonymous"></script>',
            id="sha512 on unpkg",
        ),
        pytest.param(
            "<script\n  src='https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js'\n"
            "  integrity='sha384-abc'\n  crossorigin='anonymous'></script>",
            id="tag split across lines with single quotes",
        ),
        pytest.param('<script src="/static/app.js"></script>', id="local script"),
        pytest.param("<script>window.x = 1;</script>", id="inline script"),
        pytest.param('<img src="https://avatars.githubusercontent.com/u/1?size=20">', id="remote image"),
        pytest.param('<a href="https://github.com/org/repo">repo</a>', id="link to another site"),
        pytest.param('<link rel="icon" href="https://example.com/favicon.ico">', id="remote icon"),
    ],
)
def test_allowed(invariant_repo: InvariantRepo, html: str) -> None:
    result = run_with_template(invariant_repo, html)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("html", "problem"),
    [
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js"></script>',
            "no integrity hash",
            id="no hash",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js" integrity="sha384-abc"></script>',
            "no crossorigin",
            id="no crossorigin",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js" integrity="md5-abc" '
            'crossorigin="anonymous"></script>',
            "no integrity hash",
            id="unsupported hash algorithm",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked@15/marked.min.js" ' + SRI + "></script>",
            "version not pinned",
            id="major-only range",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked@latest/marked.min.js" ' + SRI + "></script>",
            "version not pinned",
            id="latest tag",
        ),
        pytest.param(
            '<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js" ' + SRI + "></script>",
            "version not pinned",
            id="no version",
        ),
        pytest.param(
            '<script src="https://unpkg.com/@scope/pkg@1/x.js" ' + SRI + "></script>",
            "version not pinned",
            id="scoped package range",
        ),
        pytest.param(
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/latest/dagre.min.js" ' + SRI + "></script>",
            "version not pinned",
            id="cdnjs without a version",
        ),
        pytest.param(
            '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/x@1.0.0/x.css">',
            "no integrity hash",
            id="stylesheet",
        ),
        pytest.param(
            "<script\n  src='https://cdn.jsdelivr.net/npm/x@1.0.0/x.js'\n  defer></script>",
            "no integrity hash",
            id="tag split across lines",
        ),
    ],
)
def test_rejected(invariant_repo: InvariantRepo, html: str, problem: str) -> None:
    result = run_with_template(invariant_repo, html)
    assert result.returncode == 1
    assert problem in result.stdout


def test_failure_reports_file_and_line(invariant_repo: InvariantRepo) -> None:
    result = run_with_template(
        invariant_repo, PINNED + '\n<script src="https://cdn.jsdelivr.net/npm/x@1/x.js"></script>'
    )
    assert result.returncode == 1
    # The page writes a doctype and <head> first, so the offending tag is on line 4.
    assert f"{TEMPLATE}:4: no integrity hash, no crossorigin, version not pinned" in result.stdout


def test_templates_outside_the_dashboard_are_not_checked(invariant_repo: InvariantRepo) -> None:
    html = '<script src="https://cdn.jsdelivr.net/npm/x@1/x.js"></script>'
    assert run_with_template(invariant_repo, html, path="docs/example.html").returncode == 0


def test_the_dashboard_templates_pass() -> None:
    """This repository's own templates satisfy the invariant."""
    repo_root = INVARIANTS_DIR.parent
    result = subprocess.run(["bash", str(INVARIANTS_DIR / SCRIPT), str(repo_root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
