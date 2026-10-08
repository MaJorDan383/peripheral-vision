# macOS capture feasibility (a third backend)

Status: **built**, 2026-10-07, shipped in **v1.5.0** as `dashboard/capture_macos.py` with the
`darwin` branch in `capture.py`'s `_backend()` and `platforms: ["windows", "linux", "macos"]` in
`plugin.yaml`. The doc below is kept as the design record: what it would cost, what it buys, and
the two places where macOS cannot match Windows. What shipped differs from the plan in one
respect — the `screencapture` tier is a *display and camera* fallback rather than a peer tier,
because only CoreGraphics can enumerate windows. Verification level: unit-tested only (no GUI
session can grant Screen Recording on a CI runner or on this host).

## Verdict

- **Yes, macOS is reachable**, and the shape of the work is already decided by the existing
  design: one more file at the same seam (`dashboard/capture_macos.py`) plus a `darwin` branch
  in `capture.py`'s `_backend()`. Everything above the OS layer — source model, crop overlay,
  snapshot sessions, payloads, pane JS, last-frame cache, PNG save, camera, injection — is
  portable and needs no change.
- **macOS is a supported Hermes host**, not a new frontier: Hermes ships a macOS gateway
  (LaunchAgent) backend and macOS Desktop builds, and its own computer-use tooling already
  captures macOS screens through ScreenCaptureKit with TCC handling. The plugin is the thing
  opting out, via one manifest line.
- **The cost is a permission, not an API.** Screen Recording (TCC) gates window titles and
  every pixel grab; without it `screencapture` writes a black image and reports success. That
  is a user-visible one-time grant on the *host* process, and it maps onto the reason-string
  pattern the plugin already uses on Wayland rather than onto new architecture.
- **One honest capability gap:** minimized windows have no macOS counterpart to
  `DwmRegisterThumbnail`. There is no composited surface to thumbnail, so "last frame of a
  minimized window" falls back to the plugin's own last-frame cache — the same fallback
  Wayland already ships.

## What the host already supports

| Fact | Where |
|------|-------|
| `platforms:` accepts `windows` / `macos` / `linux`; `macos` normalizes to `darwin` | `hermes_cli/plugins_cmd_catalog.py`: `_PLATFORM_ALIASES`, `normalized_platforms()` |
| A plugin whose `platforms` excludes the host's `os_family()` is refused at catalog install | `hermes_cli/plugins_cmd_catalog.py`: `_refuse_unsupported_catalog_platform()` |
| `darwin` is a first-class runtime platform | `tools/computer_use/permissions.py`: `_RUNTIME_PLATFORMS = {"darwin", "win32", "linux"}` |
| macOS screen capture + TCC is already solved in-tree (ScreenCaptureKit, `tccutil` service names, "0 shareable displays" diagnosis) | `tools/computer_use/cua_backend.py`, `doctor.py`, `permissions.py` |
| macOS Desktop + gateway builds ship | `apps/desktop/package.json` (`dist:mac:dmg`, `dist:mac:zip`), `electron-builder.config.cjs` (`mac`), `hermes_cli/gateway_launchd.py` |

So the declaration is the gate and nothing else: `platforms: ["windows", "linux", "macos"]`
is the first line of the port, and it is a one-word change.

## The seam a backend must satisfy

`dashboard/capture.py` (236 lines) selects a backend once at import and re-exports a flat
module-level API. Of that API, **7 functions are unconditional** — a backend that omits any of
them fails at call time; **14 more are `hasattr`-guarded** and simply degrade:

| Required (7) | Purpose |
|--------------|---------|
| `list_monitors` / `list_windows` / `list_cameras` / `list_sources` | source enumeration |
| `grab` | one still, as a PIL image |
| `is_minimized` / `get_window_rect` | state + geometry for the settle/upgrade logic |

| Optional (14) | Effect when absent |
|---------------|--------------------|
| `grab_window_thumbnail`, `_window_iconic`, `_window_rested`, `_window_rect`, `_window_exe` | thumbnail/minimized handling degrades; snap upgrade falls back |
| `supports_snap_full_res`, `cleanup`, `release_camera`, `camera_open` (`_CAM_HANDLE`) | full-res stills off; camera LED release and process cleanup skipped |
| `probe_cameras_now`, `cameras_probing`, `abort_camera_probe`, `_camera_source`, `_CAMERA_CACHE` | camera probing becomes a plain cached enumeration |

The two shipped backends — `capture_windows.py` (1,229 lines), `capture_linux.py` (1,261) —
are that size because of per-OS detail, not shared logic; the portable layer is
`plugin_api.py` (2,474). A macOS backend is a peer of those two files, not a rewrite.

## Windows-only surface → macOS equivalents

| # | Windows primitive | macOS equivalent | Verdict |
|---|-------------------|------------------|---------|
| 1 | EnumDisplayMonitors / GetMonitorInfoW | `CGGetActiveDisplayList` + `CGDisplayBounds` (`Quartz`, via pyobjc), or `system_profiler SPDisplaysDataType -json` | parity |
| 2 | EnumWindows + IsIconic | `CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly\|ExcludeDesktopElements)` → `kCGWindowNumber`, `kCGWindowOwnerName`, `kCGWindowName`, `kCGWindowBounds`, `kCGWindowLayer` | parity, with a permission caveat (below) |
| 3 | DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) / GetWindowRect / GetWindowPlacement | `kCGWindowBounds` (+ `kCGWindowIsOnscreen`) | parity |
| 4 | PrintWindow(PW_RENDERFULLCONTENT) / GetDIBits — capture, including occluded windows | `CGWindowListCreateImage(…, kCGWindowListOptionIncludingWindow, wid, kCGWindowImageBoundsIgnoreFraming\|kCGWindowImageBestResolution)`, or ScreenCaptureKit's `SCScreenshotManager` | parity; the legacy call is deprecated from macOS 14 and SCK is the forward path |
| 5 | DwmRegisterThumbnail — minimized-window last frame | **none** — a minimized window has no composited surface and is not shareable content | gap; use the plugin's existing last-frame cache |
| 6 | OpenCV VideoCapture (DirectShow) | `cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)` — OpenCV is already a dependency | near-free |
| 7 | Camera device names (WMI/SetupAPI) | `system_profiler SPCameraDataType -json`, or `AVCaptureDevice` via pyobjc | parity |

Two reachable implementations of the whole tier:

- **`screencapture` CLI + `system_profiler`** — zero Python dependencies, monitors and regions
  by flag (`-x` silent, `-D n` display, `-R x,y,w,h` region, `-l wid` window single-window).
  Window *discovery* still needs `CGWindowListCopyWindowInfo` for a window id, and without
  titles there is no picker worth showing.
- **pyobjc** (`pyobjc-framework-Quartz`, and `pyobjc-framework-ScreenCaptureKit` for the modern
  path) — the same call the Windows tier makes, one language. Declare it behind an environment
  marker (`sys_platform == "darwin"`) in `pyproject.toml` so Windows/Linux installs are
  untouched, matching how the plugin's dependencies are installed today.

Recommended: pyobjc for enumeration + `CGWindowListCreateImage`/SCK for grabs, CLI only as the
first-run/permission fallback — the CLI alone cannot enumerate windows or titles.

## Where macOS is genuinely harder

- **Screen Recording (TCC).** Window titles (`kCGWindowName`) come back redacted and pixel
  grabs come back black until the *host process* (Hermes app/backend) holds the grant — no
  error, no exception. It is one user-visible grant, `tccutil reset ScreenCapture` re-prompts,
  and the plugin must surface the reason the way `capture_pipewire` surfaces a missing
  `gstreamer1.0-pipewire`: a probe that names the state and keeps the screenshot tier usable.
- **ScreenCaptureKit churn.** `CGWindowListCreateImage` is deprecated from macOS 14, loads
  ReplayKit as a side effect (and so arms the capture indicator); macOS 15+ adds periodic
  consent re-authorization when SCK is used without the system's own sharing picker; 26.4.x
  has a documented SCK failure on physical Macs. A macOS backend should therefore be written
  as **two grab strategies behind one seam** from day one, exactly as the Linux tier already
  is (ScreenCast → screenshot → grim).
- **Retina.** `BestResolution` returns 2× physical pixels where the Windows tier returns
  logical ones. "Native resolution" needs an explicit policy plus a scale factor per display,
  or crops come back half-size in the UI.
- **Display geometry.** macOS uses a top-left global origin with per-display maximum Y and
  negative origins for displays left of/above the primary — the same shape the Windows tier
  already handles, but it is where a naive port breaks first.
- **Minimized windows** (above): the one feature that cannot be matched, only substituted.

## What a macOS backend changes in this repo

As built in v1.5.0: every row below landed except the CI leg, which needs a token with the
`workflow` scope (the release token here has `gist, read:org, repo`) or a PR from the owner. The
one design change from the plan: the `screencapture` tier covers displays and cameras only —
window enumeration exists solely through CoreGraphics, so without pyobjc the window list is empty
and says why.

| File | Change |
|------|--------|
| `plugin.yaml` | `platforms: ["windows", "linux", "macos"]` |
| `pyproject.toml` | `pyobjc-framework-Quartz` (+ optionally `-ScreenCaptureKit`) behind `sys_platform == "darwin"`; `Operating System :: MacOS :: MacOS X` classifier |
| `dashboard/capture.py` | `darwin` branch in `_backend()`; module docstring |
| `dashboard/capture_macos.py` | new — the backend |
| `tests/test_macos_schema.py` | new — the schema/seam suite that mirrors the Linux one, with Quartz stubbed so it runs headless |
| `.github/workflows/tests.yml` | a `macos-latest` leg: install pyobjc, import the backend, run the seam tests |
| `README.md`, `.github/ISSUE_TEMPLATE`, `docs/cross-platform-capture.md` | platform table, install caveats, permission note, forward link |

## Effort and the verification ceiling

≈600–900 lines of backend plus ≈200 lines of tests and the CI leg for a first cut that covers
displays, windows (including occluded grabs), cameras, geometry, and the permission probe.
ScreenCaptureKit as the primary path, Retina policy, multi-display negative-origin handling,
and shadow trimming are follow-on polish. It shipped as a new minor version (1.5.0): the backend
came in at ~1,000 lines and the suite at ~780 lines / 65 tests.

**The ceiling:** every pure part — geometry, coordinate mapping, source modelling, command
construction, response parsing, and the stubbed Quartz seams — is unit-testable on any OS, and
a `macos-latest` CI runner proves the module imports and behaves under real pyobjc. What no CI
runner can prove is that a live window grab returns pixels on a real Mac: GitHub's macOS
runners have no GUI session, so **the first live verification has to happen on someone's
Mac**, exactly as the PipeWire tier is waiting on a real Wayland session today.

## Sources

- TCC and window titles: `CGWindowListCopyWindowInfo` returns redacted `kCGWindowName` without
  Screen Recording permission (Catalina and later), and `screencapture` writes black
  silently — ryanthomson.net/articles/screen-recording-permissions-catalina-mess;
  stackoverflow.com/questions/59337022; lazyscreenshots.com/blog/mac-terminal-screenshot-commands
- `CGWindowListCreateImage` deprecated in favour of SCK's `SCScreenshotManager`; ReplayKit
  side effect and capture indicator on macOS 14+: nonstrict.eu/blog/2023/a-look-at-screencapturekit-on-macos-sonoma;
  chromium.googlesource.com (ui/base/cocoa/permissions_utils.mm)
- macOS 15+ consent re-authorization for SCK without `SCContentSharingPicker`, and the 26.4.x
  SCK failure on physical Macs: docs.crabfleet.ai/screen-recording-indicator.html;
  cua.ai/docs/cua-driver/guide/getting-started/faq
- Quartz/ScreenCaptureKit Python bindings: pypi.org/project/pyobjc-framework-Quartz;
  pypi.org/project/pyobjc-framework-ScreenCaptureKit
- Hermes host capability: hermes_cli/plugins_cmd_catalog.py, tools/computer_use/permissions.py,
  hermes_cli/gateway_launchd.py, apps/desktop/package.json
