"""Window enumeration must never send a message to a window this process owns.

``GetWindowTextLengthW``/``GetWindowTextW`` deliver a message to the window's owning thread.
For a window owned by ANOTHER process the kernel bounds the wait, but for one owned by THIS
process there is no timeout — and the plugin's off-screen DWM-thumbnail host
(``HermesPeripheralVisionThumbHost``) is shown by design while its creator thread pumps no
messages. A title call on it blocks the enumeration thread forever, so ``list_windows`` must
skip every window of its own process BEFORE the first title call. These tests drive
``list_windows`` against a fake user32 that records the call order; a title call reaching an
own-process window can then never slip through unnoticed.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("peripheral_vision_backend_half_window_enum", PLUGIN_DIR / "dashboard" / "plugin_api.py")


def _hwnd(value) -> int:
    """A window handle as an int: ints from a real callback, or wrapped in c_void_p."""
    if isinstance(value, ctypes.c_void_p):
        value = value.value or 0
    if isinstance(value, bytes):  # some builds hand over the pointer's raw bytes
        value = int.from_bytes(value, "little")
    return int(value)


class _Win:
    def __init__(self, hwnd: int, pid: int, title: str, cls: str = "FakeClass", visible: bool = True):
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.cls = cls
        self.visible = visible


class _FakeUser32:
    """Just enough user32 for list_windows, with a call log that pins the order."""

    def __init__(self, windows: list[_Win]):
        self.windows = windows
        self.calls: list[tuple[str, int]] = []

    def _rec(self, method: str, hwnd: int = 0) -> None:
        self.calls.append((method, hwnd))

    def _win(self, h) -> _Win:
        h = _hwnd(h)
        for w in self.windows:
            if w.hwnd == h:
                return w
        raise AssertionError(f"unscripted window handle {hex(h)}")

    def titles_called_for(self, hwnd: int) -> bool:
        return any(m in ("GetWindowTextLengthW", "GetWindowTextW") and h == hwnd for m, h in self.calls)

    def SetProcessDPIAware(self):  # noqa: N802 - Win32 name
        self._rec("SetProcessDPIAware")
        return True

    def EnumWindows(self, callback, _lparam):  # noqa: N802 - Win32 name
        self._rec("EnumWindows")
        for w in self.windows:
            if not callback(ctypes.c_void_p(w.hwnd), ctypes.c_void_p(0)):
                break
        return True

    def IsWindowVisible(self, h):  # noqa: N802 - Win32 name
        self._rec("IsWindowVisible", _hwnd(h))
        return self._win(h).visible

    def GetWindowThreadProcessId(self, h, pid_out):  # noqa: N802 - Win32 name
        self._rec("GetWindowThreadProcessId", _hwnd(h))
        pid_out._obj.value = self._win(h).pid  # called with ctypes.byref()

    def GetWindowTextLengthW(self, h):  # noqa: N802 - Win32 name
        self._rec("GetWindowTextLengthW", _hwnd(h))
        return len(self._win(h).title)

    def GetWindowTextW(self, h, buf, _cap):  # noqa: N802 - Win32 name
        self._rec("GetWindowTextW", _hwnd(h))
        buf.value = self._win(h).title
        return len(self._win(h).title)

    def GetWindowLongPtrW(self, h, _index):  # noqa: N802 - Win32 name
        self._rec("GetWindowLongPtrW", _hwnd(h))
        return 0

    def GetWindowLongW(self, h, _index):  # noqa: N802 - Win32 name
        self._rec("GetWindowLongW", _hwnd(h))
        return 0

    def GetClassNameW(self, h, buf, _cap):  # noqa: N802 - Win32 name
        self._rec("GetClassNameW", _hwnd(h))
        buf.value = self._win(h).cls

    def IsIconic(self, h):  # noqa: N802 - Win32 name
        self._rec("IsIconic", _hwnd(h))
        return False

    def GetForegroundWindow(self):  # noqa: N802 - Win32 name
        self._rec("GetForegroundWindow")
        return 0


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch):
    def _install(windows: list[_Win]) -> _FakeUser32:
        fake = _FakeUser32(windows)
        monkeypatch.setattr(api.ctypes.windll, "user32", fake)
        monkeypatch.setattr(api, "_is_cloaked", lambda h: False)
        monkeypatch.setattr(api, "_window_rect", lambda h: (0, 0, 640, 480))
        monkeypatch.setattr(api, "_restored_rect", lambda h: None)
        monkeypatch.setattr(api, "_window_exe", lambda pid: "app.exe")
        # Patch shared_state directly — capture_windows reads from it at call time
        monkeypatch.setattr("shared_state.THUMB_HOST", {"hwnd": 0, "size": (0, 0)})
        # Also patch capture_windows helpers directly so test's fake user32 gets called
        monkeypatch.setattr("capture_windows._is_cloaked", lambda h: False)
        monkeypatch.setattr("capture_windows._window_rect", lambda h: (0, 0, 640, 480))
        monkeypatch.setattr("capture_windows._restored_rect", lambda h: None)
        monkeypatch.setattr("capture_windows._window_exe", lambda pid: "app.exe")
        return fake

    return _install


OWN_HWND = 0x00AA
HOST_HWND = 0x0707
FOREIGN_HWND = 0x00BB


def test_own_windows_are_skipped_before_any_title_call(install) -> None:
    """A window of this process is filtered by pid BEFORE the message-sending title calls."""
    fake = install(
        [
            _Win(OWN_HWND, os.getpid(), "Self — window of this very process"),
            _Win(FOREIGN_HWND, 424242, "Notepad"),
        ]
    )
    result = api.list_windows()

    assert not fake.titles_called_for(OWN_HWND), "a title call reached an own-process window"
    assert fake.titles_called_for(FOREIGN_HWND)
    assert [w["id"] for w in result] == [f"window-{FOREIGN_HWND}"]
    assert result[0]["label"] == "app.exe — Notepad"
    # The order matters, not just the outcome: the pid fetch for an own window must precede
    # any title call whatsoever, which is what makes the guard immune to hung own windows.
    own_pid_at = fake.calls.index(("GetWindowThreadProcessId", OWN_HWND))
    first_title_at = next(
        i for i, (m, _) in enumerate(fake.calls) if m in ("GetWindowTextLengthW", "GetWindowTextW")
    )
    assert own_pid_at < first_title_at


def test_the_thumbnail_host_is_skipped_even_when_the_pid_filter_would_pass(install) -> None:
    """The host is skipped by its handle too, independent of who owns it."""
    fake = install([_Win(HOST_HWND, 424242, " "), _Win(FOREIGN_HWND, 424242, "Notepad")])
    # Update shared_state directly — capture_windows reads from it
    import shared_state
    shared_state.THUMB_HOST.update({"hwnd": HOST_HWND, "size": (160, 90)})

    result = api.list_windows()

    assert not fake.titles_called_for(HOST_HWND), "the thumbnail host got a title call"
    assert [w["id"] for w in result] == [f"window-{FOREIGN_HWND}"]


def test_invisible_and_own_windows_together_yield_no_title_calls_at_all(install) -> None:
    """With nothing eligible, the enumeration must not send a single message."""
    fake = install(
        [
            _Win(OWN_HWND, os.getpid(), "Self"),
            _Win(FOREIGN_HWND, 424242, "Hidden", visible=False),
        ]
    )
    result = api.list_windows()

    assert result == []
    assert not [c for c in fake.calls if c[0] in ("GetWindowTextLengthW", "GetWindowTextW")]
    assert ("GetWindowThreadProcessId", FOREIGN_HWND) not in fake.calls, (
        "an invisible window was probed further than its visibility"
    )


def _module_level_defs(path: Path) -> dict:
    """Name -> count of top-level ``def``s in a source file."""
    counts: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("def "):
            name = line[4:].split("(")[0]
            counts[name] = counts.get(name, 0) + 1
    return counts


def test_no_shadowing_duplicate_defs():
    """A later ``def`` silently wins, so a duplicate is always a bug.

    capture_windows.py once shipped trailing ``_window_exe``/``_restored_rect``
    stubs that re-imported the same module's name — resolving to themselves and
    recursing. The RecursionError was swallowed inside the EnumWindows callback,
    so every window was skipped and enumeration returned [] with no error.
    """
    for backend in ("capture_windows.py", "capture_linux.py"):
        counts = _module_level_defs(PLUGIN_DIR / "dashboard" / backend)
        dupes = {name: n for name, n in counts.items() if n > 1}
        assert not dupes, f"{backend} redefines {dupes}; the later def shadows the real one"


def test_window_exe_never_recurses():
    """The exe lookup runs inside the enumeration callback; a RecursionError there
    empties the whole list."""
    import capture_windows as cw

    # _window_exe is not monkeypatched here — this is the real code path.
    result = cw._window_exe(0)
    assert isinstance(result, str)


def test_real_enumeration_is_not_silently_empty():
    """An empty list is legitimate (no windows), but it must not be the symptom
    of an exception swallowed inside the callback."""
    import capture_windows as cw

    errors = []
    original = cw._window_exe

    def spy(pid):
        try:
            return original(pid)
        except BaseException as exc:  # pragma: no cover - the bug we guard against
            errors.append(repr(exc))
            return ""

    cw._window_exe = spy
    try:
        cw.list_windows()
    finally:
        cw._window_exe = original
    assert not errors, f"the exe lookup threw inside enumeration: {errors}"
