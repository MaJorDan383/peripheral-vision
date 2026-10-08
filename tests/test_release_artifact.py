"""What ships in the release zip: the plugin, and nothing else.

The zip is what a user installs, so a stray build artifact is not cosmetic -- a stale
`*.egg-info/PKG-INFO` ships a version string that contradicts `plugin.yaml`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

build_release = pytest.importorskip("build_release")


def _shipped() -> list[str]:
    return [p.relative_to(REPO).as_posix() for p in build_release.collect()]


def test_no_build_artifacts_ship():
    names = _shipped()
    assert names, "the archive would be empty"
    for junk in ("egg-info", "__pycache__", ".pytest_cache", "dist/", "build/"):
        assert not [n for n in names if junk in n], f"{junk} must not ship: {names}"


def test_no_dev_only_files_ship():
    names = _shipped()
    for dev in ("tests/", "build_release.py", "run_tests.py", "conftest.py", ".github/"):
        assert not [n for n in names if n.startswith(dev) or f"/{dev}" in n], f"{dev} must not ship"


def test_the_things_a_plugin_needs_do_ship():
    names = _shipped()
    for required in ("plugin.yaml", "dashboard/plugin_api.py", "dashboard/manifest.json"):
        assert required in names, f"{required} is missing from the archive"


def test_version_is_the_same_in_all_three_places():
    """Hermes reads plugin.yaml; the pane reads manifest.json; pip reads pyproject.toml."""
    import json
    import re

    yaml_version = re.search(r"(?m)^version:\s*(\S+)", (REPO / "plugin.yaml").read_text(encoding="utf-8"))
    manifest = json.loads((REPO / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    toml_version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert yaml_version and toml_version
    assert yaml_version.group(1) == manifest["version"] == toml_version.group(1), (
        yaml_version.group(1), manifest["version"], toml_version.group(1)
    )
