"""A camera probe opens hardware only — a software camera waits until it is picked.

Measured on this host: opening Phone Link's virtual camera (index 1, the S23 Ultra) starts the
phone's stream and pops the ``CrossDeviceStreamingHost.exe`` window, and releasing the device
closes it again. The probe wave opens every index it can, so a picker refresh popped the phone's
window. These cases pin the policy with no camera, driver or ffmpeg in the loop: the enumeration
is a literal, cv2 is a fake that records the indices it was asked to open, and every state file
is redirected to a throwaway directory.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "dashboard"))  # capture_windows imports its siblings


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cw = _load("peripheral_vision_backend_half_camera_probe", PLUGIN_DIR / "dashboard" / "capture_windows.py")

# What this host enumerates, in the order ffmpeg prints it: one real camera, then software devices.
HARDWARE = {"name": "HD Pro Webcam C920", "moniker": "@device_pnp_\\\\?\\usb#vid_046d&pid_082d#7&1361c34f&4&0000"}
PHONE = {"name": "J's S23 Ultra (Windows Virtual Camera)", "moniker": "@device_sw_{860BB310-...}\\{fcebba03-...}"}
VIRTUAL = {"name": "Meta Quest 3", "moniker": "@device_sw_{860BB310-...}\\{0FEDCBA9-...}"}


class _Frame:
    shape = (480, 640, 3)


def _fake_cv2(opened: list[int], failing: set[int] = frozenset()) -> types.ModuleType:
    module = types.ModuleType("cv2")

    class _Capture:
        def __init__(self, index=0, backend=0):
            opened.append(int(index))
            self.index = int(index)
            self._failing = int(index) in failing

        def read(self):
            if self._failing:
                return False, None
            return True, _Frame()

        def get(self, prop):
            return 640 if prop == module.CAP_PROP_FRAME_WIDTH else 480

        def release(self):
            return None

    module.VideoCapture = _Capture
    module.CAP_DSHOW = 700
    module.CAP_PROP_FRAME_WIDTH = "w"
    module.CAP_PROP_FRAME_HEIGHT = "h"
    module.CAP_PROP_FOURCC = "f"
    module.VideoWriter_fourcc = lambda *args: 0
    return module


@pytest.fixture()
def probe(tmp_path, monkeypatch) -> list[int]:
    """Run a hermetic probe and hand back the camera indices it opened."""
    opened: list[int] = []
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2(opened))
    monkeypatch.setattr(cw, "CAMERA_CACHE_PATH", tmp_path / "cameras.json")
    monkeypatch.setattr(cw, "CAMERA_MODES_PATH", tmp_path / "camera_modes.json")
    monkeypatch.setattr(cw, "CAMERA_MAX_INDEX", 4)
    monkeypatch.setattr(cw, "CAMERA_PROBE_ALL", False)
    monkeypatch.setattr(cw, "_CAMERA_MODES_LOADED", False)
    monkeypatch.setattr(cw, "_CAMERA_MODES", {})
    monkeypatch.setattr(cw, "_CAMERA_NAMES", None)
    monkeypatch.setattr(cw, "_CAMERA_DEVICES", None)
    monkeypatch.setattr(cw, "_CAMERA_CACHE", {"devices": [], "at": 0.0, "probing": True, "thread": None})
    monkeypatch.setattr(cw, "_CAM_HANDLE", {"cap": None, "index": -1})
    return opened


@pytest.fixture()
def enumerate_devices(monkeypatch):
    def _enumerate(*devices: dict[str, str]) -> None:
        monkeypatch.setattr(cw, "_CAMERA_DEVICES", list(devices))
        monkeypatch.setattr(cw, "_CAMERA_NAMES", [device["name"] for device in devices])

    return _enumerate


def _entries() -> dict[int, dict]:
    return {int(device["index"]): device for device in cw._CAMERA_CACHE["devices"]}


def test_a_probe_opens_hardware_and_leaves_software_alone(probe, enumerate_devices):
    enumerate_devices(HARDWARE, PHONE, VIRTUAL)
    cw._probe_cameras(5.0)

    # Index 3 was never printed by ffmpeg, so it is unknown and still probed — the policy never
    # hides a camera it cannot classify.
    assert probe == [0, 3], "a probe must open hardware only"
    entries = _entries()
    assert entries[0]["readable"] is True
    assert (entries[0]["width"], entries[0]["height"]) == (640, 480)
    # The phone keeps its row — it is simply never opened to build one.
    assert entries[1]["label"] == PHONE["name"]
    assert entries[1]["badge"] == "virtual"
    assert entries[1]["quiet"] is True
    assert "readable" not in entries[1], "nobody opened it, so nothing may claim it is unreadable"


def test_a_quiet_row_carries_the_remembered_size(probe, enumerate_devices):
    enumerate_devices(HARDWARE, PHONE)
    cw._CAMERA_MODES[1] = (1280, 720)
    cw._CAMERA_CACHE["devices"] = [{"id": "camera-1", "index": 1, "readable": False}]
    cw._probe_cameras(5.0)

    entry = _entries()[1]
    assert (entry["width"], entry["height"]) == (1280, 720), "the mode it settled on last time"


def test_a_previous_readable_is_carried_forward(probe, enumerate_devices):
    enumerate_devices(HARDWARE, PHONE)
    cw._CAMERA_CACHE["devices"] = [{"id": "camera-1", "index": 1, "readable": True}]
    cw._probe_cameras(5.0)

    assert _entries()[1]["readable"] is True


def test_the_escape_hatch_probes_software_cameras_again(probe, enumerate_devices, monkeypatch):
    enumerate_devices(HARDWARE, PHONE)
    monkeypatch.setattr(cw, "CAMERA_PROBE_ALL", True)
    cw._probe_cameras(5.0)

    # Everything enumerated, plus the unknown tail: the hatch restores the old probe-everything.
    assert probe == [0, 1, 2, 3]


def test_a_name_alone_can_mark_a_virtual_camera(probe, enumerate_devices):
    """An ffmpeg that prints no alternative name still leaves the name to go on."""
    enumerate_devices(HARDWARE, {"name": "OBS Virtual Camera", "moniker": ""})
    cw._probe_cameras(5.0)

    assert probe == [0, 2, 3], "name-based quietness applies to index 1; the unknown tail probes"
    assert _entries()[1]["badge"] == "virtual"


def test_no_enumeration_falls_back_to_probing_everything(probe, monkeypatch):
    """No ffmpeg (CI, a bare install) means no moniker: the old behaviour, never a silent skip."""
    monkeypatch.setattr(cw, "_CAMERA_DEVICES", [])
    monkeypatch.setattr(cw, "_CAMERA_NAMES", [])
    cw._probe_cameras(5.0)

    assert probe == [0, 1, 2, 3]


# ── enumeration pairing, index coverage, probe seriality ────────────────────


# What this host's ffmpeg really prints: multi-pin devices carry several Alternative-name
# lines and the audio section adds more — 8 video names against 21 alt-name lines. Pairing
# by list length blanks every moniker (the bug), so the parse must walk line by line.
FFMPEG_OUT = "\n".join(
    [
        '[dshow @ 0000] "HD Pro Webcam C920" (video)',
        r'[dshow @ 0000]     Alternative name "@device_pnp_\\?\usb#vid_046d&pid_082d&mi_00#7&1361c34f&4&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"',
        '[dshow @ 0000] "J\'s S23 Ultra (Windows Virtual Camera)" (video)',
        r'[dshow @ 0000]     Alternative name "@device_pnp_\\?\swd#vcamdevapi#596a16296e6b1f4a9612b95e5c3890372f74bc1e05cd624cc9b2b503cbaed850#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\{fcebba03-9d13-4c13-9940-cc84fcd132d1}"',
        '[dshow @ 0000] "Camera (NVIDIA Broadcast)" (video)',
        r'[dshow @ 0000]     Alternative name "@device_sw_{7BBFF097-B3FB-4B26-B685-7A998DE7CEAC}"',
        r'[dshow @ 0000]     Alternative name "@device_sw_{9C981851-EB4B-44A1-B10B-E1315C91F700}"',
        '[dshow @ 0000] "Microphone (USB)" (audio)',
        r'[dshow @ 0000]     Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{A3623370-9CFD-437F-886D-8A65FFE9E2D4}"',
    ]
)


def test_monikers_pair_to_their_device_despite_multi_pin_output(probe, monkeypatch):
    """Line-paired parse: audio lines and second pins must not blank or shift the monikers."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stderr=FFMPEG_OUT, stdout=""))

    devices = cw._camera_devices()

    assert [d["name"] for d in devices] == [
        "HD Pro Webcam C920",
        "J's S23 Ultra (Windows Virtual Camera)",
        "Camera (NVIDIA Broadcast)",
    ]
    assert "usb#vid" in devices[0]["moniker"], "hardware keeps its PnP USB moniker"
    assert "swd#vcamdevapi" in devices[1]["moniker"], "Phone Link's software device keeps its own"
    assert devices[2]["moniker"].startswith("@device_sw_"), "first alternative name wins"
    # …and the classification rides on those monikers:
    assert cw._camera_is_quiet(0) is False  # USB hardware → probed
    assert cw._camera_is_quiet(1) is True  # PnP-shaped but software → never opened
    assert cw._camera_is_quiet(2) is True  # DirectShow filter → never opened


def test_probe_covers_every_enumerated_camera(probe, enumerate_devices):
    """Cameras past CAMERA_MAX_INDEX still reach the picker (this host has 8, ceiling was 4)."""
    enumerate_devices(
        *[
            {"name": f"Cam {i}", "moniker": f"@device_pnp_\\\\?\\usb#vid_{i:04x}"}
            for i in range(6)
        ]
    )
    cw._probe_cameras(5.0)

    assert probe == [0, 1, 2, 3, 4, 5], "index 5 is beyond the old ceiling but enumerated"


def test_probes_open_one_camera_at_a_time(probe, enumerate_devices, monkeypatch):
    """Concurrent DSHOW opens fail (measured on this host) — the probe must serialize them."""
    import sys
    import time as _time

    state = {"live": 0, "peak": 0}
    module = types.ModuleType("cv2")

    class _Capture:
        def __init__(self, index=0, backend=0):
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            self.index = int(index)
            _time.sleep(0.02)  # widen any race window a regression would create

        def read(self):
            return True, _Frame()

        def get(self, prop):
            return 640 if prop == module.CAP_PROP_FRAME_WIDTH else 480

        def release(self):
            state["live"] -= 1

    module.VideoCapture = _Capture
    module.CAP_DSHOW = 700
    module.CAP_PROP_FRAME_WIDTH = "w"
    module.CAP_PROP_FRAME_HEIGHT = "h"
    module.CAP_PROP_FOURCC = "f"
    module.VideoWriter_fourcc = lambda *args: 0
    monkeypatch.setitem(sys.modules, "cv2", module)

    enumerate_devices(
        *[
            {"name": f"Cam {i}", "moniker": f"@device_pnp_\\\\?\\usb#vid_{i:04x}"}
            for i in range(3)
        ]
    )
    cw._probe_cameras(5.0)

    assert state["peak"] == 1, f"two cameras were open at once (peak {state['peak']})"


def test_a_busy_camera_keeps_the_row_it_had_last_time(probe, enumerate_devices, monkeypatch):
    """A camera held by another app this round must not vanish from the picker."""
    import sys

    enumerate_devices(
        HARDWARE,
        {"name": "Second Cam", "moniker": "@device_pnp_\\\\?\\usb#vid_1111"},
    )
    cw._CAMERA_CACHE["devices"] = [
        {
            "id": "camera-1",
            "kind": "camera",
            "index": 1,
            "label": "Second Cam",
            "width": 1280,
            "height": 720,
            "readable": True,
            "badge": "camera",
            "default": False,
        }
    ]
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2(probe, failing={1}))
    cw._probe_cameras(5.0)

    entries = _entries()
    assert entries[0]["readable"] is True, "index 0 probed fresh"
    assert (entries[1]["width"], entries[1]["height"]) == (1280, 720), (
        "index 1 failed to open this round — it keeps the row it had instead of disappearing"
    )


def test_a_watched_camera_records_its_row_without_reopening(probe, enumerate_devices, monkeypatch):
    """The watch already reads camera 0 — record it from the held handle, never reopen it."""
    enumerate_devices(HARDWARE)

    class _Held:
        def get(self, prop):
            return 640 if prop == "w" else 480

    monkeypatch.setitem(cw._CAM_HANDLE, "cap", _Held())
    monkeypatch.setitem(cw._CAM_HANDLE, "index", 0)
    cw._probe_cameras(5.0)

    entries = _entries()
    assert 0 not in probe, "the held camera is read, never reopened"
    assert entries[0]["readable"] is True, "a camera the watch is reading works by definition"
    assert (entries[0]["width"], entries[0]["height"]) == (640, 480)
