#!/usr/bin/env bash
# The whole gate in one command: lint, then the suite — the same two things CI runs.
#
#   scripts/check.sh                 # lint + tests
#   scripts/check.sh -x -k label     # extra arguments go to pytest
#
# Run it from an activated venv (`pip install -e ".[dev]"`), or point PYTHON at one:
#   PYTHON=.venv/Scripts/python.exe scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v ruff >/dev/null; then
  echo 'ruff not found — run: pip install -e ".[dev]" (or activate the venv)' >&2
  exit 2
fi

ruff check .
exec "${PYTHON:-python}" run_tests.py "$@"
