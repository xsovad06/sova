"""Every ``sova/`` package documents its responsibility and the packages it may import.

The import-linter layers contract in ``pyproject.toml`` enforces the real imports of the layered packages. This
guard checks that each ``__init__.py`` states a responsibility and an ``Allowed imports:`` line, that the line names
only packages that exist and, for a layered package, sit at the same or a lower layer (never an independent sibling),
and that every import import-linter cannot see is recorded: a package outside the contract names every ``sova``
package it imports, and a layered one every package outside the contract it imports.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent
SOVA_DIR = REPO_DIR / "sova"
VERSIONS_DIR = SOVA_DIR / "db" / "migrations" / "versions"
# The Alembic revisions directory is data, not a package with a responsibility of its own.
INIT_FILES = sorted(p for p in SOVA_DIR.rglob("__init__.py") if VERSIONS_DIR not in p.parents)
ALLOWED_RE = re.compile(r"^Allowed imports:[ \t]*\S", re.MULTILINE)
ALLOWED_BLOCK_RE = re.compile(r"^Allowed imports:(.*?)(?:\n\s*\n|\Z)", re.MULTILINE | re.DOTALL)
SOVA_PKG_RE = re.compile(r"\bsova\.([a-z_0-9]+)")


def _contract_layer_lines() -> list[str]:
    data = tomllib.loads((REPO_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = [c for c in data["tool"]["importlinter"]["contracts"] if c.get("type") == "layers"]
    assert len(contracts) == 1, f"expected exactly one layers contract, found {len(contracts)}"
    return contracts[0]["layers"]


def _parse_layers(lines: list[str]) -> tuple[dict[str, int], dict[str, frozenset[str]]]:
    """Each package's layer index (0 is the highest) and, per package, the siblings it may not import.

    import-linter joins siblings that may import each other with ``:`` and independent siblings, which may not, with
    ``|``; a line mixing both is not valid in the contract, so it is rejected here too.
    """
    index: dict[str, int] = {}
    independent: dict[str, frozenset[str]] = {}
    for position, line in enumerate(lines):
        assert not (":" in line and "|" in line), f"a layer mixes ':' and '|' siblings: {line!r}"
        entries = [entry.strip().removeprefix("sova.") for entry in re.split(r"[:|]", line)]
        for entry in entries:
            index[entry] = position
            independent[entry] = frozenset(e for e in entries if e != entry) if "|" in line else frozenset()
    return index, independent


LAYERS, INDEPENDENT_SIBLINGS = _parse_layers(_contract_layer_lines())


def _top_level(init_file: Path) -> str | None:
    rel = init_file.relative_to(SOVA_DIR)
    return rel.parts[0] if len(rel.parts) > 1 else None


def _allowed_text(init_file: Path) -> str:
    docstring = ast.get_docstring(ast.parse(init_file.read_text(encoding="utf-8"))) or ""
    match = ALLOWED_BLOCK_RE.search(docstring)
    return match.group(1) if match else ""


def _bare_package_words(init_file: Path) -> list[str]:
    """Words in the ``Allowed imports:`` text that spell a ``sova`` package without the ``sova.`` prefix."""
    packages = {p.name for p in SOVA_DIR.iterdir() if (p / "__init__.py").exists()}
    # Inline code (``utils.env``) is a module reference in prose, not an import grant.
    text = re.sub(r"``[^`]*``", "", SOVA_PKG_RE.sub("", _allowed_text(init_file)))
    return sorted({word for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text) if word in packages})


def _named_packages(init_file: Path) -> set[str]:
    return set(SOVA_PKG_RE.findall(_allowed_text(init_file)))


def _imported_in_source(source: str) -> set[str]:
    """Top-level ``sova`` packages a module's source imports (function-level imports included)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
            if node.module == "sova":
                # ``from sova import config`` names the package in the alias, not in the module.
                names = [f"sova.{alias.name}" for alias in node.names if (SOVA_DIR / alias.name).is_dir()]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.update(m.group(1) for name in names if (m := re.match(r"sova\.([a-z_0-9]+)", name)))
    return found


def _imported_packages(package_dir: Path) -> set[str]:
    """Top-level ``sova`` packages imported anywhere under ``package_dir``."""
    found: set[str] = set()
    for module in package_dir.rglob("*.py"):
        found |= _imported_in_source(module.read_text(encoding="utf-8"))
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_DIR))


def test_init_files_are_found() -> None:
    assert SOVA_DIR / "__init__.py" in INIT_FILES
    assert SOVA_DIR / "dashboard" / "services" / "__init__.py" in INIT_FILES
    assert VERSIONS_DIR / "__init__.py" not in INIT_FILES


def test_every_directory_with_modules_is_a_regular_package() -> None:
    """A directory of modules without ``__init__.py`` is an implicit namespace package this guard cannot see."""
    missing = sorted(
        _rel(directory)
        for directory in {p.parent for p in SOVA_DIR.rglob("*.py")}
        if "__pycache__" not in directory.parts and not (directory / "__init__.py").exists()
    )
    assert not missing, f"directories with modules but no __init__.py: {missing}"


@pytest.mark.parametrize("init_file", INIT_FILES, ids=_rel)
def test_init_docstring_states_responsibility_and_allowed_imports(init_file: Path) -> None:
    docstring = ast.get_docstring(ast.parse(init_file.read_text(encoding="utf-8")))
    assert docstring, f"{init_file} has no module docstring"
    summary = docstring.splitlines()[0]
    assert not summary.startswith("Allowed imports:"), f"{init_file} docstring lacks a responsibility summary"
    assert ALLOWED_RE.search(docstring), f"{init_file} docstring needs a non-empty 'Allowed imports:' line"


@pytest.mark.parametrize("init_file", INIT_FILES, ids=_rel)
def test_allowed_imports_line_follows_the_grammar(init_file: Path) -> None:
    """Names are written ``sova.<package>`` (or says none): a bare ``dashboard`` would dodge the layer check."""
    text = _allowed_text(init_file)
    assert not _bare_package_words(init_file), (
        f"{_rel(init_file)} names packages without the sova. prefix: {_bare_package_words(init_file)}"
    )
    assert _named_packages(init_file) or re.search(r"\bnone\b", text, re.IGNORECASE), (
        f"{_rel(init_file)} 'Allowed imports:' must name sova.<package> packages or say none: {text.strip()!r}"
    )


def test_the_allowed_imports_grammar_rejects_a_bare_package_name(tmp_path: Path) -> None:
    init_file = tmp_path / "__init__.py"
    init_file.write_text('"""Summary.\n\nAllowed imports: dashboard, supervisor\n"""\n', encoding="utf-8")
    assert _bare_package_words(init_file) == ["dashboard", "supervisor"]
    assert not _named_packages(init_file)
    init_file.write_text('"""Summary.\n\nAllowed imports: sova.config, sova.utils\n"""\n', encoding="utf-8")
    assert _bare_package_words(init_file) == []


def _layer_violations(
    own: str, named: set[str], layers: dict[str, int], independent: dict[str, frozenset[str]]
) -> list[str]:
    """Named contract packages above *own*, or independent siblings of it; a package outside the contract is fine."""
    above = [pkg for pkg in named if pkg in layers and layers[pkg] < layers[own]]
    siblings = [pkg for pkg in named if pkg in independent.get(own, frozenset())]
    return sorted(above + siblings)


@pytest.mark.parametrize("init_file", INIT_FILES, ids=_rel)
def test_allowed_imports_name_existing_packages_at_or_below_the_layer(init_file: Path) -> None:
    named = _named_packages(init_file)
    missing = sorted(pkg for pkg in named if not (SOVA_DIR / pkg).is_dir())
    assert not missing, f"{_rel(init_file)} names packages that do not exist under sova/: {missing}"

    own = _top_level(init_file)
    if own not in LAYERS:
        return
    violations = _layer_violations(own, named, LAYERS, INDEPENDENT_SIBLINGS)
    assert not violations, f"{_rel(init_file)} names layers above {own}, or independent siblings: {violations}"


def test_an_independent_sibling_may_not_be_named() -> None:
    layers, independent = _parse_layers(["sova.cli", "sova.llm | sova.git", "sova.utils"])
    assert _layer_violations("llm", {"git", "utils"}, layers, independent) == ["git"]
    layers, independent = _parse_layers(["sova.cli", "sova.llm : sova.git", "sova.utils"])
    assert _layer_violations("llm", {"git", "utils"}, layers, independent) == []
    assert _layer_violations("llm", {"cli", "oversight"}, layers, independent) == ["cli"]


@pytest.mark.parametrize("init_file", [p for p in INIT_FILES if _top_level(p) in LAYERS], ids=_rel)
def test_allowed_imports_cover_a_layered_packages_imports_outside_the_contract(init_file: Path) -> None:
    """import-linter cannot see an edge into a package outside the contract, so the docstring must record it."""
    own = _top_level(init_file)
    outside_contract = {p.name for p in SOVA_DIR.iterdir() if (p / "__init__.py").exists()} - LAYERS.keys()
    actual = (_imported_packages(init_file.parent) - {own}) & outside_contract
    undocumented = sorted(actual - _named_packages(init_file))
    assert not undocumented, f"{_rel(init_file)} does not list the non-contract packages it imports: {undocumented}"


@pytest.mark.parametrize("init_file", [p for p in INIT_FILES if _top_level(p) is not None], ids=_rel)
def test_allowed_imports_cover_the_imports_of_a_package_outside_the_contract(init_file: Path) -> None:
    """import-linter does not check these packages, so the docstring is their only record of what they import."""
    own = _top_level(init_file)
    if own in LAYERS:
        return
    actual = _imported_packages(init_file.parent) - {own}
    undocumented = sorted(actual - _named_packages(init_file))
    assert not undocumented, f"{_rel(init_file)} does not list the sova packages it imports: {undocumented}"


def test_the_two_commands_packages_name_each_other() -> None:
    """``sova/commands/`` and ``sova/cli/commands/`` share a name and nothing else, so each explains the split."""
    cli_doc = ast.get_docstring(ast.parse((SOVA_DIR / "cli" / "commands" / "__init__.py").read_text(encoding="utf-8")))
    templates_doc = ast.get_docstring(ast.parse((SOVA_DIR / "commands" / "__init__.py").read_text(encoding="utf-8")))
    assert cli_doc and "sova.commands" in cli_doc
    assert templates_doc and "sova.cli.commands" in templates_doc


def test_imported_packages_sees_the_from_sova_import_form() -> None:
    source = (
        "from sova import config, db\nfrom sova import not_a_package\nimport sova.utils.env\nfrom sova.llm import x\n"
    )
    assert _imported_in_source(source) == {"config", "db", "utils", "llm"}
