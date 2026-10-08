"""Package the plugin for release: build a clean zip next to this plugin directory.

Portable on purpose — every path is derived from this file's own location, so the script
works from any checkout. Build-only files (this script, the test suite, caches) are excluded
from the archive; they are not part of what a user installs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
OUT = PLUGIN_DIR.parent / f"{PLUGIN_DIR.name}-release.zip"

# Directory names anywhere in the path that never belong in a release.
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".github",
    "dist", "build", ".cache", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", ".idea", ".vscode", ".venv", "venv",
}

# Top-level names that are development-only: not shipped to users.
EXCLUDE_NAMES = {
    Path(__file__).name,      # this build script
    "run_tests.py",
    "conftest.py",
    "tests",
}

# Suffixes that are compiled or previously packaged artifacts.
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".whl", ".egg", ".zip", ".tmp")


def collect() -> list[Path]:
    """Every file that should ship, sorted for a reproducible archive."""
    files: list[Path] = []
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(PLUGIN_DIR)

        # A dot-directory anywhere above the file (.git, .pytest_cache) is never shipped.
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        # A setuptools artifact from an editable install. It is gitignored and regenerated
        # locally, and its PKG-INFO would ship whatever version was current when it was
        # built — a release zip claiming a version the plugin does not have.
        if any(part.endswith(".egg-info") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in EXCLUDE_NAMES:
            continue
        if rel.name.startswith("."):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue

        files.append(path)
    return files


def main() -> int:
    files = collect()
    if not files:
        print("No files matched — refusing to write an empty archive.")
        return 1

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = path.relative_to(PLUGIN_DIR)
            zf.write(path, rel)
            print(f"  added {rel.as_posix()}")

    print(f"\nWrote {OUT} ({OUT.stat().st_size:,} bytes, {len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
