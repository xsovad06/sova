"""The checked-in tailwind.min.css must be exactly what `make css` builds from today's templates (#1388).

The bundle is rebuilt by hand, so a template class added without `make css` renders unstyled with no
error anywhere. This rebuilds it with the Makefile's pinned Tailwind package and compares byte for byte
(modulo trailing whitespace). It needs node, and npx fetches the package on a cold cache, so it skips
when node is missing or npx cannot run the build at all; a build that runs and differs fails.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
BUNDLE = REPO_ROOT / "sova" / "dashboard" / "static" / "tailwind.min.css"
_BUILD_TIMEOUT_SECONDS = 150


def _tailwind_package() -> str:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^TAILWIND := (tailwindcss@\d+\.\d+\.\d+)$", makefile, re.MULTILINE)
    assert match, "Makefile must pin TAILWIND to an exact tailwindcss version"
    return match.group(1)


@pytest.mark.slow(reason="npx may download Tailwind, then builds the whole bundle")
@pytest.mark.timeout(_BUILD_TIMEOUT_SECONDS + 30)
@pytest.mark.skipif(shutil.which("npx") is None, reason="node (npx) is not installed")
def test_checked_in_bundle_matches_a_rebuild(tmp_path: Path) -> None:
    output = tmp_path / "tailwind.min.css"
    try:
        result = subprocess.run(
            ["npx", "--yes", _tailwind_package(), "-o", str(output), "--minify"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_BUILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("the Tailwind build did not finish (cold npx cache on a slow network?)")
    if result.returncode != 0 or not output.is_file():
        pytest.skip(f"npx could not run Tailwind: {result.stderr.strip()[-500:]}")

    rebuilt = output.read_text(encoding="utf-8").rstrip()
    assert rebuilt == BUNDLE.read_text(encoding="utf-8").rstrip(), (
        "sova/dashboard/static/tailwind.min.css is stale against the templates: run `make css` and commit it"
    )
