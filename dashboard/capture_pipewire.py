"""Wayland screencast capture: xdg-desktop-portal ScreenCast + PipeWire.

The Screenshot portal hands back whole-screen stills, so a window grab there is
a crop of whatever happens to sit on top of it. ScreenCast instead yields the
compositor's own PipeWire stream for the source the user picks: the window's
real pixels even when it is covered, damage-driven frames instead of a full
screenshot round trip per frame, and — with ``persist_mode`` — one pick that
carries into every later watch.

One helper process owns a session end to end. It drives
CreateSession/SelectSources/Start over D-Bus, hands the portal-issued fd to
``gst-launch-1.0`` (``pipewiresrc fd= path=``), and that pipeline drops JPEGs
into a private runtime directory; :func:`frame` reads the newest complete one,
and :func:`shutdown` kills the process, which is what ends the portal session.

Nothing here is fatal. When the probe fails — X11, no portal ScreenCast, no
``gstreamer1.0-pipewire``, no gobject-introspection python — the Linux backend
keeps its grim and Screenshot tiers; the reason is reported by :func:`state` for
``/sources``, and reaches the pane as the reason a frame is not live.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("peripheral_vision.capture_pipewire")

GST = "gst-launch-1.0"
_INSPECT = "gst-inspect-1.0"
# Everything the pipeline needs, checked in one `gst-inspect` call. pipewiresrc
# ships with PipeWire itself (gstreamer1.0-pipewire on Debian/Ubuntu, pipewire-
# gstreamer on Fedora); the rest live in gst-plugins-base/good.
_ELEMENTS = ("pipewiresrc", "videoconvert", "videorate", "jpegenc", "multifilesink")

# A declined or unusable portal is remembered so a watch loop cannot re-open the
# picker every tick. Cleared by state(refresh=True) or by a source change.
_COOLDOWN_S = 120.0
_SESSIONS: dict[str, "Session"] = {}
_LOCK = threading.RLock()
_PROBE: dict = {"at": 0.0, "ok": False, "reason": "not probed", "gst": None}
_NOTE = {"reason": ""}


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{getattr(os, 'getuid', lambda: 0)()}"


def _root() -> Path:
    """Private scratch root: portal frames are screen content, so 0700 under tmpfs."""
    path = Path(_runtime_dir()) / "peripheral-vision" / "screencast"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


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


def _is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def probe(python: Optional[str] = None, refresh: bool = False) -> dict:
    """Whether the ScreenCast route can work here: ``{"ok", "reason", "gst"}``.

    ``python`` (the interpreter with PyGObject) is checked when given. Cached —
    ``refresh=True`` re-runs it. Cheap and side-effect free: the portal call
    itself is only made when a frame is actually wanted.
    """
    now = time.time()
    if not refresh and _PROBE["gst"] and (now - _PROBE["at"]) < 300:
        return _PROBE
    _PROBE.update(at=now, ok=False, gst=None, reason="")
    if not _is_wayland():
        _PROBE["reason"] = "not a Wayland session"
    elif python is not None and not python:
        _PROBE["reason"] = "no python3 with PyGObject (gi) for the portal helper"
    else:
        gst = shutil.which(GST)
        if not gst:
            _PROBE["reason"] = f"{GST} not found (install gstreamer1.0-tools)"
        else:
            try:
                check = subprocess.run(
                    [shutil.which(_INSPECT) or _INSPECT, *_ELEMENTS],
                    capture_output=True, text=True, timeout=15, env=_session_env(),
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                check = None
            if check is None or check.returncode != 0:
                _PROBE["reason"] = (
                    "gstreamer pipewiresrc missing (install gstreamer1.0-pipewire "
                    "+ gst-plugins-good)"
                )
            else:
                _PROBE.update(ok=True, gst=gst, reason="")
    if not _PROBE["ok"] and _PROBE["reason"]:
        _LOG.debug("pipewire screencast unavailable: %s", _PROBE["reason"])
    return _PROBE


# One ScreenCast session: CreateSession, SelectSources, Start, OpenPipeWireRemote,
# then hand the fd to gst-launch. Writes its state to argv[1] (JSON) as it goes and
# the single-use restore token to argv[2], then execs the pipeline — from that point
# the process *is* the stream, so a dead pid means a dead stream.
_SCRIPT = r'''
import fcntl, json, os, sys
runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", runtime)
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % runtime)
import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

status_path, token_path, frames_dir, kinds, timeout_ms, fps, quality, gst = sys.argv[1:9]
timeout_ms = int(timeout_ms)

STATE = {"state": "starting", "reason": ""}

def write(state, reason="", **extra):
    STATE.update(state=state, reason=reason, **extra)
    STATE["pid"] = os.getpid()
    STATE["at"] = __import__("time").time()
    tmp = status_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(STATE, fh)
    os.replace(tmp, status_path)

def die(state, reason, code):
    write(state, reason)
    sys.exit(code)

PORTAL = "org.freedesktop.portal.Desktop"
ROOT = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
sender = conn.get_unique_name()[1:].replace(".", "_")
loop = GLib.MainLoop()
seq = [0]

def portal_call(method, params, options, expect):
    """Call a portal method, follow its Request object, return (response, results)."""
    seq[0] += 1
    token = "pv%d" % seq[0]
    predicted = "%s/request/%s/%s" % (ROOT, sender, token)
    options = dict(options or {})
    options["handle_token"] = GLib.Variant("s", token)
    got = {"response": None, "results": {}}
    subs = []

    def on_response(connection, name, path, iface, signal, params_, *extra):
        if got["response"] is not None:
            return
        response, results = params_.unpack()
        got["response"], got["results"] = int(response), results
        loop.quit()

    def watch(path):
        subs.append(conn.signal_subscribe(
            None, "org.freedesktop.portal.Request", "Response", path, None,
            Gio.DBusSignalFlags.NONE, on_response, None))

    watch(predicted)
    reply = conn.call_sync(PORTAL, ROOT, IFACE, method,
                           GLib.Variant(expect, params + (options,)),
                           GLib.VariantType("r"), Gio.DBusCallFlags.NONE,
                           timeout_ms, None)
    handle = reply.unpack()[0]
    if handle != predicted:  # some portal versions pick their own handle
        watch(handle)
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
    for sub in subs:
        conn.signal_unsubscribe(sub)
    if got["response"] is None:
        die("failed", "portal %s timed out" % method, 3)
    return got["response"], got["results"]

write("awaiting-pick")
try:
    response, results = portal_call(
        "CreateSession", (),
        {"session_handle_token": GLib.Variant("s", "pvs%d" % seq[0])}, "(a{sv})")
    if response != 0:
        die("declined", "screen cast session refused (response=%d)" % response, 4)
    session = results.get("session_handle")
    if not session:
        die("failed", "portal returned no session handle", 5)

    options = {
        "types": GLib.Variant("u", int(kinds)),
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 1),
        # persist_mode 2 = persist for this app: the pick list is remembered, so
        # only the first watch of a source asks the user to choose anything.
        "persist_mode": GLib.Variant("u", 2),
    }
    try:
        with open(token_path) as fh:
            token = fh.read().strip()
        if token:
            options["restore_token"] = GLib.Variant("s", token)
    except OSError:
        pass
    response, _ = portal_call("SelectSources", (session,), options, "(oa{sv})")
    if response != 0:
        die("declined", "source selection refused (response=%d)" % response, 4)

    response, results = portal_call("Start", (session, ""), {}, "(osa{sv})")
    if response != 0:
        die("declined", "screen cast refused (response=%d)" % response, 4)
    streams = results.get("streams") or []
    if not streams:
        die("failed", "portal granted no stream", 5)
    node_id, props = streams[0][0], streams[0][1] or {}
    size = props.get("size") or (0, 0)
    write("live", node_id=int(node_id), title=str(props.get("window-title") or ""),
          width=int(size[0]), height=int(size[1]))
    if results.get("restore_token"):
        try:
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(results["restore_token"])
        except OSError:
            pass

    # The portal owns which PipeWire instance to talk to, so use its fd rather
    # than connecting by name. OpenPipeWireRemote returns exactly one fd.
    reply, fd_list = conn.call_with_unix_fd_list_sync(
        PORTAL, ROOT, IFACE, "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session, {})), None, Gio.DBusCallFlags.NONE,
        5000, None, None)
    if fd_list is None or fd_list.get_length() < 1:
        die("failed", "portal returned no PipeWire fd", 6)
    pfd = fd_list.get(0)
except GLib.Error as exc:
    die("failed", str(exc).strip() or "portal call failed", 7)

# exec keeps the fd open as long as it is not close-on-exec, and the process
# becomes the stream: killing it ends the portal session with it.
flags = fcntl.fcntl(pfd, fcntl.F_GETFD)
fcntl.fcntl(pfd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
os.set_inheritable(pfd, True)
for stale in os.listdir(frames_dir):
    try:
        os.unlink(os.path.join(frames_dir, stale))
    except OSError:
        pass
pipeline = ("pipewiresrc fd=%d path=%d ! videoconvert ! videorate drop-only=true ! "
            "video/x-raw,framerate=%s/1 ! jpegenc quality=%s ! "
            "multifilesink location=%s/f-%%05d.jpg max-files=4"
            % (pfd, int(node_id), fps, quality, frames_dir))
try:
    os.execvp(gst, [gst, "-q", pipeline])
except OSError as exc:
    die("failed", "could not run %s: %s" % (gst, exc), 8)
'''


class Session:
    """One live ScreenCast stream for one source (monitor or window)."""

    def __init__(self, kind: str, key: str, python: str, gst: str, pick_timeout: float = 35.0):
        self.kind = kind
        self.key = key
        self.python = python
        self.gst = gst
        self.pick_timeout = pick_timeout
        self.dir = _root() / f"{kind}-{key}"
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.status_path = self.dir / "status.json"
        self.token_path = self.dir / "restore.token"
        self.log_path = self.dir / "gst.log"
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self._log = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Spawn the helper and wait for it to reach ``live`` (or say why not)."""
        for path in (self.status_path, self.log_path):
            try:
                path.unlink()
            except OSError:
                pass
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [self.python, "-c", _SCRIPT, str(self.status_path), str(self.token_path),
             str(self.dir), "1" if self.kind == "monitor" else "2",
             str(int(self.pick_timeout * 1000)), "2", "80", self.gst],
            stdout=subprocess.DEVNULL, stderr=self._log, env=_session_env(),
        )
        self.started_at = time.time()
        deadline = self.started_at + self.pick_timeout
        while time.time() < deadline:
            state = self.status().get("state")
            if state == "live":
                _LOG.info("pipewire %s stream live%s", self.kind,
                          f" ({self.status().get('title')})" if self.status().get("title") else "")
                return
            if state in ("declined", "failed") or self.proc.poll() is not None:
                return
            time.sleep(0.2)

    def status(self) -> dict:
        """The helper's last written state, plus liveness of the pipeline."""
        try:
            with open(self.status_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data.setdefault("state", "starting")
        data["alive"] = bool(self.proc and self.proc.poll() is None)
        if data["state"] == "live" and not data["alive"]:
            data["state"] = "ended"
        return data

    def reason(self) -> str:
        """A one-line, user-facing explanation of the current state."""
        data = self.status()
        state = data.get("state")
        if state == "live":
            return ""
        if state == "awaiting-pick":
            what = "screen" if self.kind == "monitor" else "window"
            return (f"waiting for you to pick the {what} in the screen-sharing dialog "
                    "— allow it once and this source streams quietly afterwards")
        if state == "declined":
            return f"screen sharing was declined ({data.get('reason') or 'no reason given'})"
        if state == "ended":
            return "the screen-sharing stream ended — restart the watch to pick again"
        return data.get("reason") or f"screen cast session {state}"

    def frame(self, timeout: float = 2.0):
        """Newest complete JPEG as a PIL image, or None (see :meth:`reason`)."""
        deadline = time.time() + max(0.0, timeout)
        while True:
            img = self._newest_frame()
            if img is not None:
                return img
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def _newest_frame(self):
        """Newest frame that decodes; the newest file may still be mid-write."""
        from PIL import Image

        try:
            files = sorted(self.dir.glob("f-*.jpg"))
        except OSError:
            return None
        for path in reversed(files[-2:] if len(files) > 1 else files):
            try:
                with Image.open(path) as img:
                    img.load()
                    return img.convert("RGB")
            except Exception:
                continue  # torn write: try the previous frame
        return None

    def stop(self) -> None:
        """Kill the pipeline — the portal session ends with the process."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        self.proc = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def tail(self) -> str:
        """Tail of the pipeline's stderr — the real error when gst exits."""
        try:
            return self.log_path.read_text(errors="replace")[-400:].strip()
        except OSError:
            return ""


def _get(kind: str, key: str, python: str, gst: str, pick_timeout: float) -> Optional[Session]:
    """The live session for this source, starting one if needed. None when unusable."""
    with _LOCK:
        session = _SESSIONS.get(key)
        if session and session.status().get("state") in ("live", "awaiting-pick", "starting"):
            return session
        if session:
            reason = session.reason()
            _LOG.warning("pipewire %s stream unusable: %s %s", kind, reason, session.tail()[:200])
            session.stop()
            _SESSIONS.pop(key, None)
            _NOTE["reason"] = reason
            return None
        probe_result = probe(python)
        if not probe_result["ok"]:
            _NOTE["reason"] = probe_result["reason"]
            return None
        if time.time() - _PROBE.get("fail_at", 0.0) < _COOLDOWN_S:
            _NOTE["reason"] = _PROBE.get("fail_reason") or "screen sharing was declined"
            return None
        session = Session(kind, key, python, gst, pick_timeout)
        _SESSIONS[key] = session
        session.start()
        state = session.status().get("state")
        if state != "live":
            # Remember the outcome so another source cannot stack a second dialog
            # on top of a pending or refused one. A live session is returned
            # above, so this never suppresses a stream that already works.
            _PROBE["fail_at"] = time.time()
            _PROBE["fail_reason"] = session.reason()
            _NOTE["reason"] = session.reason()
            if state in ("declined", "failed"):
                _LOG.warning("pipewire %s capture unavailable: %s %s",
                             kind, _NOTE["reason"], session.tail()[:200])
                session.stop()
                _SESSIONS.pop(key, None)
            return None
        _PROBE.pop("fail_at", None)
        return session


def live_frame(kind: str, key: str, timeout: float = 0.5):
    """A frame from an already-running stream — never starts a session or a dialog.

    The fast path for a watch that is already streaming: costs one file read and
    returns None the moment the stream stalls, so callers can fall back.
    """
    with _LOCK:
        session = _SESSIONS.get(key)
    if session is None or session.status().get("state") != "live":
        return None
    img = session.frame(timeout)
    if img is None:
        _NOTE["reason"] = session.reason()
    return img


def frame(kind: str, key: str, python: Optional[str], timeout: float = 2.0,
          pick_timeout: float = 35.0) -> Optional[object]:
    """A frame from the ScreenCast stream for ``key``, or None with a reason in :func:`state`."""
    if not python:
        _NOTE["reason"] = "no python3 with PyGObject (gi) for the portal helper"
        return None
    probed = probe(python)
    if not probed["ok"]:
        _NOTE["reason"] = probed["reason"]
        return None
    session = _get(kind, key, python, probed["gst"], pick_timeout)
    if session is None:
        return None
    img = session.frame(timeout)
    if img is None:
        _NOTE["reason"] = session.reason()
    return img


def state(python: Optional[str] = None, refresh: bool = False) -> dict:
    """Diagnostics for ``list_sources()["capture"]`` — never raises."""
    try:
        with _LOCK:
            sessions = [
                {"kind": s.kind, "key": s.key, **{k: v for k, v in s.status().items()
                                                  if k in ("state", "title", "width", "height")}}
                for s in _SESSIONS.values()
            ]
        probed = probe(python, refresh=refresh)
        return {
            "method": "pipewire" if probed["ok"] else "",
            "state": sessions[0]["state"] if sessions else ("available" if probed["ok"] else "unsupported"),
            "reason": "" if probed["ok"] else probed["reason"],
            "note": _NOTE["reason"],
            "sessions": sessions,
        }
    except Exception as exc:  # diagnostics must never break a source listing
        return {"method": "", "state": "unknown", "reason": str(exc), "note": "", "sessions": []}


def note() -> str:
    """Why the last :func:`frame` call produced nothing ('' when it produced one)."""
    return _NOTE["reason"]


def clear_note() -> None:
    _NOTE["reason"] = ""


def shutdown() -> None:
    """Stop every stream and forget the probe (a session change invalidates both)."""
    with _LOCK:
        for session in _SESSIONS.values():
            session.stop()
        _SESSIONS.clear()
        _PROBE.pop("fail_at", None)
        _PROBE.pop("fail_reason", None)
        clear_note()
