# Cross-platform capture feasibility (Windows + Linux in one plugin)

Status: findings, 2026-09-23. Today every grab is Windows-only; this documents what a Linux
tier would take, what survives, and what does not.

## Verdict

- **No single capture library delivers parity on both OSes.** mss / Pillow / pyscreenshot are
  monitors-only on Linux, X11-only (broken on Wayland), and cannot grab a single window.
- **The plugin as a whole can work on both** — and on Windows nothing needs to change. The
  OS-specific surface is six small primitives; everything above them (source model, crop
  overlay, snapshot session, payloads, pane JS, last-frame cache, PNG save, camera) is already
  portable. The cross-OS layer is the *interface* (a still-frame contract), not one library.
- On Linux the display server decides the ceiling:
  - **X11: near-parity.** Window enumeration (EWMH), per-window grabs (XComposite), region
    grabs (XGetImage / XSHM) — ~5-15 ms per grab, native resolution, stable.
  - **Wayland: parity minus two functions.** Capture is only possible through
    xdg-desktop-portal ScreenCast → PipeWire; the compositor shows the source picker, apps can
    never enumerate windows/titles, and the first consent needs a dialog (persistable via
    restore tokens). Steady-state 30-60 ms per frame, native resolution, one code path across
    GNOME/KDE/wlroots — this is the officially supported API and the stable choice.

## The Windows-only surface today (all in dashboard/plugin_api.py)

| # | Primitive | Used for |
|---|-----------|----------|
| 1 | EnumDisplayMonitors / GetMonitorInfoW | display sources |
| 2 | EnumWindows + IsIconic | window source list, minimized flag |
| 3 | DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) / GetWindowPlacement | window geometry |
| 4 | PrintWindow(PW_RENDERFULLCONTENT) / GetDIBits | capture (occluded windows, stills) |
| 5 | WindowFromPoint + z-order probe | obstructed-window detection |
| 6 | DwmRegisterThumbnail (off-screen host + PrintWindow) | minimized-window last frame |

## Linux equivalents

| Need | X11 | Wayland |
|------|-----|---------|
| Display list | XRandR / xrandr | wl_output (compositor exposes monitors) |
| Window list + titles | EWMH `_NET_CLIENT_LIST` (python-xlib, wmctrl, pywinctl) | **Impossible** by design — the portal picker replaces it |
| Window geometry / state | XGetWindowAttributes, `_NET_FRAME_EXTENTS`, `_NET_WM_STATE_HIDDEN`, map_state | Not needed: the picked source IS the stream; no geometry bookkeeping |
| Capture occluded window | XComposite RedirectWindow + NameWindowPixmap + XGetImage (caveat: Chromium/Electron surfaces can come back mangled — same flavour as PrintWindow quirks) | **Works, even occluded** — compositor-side; portal window capture |
| Screen-region grab | XGetImage / XSHM (~5-15 ms) | Grab latest frame from the PipeWire stream (~30-60 ms) |
| Minimized last frame | none — window is unmapped | stream pauses/ freezes; the plugin's existing last-frame cache covers it |
| One-shot screenshot | XGetImage | portal Screenshot is interactive and cannot pick a window on GNOME — stills should ride the ScreenCast stream instead |
| Camera | OpenCV VideoCapture (V4L2) | same |

## Methods considered and rejected as "one library for both"

- **mss / Pillow ImageGrab / pyscreenshot**: Windows fine, Linux = X11 only (mss raises
  XDefaultRootWindow() failed on Wayland), monitors only (no window pick), no minimized-window
  story. Cannot preserve function.
- **Electron desktopCapturer / getDisplayMedia**: genuinely cross-OS, but it is a main-process
  API (would need desktop-app changes — forbidden for plugin features), returns a single
  portal-mediated source on Linux, and its model is a live video stream, not our still+crop
  session.
- **grim**: wlroots compositors only (Sway/Hyprland); not GNOME/KDE.
- **XWayland + X11 grabs**: XWayland's root window has no pixels and native Wayland windows
  are invisible to X11 clients — do not rely on it.

## Recommended architecture if this is built

A capability-tiered capture backend behind the existing seams (`_grab`, window enumeration,
grab-state probes), selected once at import time by session type
(`WAYLAND_DISPLAY` / `XDG_SESSION_TYPE` / `DISPLAY`):

1. **win tier** — today's code, untouched (zero regression on the platform in use).
2. **x11 tier** — python-xlib: EWMH window list, XComposite per-window pixmap, XGetImage/XSHM
   region grabs; minimize via `_NET_WM_STATE_HIDDEN`/map_state. Full picker UX preserved.
3. **wayland tier** — xdg-desktop-portal ScreenCast via D-Bus (jeepney/dbus-next) + PipeWire:
   `pipewire-capture` (PyPI, prebuilt wheels, window selection + BGRA frames) or a GStreamer
   `pipewiresrc` pipeline; ScreenCast restore tokens make consent one-time per source. The
   picker's "Choose source" step becomes: enumerate monitors + request a window pick; snapshot
   = latest stream frame; crop math unchanged (normalized rect).

Shared on all tiers: pane JS (Electron both OSes), still/session machinery, crop overlay at
full resolution, last-frame fallback, the settle/auto-upgrade logic (on Wayland it mostly
becomes moot — the stream simply resumes), camera, and the whole test-suite pattern of
stubbing seams (as `_window_iconic` is stubbed today) so CI runs headless on any OS.

Honest deltas on Wayland only: (a) source picker is the OS dialog, not a plugin list of window
titles; (b) minimized-window "last frame" relies on the plugin's own cache; (c) one-time
consent latency at first capture. Stability, speed, resolution, and every other function are
preserved.

## Sources

- xdg-desktop-portal ScreenCast spec + D-Bus flow, PipeWire→GStreamer pipeline, restore
  tokens: etducky.com/blog/wayland-screen-capture-portal-pipewire; flatpak.github.io/xdg-desktop-portal
- mss Wayland support request + community latency table (X11 5-15 ms, grim 10-25 ms,
  PipeWire 30-60 ms, XWayland+mss 40-80 ms): github.com/BoboTiG/python-mss/issues/155
- pipewire-capture (portal window selection, BGRA frames, prebuilt wheels):
  github.com/bquenin/pipewire-capture
- pywinctl platform notes (X11 OK; Wayland: no window list): pywinctl.readthedocs.io
- XComposite mangled Chromium surfaces:
  stackoverflow.com/questions/47980608; XWayland root has no pixels:
  mail.openjdk.org/pipermail/wakefield-dev/2021-October/000019.html
- Pillow ImageGrab (window= = Windows/macOS only; Linux = X11 + CLI fallbacks):
  pillow.readthedocs.io/en/latest/reference/ImageGrab.html
- Electron desktopCapturer caveats (single PipeWire source on Linux):
  electronjs.org/docs/api/desktop-capturer
