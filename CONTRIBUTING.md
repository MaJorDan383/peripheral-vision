# Contributing

Thanks for taking a look. Peripheral Vision is a [Hermes](https://github.com/NousResearch/hermes-agent)
plugin: a self-contained directory that Hermes loads at runtime. Everything it needs ships in
this repo — there is no separate build step, and no desktop-app changes (see [Scope](#scope)).

## Scope

Two rules keep the plugin installable by anyone:

1. **Plugin-only.** Features live in this directory. A change that needs a new host API, a new
   desktop-app hook, or a patched Hermes build is out of scope until that API exists upstream —
   feature-detect the capability and degrade honestly instead (the pane does this with
   `host.dismissPane`).
2. **No new runtime dependency without a reason.** `[project].dependencies` in `pyproject.toml`
   is what Hermes installs for every user of the plugin, so an addition has to cover a concrete
   capability that the standard library and the existing three packages cannot.

## Development setup

Python 3.9+ (CI covers 3.9, 3.11 and 3.13). A Hermes install is **not** needed to develop or
test the plugin.

```bash
# POSIX
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Windows (PowerShell or Git Bash)
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
```

`-e ".[dev]"` installs the runtime dependencies plus `pytest` and `ruff`. The plugin declares no
importable package (`[tool.setuptools] packages = []`) because Hermes loads the directory by
path, not through the package index.

## Running the tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q    # Windows
.venv/bin/python -m pytest tests/ -q            # POSIX
python run_tests.py                             # the same run, from any interpreter with pytest
```

The suite is **hermetic**: no desktop session, no camera, no model, no network, no Hermes
install. Anything that would need one of those is stubbed at a seam instead.

| Seam | What it replaces |
|---|---|
| `plugin_api._window_iconic` / `._window_rect` / `._window_exe` | window geometry and identity, without user32 |
| `plugin_api._thumb_capture` | minimized-window thumbnails, without the DWM thumbnail host |
| `ctypes.windll.user32` (monkeypatched in `tests/test_window_enum.py`) | full window enumeration, against a fake user32 that records call order |
| `tests/test_linux_schema.py` | stubs `cv2`/`numpy` so the Linux parser contract is testable without a camera stack |

Consequences worth knowing before adding a test:

- **Platform-specific tests must skip, not fail.** `pytest.skip("Windows-only (ctypes.windll)")`
  and `pytest.skip("POSIX only")` are the established patterns, and CI runs Linux *and* Windows.
- **A test that needs a real screen, window or camera does not belong in CI.** If behaviour can
  only be observed live, say so in the PR and keep a stubbed unit test for the logic around it.

## Lint

```bash
ruff check .
```

`pyproject.toml` selects `F` (pyflakes), `E9` (real runtime errors) and `I` (import order) only.
The wider pycodestyle/pylint families are intentionally off: this is a resilience-first capture
layer where broad `except` blocks and best-effort fallbacks *are* the design, so enabling them
would mostly mean suppressing them. `ruff format` is not enforced — match the surrounding code.

CI runs the same command, so a clean local run means a green lint job.

## Layout

| Path | What lives there |
|---|---|
| `plugin.yaml` | plugin manifest: name, version, `platforms`, `requires_hermes`, hooks |
| `__init__.py` | Hermes entry points: `pre_llm_call` injection, heartbeat gates, mention patterns |
| `dashboard/plugin_api.py` | FastAPI routes for the pane plus the capture engine (status, start/stop, preview, snapshots) |
| `dashboard/capture.py` | the cross-platform capture contract; picks a backend once at import time |
| `dashboard/capture_windows.py` | Win32 backend (GDI/DWM/PrintWindow, DirectShow cameras) |
| `dashboard/capture_linux.py` | Linux backend (xrandr/wmctrl/import/grim/portal, V4L2 cameras) |
| `dashboard/shared_state.py` | state shared by the engine, the routes and the capture backends |
| `desktop/plugin.js` | the pane UI (source picker, preview, crop overlay, snapshot flow) |
| `docs/` | design and feasibility notes |
| `tests/` | the hermetic pytest suite |

A new platform means implementing the backend contract declared in `dashboard/capture.py` and
registering it in the dispatch there.

## Pull requests

- Keep it focused: one behaviour per PR, with the test that pins it.
- Add a line to `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed / Removed,
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style).
- Say which platform you ran it on, and whether it was verified live or unit-tested only.
  Honest coverage notes are welcome; overclaiming is not.
- Never include secrets, API keys, or a screenshot or window title you would not want public.

## Releasing (maintainers)

1. Move `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`.
2. Set the same version in `plugin.yaml` and `pyproject.toml`.
3. Tag `vX.Y.Z` and publish the GitHub release from that changelog section.
4. `python build_release.py` writes a clean `peripheral-vision-release.zip` next to the plugin
   directory (tests, caches and dot-directories are excluded).
