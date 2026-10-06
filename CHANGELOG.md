# Changelog

All notable changes to this plugin. Nothing before the first public release was published, so
`1.0.0` covers the whole plugin as shipped.

## [Unreleased]

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

### Fixed
- **Two undefined names in the snapshot seams.** `plugin_api._thumb_capture` called
  `capture_grab_window_thumbnail`, and `plugin_api._window_exe` called `capture_window_exe`;
  neither name was bound in that module, so a minimized-window thumbnail grab on the live path
  would have raised `NameError`. Both are now imported under their `capture_*` names, so the
  seams resolve at runtime while the tests keep monkeypatching them.
- **Stale capture-backend leftovers.** An unused PipeWire session/stream pair in the Linux
  backend (the fast path was never implemented — `cleanup()` now says what it actually holds,
  which is nothing), a no-op `SetWindowPos` and a dead DC binding in the PrintWindow grab, and
  unused imports and locals across both backends.

### Changed
- **Lint gate added.** `ruff` (`F`, `E9`, `I` only) now runs in CI and is documented in
  [CONTRIBUTING.md](CONTRIBUTING.md); the test matrix also runs on Windows, not just Linux.
- **Five unreferenced private helpers removed** from the capture backends (a PipeWire probe
  along with four Windows window-geometry helpers). Verified unreferenced across the plugin,
  the tests and the pane before removal; the suite is unchanged at 132 passed, 1 skipped.
- **Documentation set.** `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue forms, a
  pull-request template and a README badge row, alongside corrections where the docs had drifted
  from the code (the Windows capture primitives table, the install path, and the Linux
  dependency list).

### Removed
- The declared `linux` extra (`pipewire-capture`), which no code imports. The PipeWire ScreenCast
  fast path is still tracked in [#1](https://github.com/MaJorDan383/peripheral-vision/issues/1).

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
