"""The test environment, established before any test module imports a backend.

`capture_linux` and `capture_windows` import cv2 and numpy at module scope, and CI installs
neither — the workflow is `pip install pytest fastapi Pillow`. So the suite has to supply
them, and it has to do it *here*.

A stub installed from a test file is a stub installed when that file happens to be imported:
`sys.modules` persists for the whole pytest process, so whichever file puts cv2 there first
decides what every later file sees. That is how three macOS tests passed on a dev box that
has opencv — where they opened a real webcam — and died on the runner on `None(...)`. One
place, loaded for the whole rootdir, gives every file the same environment whether the suite
runs whole or a single file is run alone.

`VideoCapture = None` is deliberate: a probe against the stub is a probe against nothing.
"""
from __future__ import annotations

import sys
import types

for _name in ("cv2", "numpy"):
    try:
        __import__(_name)
    except Exception:  # pragma: no cover - environment dependent
        _mod = types.ModuleType(_name)
        _mod.ndarray = object
        _mod.VideoCapture = None
        _mod.CAP_V4L2 = 0
        _mod.CAP_AVFOUNDATION = 0
        _mod.CAP_PROP_FRAME_WIDTH = 3
        _mod.CAP_PROP_FRAME_HEIGHT = 4
        _mod.CAP_PROP_FPS = 5
        sys.modules[_name] = _mod

# pytest.approx inspects numpy itself (python_api calls np.isscalar); a numpy stub missing
# the API pytest expects poisons every later test file that uses approx. Give the stub the
# surface pytest actually touches.
if "numpy" in sys.modules and not hasattr(sys.modules["numpy"], "isscalar"):
    import numbers as _numbers

    _np = sys.modules["numpy"]
    _np.isscalar = lambda x: isinstance(x, _numbers.Number) or isinstance(x, (str, bytes, bool))
    _np.number = _numbers.Number
    _np.integer = int
    _np.floating = float
    _np.bool_ = bool
    _np.ndarray = getattr(_np, "ndarray", object)

    # approx() may route scalar-looking values through np.asarray; enough for floats.
    def _asarray(x, dtype=None):
        try:
            return float(x)
        except (TypeError, ValueError):
            return x

    _np.asarray = _asarray
    _np.ndindex = lambda shape: iter([()])
