# Peripheral Vision

[![tests](https://github.com/MaJorDan383/peripheral-vision/actions/workflows/tests.yml/badge.svg)](https://github.com/MaJorDan383/peripheral-vision/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platforms: Windows stable, Linux preview](https://img.shields.io/badge/platforms-Windows%20stable%20%C2%B7%20Linux%20preview-9cf.svg)](#platform-support)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Hermes 0.20.1+](https://img.shields.io/badge/Hermes-%E2%89%A50.20.1-8a63d2.svg)](plugin.yaml)

Peripheral Vision is a plugin for [Hermes](https://hermes-agent.nousresearch.com/docs) that keeps a live view of your screen, windows, and cameras, and feeds fresh descriptions to Hermes before each response. It turns your desktop into a persistent context source so Hermes can see what's on your screen, which window is focused, and what your cameras see — without you having to screenshot every time.

## Features

- **Display capture** — grabs whole displays: a GDI screen grab on Windows, `import`/`grim` or the desktop portal on Linux
- **Window capture** — captures a single application window (PrintWindow on Windows, or a DWM thumbnail while it is minimized)
- **Camera capture** — enumerates cameras and grabs frames (DirectShow on Windows, V4L2 on Linux)
- **Vision routing** — sends frames to your auxiliary vision model when one is picked in
  Hermes' own settings, otherwise to the model the live session is running
- **Pre-LLM context injection** — adds live descriptions to a turn through the `pre_llm_call` hook, gated by the inject mode (picked in the pane, or `PV_VISION_INJECT_MODE`) so a screen that has not moved does not pay for itself on every turn
- **Desktop pane** — shows a live preview with monitor/window/camera picker, vision model candidates, and pin support
- **Manual snapshots** — set "Blink rate" to manual and snapshot on demand: a camera frame, or a drag-cropped screenshot of a display or window, lands in the message input ready to send

## Platform support

| Platform | Status | Notes |
|---|---|---|
| **Windows 10/11** | ✅ **Stable** | DWM monitor + window capture, DirectShow cameras — the original, battle-tested path. |
| **Linux — GNOME/Wayland** | 🧪 **Preview** | Live-verified on Ubuntu 24.04 GNOME (Wayland). Monitors, window list and per-window crops work via `xdg-desktop-portal`. Caveats below. |
| **Linux — X11 / KDE / wlroots** | 🧪 **Preview (unverified)** | Code-complete and unit-tested (EWMH/`wmctrl`, XComposite, grim), but not yet run on a real session of those types. |
| **macOS** | ❌ Unsupported | Not implemented; the installer refuses on `darwin`. |

**Linux preview caveats:**
- **Grab speed:** each frame goes through a one-shot portal screenshot (~1.5–3.2 s), so a watch
  loop runs slower than on Windows (~50 ms). A PipeWire screencast path is planned to close this
  gap — see issue #1.
- **Window crops are region-crops on Wayland** — an overlapping window can bleed in. Wayland
  cannot expose window geometry, so this is inherent to the platform, not a bug.
- **First grab shows a one-time portal consent dialog** (persisted afterwards via the portal).
- **Camera capture is untested on real Linux hardware** — reports welcome.

## Installation

Source, issues and releases: **https://github.com/MaJorDan383/peripheral-vision**

### Requirements
- **Windows 10/11** (Win32/DWM/DirectShow) or **Linux** (see [Platform support](#platform-support);
  macOS unsupported). See [docs/cross-platform-capture.md](docs/cross-platform-capture.md) for the exact per-session
  feature matrix.
- Python 3.9+
- Hermes **0.20.1 or newer** (declared as `requires_hermes` in `plugin.yaml`). 0.20.1 is the
  oldest host that has everything the plugin calls — `ctx.on_unload` and the `pre_llm_call`
  hook both landed in it. Hosts before 0.21.2 ignore the declaration entirely (they warn, they
  never refuse); from 0.21.2 on, the loader enforces it. Use **0.21.4+** if the vision model
  lives on OpenCode Zen/Go: those endpoints reject requests without session-affinity headers,
  and the host helper that builds them only exists from 0.21.4.
- Python packages `opencv-python-headless` (`>=4.5,<6`), `Pillow` (`>=10.0,<13`) and `openai`
  (`>=1.0.0,<3`) — declared in `pyproject.toml` (`[project].dependencies`), the file Hermes reads
  for plugins. Hermes installs them when you install or enable the plugin, resolved together with
  Hermes's own dependency ranges (a combination that cannot resolve is refused up front), and
  re-applies them after `hermes update`. To install them yourself instead:
  `pip install "opencv-python-headless>=4.5,<6" "Pillow>=10.0,<13" "openai>=1.0.0,<3"`

### Install from the Hermes CLI (recommended)

```bash
hermes plugins install MaJorDan383/peripheral-vision --enable
```

Hermes fetches the repository, installs the declared dependencies, and — because of `--enable` —
turns the plugin on in the same step, skipping the "Enable now?" prompt. Add `--yes-deps` when
nothing can answer that prompt (CI, SSH automation, a container entrypoint).

### Manual install

Clone or download the repo, then copy the plugin directory into your plugins folder:

**PowerShell (Windows):**
```powershell
Copy-Item -Recurse peripheral-vision "$env:LOCALAPPDATA\hermes\plugins\peripheral-vision"
```

**Git Bash / MSYS:**
```bash
cp -r peripheral-vision/ "$LOCALAPPDATA/hermes/plugins/peripheral-vision/"
```

If you run Hermes with a custom `HERMES_HOME`, use `$HERMES_HOME/plugins/peripheral-vision` instead.

### Enable it

Hermes loads a **standalone** plugin only when its key is listed under `plugins.enabled`, so a
directory you copied in stays inert — no capture, no context injection — until you say so:

```bash
hermes plugins enable peripheral-vision     # turn it on
hermes plugins disable peripheral-vision    # turn it off without deleting anything
hermes plugins list                         # state of everything installed
hermes plugins show peripheral-vision       # version, hooks and details for this plugin
```

## Configuration

The plugin reads environment variables for tuning. They are read from the process that runs
the Hermes backend (the desktop app inherits your user environment):

| Variable | Default | Description |
|----------|---------|-------------|
| `PV_VISION_TIMEOUT_S` | `60` | Timeout (seconds) for a single vision API call |
| `PV_VISION_ATTEMPTS` | `2` | Attempts per frame before reporting failure |
| `PV_VISION_MAX_EDGE` | `1024` | Longest edge of the frame sent to the model (wins over `PV_VISION_MAX_WIDTH`) |
| `PV_VISION_MAX_PIXELS` | _(unset)_ | Total-pixel budget for that frame; takes precedence over the edge caps |
| `PV_VISION_MAX_WIDTH` | `1024` | Older alias for the edge cap, and the width cap on the watch's own frames |
| `PV_VISION_SQUARE` | _(unset)_ | Force a square intake (auto-enabled for CLIP-style encoders) |
| `PV_VISION_PROMPT` | _(built-in)_ | Prompt sent with each frame for description |
| `PV_VISION_INJECT_MODE` | `on_change` | When a fresh reading rides a turn; a pick in the desktop pane overrides it — see [Injection modes](#injection-modes) |
| `PV_PREVIEW_MAX_AGE_S` | `6.0` | How stale the frame behind the pane preview may get while nothing moves (matches the pane's own 6s poll) |
| `PV_SOURCE_FAILURE_LIMIT` | `3` | Consecutive capture failures before the watch stops |
| `PV_CAMERA_MAX_INDEX` | `4` | Highest DirectShow camera index to probe |
| `PV_CAMERA_CACHE_S` | `600` | Seconds the enumerated camera list stays cached |
| `PV_CAMERA_PROBE_TIMEOUT_S` | `5.0` | Timeout (seconds) for detecting one camera |
| `PV_CAMERA_PROBE_WAVE` | `2` | Camera probe waves before giving up |

Choosing the vision model is **not** an environment variable. Precedence: an explicit
`auxiliary.vision` model in Hermes' own settings (Settings → Models) is the vision route —
frames go there even when the session model can see; with no pick, frames go to the model the
live session is running while it can see; a text-only session falls back to a model pinned from
the desktop pane (`vision_model.json` in the plugin's state directory). An empty pin restores
automatic routing.

## Usage

The plugin loads with Hermes but **never picks a source for you**: watching starts only when you
choose a display, window, or camera in the desktop pane. The pane shows:

1. **Monitor picker** — click a monitor to preview its frame
2. **Window picker** — select an application window to capture
3. **Camera picker** — switch between connected cameras
4. **Vision candidates** — model suggestions based on intake size
5. **Pin button** — lock a specific vision model for the session
6. **Inject picker** ("Rides your turns") — when fresh readings are shared; see [Injection modes](#injection-modes)
7. **Blink rate** — how often the watch checks for changes; on **manual** it also shows the **Snapshot** buttons beside its picker on the same line; see [Manual snapshots](#manual-snapshots)
8. **Status** — live status of the capture engine, frame count, source info

Once a source is selected, descriptions are injected into Hermes context before a response,
giving the assistant a live view of your screen. How often is yours to decide:

### Injection modes

Pick it live in the desktop pane — the **"Rides your turns"** select, just above the
Blink-rate picker. The pick is stored in the plugin's state directory (`inject_mode`) and read on every
turn, so it applies to the next turn in every session without a restart. Without a pane pick,
`PV_VISION_INJECT_MODE` — set in the environment that runs the Hermes backend — supplies the
value; without either, the default applies. A pane pick outranks the environment variable;
deleting the `inject_mode` file (or posting an empty mode to `POST /inject_mode`) restores the
environment/default value. The select's tooltip names where the current value comes from.

| Mode | What rides a turn |
|------|-------------------|
| `on_change` *(default)* | Only when the reading differs from the last one sent in that session — plus a re-send of unchanged content every 10 minutes, so a screen that never moves is still re-anchored instead of going silent for the rest of a long session |
| `always` | Every turn while the reading is fresh (what the plugin did before modes existed) |
| `on_mention` | Only when your own message points at the screen — `screen`, `monitor`, `display`, `desktop`, `what do you see`, `can you see`, `look at`, `see this`, `this window`, `visible` (the full list is `MENTION_PATTERNS` in `__init__.py`, kept narrow on purpose: firing on the word "window" in "open a new window" costs tokens on a turn that never needed eyes) |
| `tool_only` | Never ambient — reserved for builds that expose a live-view *tool*. This plugin registers no such tool, so the model is told nothing and the plugin logs a warning rather than looking like a silent failure |

Every mode keeps the same hard gates: the capture loop must be **alive** — its heartbeat in
`status.json` newer than 120 seconds, so a backend that crashed or was killed stops injecting
instead of riding its last reading — and the newest frame must have arrived within the last 90
seconds (a stale description is misleading, so it is never injected). The block is capped at 1200
characters and 3 descriptions. An unrecognised value logs a warning once and falls back to
`on_change`.

`on_change` compares the descriptions themselves, not the whole block: the header states the
frame's age, which changes every turn, so a whole-text comparison would never match and the mode
would save nothing.

### Manual snapshots

Set **"Blink rate"** to **manual** and the watch stops checking on its own — nothing is
described until you snapshot — while a **Snapshot** pair appears beside the picker on that same
line: one button with a camera icon (a camera frame) and one with a screen-crop icon
(a screenshot). Each button
first pops the matching source list — cameras for the camera button; displays and application
windows for the screen button — and picking a source opens a live view where you drag out the
area to keep. Snapping crops at the source's full resolution, saves a PNG under the plugin's
state directory (`snaps/`), and stages it into the chat **message input**, ready to send like
any other image. No drag? The whole frame is taken. Switching back to a rhythm hides it
again, and either pick is applied to a running watch without a restart. The crop view is fed at
up to 1920 px wide while the crop itself is cut from the full-resolution capture, so the image
that reaches the input stays sharp from a 4K display; and if a live capture fails — a minimized
window whose surface has gone blank, a camera that hands out no frame — the snapshot answers
with the **last frame captured** for that source, labelled with its age. Restoring a minimized window while its crop
view is open upgrades the feed to a fresh full-resolution capture on the next poll — waiting
out the restore animation and matching the capture to the window's live frame bounds, so a
poll that lands mid-restore keeps the frozen frame instead of adopting a clipped one — no
Retake needed.

## Security & Privacy

This plugin captures screen content and enumerates window titles. Key facts:

- **The plugin's routes are served by the Hermes dashboard** at
  `/api/plugins/peripheral-vision/*`, so they are reachable wherever that dashboard is
  reachable. Access control is inherited from the dashboard's own auth and binding: if the
  dashboard is reachable at an address, these routes are reachable there too.
- **Content is gated on the pane's selection.** `/preview`, `/start`, `/snap/start`,
  `/snap/frame` and `/snap` — and the description text inside `/status` — refuse unless the
  plugin pane holds a selected source, re-published every 10 seconds with a 45-second TTL, so
  a closed or crashed pane closes the gate by itself. A dashboard session that never opened
  the pane gets a `selection_required` refusal and an empty ring buffer, never your screen.
  Window titles stay in the source lists (source metadata, the same class of thing a task
  manager shows); stop, interval, inject-mode and vision settings stay reachable so a watch
  can always be torn down — only pixel-derived content is gated.
- **Treat a running watch as screen-sharing** — while a source is selected in the pane,
  whatever is in the watched region is readable through the preview and status routes by
  anything that can reach the dashboard, so do not expose the dashboard to an untrusted
  network while watching.
- **Camera access is opt-in** — a camera is opened only when explicitly selected, and the
  device is released on stop so its LED does not stay lit.
- **Descriptions persist on disk** — `log.jsonl` keeps recent descriptions in the plugin's state
  directory, `$HERMES_HOME/cache/peripheral-vision/` (default `~/.hermes/cache/peripheral-vision/`,
  where `status.json`, `vision_model.json`, `inject_mode` and `stop_request` also live). Delete `log.jsonl` to
  clear that history.
- **An injected description also becomes part of the session transcript.** The block rides the
  turn's user message, and the host stores the exact bytes sent to the model in the session
  database (the `api_content` sidecar, kept so a replay matches what the model saw). Deleting
  `log.jsonl` clears the plugin's own log; it does not remove the copies already recorded in your
  sessions.
- **Frames leave this machine.** Each changed frame is encoded and sent to whichever
  vision-capable model the live session is running, with the built-in prompt. On a cloud
  provider that is a third-party API. Nothing is captured or sent while no source is selected,
  and stopping the watch ends it immediately.
- **Every described frame is a model call** — billed and rate-limited like any other call on
  that model. A busy screen at a short interval can mean hundreds of calls an hour; raise the
  interval or pin a cheaper vision model if that matters to you.
- **A watching turn can cost more tokens.** While the watch is running and the newest
  description is under 90 seconds old, up to ~1200 characters are eligible for injection — and
  with the default `on_change` mode that happens once per changed reading, not once per turn.

## Language of descriptions

Frames are sent to the session's vision-capable model with a prompt asking for a concise
**English** description. That is a prompt instruction, not a filter: the pipeline stores and
injects whatever the model returns, including non-English text
(`tests/test_vision_pipeline.py::test_vision_description_passes_through_model_output` asserts
exactly that pass-through). If descriptions arrive in another language, the model or the prompt
(`PV_VISION_PROMPT`) is the thing to change — the plugin will not silently drop or translate a
response.

## Development

A Hermes install is not needed to work on the plugin — the suite is hermetic (no desktop session,
no camera, no model, no network).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"       # Windows: .venv/Scripts/pip install -e ".[dev]"

.venv/bin/python -m pytest tests/ -q    # Windows: .venv/Scripts/python.exe -m pytest tests/ -q
ruff check .                            # the lint gate CI runs
```

CI (`.github/workflows/tests.yml`) runs those same two commands: the test suite on Linux with
Python 3.9/3.11/3.13 and on Windows with 3.11, plus the ruff lint job. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the file layout, the test seams, and the two scope rules
(plugin-only, dependency-light).

## Security

A running watch is screen sharing. Read [SECURITY.md](SECURITY.md) before exposing the Hermes
dashboard to any network you do not trust, and report anything that could expose captured content
through a [private security advisory](https://github.com/MaJorDan383/peripheral-vision/security/advisories/new)
rather than a public issue.

## License

MIT — see [LICENSE](LICENSE).

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Each version is published as a
[GitHub release](https://github.com/MaJorDan383/peripheral-vision/releases).
