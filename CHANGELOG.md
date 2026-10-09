# Changelog

All notable changes to this plugin. Nothing before the first public release was published, so
`1.0.0` covers the whole plugin as shipped.

## [Unreleased]

### Changed
- **The out-of-the-box defaults are `on_mention` and a 5-second blink rate** (were `on_change`
  and 2 s). Nothing anyone has already picked moves: a pane pick and `PV_VISION_INJECT_MODE`
  still outrank these, so the new values only decide what a fresh install — or a cleared state
  directory — starts from. `on_mention` keeps a watching plugin off turns that never asked for
  eyes, and a 5 s blink rate checks a screen 12 times a minute instead of 30.

### Fixed
- **Descriptions survive an auxiliary vision model that cannot serve.** A pick whose key,
  permission or endpoint refuses images (`400/401/403/404/405/422`, or a refused connection) is
  set aside for five minutes and the frame is described by the active session model instead: the
  pick is a preference, not a hard requirement, and a user who fixes it recovers without a
  restart. `429`, `5xx` and timeouts stay transient and keep their retry-then-report path. The
  pane's status now names the model that actually read the screen, not the one that was asked
  first.
- **The "nothing can see" error says what to do.** It names the model that blocks the user, the
  auxiliary pick that forced the question (with its HTTP status — never an upstream body), and
  the way to resume: switch this chat to a vision-capable model, or pin one for descriptions only.

## [1.5.0] - 2026-10-07

### Added
- **macOS capture.** `dashboard/capture_macos.py` is a third backend behind the same seams:
  displays and per-window pixels through CoreGraphics (`CGWindowListCopyWindowInfo`,
  `CGDisplayCreateImage`, `CGWindowListCreateImage` — the window's *own* surface, so an
  occluded window still comes back whole), cameras through AVFoundation, and a
  `screencapture`/`system_profiler` tier for a host without pyobjc. `platforms` in
  `plugin.yaml` now lists `macos`; `capture.py` routes `darwin` there instead of falling
  back to the X11 paths.
- **The Screen Recording grant is reported, not guessed.** Until the host process holds it,
  macOS hands back black frames *with no error* and hides window titles. `GET /sources`
  gains the same `capture` block Linux has — platform, tier, live grab state, and
  `permission.screen_recording: granted|denied|unknown` with the System Settings route — the
  pane's status line names the reason, and the system prompt is requested once, from a
  capture attempt only, so enumerating sources can never pop a dialog.
- **`pyobjc-framework-Quartz` is declared for macOS only** (`sys_platform == 'darwin'`), so
  the dependency reaches a Mac and no other platform. A host that ends up without it still
  captures displays and cameras, and `/sources` says why the window list is empty.
- **65 tests** (`tests/test_macos_schema.py`) drive the backend from fakes shaped like real
  Quartz returns, on every OS: the source schema, geometry, tier selection, the permission
  states, the one-shot prompt, camera probing and abandonment, and the failure paths that
  must degrade instead of raising.

### Changed
- `docs/macos-feasibility.md` keeps its assessment and gains its outcome; the README platform
  table moves macOS from ❌ to 🧪 preview, and `docs/cross-platform-capture.md` carries the
  macOS tier map.

### Notes
- **macOS is unit-tested only** — no CI runner and no host here can grant Screen Recording in
  a GUI session, so "does a live window grab return pixels on a Mac" is unverified, the same
  caveat the PipeWire tier carries. There is also no macOS peer for `DwmRegisterThumbnail`:
  a minimized window has no live frame, and the plugin's last-frame cache serves it with the
  reason attached.

## [1.4.0] - 2026-10-07

### Security
- **Screen content is gated on the pane's selection.** `/preview`, `/start`, `/snap/start`,
  `/snap/frame` and `/snap` — plus the descriptions inside `/status` — now refuse until the
  plugin pane has published a selected source over the new `POST /select`, re-verified by a
  10-second heartbeat with a 45-second TTL (a closed or crashed pane closes the gate itself,
  and publishing requires the dashboard's own session). A dashboard caller with a valid
  session but no open pane — a token console, another tab, a local script — can no longer
  pull live frames, open a watch, or read the description ring buffer while the pane isn't
  showing anything. Stop, sources, interval, inject-mode and vision stay open: metadata and
  teardown must never be gateable.

### Added
- **An auxiliary vision model is honored.** When one is picked in Hermes' own settings
  (`auxiliary.vision` in `config.yaml`), the watch's live descriptions route to it — the same
  rule Hermes applies to images attached to a turn, and it holds even when the session model
  could see. With no pick, routing is unchanged: the session model while it can see, then a
  pane-pinned model for text-only sessions. The pane's route line says `(auxiliary vision)` so
  what describes frames is never a guess.
- **The vision box names the watched source.** A small title line under the model line shows
  what the watch is looking at right now — display, window, or camera.
- **A pre-commit hook runs the same lint gate as CI.** `.pre-commit-config.yaml` pins the ruff
  version CI pins; `uvx pre-commit install` once per clone, then a commit that clears the hook
  clears the lint job. The rule families and their rationale stay in `pyproject.toml`.
- **Wayland capture got a fast path: the portal's PipeWire ScreenCast.** `capture_pipewire.py`
  drives CreateSession → SelectSources → Start → OpenPipeWireRemote and hands the portal-issued
  fd to a `gst-launch-1.0 pipewiresrc fd= path=` pipeline, so a frame is one file read instead of
  a full-screen screenshot round trip — and a *window* watch finally gets the window's own pixels
  rather than a crop of the region it covers. Consent is one-time (`persist_mode`), a stream that
  is already running is reused without a dialog, and every probe failure leaves the screenshot
  tier untouched, so this is additive on Linux and inert everywhere else. Needs
  `gstreamer1.0-pipewire`, `gstreamer1.0-tools` and `python3-gi`; see
  [Faster Wayland capture](README.md#faster-wayland-capture-optional). *Unit-tested only — the
  portal handshake, the pipeline it builds and every fallback are pinned by tests, but no live
  Wayland session was available to run it against hardware.*

### Changed
- **The pane's controls regrouped.** "check for changes" is now **Blink rate**; the
  **"Rides your turns"** picker sits above it; and while Blink rate is manual, the snapshot
  buttons sit beside the picker on that same line, labelled **Snapshot** (camera
  `device-camera`, crop `screen-cut` — glyphs the desktop icon font ships).
- **Manual snapshots crop the full-resolution still.** A display or window snapshot session
  captures once, at the source's native resolution; every frame the overlay shows is that same
  capture replayed, so the saved crop is exactly the region you dragged, at full detail — and a
  new **Retake** button re-captures in place. Display sessions open in a wide dialog: the still
  is the whole screen. Camera snapshots stay live, unchanged.
- **Snapshot captures hold their resolution and their fallback.** The crop view's feed renders
  at up to 1920 px wide (the dialog never upscales it on a 4K screen); a live capture that fails
  — a minimized window whose DWM surface came back blank, a camera that hands out no frame —
  serves the **last frame captured** for that source — the picker rows, the crop
  feed, and the saved snap alike — labelled with its age; a grab that stalls
  behind a window that stopped pumping its queue times out with an answer instead of holding the
  pane, and minimized-window thumbnail captures run on the one thread that owns their helper
  window, so one stalled capture can never hold back the next. A crop view opened on a window
  that is minimized upgrades itself once that window is restored and settled — the upgrade
  waits out the restore animation and matches the capture to the window's live frame bounds,
  so a poll that lands mid-restore can never adopt a clipped band — and the frozen DWM frame
  sharpens into a live full-resolution capture, no Retake.
- **Lint gate added.** `ruff` (`F`, `E9`, `I` only) now runs in CI and is documented in
  [CONTRIBUTING.md](CONTRIBUTING.md); the test matrix also runs on Windows, not just Linux.
- **Five unreferenced private helpers removed** from the capture backends (a PipeWire probe
  along with four Windows window-geometry helpers). Verified unreferenced across the plugin,
  the tests and the pane before removal; the suite's pass count was the same before and after
  (132 passed, 1 skipped).
- **Documentation set.** `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue forms, a
  pull-request template and a README badge row, alongside corrections where the docs had drifted
  from the code (the Windows capture primitives table, the install path, and the Linux
  dependency list).

### Fixed
- **Two undefined names in the snapshot seams.** `plugin_api._thumb_capture` called
  `capture_grab_window_thumbnail`, and `plugin_api._window_exe` called `capture_window_exe`;
  neither name was bound in that module, so a minimized-window thumbnail grab on the live path
  would have raised `NameError`. Both are now imported under their `capture_*` names, so the
  seams resolve at runtime while the tests keep monkeypatching them.
- **Stale capture-backend leftovers.** A no-op `SetWindowPos` and a dead DC binding in the
  PrintWindow grab, unused imports and locals across both backends, and a session/stream pair the
  Linux backend declared but never used — the ScreenCast tier above owns that job now, with a
  lifecycle `cleanup()` really does shut down.

### Removed
- The declared `linux` extra (`pipewire-capture`), which no code imports. The capture path it
  named ships in this plugin now (see **Added**), driving the portal and `gst-launch-1.0`
  directly rather than through a Python dependency.

## [1.3.0] - 2026-09-23

### Added
- **Manual snapshots.** "check for changes" gains a **manual** option: the watch then checks
  nothing on its own, and a **snapshot** row appears with two buttons — a camera, and a
  cropping rectangle. Each button opens the matching source list (cameras; displays and
  application windows), then a live view where you drag out the area you want. The capture is
  cropped at the source's full resolution, saved as a PNG under the plugin's state directory
  (`snaps/`), and staged straight into the chat **message input**, ready to send — without a
  drag, the whole frame is taken. Switching back to a checking rhythm hides the row again, and
  either way the pick is applied to a running watch without a restart.

## [1.2.1] - 2026-09-23

### Fixed
- The statusbar chip label is title-cased — `Vision`, `Vision · N`, `Vision · waiting`,
  `Vision · source gone` (it read `vision …`). The source/model toasts read
  `Peripheral Vision → …`, matching the name everywhere else.

## [1.2.0] - 2026-09-23

### Added
- **The inject mode is pickable in the pane.** A "rides your turns" select below the interval
  picker chooses when fresh readings are shared — on change (default), every turn, on mention,
  or never — and applies to the next turn in every session, no restart. The pick is persisted
  in the state directory as `inject_mode` (plain one-line text, written atomically) and read
  by the injection hook on every turn. Precedence: the pane's pick, then
  `PV_VISION_INJECT_MODE`, then the built-in default — and the select's tooltip names which of
  the three is in effect, so what the pane shows is what the hook will read. On a backend too
  old to serve the route the row stays hidden rather than rendering a control that cannot work.
- `POST /inject_mode` — validates the pick (case/hyphen-insensitive; an unknown value is
  refused rather than stored, an empty one clears the pick back to the environment/default);
  `GET /status` now carries `inject_mode` (the mode plus its source).
- Tests: the pane-pick precedence in `test_injection_modes.py` (the file beats the
  environment, hyphenated values, a broken file falling back, clearing, and the hook honoring
  the pick end to end), and `test_inject_mode_api.py` (the route's validation and round-trip,
  the cross-half handoff — the route writes exactly what the hook reads — and a guard that the
  constants the two halves share by duplication cannot drift apart).

### Changed
- `_write_json`'s atomic swap is now `_write_text_atomic`, shared with the new pick file (same
  pid + random temp name, same brief retry when Windows readers hold the target).

## [1.1.0] - 2026-09-23

### Added
- **A vision-mode picker in the pane, beside the source button.** The source picker now
  shows one kind at a time — Displays, Windows or Cameras — and the new select next to
  "Choose source…" decides which kind it lists. While a watch is up the select mirrors the
  kind being watched (read from the backend, with a fallback for older backends); picking a
  source re-syncs it to the watch that starts.

## [1.0.2] - 2026-09-23

### Changed
- **Renamed to Peripheral Vision** - the name now says how it watches: at the edge, in the
  background, continuously. The plugin key is `peripheral-vision` (was `continuous-vision`),
  the repository moved to https://github.com/MaJorDan383/peripheral-vision (old links
  redirect), and the environment variables use the `PV_` prefix - `PV_VISION_INJECT_MODE`,
  `PV_CAMERA_MAX_INDEX`, and so on (previously `CV_`).
- The state directory is now `$HERMES_HOME/cache/peripheral-vision/`. Existing installs:
  enable the plugin under its new key (`hermes plugins enable peripheral-vision`) and copy
  anything to keep - a pinned `vision_model.json`, `log.jsonl` - from the old
  `cache/continuous-vision/` directory.

## [1.0.1] - 2026-09-23

### Changed
- The manifest now asks for **Hermes >=0.20.1** — the oldest host that has everything the plugin
  calls (`ctx.on_unload` and the `pre_llm_call` hook both land in it; 0.20.0 has no `on_unload`,
  so its unload path would leak the capture loop). The loader only understands this key from
  0.21.2 on; older hosts warn and ignore it rather than refusing. OpenCode Zen/Go as the vision
  model still needs 0.21.4+, where `agent.opencode_affinity` (the session-affinity headers those
  endpoints require) appears.
- The pip dependencies are declared with upper bounds — `opencv-python-headless>=4.5,<6`,
  `Pillow>=10.0,<13`, `openai>=1.0.0,<3` in `pyproject.toml`. Hermes resolves a plugin's
  declarations together with its own ranges when installing or enabling it, so a major release
  of either library can no longer make that resolution fail at install time.

## [1.0.0] - 2026-09-23

### Added
- Monitor, Window and Camera frame capture for Windows (DWM / Win32 window / DirectShow).
- Live desktop pane: preview plus monitor, window and camera pickers, vision-model candidates,
  and pin support.
- Intelligent vision model routing; pinning a model writes `vision_model.json` into the state
  directory, and an empty pin restores automatic routing.
- Pre-LLM context injection: fresh descriptions ride the turn through the `pre_llm_call` hook.
- **`CV_VISION_INJECT_MODE`** — `on_change` (default), `always`, `on_mention`, `tool_only`.
  Injection was previously hardcoded to "inject whenever the loop is running and the reading is
  fresh", so an unchanged screen was re-sent on every turn for the life of a session and every
  copy was persisted with its turn. `on_change` compares the *descriptions* against what that
  session was last sent (not the rendered block — its header carries the frame age, which differs
  every turn), and re-sends unchanged content after 10 minutes so a static screen is still
  re-anchored. `on_mention` waits for the user's own turn to point at the screen. Invalid values
  log a warning once and fall back to the default. `tool_only` is reserved for builds that expose
  a live-view tool: this plugin registers none, so it injects nothing and logs a warning rather
  than looking like a silent failure.
- Tests for the unload path, the injection gates, the injection modes, the vision pipeline and
  the status heartbeat (`test_unload_stop.py`, `test_context_injection.py`,
  `test_injection_modes.py`, `test_vision_pipeline.py`, `test_heartbeat_gate.py`,
  `test_status_heartbeat.py`), and a GitHub Actions workflow that runs them on every push.

### Changed
- **The default injection cadence is `on_change`, not "every turn."** Users who want the old
  behaviour set `CV_VISION_INJECT_MODE=always`.
- **The plugin is opt-in.** It captures screen content, so nothing happens until its key is in
  `plugins.enabled`.
- Frame diffing runs on a 64x64 fingerprint of a box-reduced copy instead of a PNG round trip,
  and the intake resize + PNG encode happen only when a frame actually changes (or the preview
  copy goes stale). Per-tick cost on a 4K display: ~290 ms -> ~115 ms of CPU.
- `/preview` serves JPEG (~3x smaller, much faster to encode) and reuses the encoded preview for
  an unchanged frame, so the pane's 6 s poll costs ~2 ms instead of ~30 ms.
- `/preview?hwnd=` resizes inside the capture thread at the requested width instead of encoding a
  full-size PNG and shrinking it on the way out (~4x faster per picker row).
- `desktop/plugin.js`: hardcoded `#e5484d` fallbacks replaced by the app's `--ui-danger` token.
- The "English verification" claim is gone. The pipeline asks for English in the prompt and
  passes through whatever the model returns — including non-English — so the README states that
  contract instead of claiming a gate that does not exist.
- The privacy section discloses what it never said: frames go to the session's model (a
  third-party API on a cloud provider), every described frame is a billable call, an injected
  block becomes part of the session transcript (`api_content` sidecar), and a watching turn can
  carry up to ~1200 characters.

### Fixed
- **Unloading the plugin never stopped the watch.** The agent half registered `plugin_unload` as
  a hook, but that name is not in the host's hook registry, so the callback was never dispatched —
  `hermes plugins validate` failed and a disabled plugin left its capture loop (and an open
  camera) running. It now uses `ctx.on_unload()` and asks for a stop through the state directory
  the two halves already share, because the agent half runs in a different process and cannot
  stop the backend's engine directly. The loop consumes `stop_request` on its next tick, releases
  the source, and publishes a terminal `running: false` status.
- **A capture loop that crashed or was killed left `running: true` in `status.json` for good.**
  Seen live in practice: a loop died after a vision-model failure and its status file still
  claimed to be running twenty hours later, because only a *clean* stop reached the terminal
  write. Two halves, one rule: the loop now heartbeats the file on every tick (`heartbeat_at`,
  refreshed in `_publish`), the thread wrapper `_run_guarded` publishes the terminal state on
  every exit path including a crash (logging `loop_crashed` with the traceback), and the reader
  in the agent half refuses a `running` claim whose heartbeat — or, for writers predating the
  field, whose file mtime — is older than 120 s. The backend also heals such a file itself the
  next time a `/status` request wakes it, rewriting it as stopped and logging
  `stale_status_corrected`.
- `_write_json` used a fixed `<name>.tmp` path, so two writers (the engine thread plus a probe, a
  CLI call or a second start) collided and a tick died with `PermissionError`. The temp name now
  carries pid + a random suffix, and the final swap retries briefly for readers holding the
  target — 8 threads x 25 writes: 100 collisions before, 0 after.
- **The config table was incomplete.** `CV_PREVIEW_MAX_AGE_S` (the preview-refresh cadence) and
  `CV_VISION_MAX_WIDTH` had no row, and the intake precedence is now spelled out as
  `_vision_intake()` applies it — `CV_VISION_MAX_EDGE` (older alias `CV_VISION_MAX_WIDTH`), with
  `CV_VISION_MAX_PIXELS` taking precedence over the edge caps. Every variable the code reads is in
  the table, and the `CV_*` variables are described as read from the backend process environment
  (not `config.yaml`).
- `pyproject.toml` shipped an invented author email and three dead repository URLs.
- `plugin.yaml` declared a `repository` key that is not in the manifest schema. Unknown fields are
  warned about and ignored, so every load logged an unknown-field warning and the real repository
  link was never surfaced — the key is `homepage`.
- **The README never said how to enable the plugin, and it misstated the install path.** Standalone
  plugins load only when their key is in `plugins.enabled`; the install steps now cover that, and
  the claim that `hermes plugins install ...` "enables it for you" was wrong — the command asks
  "Enable now? [y/N]" and defaults to no, so the docs now pass `--enable` explicitly.
