"""
Linux capture backend.

Implements the cross-platform capture API using:

- xrandr (X11) for monitor enumeration; the XDG desktop portal on Wayland
- X11 window ids, or the portal on Wayland, for window enumeration and capture
- a portal ScreenCast (PipeWire) stream when one can be opened: the only route
  to a window's own pixels on Wayland, and the cheapest way to keep a monitor
  warm once the user has allowed it once (see ``capture_pipewire``)
- V4L2/OpenCV for camera capture
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from shared_state import (
    CAM_HANDLE as _CAM_HANDLE,
    CAM_LOCK as _CAM_LOCK,
    GRAB_STATE as _GRAB_STATE,
)

_LOG = logging.getLogger("peripheral_vision.capture_linux")

# xdg-desktop-portal state: the first Screenshot call on a GNOME session pops a
# one-time consent dialog; once granted, later calls are silent (verified on
# GNOME 46 Wayland — the grant persists). A failure arms a cooldown so a watch
# loop cannot spam consent dialogs.
_PORTAL_STATE = {"primed": False, "fail_at": 0.0}
_HELPER_PY = {"checked": False, "path": None}


def _method(name: str) -> None:
    """Record how this thread's frame was obtained (the pane displays it)."""
    _GRAB_STATE.method = name


def _waiting(reason: str) -> None:
    """Record why this thread's frame is late or wrong (the pane displays it)."""
    _GRAB_STATE.waiting = reason


def _is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{getattr(os, 'getuid', lambda: 0)()}"


def _session_env() -> dict:
    """Environment with a session D-BUS address, even under SSH/no-systemd contexts."""
    env = dict(os.environ)
    runtime = _runtime_dir()
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(runtime) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _xenv() -> dict:
    """Environment for X11 client tools with the display cookie resolved.

    A Hermes backend or SSH session never inherits the session's auth cookie:
    mutter keeps Xwayland's in $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*, Xorg
    keeps ~/.Xauthority. Without it xrandr/wmctrl/xwininfo/xprop all fail with
    "Can't open display" — which is exactly how a Wayland desktop used to
    enumerate as zero monitors and zero windows.
    """
    env = dict(os.environ)
    if env.get("XAUTHORITY"):
        return env
    try:
        cookies = sorted(
            Path(_runtime_dir()).glob(".mutter-Xwaylandauth.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        cookies = []
    if cookies:
        env["XAUTHORITY"] = str(cookies[0])
    else:
        legacy = Path.home() / ".Xauthority"
        if legacy.exists():
            env["XAUTHORITY"] = str(legacy)
    return env


def _helper_python() -> Optional[str]:
    """A Python interpreter with PyGObject (gi) for the portal/AT-SPI helpers.

    Hermes' bundled Python ships no gi, so fall back to the system
    interpreter — every GNOME/Wayland distro ships it with the desktop itself.
    """
    if _HELPER_PY["checked"]:
        return _HELPER_PY["path"]
    _HELPER_PY["checked"] = True
    for cand in (sys.executable, "/usr/bin/python3", shutil.which("python3") or ""):
        if not cand:
            continue
        try:
            probe = subprocess.run(
                [cand, "-c", "import gi"],
                capture_output=True, timeout=10, env=_session_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            _HELPER_PY["path"] = cand
            return cand
    return None


# One xdg-desktop-portal Screenshot round trip: call the Screenshot method
# (non-interactive), await the org.freedesktop.portal.Request Response signal,
# print {"response", "uri"} as JSON.
_PORTAL_SCRIPT = """
import json, os, sys
runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", runtime)
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % runtime)
import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

timeout_ms = int(sys.argv[1])
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
state = {"response": None, "uri": None}
loop = GLib.MainLoop()

def on_response(connection, sender, path, iface, signal, params, *extra):
    if signal == "Response" and state["response"] is None:
        resp, results = params.unpack()
        state["response"] = int(resp)
        state["uri"] = results.get("uri")
        loop.quit()

try:
    reply = conn.call_sync(
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot", "Screenshot",
        GLib.Variant("(sa{sv})", ("", {"interactive": GLib.Variant("b", False)})),
        # Expected type is the abstract tuple: newer portals reply (oa{sv}),
        # older builds (Ubuntu 22.04 era) reply a bare (o) object path.
        GLib.VariantType("r"), Gio.DBusCallFlags.NONE, timeout_ms, None)
    obj_path = reply.unpack()[0]
    conn.signal_subscribe(None, "org.freedesktop.portal.Request", "Response",
                          obj_path, None, Gio.DBusSignalFlags.NONE, on_response, None)
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
except Exception as exc:
    print(json.dumps({"response": -1, "uri": None, "error": str(exc)}))
    sys.exit(0)
print(json.dumps({"response": -1 if state["response"] is None else state["response"],
                  "uri": state["uri"]}))
"""


def _portal_screenshot(timeout: float) -> Optional[str]:
    """Full-screen PNG via xdg-desktop-portal; returns its local path (caller deletes).

    This is the only non-interactive capture path GNOME/mutter offers
    unprivileged clients. The first use shows a consent dialog; allow it once
    and the grant persists for every later frame.
    """
    from urllib.parse import unquote, urlparse

    now = time.time()
    if not _PORTAL_STATE["primed"]:
        failed_at = _PORTAL_STATE["fail_at"]
        if failed_at and (now - failed_at) < 120:
            return None
    py = _helper_python()
    if not py:
        return None
    wait = timeout if not _PORTAL_STATE["primed"] else min(timeout, 10.0)
    try:
        result = subprocess.run(
            [py, "-c", _PORTAL_SCRIPT, str(int(wait * 1000))],
            capture_output=True, text=True, timeout=wait + 10, env=_session_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _PORTAL_STATE["fail_at"] = time.time()
        return None
    out_lines = (result.stdout or "").strip().splitlines()
    if not out_lines:
        _LOG.debug("portal screenshot produced no output: %s",
                   (result.stderr or "")[-300:])
        return None
    try:
        data = json.loads(out_lines[-1])
    except ValueError:
        return None
    uri = data.get("uri")
    if data.get("response") != 0 or not uri:
        if not _PORTAL_STATE["primed"]:
            _PORTAL_STATE["fail_at"] = time.time()
            err = (data.get("error") or "").strip()
            if err:
                _LOG.warning("portal screenshot failed: %s", err[:300])
            elif data.get("response") == 2:
                _LOG.warning(
                    "portal screenshot denied — click Allow on the screenshot "
                    "permission dialog to enable Wayland capture",
                )
            else:
                _LOG.warning(
                    "portal screenshot failed (response=%s)",
                    data.get("response"),
                )
        return None
    path = unquote(urlparse(uri).path) if uri.startswith("file://") else uri
    if not os.path.exists(path):
        return None
    if not _PORTAL_STATE["primed"]:
        _LOG.info("portal screenshot granted — Wayland capture is live "
                  "(the grant persists)")
    _PORTAL_STATE["primed"] = True
    return path


# Window enumeration through the accessibility bus. GNOME denies
# org.gnome.Shell.Introspect.GetWindows to every caller except its own portal
# helpers (js/misc/introspect.js allow-lists them), so AT-SPI is the only
# non-interactive window list on a GNOME Wayland desktop. Toplevel roles only;
# geometry comes from the Component interface (get_extents).
_ATSPI_SCRIPT = """
import json, os
runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", runtime)
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % runtime)
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
rows = []
desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if app is None:
        continue
    try:
        pid = int(app.get_process_id() or 0)
    except Exception:
        pid = 0
    for j in range(app.get_child_count()):
        window = app.get_child_at_index(j)
        if window is None:
            continue
        try:
            if window.get_role_name() not in ("frame", "window", "dialog", "alert"):
                continue
            rect = window.get_extents(Atspi.CoordType.SCREEN)
            title = (window.get_name() or "").strip()
        except Exception:
            continue
        rows.append({
            "pid": pid, "title": title,
            "x": int(rect.x), "y": int(rect.y),
            "width": int(rect.width), "height": int(rect.height),
        })
print(json.dumps(rows))
"""


def _windows_from_atspi(payload: str) -> list[dict]:
    """AT-SPI JSON rows -> raw window dicts (same floors as the X11 parsers)."""
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError):
        return []
    windows: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid") or 0)
            x, y = int(row.get("x") or 0), int(row.get("y") or 0)
            width = int(row.get("width") or 0)
            height = int(row.get("height") or 0)
        except (TypeError, ValueError):
            continue
        title = str(row.get("title") or "").strip()
        # Untitled toplevels are shell chrome (GNOME Shell's own overlay) and
        # the size floor drops slivers — same rules as _parse_wmctrl.
        if not title or width < 50 or height < 50:
            continue
        windows.append({"pid": pid, "title": title, "x": x, "y": y,
                        "width": width, "height": height})
    return windows


def _run_atspi(timeout: float = 15.0) -> Optional[str]:
    """AT-SPI window list as a JSON string, or None when unavailable."""
    py = _helper_python()
    if not py:
        return None
    try:
        result = subprocess.run(
            [py, "-c", _ATSPI_SCRIPT],
            capture_output=True, text=True, timeout=timeout, env=_session_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        _LOG.debug("AT-SPI window list failed: %s", (result.stderr or "")[-300:])
        return None
    return result.stdout




def _pipewire_key(source: dict) -> str:
    """A stable stream key per watched source (monitors and windows differ)."""
    kind = source.get("kind") or "monitor"
    ident = source.get("index") if kind == "monitor" else source.get("hwnd")
    return f"{kind}-{ident if ident is not None else 0}"


def _pipewire_frame(source: dict, live_only: bool = False):
    """A frame from the portal's ScreenCast stream, or None with a reason set.

    ``live_only`` never opens a session — it only reads a stream that is already
    running, so the fast path costs nothing when ScreenCast is not in use.
    """
    try:
        import capture_pipewire
    except ImportError as exc:  # pragma: no cover - the module ships with the plugin
        _LOG.debug("pipewire tier unavailable: %s", exc)
        return None
    kind = "monitor" if (source.get("kind") or "monitor") == "monitor" else "window"
    python = _helper_python()
    img = (
        capture_pipewire.live_frame(kind, _pipewire_key(source), timeout=1.0)
        if live_only
        else capture_pipewire.frame(kind, _pipewire_key(source), python, timeout=1.5)
    )
    if img is None:
        note = capture_pipewire.note() or capture_pipewire.state(python)["reason"]
        if note:
            _waiting(note)
    return img


def _capture_reason() -> str:
    """Why no live capture path exists right now, for the pane's status line."""
    notes = []
    if _is_wayland():
        try:
            import capture_pipewire
            note = capture_pipewire.note() or capture_pipewire.state(_helper_python())["reason"]
            if note:
                notes.append(note)
        except ImportError:
            pass
        if _PORTAL_STATE.get("fail_at"):
            notes.append("the screenshot portal was refused")
        if not _HELPER_PY.get("path"):
            notes.append("no python3 with PyGObject (gi) for the portal helper")
    else:
        notes.append("ImageMagick (import) is unavailable")
    return "; ".join(notes) or "no capture backend is available"


def _capture_state() -> dict:
    """What the capture tiers are doing here, for the pane's HUD."""
    state = {
        "platform": "wayland" if _is_wayland() else "x11",
        "method": getattr(_GRAB_STATE, "method", "") or "",
        "waiting": getattr(_GRAB_STATE, "waiting", "") or "",
    }
    if not _is_wayland():
        state["screencast"] = {"method": "", "state": "unsupported", "sessions": [],
                               "reason": "X11 grabs the root window directly", "note": ""}
    else:
        try:
            import capture_pipewire
            state["screencast"] = capture_pipewire.state(_helper_python())
        except ImportError as exc:  # pragma: no cover - the module ships with the plugin
            state["screencast"] = {"method": "", "state": "unsupported", "sessions": [],
                                   "reason": str(exc), "note": ""}
    return state


class LinuxCapture:
    """Linux implementation of the capture API."""
    
    def __init__(self):
        self._initialized = False
        self._camera_cache = {}
        self._camera_cache_time = 0
        self._camera_probe_lock = threading.Lock()
    
    def list_monitors(self) -> list[dict]:
        """List all displays/monitors."""
        monitors = []
        
        # xrandr — X11, and also Xwayland under a Wayland session (mutter
        # exposes the real connectors there) once the auth cookie is supplied.
        try:
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True,
                text=True,
                timeout=5,
                env=_xenv(),
            )
            if result.returncode == 0:
                monitors = self._parse_xrandr(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Pure Wayland with no Xwayland/xrandr: wl_output via wayland-info.
        if not monitors:
            try:
                result = subprocess.run(
                    ["wayland-info", "-i", "wl_output"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=_session_env(),
                )
                if result.returncode == 0:
                    monitors = self._parse_wayland_info(result.stdout)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # If every path failed, fall back to DRM connectors.
        if not monitors:
            monitors = self._list_drm_monitors()
        
        # Add synthetic primary monitor if nothing found
        if not monitors:
            monitors = [self._monitor(0, "Default", 0, 0, 1920, 1080, True)]
        
        return monitors
    
    def _parse_xrandr(self, output: str) -> list[dict]:
        """Parse xrandr --query output into the flat schema the dashboard consumes.

        Geometry rides on the connector line itself ("eDP-1 connected primary
        1920x1080+0+0 (...)"), so position and size come from one match; the
        starred mode line is only a fallback for older xrandr builds.
        """
        monitors: list[dict] = []
        lines = output.strip().split('\n')
        index = 0

        for i, line in enumerate(lines):
            if " connected" not in line or " disconnected" in line:
                continue

            parts = line.split()
            name = parts[0]
            is_primary = "primary" in parts

            x = y = 0
            width, height = 1920, 1080
            geo = re.search(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", line)
            if geo:
                width, height = int(geo.group(1)), int(geo.group(2))
                x, y = int(geo.group(3)), int(geo.group(4))
            else:
                for later in lines[i + 1:]:
                    if " connected" in later or " disconnected" in later:
                        break
                    if "*" in later:
                        try:
                            width, height = map(int, later.split()[0].split('x'))
                        except (ValueError, IndexError):
                            pass
                        break

            monitors.append(self._monitor(index, name, x, y, width, height, is_primary))
            index += 1

        return monitors

    def _parse_wayland_info(self, output: str) -> list[dict]:
        """Parse ``wayland-info -i wl_output`` into the flat monitor schema.

        One block per output introduced by ``interface: 'wl_output'``: name,
        x/y, and mode lines where only the current mode carries the ``current``
        flag. Used on a Wayland session that has no Xwayland/xrandr at all.
        """
        monitors: list[dict] = []
        for block in re.split(r"(?m)^(?=interface:)", output):
            if "interface: 'wl_output'" not in block:
                continue
            name = ""
            x = y = 0
            width = height = 0
            pending: Optional[tuple] = None
            for raw_line in block.split("\n"):
                line = raw_line.strip()
                m = re.match(r"name: (.+)$", line)
                if m and not name:
                    name = m.group(1).strip()
                    continue
                m = re.match(r"x: (-?\d+),\s*y: (-?\d+)", line)
                if m:
                    x, y = int(m.group(1)), int(m.group(2))
                    continue
                m = re.match(r"width: (\d+) px,\s*height: (\d+) px", line)
                if m:
                    pending = (int(m.group(1)), int(m.group(2)))
                    continue
                if pending and line.startswith("flags:") and "current" in line:
                    width, height = pending
            if name and width and height:
                monitors.append(self._monitor(
                    len(monitors), name, x, y, width, height,
                    primary=len(monitors) == 0,
                ))
        return monitors


    @staticmethod
    def _monitor(index: int, name: str, x: int, y: int, width: int, height: int,
                 primary: bool) -> dict:
        """One monitor dict, shaped exactly like capture_windows emits."""
        return {
            "id": f"monitor-{index}",
            "index": index,
            "device": name,
            "label": f"Display {index + 1}",
            "x": x, "y": y, "width": width, "height": height,
            "primary": bool(primary),
            "primary_label": "primary" if primary else "",
            "kind": "monitor",
        }
    
    @staticmethod
    def _drm_label(connector: str) -> str:
        """'card1-Virtual-1' -> 'Virtual-1' — sysfs prefixes connectors with
        cardN-, which a naive replace chain mangled into '1 Virtual 1'."""
        return re.sub(r"^card\d+-", "", connector)


    def _list_drm_monitors(self) -> list[dict]:
        """List monitors via DRM/KMS."""
        monitors = []
        try:
            # Check /sys/class/drm for connectors
            drm_path = Path("/sys/class/drm")
            if drm_path.exists():
                index = 0
                for connector in drm_path.glob("card*-*"):
                    status_path = connector / "status"
                    if status_path.exists() and status_path.read_text().strip() == "connected":
                        # Try to get modes
                        modes_path = connector / "modes"
                        width, height = 1920, 1080
                        if modes_path.exists():
                            modes = modes_path.read_text().strip().split()
                            if modes:
                                try:
                                    w, h = map(int, modes[0].split('x'))
                                    width, height = w, h
                                except (ValueError, IndexError):
                                    pass
                        
                        name = self._drm_label(connector.name)
                        monitors.append(self._monitor(index, name, 0, 0, width, height, index == 0))
                        index += 1
        except Exception:
            pass
        return monitors
    
    def list_windows(self, limit: int = 150) -> list[dict]:
        """List all application windows (z-order first), capped at ``limit``.

        The facade calls this positionally as ``list_windows(limit)``; taking no
        argument raised TypeError on every enumeration on Linux.
        """
        windows: list[dict] = []

        # Wayland FIRST: wmctrl/xwininfo enumerate the Xwayland subtree only —
        # they "succeed" with a junk list (mutter's guard window, any stray X11
        # client) that shadows the real toplevels, so AT-SPI (the desktop's
        # unconditional answer on GNOME) must be tried before them. X11 sessions
        # skip this and go straight to wmctrl below, where AT-SPI is optional.
        if _is_wayland():
            rows = _windows_from_atspi(_run_atspi() or "")
            windows = [
                self._window(
                    hwnd=row["pid"], pid=row["pid"], title=row["title"],
                    x=row["x"], y=row["y"], width=row["width"], height=row["height"],
                )
                for row in rows
            ]
            if windows:
                return windows

        # X11 enumeration — this also reaches Xwayland clients under a Wayland
        # session once the display auth cookie is supplied.
        try:
            result = subprocess.run(
                ["wmctrl", "-lp", "-G"],
                capture_output=True,
                text=True,
                timeout=5,
                env=_xenv(),
            )
            if result.returncode == 0:
                windows = self._parse_wmctrl(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Another fallback: xwininfo tree
        if not windows:
            try:
                result = subprocess.run(
                    ["xwininfo", "-tree", "-root"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=_xenv(),
                )
                if result.returncode == 0:
                    windows = self._parse_xwininfo(result.stdout)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Wayland: X11 sees only Xwayland clients, and GNOME denies
        # org.gnome.Shell.Introspect.GetWindows to every caller except its own
        # portal helpers — so list toplevels through the accessibility bus
        # (AT-SPI), which the desktop serves unconditionally. There is no X id
        # here, so ``hwnd`` carries the PID; the facade keys watching on hwnd
        # and every consumer reads pid alongside it.
        if not windows and _is_wayland():
            rows = _windows_from_atspi(_run_atspi() or "")
            windows = [
                self._window(
                    hwnd=row["pid"], pid=row["pid"], title=row["title"],
                    x=row["x"], y=row["y"], width=row["width"], height=row["height"],
                )
                for row in rows
            ]

        return windows[:max(0, int(limit))]
    
    def _parse_wmctrl(self, output: str) -> list[dict]:
        """Parse ``wmctrl -lp -G`` output into the flat window schema.

        Columns are id, desktop, [pid], x, y, w, h, host, title — PID is present
        only when ``-p`` succeeded, so detect the layout by counting columns and
        always take the title as the remainder after the host.
        """
        windows: list[dict] = []
        for line in output.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(None, 8)
            if len(parts) < 8:
                continue
            try:
                hwnd = int(parts[0], 16)
                # parts[6] is the frame height in the PID layout but the host
                # name without it — that, not the column count, is the reliable
                # discriminator (a multi-word title pads both layouts to 9 fields).
                try:
                    int(parts[6])
                    has_pid = True
                except ValueError:
                    has_pid = False
                if has_pid:
                    # id desktop pid x y w h host title
                    pid = int(parts[2])
                    x, y, w, h = map(int, parts[3:7])
                    title = " ".join(parts[8:]).strip()
                else:
                    # id desktop x y w h host title
                    pid = 0
                    x, y, w, h = map(int, parts[2:6])
                    title = " ".join(parts[7:]).strip()
            except (ValueError, IndexError):
                continue

            # Skip panels, desktops and slivers that can't be watched.
            if w < 50 or h < 50:
                continue
            if title.lower() in ("desktop", "xfce4-panel", "plasma", "gnome-shell"):
                continue

            windows.append(self._window(
                hwnd=hwnd, pid=pid, title=title, x=x, y=y, width=w, height=h,
            ))
        return windows
    
    def _parse_xwininfo(self, output: str) -> list[dict]:
        """Parse ``xwininfo -tree -root`` output into the flat window schema."""
        windows: list[dict] = []
        for line in output.strip().split('\n'):
            stripped = line.strip()
            if not stripped.startswith("0x"):
                continue
            # Format: 0x<hex> "<title>" = 0x<hex>: <WxH+X+Y> ...
            head = re.match(r'(0x[0-9a-fA-F]+)\s+"([^"]*)"', stripped)
            geom = re.search(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)', stripped)
            if not head or not geom:
                continue
            try:
                hwnd = int(head.group(1), 16)
                title = head.group(2)
                width, height = int(geom.group(1)), int(geom.group(2))
                x, y = int(geom.group(3)), int(geom.group(4))
            except (ValueError, IndexError):
                continue

            if width < 50 or height < 50:
                continue

            windows.append(self._window(
                hwnd=hwnd, pid=0, title=title, x=x, y=y, width=width, height=height,
            ))
        return windows

    @staticmethod
    def _window(*, hwnd: int, pid: int, title: str, x: int, y: int,
                width: int, height: int) -> dict:
        """One window dict, shaped exactly like capture_windows emits."""
        exe = _window_exe(pid) if pid else ""
        app = exe or ""
        if title:
            label = f"{app or 'window'} — {title[:120]}"
        else:
            label = app or f"Window {hwnd}"
        return {
            "id": f"window-{hwnd}",
            "kind": "window",
            "hwnd": hwnd,
            "title": title[:120],
            "class": "",
            "exe": exe,
            "pid": int(pid),
            "x": x, "y": y, "width": width, "height": height,
            # X11 exposes minimized state via _NET_WM_STATE_HIDDEN; the facade
            # queries it live through is_minimized(), so the list stays cheap.
            "minimized": False,
            "foreground": False,
            "label": label,
            "badge": "background",
        }
    
    def list_cameras(self, force: bool = False) -> list[dict]:
        """List all cameras using V4L2."""
        # Cache for 10 seconds
        if not force and self._camera_cache and (time.time() - self._camera_cache_time) < 10:
            return self._camera_cache.get("devices", [])
        
        with self._camera_probe_lock:
            devices = []
            # Check /dev/video* devices
            for i in range(16):
                dev_path = f"/dev/video{i}"
                if not os.path.exists(dev_path):
                    continue
                
                # Try to open with OpenCV to get info
                cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
                if cap.isOpened():
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    
                    # Get device name via v4l2-ctl if available
                    name = f"Camera {i}"
                    try:
                        result = subprocess.run(
                            ["v4l2-ctl", "-d", dev_path, "--info"],
                            capture_output=True,
                            text=True,
                            timeout=2
                        )
                        if result.returncode == 0:
                            for line in result.stdout.split('\n'):
                                if "Card type" in line:
                                    name = line.split(":", 1)[1].strip()
                                    break
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass
                    
                    devices.append({
                        "id": f"camera-{i}",
                        "kind": "camera",
                        "index": i,
                        "label": name,
                        "name": name,
                        "width": width,
                        "height": height,
                        "default": i == 0,
                        "readable": True,
                        "badge": "default" if i == 0 else "camera",
                    })
                    cap.release()
            
            self._camera_cache = {"devices": devices, "at": time.time()}
            self._camera_cache_time = time.time()
            return devices
    
    def list_sources(self) -> dict:
        """List all sources (monitors, windows, cameras)."""
        monitors = self.list_monitors()
        windows = self.list_windows()
        cameras = self.list_cameras()
        return {
            "monitors": monitors,
            "windows": windows,
            "cameras": cameras,
            "count": len(monitors) + len(windows) + len(cameras),
            "capture": _capture_state(),
        }
    
    def grab(self, source: dict):
        """Capture a frame from the given source. Returns a PIL Image."""
        _method("")
        _waiting("")
        kind = source.get("kind")
        
        if kind == "monitor":
            return self._grab_monitor(source)
        elif kind == "window":
            return self._grab_window(source)
        elif kind == "camera":
            return self._grab_camera(source)
        else:
            raise ValueError(f"Unknown source kind: {kind}")
    
    def _grab_monitor(self, source: dict):
        """Capture a monitor (full screen). Returns a PIL Image."""
        x, y, w, h = _flat_rect(source)
        if not w or not h:
            x, y, w, h = 0, 0, 1920, 1080

        # A stream that is already running is the cheapest correct frame on
        # Wayland: no subprocess, no encode/decode round trip, no dialog.
        if _is_wayland():
            img = _pipewire_frame(source, live_only=True)
            if img is not None:
                return img

        # X11: ImageMagick grabs the root window. Skipped on Wayland — the
        # Xwayland root holds only the X11 subtree, so it "succeeds" with a
        # wrong (often blank) frame instead of failing.
        if not _is_wayland():
            try:
                result = subprocess.run(
                    ["import", "-window", "root", "-crop", f"{w}x{h}+{x}+{y}", "png:-"],
                    capture_output=True,
                    timeout=5,
                    env=_xenv(),
                )
                if result.returncode == 0 and result.stdout:
                    import io

                    from PIL import Image
                    img = Image.open(io.BytesIO(result.stdout))
                    _method("x11")
                    return img
            except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
                pass

        # wlroots compositors (Sway, Hyprland, ...): grim.
        try:
            result = subprocess.run(
                ["grim", "-g", f"{x},{y} {w}x{h}", "-"],
                capture_output=True,
                timeout=5,
                env=_session_env(),
            )
            if result.returncode == 0 and result.stdout:
                import io

                from PIL import Image
                img = Image.open(io.BytesIO(result.stdout))
                _method("grim")
                return img
        except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
            pass

        # A fresh portal ScreenCast. On mutter this replaces the screenshot
        # portal's full-screen PNG plus crop with the compositor's own stream,
        # and persist_mode means only the first watch asks the user anything.
        if _is_wayland():
            img = _pipewire_frame(source)
            if img is not None:
                return img

        # GNOME/mutter: the xdg-desktop-portal — the only non-interactive
        # capture path mutter offers unprivileged clients (grim needs
        # wlr-screencopy, which mutter does not implement).
        if _is_wayland():
            png = _portal_screenshot(35.0)
            if png:
                try:
                    from PIL import Image
                    img = Image.open(png).convert("RGB")
                    # The portal saves the whole virtual screen; crop to rect.
                    if x >= 0 and y >= 0 and x + w <= img.width and y + h <= img.height:
                        img = img.crop((x, y, x + w, y + h))
                    _method("portal")
                    return img
                except Exception as exc:
                    _LOG.debug("portal frame unreadable: %s", exc)
                finally:
                    try:
                        os.unlink(png)
                    except OSError:
                        pass

        # Last resort: a black frame — LOUDLY. A silently black watch feed was
        # how a dead capture backend stayed invisible.
        reason = _capture_reason()
        _method("black")
        _LOG.warning(
            "no working capture path for rect %sx%s+%s+%s (%s) — returning a black frame",
            w, h, x, y, reason,
        )
        from PIL import Image
        return Image.new("RGB", (max(1, w), max(1, h)), (0, 0, 0))
    
    def _grab_window(self, source: dict):
        """Capture a window. Returns a PIL Image."""
        hwnd = source.get("hwnd")
        _, _, w, h = _flat_rect(source)
        if not w or not h:
            w, h = 1920, 1080
        
        # X11 window grabs — skipped on Wayland, where hwnd is a PID (there
        # is no X id) and hex(pid) could collide with a real Xwayland window.
        if not _is_wayland():
            try:
                result = subprocess.run(
                    ["import", "-window", hex(hwnd), "png:-"],
                    capture_output=True,
                    timeout=5,
                    env=_xenv(),
                )
                if result.returncode == 0 and result.stdout:
                    import io

                    from PIL import Image
                    img = Image.open(io.BytesIO(result.stdout))
                    return img
            except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
                pass

        # Wayland: only ScreenCast returns the window's *own* pixels. The
        # Screenshot portal and grim can only crop the screen region the window
        # occupies, which shows whatever else is drawn on top of it.
        if _is_wayland():
            img = _pipewire_frame(source, live_only=True) or _pipewire_frame(source)
            if img is not None:
                return img

        # Fallback: capture the screen region the window occupies — on Wayland
        # that resolves to a portal/grim crop of the window's AT-SPI rect.
        img = self._grab_monitor(source)
        _method("region")
        if _is_wayland():
            _waiting("window streaming is not available here — showing the window's "
                     "screen region, which other windows can cover")
        return img
    
    def _grab_camera(self, source: dict):
        """Capture from a camera (held open between frames, like the Windows backend)."""
        index = source.get("index", 0)
        with _CAM_LOCK:
            cap = _CAM_HANDLE.get("cap")
            if cap is None or _CAM_HANDLE.get("index") != index:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap.release()
                    raise RuntimeError(f"Cannot open camera {index}")
                _CAM_HANDLE["cap"] = cap
                _CAM_HANDLE["index"] = index

            try:
                ret, frame = cap.read()
            except Exception:
                # The device may have been yanked: drop the handle so the next
                # grab reopens rather than reading a dead capture forever.
                try:
                    cap.release()
                except Exception:
                    pass
                if _CAM_HANDLE.get("cap") is cap:
                    _CAM_HANDLE["cap"] = None
                    _CAM_HANDLE["index"] = None
                raise

        if not ret or frame is None:
            raise RuntimeError(f"Camera {index} returned empty frame")
        
        # Convert BGR to RGB for PIL
        if frame.shape[2] == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGBA)
        
        from PIL import Image
        return Image.fromarray(frame)
    
    def grab_window_thumbnail(self, hwnd: int):
        """Capture a window thumbnail (for minimized windows). Returns a PIL Image.

        On Linux there is no DWM-equivalent thumbnail API. X11 keeps the frame
        buffer of a minimized window, so a normal grab usually still works; when
        it does not we fall back to a placeholder rather than raising.
        """
        from PIL import Image
        try:
            img = self._grab_window({"kind": "window", "hwnd": int(hwnd)})
            if img is not None:
                return img
        except Exception:
            pass
        return Image.new("RGB", (192, 108), (0, 0, 0))
    
    def is_minimized(self, source: dict) -> bool:
        """Check if a window source is minimized."""
        # On X11, we can check _NET_WM_STATE_HIDDEN
        # On Wayland, this is more complex
        hwnd = source.get("hwnd")
        if hwnd is None:
            return False
        
        try:
            import subprocess
            result = subprocess.run(
                ["xprop", "-id", hex(hwnd), "_NET_WM_STATE"],
                capture_output=True,
                text=True,
                timeout=2,
                env=_xenv(),
            )
            if result.returncode == 0 and "_NET_WM_STATE_HIDDEN" in result.stdout:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        
        return False
    
    def get_window_rect(self, source: dict) -> tuple:
        """(x, y, width, height) of a window source — resolved live from X11.

        The facade hands us ``{"kind": "window", "hwnd": ...}`` with no geometry,
        so reading only a ``bounds`` key always returned (0, 0, 0, 0).
        """
        if source.get("kind") != "window":
            return (0, 0, 0, 0)

        hwnd = source.get("hwnd")
        if hwnd:
            rect = _window_rect(hwnd)
            if rect:
                return (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])

        # Fall back to geometry the caller already carries.
        x, y, w, h = _flat_rect(source)
        if w and h:
            return (x, y, w, h)
        return (0, 0, 0, 0)
    
    def supports_snap_full_res(self) -> bool:
        """Whether full-resolution snaps are supported."""
        return True
    
    def cleanup(self) -> None:
        """Release any held resources.

        Nothing is held on Linux beyond an optional ScreenCast stream: the shared
        camera handle is released through ``release_camera()``, every still grab
        is a short-lived subprocess, and any open stream is stopped here —
        killing its pipeline is what ends the portal session.
        """
        try:
            import capture_pipewire
            capture_pipewire.shutdown()
        except ImportError:  # pragma: no cover - the module ships with the plugin
            pass


def _flat_rect(source: dict) -> tuple[int, int, int, int]:
    """(x, y, w, h) from a source dict, tolerating a legacy ``bounds`` tuple."""
    if all(k in source for k in ("x", "y", "width", "height")):
        return (int(source["x"]), int(source["y"]),
                int(source["width"]), int(source["height"]))
    bounds = source.get("bounds")
    if isinstance(bounds, (tuple, list)) and len(bounds) == 4:
        # Legacy shape stored (x, y, w, h).
        return tuple(int(v) for v in bounds)  # type: ignore[return-value]
    return (0, 0, 0, 0)


# Backwards-compatible function exports (for plugin_api.py transition)
_linux_capture = None


def _get_linux_capture() -> LinuxCapture:
    global _linux_capture
    if _linux_capture is None:
        _linux_capture = LinuxCapture()
    return _linux_capture


def list_monitors() -> list[dict]:
    return _get_linux_capture().list_monitors()


def list_windows(limit: int = 150) -> list[dict]:
    return _get_linux_capture().list_windows(limit)


def list_cameras(force: bool = False) -> list[dict]:
    return _get_linux_capture().list_cameras(force)


def list_sources() -> dict:
    return _get_linux_capture().list_sources()


def grab(source: dict) -> np.ndarray:
    return _get_linux_capture().grab(source)


def grab_window_thumbnail(hwnd: int) -> np.ndarray:
    return _get_linux_capture().grab_window_thumbnail(hwnd)


def is_minimized(source: dict) -> bool:
    return _get_linux_capture().is_minimized(source)


def get_window_rect(source: dict) -> tuple:
    return _get_linux_capture().get_window_rect(source)


def supports_snap_full_res() -> bool:
    return _get_linux_capture().supports_snap_full_res()


def release_camera() -> None:
    """Close the camera device so its LED turns off when watching stops."""
    with _CAM_LOCK:
        cap = _CAM_HANDLE.get("cap")
        _CAM_HANDLE["cap"] = None
        _CAM_HANDLE["index"] = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def cleanup() -> None:
    global _linux_capture
    release_camera()
    if _linux_capture:
        _linux_capture.cleanup()
        _linux_capture = None


def _restored_rect(hwnd: int):
    # X11 has no "restore from minimized" geometry separate from the current
    # rect; the window is placed where it was when it was iconified.
    return _window_rect(hwnd)


def _window_exe(pid: int) -> str:
    """Executable basename of a process, for a readable label."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    # Kernel threads and zombie processes expose only /proc/<pid>/comm.
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """(x1, y1, x2, y2) of a window's frame, or None when it has no geometry."""
    if not hwnd:
        return None
    # Wayland: hwnd carries the PID (see LinuxCapture.list_windows) — AT-SPI is
    # the only non-interactive geometry source there, and xwininfo would
    # misread a PID as an X id.
    if _is_wayland():
        try:
            pid = int(hwnd)
        except (TypeError, ValueError):
            return None
        for row in _windows_from_atspi(_run_atspi() or ""):
            if row["pid"] == pid:
                return (row["x"], row["y"],
                        row["x"] + row["width"], row["y"] + row["height"])
    try:
        result = subprocess.run(
            ["xwininfo", "-id", hex(int(hwnd))],
            capture_output=True, text=True, timeout=3, env=_xenv(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    return _parse_xwininfo_geometry(result.stdout)


def _parse_xwininfo_geometry(output: str) -> Optional[tuple[int, int, int, int]]:
    """Pull (x1, y1, x2, y2) from ``xwininfo`` absolute position + size."""
    def _field(label: str) -> Optional[int]:
        m = re.search(rf"^\s*{re.escape(label)}\s+(-?\d+)\s*$", output, re.MULTILINE)
        return int(m.group(1)) if m else None

    x = _field("Absolute upper-left X:")
    y = _field("Absolute upper-left Y:")
    w = _field("Width:")
    h = _field("Height:")
    if x is None or y is None or w is None or h is None:
        return None
    return (x, y, x + w, y + h)


def _window_iconic(hwnd: int) -> bool:
    return is_minimized({"kind": "window", "hwnd": hwnd})

def _window_rested(hwnd: int) -> bool:
    return True