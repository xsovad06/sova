"""Tests for sova.knowledge.personas: tech-stack detection."""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# personas.py: detect_persona
# ---------------------------------------------------------------------------


def test_detect_persona_django(tmp_path: Path) -> None:
    """detect_persona() returns 'django' for manage.py + django in requirements."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python")
    (tmp_path / "requirements.txt").write_text("django>=4.2\ncelery\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "django"


def test_detect_persona_node(tmp_path: Path) -> None:
    """detect_persona() returns 'node' for package.json."""
    (tmp_path / "package.json").write_text('{"name": "test"}')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "node"


def test_detect_persona_go(tmp_path: Path) -> None:
    """detect_persona() returns 'go' for go.mod."""
    (tmp_path / "go.mod").write_text("module example.com/test")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "go"


def test_detect_persona_rust(tmp_path: Path) -> None:
    """detect_persona() returns 'rust' for Cargo.toml."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "rust"


def test_detect_persona_python(tmp_path: Path) -> None:
    """detect_persona() returns 'python' for pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "python"


def test_detect_persona_ruby(tmp_path: Path) -> None:
    """detect_persona() returns 'ruby' for Gemfile."""
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "ruby"


def test_detect_persona_fastapi(tmp_path: Path) -> None:
    """detect_persona() returns 'fastapi' when fastapi is in requirements."""
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "fastapi"


def test_detect_persona_fastapi_in_nested_setup(tmp_path: Path) -> None:
    """detect_persona() finds fastapi in a nested setup.py (monorepo layout)."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "setup.py").write_text('install_requires=["fastapi>=0.104"]\n')

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "fastapi"


def test_detect_persona_odoo(tmp_path: Path) -> None:
    """detect_persona() returns 'odoo' for __manifest__.py with Odoo keys."""
    mod_dir = tmp_path / "my_module"
    mod_dir.mkdir()
    (mod_dir / "__manifest__.py").write_text("{'name': 'Test', 'installable': True, 'depends': ['base']}")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "odoo"


def test_detect_persona_odoo_requires_odoo_keys(tmp_path: Path) -> None:
    """__manifest__.py without Odoo-specific keys does not trigger Odoo detection."""
    mod_dir = tmp_path / "my_module"
    mod_dir.mkdir()
    (mod_dir / "__manifest__.py").write_text("{'name': 'Test'}")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "python"


def test_detect_persona_django_over_fastapi(tmp_path: Path) -> None:
    """Django detection takes priority over FastAPI when both present."""
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python")
    (tmp_path / "requirements.txt").write_text("django>=4.2\nfastapi\n")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "django"


def test_detect_persona_no_match(tmp_path: Path) -> None:
    """detect_persona() returns None when no markers found."""
    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) is None


# ---------------------------------------------------------------------------
# personas.py: package_json_mention
# ---------------------------------------------------------------------------


def test_package_json_mention_found_in_deps(tmp_path: Path) -> None:
    """package_json_mention detects package in dependencies."""
    pkg = json.dumps({"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is True


def test_package_json_mention_found_in_devdeps(tmp_path: Path) -> None:
    """package_json_mention detects package in devDependencies."""
    pkg = json.dumps({"name": "test", "devDependencies": {"@patternfly/react-core": "^5.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is True


def test_package_json_mention_not_found(tmp_path: Path) -> None:
    """package_json_mention returns False when package not listed."""
    pkg = json.dumps({"name": "test", "dependencies": {"react": "^18.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_no_file(tmp_path: Path) -> None:
    """package_json_mention returns False when no package.json."""
    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_malformed(tmp_path: Path) -> None:
    """package_json_mention returns False for malformed JSON."""
    (tmp_path / "package.json").write_text("{bad json")

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


def test_package_json_mention_non_dict(tmp_path: Path) -> None:
    """package_json_mention returns False when JSON is not a dict."""
    (tmp_path / "package.json").write_text("[1, 2, 3]")

    from sova.knowledge.personas import package_json_mention

    assert package_json_mention(tmp_path, "@patternfly/react-core") is False


# ---------------------------------------------------------------------------
# personas.py: PatternFly detection
# ---------------------------------------------------------------------------


def test_detect_persona_patternfly(tmp_path: Path) -> None:
    """detect_persona() returns 'patternfly' for @patternfly/react-core in package.json."""
    pkg = json.dumps({"name": "test", "dependencies": {"@patternfly/react-core": "^5.0.0", "react": "^18.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "patternfly"


def test_detect_persona_patternfly_over_node(tmp_path: Path) -> None:
    """PatternFly wins even when other node markers (lockfile, engines.node) are present."""
    pkg = json.dumps(
        {
            "name": "test",
            "engines": {"node": ">=18"},
            "dependencies": {"express": "^4.0.0", "@patternfly/react-core": "^5.0.0"},
        }
    )
    (tmp_path / "package.json").write_text(pkg)
    (tmp_path / "package-lock.json").write_text("{}")

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "patternfly"


def test_detect_persona_node_without_patternfly(tmp_path: Path) -> None:
    """Plain node project without PatternFly still returns 'node'."""
    pkg = json.dumps({"name": "test", "dependencies": {"express": "^4.0.0"}})
    (tmp_path / "package.json").write_text(pkg)

    from sova.knowledge.personas import detect_persona

    assert detect_persona(tmp_path) == "node"


def test_load_persona_and_packaged_persona_lookup_are_gone() -> None:
    import sova.commands.catalog as catalog
    import sova.knowledge.personas as personas

    assert not hasattr(personas, "load_persona")
    assert not hasattr(catalog, "get_personas_dir")
