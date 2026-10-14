"""The pane's pure helpers, driven for real through node.

The UI half is one uncompiled file the app loads (`desktop/plugin.js`); it imports
`@hermes/plugin-sdk`, which node cannot resolve, so the helpers under test are lifted out by brace
matching and run in an empty vm context. That is coverage where there was none: the pane's cadence
and its virtual-camera filter both regressed in the field (a 5 s Blink rate stepped on a hard-coded
6 s clock; virtual rows stayed in the list) and neither had a test.

Skipped cleanly when node is unavailable, so the suite still runs on a bare interpreter.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
PANE = PLUGIN_DIR / "desktop" / "plugin.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm')
const src = fs.readFileSync(process.argv[2], 'utf8')

function lift(name) {
  const start = src.indexOf(`function ${name}(`)
  if (start < 0) return null
  let depth = 0
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}' && --depth === 0) {
      return vm.runInNewContext(`(${src.slice(start, i + 1)})`, Object.create(null), { timeout: 1000 })
    }
  }
  return null
}

const out = []
const check = (label, ok, detail = '') => out.push([label, !!ok, detail])

const visibleCameras = lift('visibleCameras')
check('visibleCameras exists', typeof visibleCameras === 'function')
if (typeof visibleCameras === 'function') {
  const oldBackend = { cameras: [{ id: 'camera-0' }, { id: 'camera-1', quiet: true }, { id: 'camera-2', quiet: true }] }
  const fixedBackend = { cameras: [{ id: 'camera-0', quiet: false }], cameras_hidden: 7 }
  let v = visibleCameras(oldBackend, false)
  check('virtual rows are hidden by default', v.cameras.map(c => c.id).join(',') === 'camera-0', v.cameras.map(c => c.id).join(','))
  check('the hidden count is derived when the backend sends none', v.hidden === 2, String(v.hidden))
  v = visibleCameras(oldBackend, true)
  check('the toggle lists every camera', v.cameras.length === 3 && v.hidden === 0)
  v = visibleCameras(fixedBackend, false)
  check('an already-filtered list passes through', v.cameras.length === 1 && v.hidden === 7)
  v = visibleCameras(undefined, false)
  check('no payload is not an error', v.cameras.length === 0 && v.hidden === 0)
}

const previewRefetchMs = lift('previewRefetchMs')
check('previewRefetchMs exists', typeof previewRefetchMs === 'function')
if (typeof previewRefetchMs === 'function') {
  check('a 5s Blink rate polls at 5s', previewRefetchMs(true, '5000') === 5000, String(previewRefetchMs(true, '5000')))
  check('a 30s Blink rate polls at 30s', previewRefetchMs(true, '30000') === 30000)
  check('manual never polls', previewRefetchMs(true, '0') === false)
  check('a stopped watch never polls', previewRefetchMs(false, '5000') === false)
  check('a nonsense interval never polls', previewRefetchMs(true, 'abc') === false)
  check('a sub-second pick is clamped to 1s', previewRefetchMs(true, '250') === 1000)
}

for (const [label, ok, detail] of out) console.log((ok ? 'PASS  ' : 'FAIL  ') + label + (detail ? `   [${detail}]` : ''))
const passed = out.filter(([, ok]) => ok).length
console.log(`\n${passed}/${out.length} checks passed`)
console.log('RESULT ' + (passed === out.length ? 'PASS' : 'FAIL'))
process.exit(passed === out.length ? 0 : 1)
"""


@pytest.fixture()
def harness(tmp_path):
    path = tmp_path / "pane-helpers.cjs"
    path.write_text(HARNESS, encoding="utf-8")
    return path


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_panes_pure_helpers_behave(harness) -> None:
    result = subprocess.run(
        ["node", str(harness), str(PANE)], capture_output=True, text=True, timeout=120
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT PASS" in result.stdout


def test_the_watched_image_is_keyed_on_its_source_and_follows_the_blink_rate() -> None:
    """Wiring, not behaviour: the helper only helps if the query actually uses it.

    Both regressions this pins were call-site bugs — a literal 6000 ms instead of the Blink rate,
    and a source-agnostic query key that served the previous source's frame.
    """
    pane = PANE.read_text(encoding="utf-8")

    assert "refetchInterval: previewRefetchMs(running, intervalMs)" in pane
    assert "refetchInterval: running ? 6000 : false" not in pane
    assert "queryKey: [ID, 'preview', 'watch', watchedId]" in pane
    assert "const watchedId = (data.source && data.source.id)" in pane
