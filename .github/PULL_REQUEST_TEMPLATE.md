## What this changes

<!-- One or two sentences: the behaviour before, the behaviour after, and why. -->

## How it was verified

- `python -m pytest tests/ -q` →
- `ruff check .` →
- Live-tested on: <!-- Windows 10/11 · Linux Wayland · Linux X11 · not live-tested (unit tests only) -->

## Checklist

- [ ] **Plugin-only.** No new host or desktop-app API is required; any capability the host may not
      have is feature-detected and degrades honestly.
- [ ] **No new runtime dependency**, or the reason is stated above and in `pyproject.toml`.
- [ ] `CHANGELOG.md` `## [Unreleased]` describes the change.
- [ ] New behaviour is pinned by a hermetic test, or the PR explains why it cannot be.
- [ ] No secrets, API keys, tokens, or sensitive screen/window content in the diff, logs or screenshots.
