"""Every third-party import must be declared somewhere that CI installs from.

LAW.md §15: extracting a component exposes its suite's undeclared
dependencies, and the first run on a clean runner is when it learns this. A
suite that has only ever run inside one venv imports whatever that venv
happened to carry for other reasons, and nothing says so while the code stays
put. tempest_wx is the estate's worked example -- `import yaml`, green for
months, red at collection on its own repo's first run.

The check is cheap and static, which is the point: it runs in milliseconds on
every commit rather than waiting for an extraction that may be years away.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

import custom_components.linux_monitor as _component

# DERIVED FROM THE IMPORTED PACKAGE, not from this file's position. The suite
# runs against a staged layout (tests/conftest.py says why), so the
# integration is NOT this directory's sibling -- it is wherever
# custom_components.linux_monitor was imported from. Walking up from __file__
# found the workspace root and looked for a manifest.json that is one level
# further down, which is how this check failed while the thing it checks was
# fine.
COMPONENT_DIR = pathlib.Path(_component.__file__).resolve().parent
TESTS_DIR = pathlib.Path(__file__).resolve().parent
# requirements_test.txt belongs to the WORKSPACE, beside the suite, not to the
# integration -- the staging step moves it out of the package for that reason.
WORKSPACE_ROOT = TESTS_DIR.parent

# Ships INSIDE Home Assistant core, so manifest.json's requirements can stay
# empty and still be truthful. Anything else the component imports has to be
# declared there -- that is what this file exists to notice.
RUNTIME_PROVIDED = {"homeassistant", "voluptuous"}

# Pulled in by requirements_test.txt, directly or through
# pytest-homeassistant-custom-component's own pins.
TEST_PROVIDED = RUNTIME_PROVIDED | {
    "pytest",
    "pytest_homeassistant_custom_component",
    "custom_components",  # this integration, via tests/conftest.py's staging
}


def _toplevel_imports(path: pathlib.Path) -> set[str]:
    """Top-level module names imported by one file, third-party or not.

    By AST rather than by regex: a name inside a string or a comment is not an
    import, and a scan that cannot tell the difference reports work that does
    not exist.
    """
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import -- this package, never a
            # dependency. node.module is None for `from . import x`.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def _third_party(names: set[str]) -> set[str]:
    return {n for n in names if n not in sys.stdlib_module_names}


def _component_files() -> list[pathlib.Path]:
    return sorted(COMPONENT_DIR.glob("*.py"))


def _test_files() -> list[pathlib.Path]:
    return sorted(TESTS_DIR.glob("*.py"))


def test_the_component_imports_nothing_it_has_not_declared() -> None:
    """manifest.json's `requirements: []` is a claim. This is the check."""
    import json

    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
    declared = {
        # "foo==1.2" / "foo>=1.2" / "foo" -> "foo"
        req.split("==")[0].split(">=")[0].split("[")[0].strip().replace("-", "_")
        for req in manifest.get("requirements", [])
    }
    allowed = RUNTIME_PROVIDED | declared

    files = _component_files()
    assert files, "sweep found no component files -- it would pass vacuously"

    undeclared: dict[str, set[str]] = {}
    for path in files:
        extra = _third_party(_toplevel_imports(path)) - allowed
        if extra:
            undeclared[path.name] = extra
    assert not undeclared, (
        "third-party imports not covered by manifest.json requirements nor "
        f"shipped inside Home Assistant: {undeclared}"
    )


def test_the_suite_imports_nothing_requirements_test_does_not_install() -> None:
    files = _test_files()
    assert files, "sweep found no test files -- it would pass vacuously"

    undeclared: dict[str, set[str]] = {}
    for path in files:
        extra = _third_party(_toplevel_imports(path)) - TEST_PROVIDED
        if extra:
            undeclared[path.name] = extra
    assert not undeclared, (
        "test imports not installed by requirements_test.txt: "
        f"{undeclared}. Declare them in that file, never in the workflow."
    )


def test_requirements_test_pins_the_harness_exactly() -> None:
    """An unpinned harness silently changes which Home Assistant the suite
    runs against, which is the one thing this file's pin is for."""
    text = (WORKSPACE_ROOT / "requirements_test.txt").read_text()
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements_test.txt declares nothing"
    assert any(
        line.startswith("pytest-homeassistant-custom-component==")
        for line in lines
    ), f"the harness must be pinned with ==, got {lines}"


def test_the_detector_can_actually_fail(tmp_path: pathlib.Path) -> None:
    """A sweep that cannot report a hit is not a sweep. Plant one.

    Also pins the two behaviours that would make this file pass over a real
    problem: a relative import must not be mistaken for a dependency, and a
    module name appearing only in a string must not be counted.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import os\n"
        "import some_undeclared_package\n"
        "from . import sibling\n"
        "from .const import DOMAIN\n"
        "NOT_AN_IMPORT = 'import another_undeclared_package'\n",
        encoding="utf-8",
    )
    third = _third_party(_toplevel_imports(planted))
    assert "some_undeclared_package" in third, "the detector missed a real import"
    assert "another_undeclared_package" not in third, "a string was read as an import"
    assert "sibling" not in third and "const" not in third, (
        "a relative import was read as a dependency"
    )
    assert "os" not in third, "a stdlib module was reported as third-party"


@pytest.mark.parametrize("path", _component_files(), ids=lambda p: p.name)
def test_every_component_file_parses(path: pathlib.Path) -> None:
    """Cheap, and it makes the sweep above meaningful: a file that cannot be
    parsed contributes no imports and would otherwise read as clean."""
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
