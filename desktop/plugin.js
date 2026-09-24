/**
 * Peripheral Vision — desktop half.
 *
 * A pane that watches ONE explicitly chosen source: a whole display, an
 * individual application window, or a camera. The mode picker beside the source
 * button chooses which kind the picker lists, and mirrors the kind being watched;
 * the picker always prompts and nothing is captured until the user picks a source
 * and confirms (no default, no auto-pick).
 *
 * A second picker ("rides your turns") chooses WHEN fresh readings are shared: picking
 * one POSTs to the backend, which persists it where the agent half reads it on the next
 * turn — no restart. That pick outranks PV_VISION_INJECT_MODE, which outranks the
 * built-in default.
 *
 * When "Blink rate" is set to manual, the backend checks nothing on its own and a Snapshot
 * pair appears at the right of that row: a camera button and a crop button. Each pops the
 * matching source list (cameras; displays and windows), opens a live view, and the drag
 * selection is saved to disk and staged into the chat INPUT through the app's own paste
 * route — a synthetic paste the composer cannot tell from ⌘V, so no desktop change and no
 * clipboard takeover is involved.
 *
 * Backend: /api/plugins/peripheral-vision/* (dashboard/plugin_api.py).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only @hermes/plugin-sdk, react and react/jsx-runtime resolve.
 */

import {
  atom,
  Button,
  cn,
  Codicon,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  GlyphSpinner,
  haptic,
  host,
  icons,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tip,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'peripheral-vision'
// The registry namespaces a contribution id as `<pluginId>:<localId>`, so the pane
// registered below is reachable as `peripheral-vision:pane`.
const PANE_ID = `${ID}:pane`
// On-screen visibility of that pane (`host.paneVisibility` is memoized per id, so this
// is resolved once and subscribed to normally — atom(false) for desktops without it).
const PANE_VISIBLE = typeof host.paneVisibility === 'function' ? host.paneVisibility(PANE_ID) : atom(false)

const INTERVALS = [
  { value: '1000', label: 'every 1s' },
  { value: '2000', label: 'every 2s' },
  { value: '5000', label: 'every 5s' },
  { value: '10000', label: 'every 10s' },
  { value: '0', label: 'manual' }
]

// The three kinds of vision this plugin can run - one section of the source picker
// each. The mode picker next to the source button pins which kind the NEXT pick
// comes from, and mirrors whatever is being watched while a watch is up.
const VISION_MODES = [
  { value: 'displays', label: 'Displays' },
  { value: 'windows', label: 'Windows' },
  { value: 'cameras', label: 'Cameras' }
]
// A backend source kind (or, for older backends, an id prefix) -> a vision mode.
const KIND_MODE = { monitor: 'displays', window: 'windows', camera: 'cameras' }

// When a fresh reading rides a turn — the same four modes the agent half knows, ordered
// everyday-first. The labels are the user's words; the tooltip carries the full sentence.
const INJECT_MODES = [
  { value: 'on_change', label: 'on change' },
  { value: 'always', label: 'every turn' },
  { value: 'on_mention', label: 'on mention' },
  { value: 'tool_only', label: 'never' }
]
const INJECT_HINT = {
  on_change:
    'a fresh reading rides a turn only when the screen changed since the last one sent (re-anchored every 10 minutes)',
  always: 'every turn while the reading is fresh',
  on_mention: 'only when your message points at the screen, e.g. “look at my screen”',
  tool_only:
    'never ambient — for setups with a live-view tool; this build registers none, so nothing is shared'
}
// Where the current value comes from, named so the tooltip can say it.
const INJECT_SOURCE = {
  pane: 'picked here',
  environment: 'from PV_VISION_INJECT_MODE',
  default: 'built-in default'
}

function SourceRow({ source, selected, onSelect, preview }) {
  const size = `${source.width}×${source.height}`
  const kind = source.kind || 'monitor'
  const isWindow = kind === 'window'
  const isCamera = kind === 'camera'
  const detail = isWindow
    ? `${size} · ${source.exe || source.class || 'application'}${source.minimized ? ' · minimized' : ''}`
    : isCamera
      ? `${size}${source.default ? ' · OS default' : ''}${source.readable === false ? ' · not readable' : ''}`
      : `${size} · ${source.device}`
  const shot = preview && preview.data && preview.data.ok ? preview.data.data_url : null
  // A minimized program's preview IS its last frame; say so on hover instead of implying it is live.
  const shotNote = shot && preview.data.waiting ? preview.data.waiting : 'live preview'
  const failed =
    preview && !shot && (preview.isError || (preview.data && preview.data.ok === false))
      ? String(
          (preview.error && preview.error.message) || (preview.data && preview.data.error) || 'no preview'
        )
      : null
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(source),
    className: cn(
      'flex w-full items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition-colors',
      selected
        ? 'border-(--ui-accent) bg-(--ui-surface-active) text-foreground'
        : 'border-(--ui-stroke-secondary) hover:bg-(--chrome-action-hover)'
    ),
    children: [
      jsxs('span', {
        className: 'flex min-w-0 flex-col',
        children: [
          jsxs('span', {
            className: 'flex items-center gap-1 font-medium',
            children: [
              jsx('span', { children: source.label }),
              source.primary
                ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: '· primary' })
                : null,
              (isWindow || isCamera) && source.badge
                ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: `· ${source.badge}` })
                : null
            ]
          }),
          jsx('span', {
            className: 'text-(--ui-text-quaternary)',
            children: detail
          })
        ]
      }),
      shot
        ? jsx('img', {
            src: shot,
            alt: preview.data.waiting ? 'last frame (minimized)' : 'live preview',
            title: shotNote,
            className:
              'h-9 w-16 shrink-0 rounded border border-(--ui-stroke-secondary) bg-black/20 object-cover'
          })
        : preview
          ? jsx('span', {
              title: failed || 'capturing…',
              className: cn(
                'flex h-9 w-16 shrink-0 items-center justify-center rounded border border-(--ui-stroke-secondary) text-[10px] text-(--ui-text-quaternary)',
                failed ? '' : 'animate-pulse'
              ),
              children: failed ? 'no frame' : ''
            })
          : null
    ]
  })
}

// Windows come with a live thumbnail of their own: what the watch would actually see right now —
// including a minimized window's last frame.
function WindowRow({ source, selected, onSelect, ctx }) {
  const preview = useQuery({
    queryKey: [ID, 'preview', source.hwnd || source.id],
    queryFn: () => ctx.rest(`/preview?hwnd=${source.hwnd || 0}&width=160`),
    staleTime: 20000,
    refetchInterval: false
  })
  return jsx(SourceRow, { source, selected, onSelect, preview })
}

// ---------------------------------------------------------------------------
// Manual snapshots — the two buttons at the right of the "Blink rate" row when its
// pick is "manual". One lists cameras, the other displays + application windows; picking a
// source opens a live-view overlay whose drag selection becomes the still.
// ---------------------------------------------------------------------------

const clamp01 = value => Math.max(0, Math.min(1, value))
const pctOf = value => `${(value * 100).toFixed(3)}%`

// Hand the cropped PNG to the chat input. The app routes an unfocused paste landing
// on non-editable chrome into the active composer (attaching image blobs and focusing
// it) — the "paste-to-focus" twin of type-to-focus — so a synthetic paste event
// carrying the file rides exactly the user's ⌘V path. That is the whole attach:
// no desktop API, no clipboard takeover. `defaultPrevented` on the event is the
// app's own receipt that it routed; false means a surface blocked it (a dialog still
// animating out, the terminal pane, a full-page view) and the caller says so instead
// of pretending.
function dispatchPasteToComposer(pngBase64, name) {
  const bin = atob(pngBase64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i)
  const dataTransfer = new DataTransfer()
  dataTransfer.items.add(new File([bytes], name, { type: 'image/png' }))
  let event
  try {
    event = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dataTransfer })
  } catch (err) {
    event = new ClipboardEvent('paste', { bubbles: true, cancelable: true })
  }
  if (!event.clipboardData) {
    try {
      Object.defineProperty(event, 'clipboardData', { value: dataTransfer })
    } catch (err) {
      /* the app's clipboardData guard will refuse it; the caller tells the truth */
    }
  }
  window.dispatchEvent(event)
  return event.defaultPrevented === true
}

function SnapMenu({ ctx, sources, kind, disabled, onPick }) {
  const data = (sources && sources.data) || {}
  const camera = kind === 'cameras'
  const cameras = camera ? data.cameras || [] : []
  const monitors = camera ? [] : data.monitors || []
  const windows = camera ? [] : data.windows || []
  const empty = camera
    ? data.cameras_probing
      ? 'scanning for cameras…'
      : 'No camera was detected.'
    : 'No display or window was found.'
  const rows = []
  const header = text =>
    rows.push(
      jsx(
        'div',
        {
          className:
            'px-2 pt-1.5 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
          children: text
        },
        `pv-snap-${text}`
      )
    )
  const item = source =>
    rows.push(
      jsxs(
        DropdownMenuItem,
        {
          onSelect: () => onPick(source),
          children: [
            jsx('span', { children: source.label }),
            jsx('span', {
              className: 'text-(--ui-text-quaternary)',
              children: ` · ${source.width}×${source.height}${source.minimized ? ' · minimized' : ''}`
            })
          ]
        },
        source.id
      )
    )
  if (camera) {
    header('Cameras')
    cameras.forEach(item)
  } else {
    if (monitors.length) {
      header('Displays')
      monitors.forEach(item)
    }
    if (windows.length) {
      header('Application windows')
      windows.forEach(item)
    }
  }
  if (!cameras.length && !monitors.length && !windows.length) {
    rows.push(jsx(DropdownMenuItem, { disabled: true, children: empty }, 'pv-snap-empty'))
  }
  return jsx(DropdownMenu, {
    children: [
      jsx(Tip, {
        label: camera
          ? 'Snapshot from a camera — pick one, then drag a crop'
          : 'Drag-and-crop a screenshot — pick a display or window first',
        children: jsx(DropdownMenuTrigger, {
          asChild: true,
          children: jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            disabled,
            className: 'h-6 w-7 p-0',
            'aria-label': camera ? 'snapshot from a camera' : 'crop a screen area',
            children: jsx(Codicon, { name: camera ? 'device-camera' : 'crop' })
          })
        })
      }),
      jsx(DropdownMenuContent, {
        align: 'start',
        className: 'max-h-80 w-72 overflow-y-auto',
        children: [
          ...rows,
          jsx(DropdownMenuSeparator, {}, 'pv-snap-separator'),
          jsx(
            DropdownMenuItem,
            {
              onSelect: () => ctx.rest('/sources?refresh=1').then(() => sources.refetch()),
              children: 'Refresh the list'
            },
            'pv-snap-refresh'
          )
        ]
      })
    ]
  })
}

function PeripheralVisionPane({ ctx }) {
  const queryClient = useQueryClient()
  const [pickerOpen, setPickerOpen] = useState(false)
  const [chosen, setChosen] = useState(null)
  // The Blink-rate pick: optimistic while its POST is in flight; '0' is manual snapshots.
  const [intervalPick, setIntervalPick] = useState('')
  // '' = follow whatever is being watched (Displays while idle); a pick pins the
  // scope for the NEXT source choice, and starting a watch re-syncs it (see start()).
  const [mode, setMode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pinChoice, setPinChoice] = useState('')
  // The inject-mode pick: optimistic while the POST is in flight, then /status is the
  // authority (the same value the agent half's hook reads on the next turn).
  const [injectPick, setInjectPick] = useState('')
  // The manual snapshot overlay: {session_id, label, kind} while it is up.
  const [snap, setSnap] = useState(null)
  const [snapSel, setSnapSel] = useState(null)
  const [snapBusy, setSnapBusy] = useState(false)
  const [snapError, setSnapError] = useState('')
  const snapBoxRef = useRef(null)
  const snapDragRef = useRef(null)

  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: 2500
  })
  const sources = useQuery({
    queryKey: [ID, 'sources'],
    queryFn: () => ctx.rest('/sources'),
    enabled: true,
    refetchInterval: pickerOpen ? 5000 : false
  })
  const data = status.data || {}
  const running = Boolean(data.running)
  // Which kind of vision is being watched right now, and which kind the picker
  // will list next. status.source_kind is the authority while a watch is up; the
  // source id prefix (monitor- / window- / camera-) covers older backends.
  const watchedId = String((data.source && data.source.id) || '')
  const watchedMode = running
    ? KIND_MODE[data.source_kind || (data.source && data.source.kind)] ||
      (watchedId.indexOf('camera') === 0
        ? 'cameras'
        : watchedId.indexOf('window') === 0
          ? 'windows'
          : 'displays')
    : ''
  const activeMode = mode || watchedMode || 'displays'
  // When fresh readings ride turns. Absent on a backend that predates POST /inject_mode —
  // the row below then does not render (nothing to pick against, no dead control).
  const injectState = data.inject_mode || null
  const injectMode = injectPick || (injectState && injectState.mode) || 'on_change'
  const injectOrigin = injectPick ? 'pane' : (injectState && injectState.source) || 'default'
  // The blink rate — pane pick > backend state > default; '0' = manual, which puts the
  // Snapshot pair on that row (only a backend that advertises `snap` can serve it).
  const intervalState = data.interval || null
  const intervalMs = intervalPick || (intervalState ? String(intervalState.ms) : '2000')
  const manualMode = intervalMs === '0'
  const snapshotsSupported = Boolean(data.snap)
  // The chat on screen — peripheral vision describes frames with ITS model.
  const focusedSessionId = useValue(host.state.focusedSessionId)
  const activeSessionId = useValue(host.state.activeSessionId)
  const liveSessionId = focusedSessionId || activeSessionId
  const preview = useQuery({
    queryKey: [ID, 'preview'],
    queryFn: () => ctx.rest('/preview'),
    refetchInterval: running ? 6000 : false,
    enabled: running
  })
  // The crop overlay's live feed (only while the overlay is open).
  const snapFrame = useQuery({
    queryKey: [ID, 'snapframe', snap ? snap.session_id : 'none'],
    queryFn: () => ctx.rest(`/snap/frame?session_id=${encodeURIComponent(snap.session_id)}`),
    enabled: Boolean(snap),
    refetchInterval: snap ? 500 : false
  })
  const frameData = snapFrame.data || {}
  const frameUrl = frameData.ok && frameData.data_url ? frameData.data_url : null
  const frameNote = frameData.ok ? frameData.waiting || null : frameData.error || null
  // A display/window session is a still — one full-resolution capture, cropped exactly as
  // shown; only a camera is a live feed.
  const snapStill = Boolean(snap) && snap.kind !== 'camera'
  // Which model describes frames, and whether that model can even see them.
  const vision = useQuery({
    queryKey: [ID, 'vision'],
    queryFn: () => ctx.rest('/vision'),
    refetchInterval: 5000
  })
  const visionData = vision.data || {}
  const visionRoute = visionData.route || {}
  const needsVisionModel =
    Boolean(visionData.needs_vision_model) || visionRoute.supports_vision === false
  const visionCandidates = visionData.candidates || []

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
  }, [queryClient])

  const openPicker = useCallback(() => {
    haptic('tap')
    setChosen(null) // ALWAYS a fresh decision — never reuse the last source
    setError('')
    setPickerOpen(true)
    queryClient.invalidateQueries({ queryKey: [ID, 'sources'] })
  }, [queryClient])

  const start = useCallback(async () => {
    if (!chosen) {
      return
    }
    setBusy(true)
    setError('')
    try {
      // The chat the user is looking at: frames are described by ITS model, not by
      // the profile default in config.yaml.
      const sessionId = liveSessionId || ''
      const result = await ctx.rest('/start', {
        method: 'POST',
        body: { source_id: chosen.id, interval_ms: Number(intervalMs), session_id: sessionId }
      })
      if (result && result.ok === false) {
        setError(result.error || 'could not start')
      } else {
        setPickerOpen(false)
        setMode('') // re-sync the picker to the watch that just started
        host.notify({ kind: 'info', message: `Peripheral Vision → ${chosen.label}` })
      }
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
      refresh()
    }
  }, [chosen, ctx, intervalMs, liveSessionId, refresh])

  const stop = useCallback(async () => {
    haptic('tap')
    setBusy(true)
    try {
      await ctx.rest('/stop', { method: 'POST', body: {} })
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
      refresh()
    }
  }, [ctx, refresh])

  const changeInjectMode = useCallback(
    async value => {
      haptic('tap')
      setInjectPick(value)
      setError('')
      try {
        const result = await ctx.rest('/inject_mode', { method: 'POST', body: { mode: value } })
        if (result && result.ok === false) {
          setError(result.error || 'could not set the inject mode')
          return
        }
        // Refetch first: dropping the optimistic value before the new one is on its way
        // back would flash the old mode.
        await queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
      } catch (err) {
        setError(String((err && err.message) || err))
      } finally {
        setInjectPick('')
      }
    },
    [ctx, queryClient]
  )

  // The interval pick (0 = manual): applied to a running watch within a tick and
  // persisted for the next one — no restart anywhere.
  const changeInterval = useCallback(
    async value => {
      haptic('tap')
      setIntervalPick(value)
      setError('')
      try {
        const result = await ctx.rest('/interval', { method: 'POST', body: { interval_ms: Number(value) } })
        if (result && result.ok === false) {
          setError(result.error || 'could not set the rhythm')
          return
        }
        // Refetch first (same reason as the inject pick): no flash of the old value.
        await queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
      } catch (err) {
        setError(String((err && err.message) || err))
      } finally {
        setIntervalPick('')
      }
    },
    [ctx, queryClient]
  )

  const openSnap = useCallback(
    async source => {
      haptic('tap')
      setSnapError('')
      setSnapSel(null)
      setSnapBusy(true)
      try {
        const result = await ctx.rest('/snap/start', { method: 'POST', body: { source_id: source.id } })
        if (!result || result.ok === false) {
          setError((result && result.error) || 'could not open the snapshot')
          return
        }
        setSnap({
          session_id: result.session_id,
          label: (result.source && result.source.label) || source.label,
          kind: (result.source && result.source.kind) || source.kind || '',
          source
        })
      } catch (err) {
        setError(String((err && err.message) || err))
      } finally {
        setSnapBusy(false)
      }
    },
    [ctx]
  )

  const stopSnap = useCallback(() => {
    const held = snap
    setSnap(null)
    setSnapSel(null)
    setSnapError('')
    if (held && held.session_id) {
      // Let the backend close the session (and its camera) — nothing waits for it.
      ctx.rest('/snap/stop', { method: 'POST', body: { session_id: held.session_id } }).catch(() => {})
    }
  }, [ctx, snap])

  const doSnap = useCallback(async () => {
    if (!snap || snapBusy) {
      return
    }
    setSnapBusy(true)
    setSnapError('')
    try {
      const rect = snapSel && snapSel.w >= 0.005 && snapSel.h >= 0.005 ? snapSel : null
      const result = await ctx.rest('/snap', { method: 'POST', body: { session_id: snap.session_id, rect } })
      if (!result || result.ok === false) {
        setSnapError((result && result.error) || 'the snapshot failed')
        return
      }
      const held = snap
      // Close the overlay FIRST: open dialogs block the app's paste router, so the
      // hand-off waits for it to be gone (Radix exit animations included — hence a
      // few attempts). `defaultPrevented` is the app's own receipt that it routed.
      setSnap(null)
      setSnapSel(null)
      ctx.rest('/snap/stop', { method: 'POST', body: { session_id: held.session_id } }).catch(() => {})
      let routed = false
      for (let attempt = 0; attempt < 6 && !routed; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, attempt === 0 ? 60 : 140))
        routed = dispatchPasteToComposer(result.png_b64, result.name || 'snapshot.png')
      }
      haptic('tap')
      if (routed) {
        host.notify({ kind: 'info', message: 'Peripheral Vision → snapshot added to the message input' })
      } else {
        host.notify({
          kind: 'warning',
          message: `Peripheral Vision — the input did not take it; the snapshot is saved at ${result.path}`
        })
      }
    } catch (err) {
      setSnapError(String((err && err.message) || err))
    } finally {
      setSnapBusy(false)
    }
  }, [ctx, snap, snapBusy, snapSel])

  const onSnapPointerDown = useCallback(event => {
    if (event.button !== 0) {
      return
    }
    const box = snapBoxRef.current
    if (!box) {
      return
    }
    const rect = box.getBoundingClientRect()
    if (!rect.width || !rect.height) {
      return
    }
    snapDragRef.current = {
      x: clamp01((event.clientX - rect.left) / rect.width),
      y: clamp01((event.clientY - rect.top) / rect.height)
    }
    setSnapSel(null)
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch (err) {
      /* pointer capture is a nicety: dragging still works without it */
    }
  }, [])

  const onSnapPointerMove = useCallback(event => {
    const start = snapDragRef.current
    const box = snapBoxRef.current
    if (!start || !box) {
      return
    }
    const rect = box.getBoundingClientRect()
    if (!rect.width || !rect.height) {
      return
    }
    const x = clamp01((event.clientX - rect.left) / rect.width)
    const y = clamp01((event.clientY - rect.top) / rect.height)
    setSnapSel({
      x: Math.min(start.x, x),
      y: Math.min(start.y, y),
      w: Math.abs(x - start.x),
      h: Math.abs(y - start.y)
    })
  }, [])

  const onSnapPointerUp = useCallback(() => {
    snapDragRef.current = null
  }, [])

  const usePinnedModel = useCallback(async () => {
    if (!pinChoice) {
      return
    }
    const separator = pinChoice.indexOf('::')
    const provider = separator >= 0 ? pinChoice.slice(0, separator) : ''
    const model = separator >= 0 ? pinChoice.slice(separator + 2) : pinChoice
    setBusy(true)
    setError('')
    try {
      await ctx.rest('/vision', { method: 'POST', body: { provider, model } })
      host.notify({ kind: 'info', message: `Peripheral Vision → ${model}` })
      await queryClient.invalidateQueries({ queryKey: [ID, 'vision'] })
      await queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
    }
  }, [ctx, pinChoice, queryClient])

  const followCurrentModel = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      await ctx.rest('/vision', { method: 'POST', body: { provider: '', model: '' } })
      setPinChoice('')
      await queryClient.invalidateQueries({ queryKey: [ID, 'vision'] })
      await queryClient.invalidateQueries({ queryKey: [ID, 'status'] })
    } catch (err) {
      setError(String((err && err.message) || err))
    } finally {
      setBusy(false)
    }
  }, [ctx, queryClient])

  const recent = data.recent || []
  const shot = preview.data && preview.data.ok ? preview.data.data_url : null

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-3 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('div', {
            className: 'font-medium',
            children: 'Peripheral Vision'
          }),
          running
            ? jsxs('span', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: ['watching ', data.source_label || 'a source']
              })
            : jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'idle' })
        ]
      }),

      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: running ? 'secondary' : 'default',
            disabled: busy,
            onClick: openPicker,
            children: running ? 'Change source…' : 'Choose source…'
          }),
          jsx(Tip, {
            label: 'Which kind of vision to pick from — the source list follows it',
            children: jsx(Select, {
              value: activeMode,
              onValueChange: value => {
                haptic('tap')
                setMode(value)
              },
              children: jsxs(SelectTrigger, {
                className: 'h-6 w-28 text-xs',
                'aria-label': 'vision mode',
                children: [
                  jsx(SelectValue, {}),
                  jsx(SelectContent, {
                    children: VISION_MODES.map(option =>
                      jsx(SelectItem, { value: option.value, children: option.label }, option.value)
                    )
                  })
                ]
              })
            })
          }),
          running
            ? jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: stop, children: 'Stop' })
            : null,
          busy ? jsx(GlyphSpinner, {}) : null
        ]
      }),

      jsx('div', {
        className: 'text-xs text-(--ui-text-quaternary)',
        children: 'Nothing is captured until you pick a source in the picker — the mode picker decides which kind it lists.'
      }),

      injectState
        ? jsxs('div', {
            className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
            children: [
              jsx('span', { children: 'rides your turns' }),
              jsx(Tip, {
                label: `Sharing into turns — ${INJECT_HINT[injectMode] || INJECT_HINT.on_change} (${
                  INJECT_SOURCE[injectOrigin] || INJECT_SOURCE.default
                })`,
                children: jsx(Select, {
                  value: injectMode,
                  onValueChange: changeInjectMode,
                  children: jsxs(SelectTrigger, {
                    className: 'h-6 w-28 text-xs',
                    'aria-label': 'inject mode',
                    children: [
                      jsx(SelectValue, {}),
                      jsx(SelectContent, {
                        children: INJECT_MODES.map(option =>
                          jsx(SelectItem, { value: option.value, children: option.label }, option.value)
                        )
                      })
                    ]
                  })
                })
              })
            ]
          })
        : null,

      jsxs('div', {
        className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: 'Blink rate' }),
          jsx(Tip, {
            label:
              'How often the watch looks for changes. Manual checks nothing on its own — you snapshot on demand instead.',
            children: jsx(Select, {
              value: intervalMs,
              onValueChange: changeInterval,
              children: jsxs(SelectTrigger, {
                className: 'h-6 w-28 text-xs',
                'aria-label': 'blink rate',
                children: [jsx(SelectValue, {}), jsx(SelectContent, {
                  children: (snapshotsSupported ? INTERVALS : INTERVALS.filter(option => option.value !== '0')).map(option =>
                    jsx(SelectItem, { value: option.value, children: option.label }, option.value)
                  )
                })]
              })
            })
          }),
          manualMode && snapshotsSupported
            ? jsxs('span', {
                className: 'ml-auto flex items-center gap-2',
                children: [
                  jsx('span', { children: 'Snapshot' }),
                  jsx(SnapMenu, { ctx, sources, kind: 'cameras', disabled: busy || snapBusy, onPick: openSnap }),
                  jsx(SnapMenu, { ctx, sources, kind: 'screens', disabled: busy || snapBusy, onPick: openSnap })
                ]
              })
            : null
        ]
      }),

      jsxs('div', {
        className:
          'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-xs',
        children: [
          jsxs('span', {
            className: 'text-(--ui-text-quaternary)',
            children: [
              'describing with ',
              visionRoute.model
                ? `${visionRoute.provider ? visionRoute.provider + ' · ' : ''}${visionRoute.model}`
                : 'no model',
              visionRoute.source === 'pinned'
                ? ' (pinned)'
                : visionRoute.source === 'auxiliary'
                  ? ' (auxiliary vision)'
                  : visionRoute.active_source === 'session'
                    ? ' (this session)'
                    : ' (config default — no live session reported yet)'
            ]
          }),
          data.source_label
            ? jsx('span', {
                className: 'font-medium text-(--ui-text-tertiary)',
                children: data.source_label
              })
            : jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: 'nothing watched yet'
              }),
          visionRoute.source === 'pinned'
            ? jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: 'vision model pinned'
              })
            : null,
          visionRoute.source === 'auxiliary'
            ? jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: 'using your auxiliary vision model'
              })
            : null,
          visionRoute.source !== 'pinned' && visionRoute.fallback_model
            ? jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: `fallback if it can't see: ${visionRoute.fallback_model}`
              })
            : null,
          needsVisionModel
            ? jsxs('div', {
                className: 'flex flex-col gap-1 pt-1',
                children: [
                  jsx('span', {
                    className: 'text-(--ui-danger)',
                    children: `Live descriptions are paused — ${
                      [visionRoute.provider, visionRoute.model].filter(Boolean).join(' / ') ||
                      'the current model'
                    } can't accept images.`
                  }),
                  jsx('span', {
                    className: 'text-(--ui-text-quaternary)',
                    children:
                      "Watching keeps running, but nothing is sent to a model that can't see images. Switch this chat to a vision-capable model, or pick one below to pin it for descriptions only."
                  }),
                  visionData.detail
                    ? jsx('span', {
                        className: 'text-(--ui-text-quaternary)',
                        children: `Reason: ${String(visionData.detail).slice(0, 200)}`
                      })
                    : null,
                  visionCandidates.length > 0
                    ? jsx(Select, {
                        value: pinChoice,
                        onValueChange: setPinChoice,
                        children: jsxs(SelectTrigger, {
                          className: 'h-6 w-full text-xs',
                          children: [
                            jsx(SelectValue, { placeholder: 'choose a vision model' }),
                            jsx(SelectContent, {
                              children: visionCandidates.map(option =>
                                jsx(
                                  SelectItem,
                                  {
                                    value: `${option.provider}::${option.model}`,
                                    children: option.label
                                  },
                                  `${option.provider}::${option.model}`
                                )
                              )
                            })
                          ]
                        })
                      })
                    : jsx('span', {
                        className: 'text-(--ui-text-quaternary)',
                        children: 'No vision-capable model in your config — add one in Settings → Models, or switch this chat to a model that can see.'
                      }),
                  jsxs('div', {
                    className: 'flex items-center gap-2',
                    children: [
                      jsx(Button, {
                        size: 'sm',
                        disabled: !pinChoice || busy,
                        onClick: usePinnedModel,
                        children: 'Use this model'
                      }),
                      jsx(Button, {
                        size: 'sm',
                        variant: 'ghost',
                        disabled: busy,
                        onClick: followCurrentModel,
                        children: 'Follow current model'
                      })
                    ]
                  })
                ]
              })
            : null
        ]
      }),

      error
        ? jsx('div', { className: 'text-xs text-(--ui-danger)', children: error })
        : null,
      data.error
        ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: data.error })
        : null,
      data.waiting
        ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: data.waiting })
        : null,

      running
        ? jsxs('div', {
            className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
            children: [
              jsx('span', { children: `${data.count || 0} descriptions` }),
              data.last_seconds
                ? jsx('span', { children: `· ${data.last_seconds}s per frame` })
                : null
            ]
          })
        : null,

      shot
        ? jsx('img', {
            src: shot,
            alt: 'watched display (sensitive)',
            className: 'w-full rounded border border-(--ui-stroke-secondary)'
          })
        : null,

      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto rounded border border-(--ui-stroke-secondary) p-2 text-xs',
        children:
          recent.length > 0
            ? jsx('div', {
                className: 'flex flex-col gap-2',
                children: recent.map((entry, index) =>
                  jsxs(
                    'div',
                    {
                      className: 'flex flex-col gap-0.5',
                      children: [
                        jsx('span', {
                          className: 'text-(--ui-text-quaternary)',
                          children: entry.iso || ''
                        }),
                        jsx('span', { children: entry.description })
                      ]
                    },
                    `${entry.ts}-${index}`
                  )
                )
              })
            : jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: running ? 'waiting for the first change…' : 'no descriptions yet'
              })
      }),

      jsx(Dialog, {
        open: pickerOpen,
        onOpenChange: setPickerOpen,
        children: jsx(DialogContent, {
          className: 'max-w-md',
          children: jsxs('div', {
            className: 'flex flex-col gap-3',
            children: [
              jsxs(DialogHeader, {
                children: [
                  jsx(DialogTitle, { children: 'What should I watch?' }),
                  jsxs('div', {
                    className: 'flex items-start justify-between gap-3',
                    children: [
                      jsx(DialogDescription, {
                        children: 'Choose a whole display, one application window, or a camera. A window can be watched while it is behind another app; nothing is captured until you confirm.'
                      }),
                      jsx('button', {
                        type: 'button',
                        onClick: () => ctx.rest('/sources?refresh=1').then(() => sources.refetch()),
                        className: 'shrink-0 rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-xs hover:bg-(--chrome-action-hover)',
                        children: 'Refresh'
                      })
                    ]
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex max-h-80 flex-col gap-2 overflow-y-auto',
                children: [
                  sources.isError
                    ? jsxs('div', {
                        className: 'flex flex-col gap-1',
                        children: [
                          jsx('span', {
                            className: 'text-(--ui-danger)',
                            children: 'The backend did not list any source.'
                          }),
                          jsx('span', {
                            className: 'text-(--ui-text-quaternary)',
                            children: String(
                              (sources.error && sources.error.message) || sources.error || 'no detail'
                            )
                          }),
                          jsx('span', {
                            className: 'text-(--ui-text-quaternary)',
                            children:
                              'If the plugin was just edited, the running backend still holds the old code — restart Hermes so the new routes load.'
                          })
                        ]
                      })
                    : null,
                  sources.isLoading && !sources.data
                    ? jsxs('div', {
                        className: 'flex items-center gap-2 text-xs text-(--ui-text-quaternary)',
                        children: [jsx(GlyphSpinner, {}), jsx('span', { children: 'asking the backend…' })]
                      })
                    : null,
                  // One section at a time — the mode picker beside the source button
                  // decides which, so the list never makes the eye scroll past the
                  // kinds it is not choosing from.
                  activeMode === 'displays'
                    ? jsx('div', {
                        className: 'text-[11px] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
                        children: 'Displays'
                      })
                    : null,
                  activeMode === 'displays'
                    ? (sources.data && sources.data.monitors ? sources.data.monitors : []).map(
                        source =>
                          jsx(
                            SourceRow,
                            { source, selected: chosen && chosen.id === source.id, onSelect: setChosen },
                            source.id
                          )
                      )
                    : null,
                  activeMode === 'windows'
                    ? jsx('div', {
                        className: 'text-[11px] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
                        children: 'Application windows'
                      })
                    : null,
                  activeMode === 'windows'
                    ? (sources.data && sources.data.windows && sources.data.windows.length
                        ? sources.data.windows
                        : []
                      ).map(
                        source =>
                          jsx(
                            WindowRow,
                            { source, selected: chosen && chosen.id === source.id, onSelect: setChosen, ctx },
                            source.id
                          )
                      )
                    : null,
                  activeMode === 'windows' &&
                  sources.data && sources.data.windows && sources.data.windows.length === 0
                    ? jsx('div', {
                        className: 'text-xs text-(--ui-text-quaternary)',
                        children: 'No application windows were found.'
                      })
                    : null,
                  activeMode === 'cameras'
                    ? jsx('div', {
                        className: 'text-[11px] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
                        children: 'Cameras'
                      })
                    : null,
                  activeMode === 'cameras'
                    ? (sources.data && sources.data.cameras ? sources.data.cameras : []).map(
                        source =>
                          jsx(
                            SourceRow,
                            { source, selected: chosen && chosen.id === source.id, onSelect: setChosen },
                            source.id
                          )
                      )
                    : null,
                  activeMode === 'cameras' &&
                  sources.data && sources.data.cameras && sources.data.cameras.length === 0
                    ? jsxs('div', {
                        className: 'flex items-center gap-2 text-xs text-(--ui-text-quaternary)',
                        children: [
                          sources.data.cameras_probing ? jsx(GlyphSpinner, {}) : null,
                          jsx('span', {
                            children: sources.data.cameras_probing
                              ? 'scanning for cameras…'
                              : 'No camera was detected.'
                          })
                        ]
                      })
                    : null,
                  activeMode === 'cameras'
                    ? jsx('div', {
                        className: 'text-xs text-(--ui-text-quaternary)',
                        children: 'A camera frame is described and sent like any other capture — the camera LED stays on while it is watched, and turns off when watching stops.'
                      })
                    : null
                    ]
                  }),
              jsx(DialogFooter, {
                children: jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx(Button, {
                      variant: 'ghost',
                      onClick: () => setPickerOpen(false),
                      children: 'Cancel'
                    }),
                    Tip
                      ? jsx(Tip, {
                          label: chosen ? `${chosen.label} · ${chosen.width}×${chosen.height}` : 'Pick a display first',
                          children: jsx('span', {
                            children: jsx(Button, {
                              disabled: !chosen || busy,
                              onClick: start,
                              children: 'Start watching'
                            })
                          })
                        })
                      : jsx(Button, { disabled: !chosen || busy, onClick: start, children: 'Start watching' })
                  ]
                })
              })
            ]
          })
        })
      }),

      // The live snapshot overlay: a feed of the chosen source with a drag-to-crop
      // selection. Snap closes it first, then hands the PNG to the chat input.
      snap
        ? jsx(Dialog, {
            open: true,
            onOpenChange: open => {
              if (!open) {
                stopSnap()
              }
            },
            children: jsx(DialogContent, {
              className: 'max-w-3xl',
              style: snap.kind === 'monitor' ? { maxWidth: 'min(96vw, 1500px)' } : undefined,
              children: jsxs('div', {
                className: 'flex flex-col gap-3',
                children: [
                  jsxs(DialogHeader, {
                    children: [
                      jsx(DialogTitle, { children: `Snapshot — ${snap.label}` }),
                      jsx(DialogDescription, {
                        children: snapStill
                          ? snap.kind === 'monitor'
                            ? 'Full screen, at full resolution — the whole display. Drag over the image to choose an area; the crop goes to the message input when you snap, and without a selection the whole frame does.'
                            : 'Full-resolution capture of the window. Drag over the image to choose an area; the crop goes to the message input when you snap, and without a selection the whole frame does.'
                          : 'Live view. Drag over the image to choose an area — the crop goes to the message input when you snap; without a selection the whole frame does.'
                      })
                    ]
                  }),
                  jsxs('div', {
                    ref: snapBoxRef,
                    className:
                      'relative select-none overflow-hidden rounded-md border border-(--ui-stroke-secondary) bg-black/20',
                    style: snapStill
                      ? { touchAction: 'none', cursor: 'crosshair', width: 'fit-content', margin: '0 auto' }
                      : { touchAction: 'none', cursor: 'crosshair' },
                    onPointerDown: onSnapPointerDown,
                    onPointerMove: onSnapPointerMove,
                    onPointerUp: onSnapPointerUp,
                    children: [
                      frameUrl
                        ? jsx('img', {
                            src: frameUrl,
                            alt: snapStill ? 'snapshot view' : 'live snapshot view',
                            draggable: false,
                            className: snapStill ? 'block select-none' : 'block w-full select-none',
                            style: snapStill ? { maxWidth: '100%', maxHeight: '72vh' } : undefined
                          })
                        : jsx('div', {
                            className:
                              'flex h-44 items-center justify-center px-4 text-center text-xs text-(--ui-text-quaternary)',
                            children: frameNote || 'waiting for a frame…'
                          }),
                      snapSel
                        ? jsx('div', {
                            className: 'pointer-events-none absolute border-2 border-(--ui-accent)',
                            style: {
                              left: pctOf(snapSel.x),
                              top: pctOf(snapSel.y),
                              width: pctOf(snapSel.w),
                              height: pctOf(snapSel.h)
                            }
                          })
                        : null
                    ]
                  }),
                  frameUrl && frameNote
                    ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: frameNote })
                    : null,
                  snapError
                    ? jsx('div', { className: 'text-xs text-(--ui-danger)', children: snapError })
                    : null,
                  jsx(DialogFooter, {
                    children: jsxs('div', {
                      className: 'flex items-center gap-2',
                      children: [
                        jsx(Button, { variant: 'ghost', onClick: stopSnap, children: 'Cancel' }),
                        snapStill
                          ? jsx(Button, {
                              variant: 'ghost',
                              disabled: snapBusy,
                              onClick: () => snap.source && openSnap(snap.source),
                              children: 'Retake'
                            })
                          : null,
                        jsx(Button, {
                          disabled: snapBusy,
                          onClick: doSnap,
                          children:
                            snapSel && snapSel.w >= 0.005 && snapSel.h >= 0.005
                              ? 'Snap selection'
                              : 'Snap whole frame'
                        })
                      ]
                    })
                  })
                ]
              })
            })
          })
        : null
    ]
  })
}

// ---------------------------------------------------------------------------
// Statusbar chip — the watch at a glance, and one click to its pane.
//
// Own stylesheet rather than Tailwind utilities: this file loads at RUNTIME, so a
// utility the app's build never saw would simply not exist (Radio does the same).
// ---------------------------------------------------------------------------

const CHIP_CSS = `
.hermes-pv-chip{gap:4px;padding:0 4px;font-size:0.6875rem;line-height:1;color:var(--ui-text-tertiary)}
.hermes-pv-chip:hover{color:var(--ui-text-primary)}
.hermes-pv-chip.is-live{color:var(--ui-accent)}
.hermes-pv-chip.is-open{color:var(--ui-text-primary)}
.hermes-pv-chip-icon{display:flex;align-items:center;justify-content:center;width:12px;height:12px;flex-shrink:0;overflow:hidden}
.hermes-pv-chip-icon svg{width:12px;height:12px}
.hermes-pv-chip-label{white-space:nowrap;font-variant-numeric:tabular-nums}
`

function VisionChip({ ctx }) {
  // The SAME query key the pane uses, so React Query serves both from one poll.
  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: 2500
  })
  const data = status.data || {}
  const running = Boolean(data.running)
  const open = useValue(PANE_VISIBLE)
  const source = data.source_label || 'a source'
  // "waiting" and "source gone" have to be distinguishable from "off" at a glance:
  // a watch that is up but describing nothing must not look like a healthy one.
  const label = !running
    ? 'Vision'
    : data.source_missing
      ? 'Vision · source gone'
      : data.waiting
        ? 'Vision · waiting'
        : `Vision · ${data.count || 0}`
  const detail = data.source_missing
    ? `the watched source is gone (${source})`
    : data.waiting
      ? data.waiting
      : `watching ${source}`
  // The pane frame's own Close is NOT the pair for revealPane: for a plugin that
  // contributes a single pane it calls closeTreePane -> setPluginEnabled(false),
  // which unregisters the whole plugin and takes this chip down with it (the app
  // toasts "Plugin "peripheral-vision" disabled" and sends the user to
  // Capabilities → Plugins). So the chip asks for the dismiss half explicitly,
  // feature-detected: without it the click stays "bring the pane forward" and
  // nothing regresses on a desktop that predates it.
  const canHide = typeof host.dismissPane === 'function'
  const verb = open ? (canHide ? 'hide the pane' : 'bring the pane forward') : 'open the pane'
  const title = running
    ? `Peripheral Vision — ${detail}, ${data.count || 0} described, ${data.skipped || 0} unchanged. Click to ${verb}.`
    : `Peripheral Vision is off — click to ${verb}.`

  return jsx(Tip, {
    label: title,
    children: jsxs(Button, {
      variant: 'ghost',
      size: 'micro',
      className: `hermes-pv-chip${running ? ' is-live' : ''}${open ? ' is-open' : ''}`,
      'aria-label': title,
      'data-pv-state': running ? (data.source_missing || data.waiting ? 'waiting' : 'live') : 'off',
      onClick: () => {
        haptic('tap')
        // The toggle: dismiss when it is on screen, reveal when it is away.
        // dismissTreePane hides a contributed pane and REMEMBERS the dismissal
        // while the plugin stays loaded, so the chip survives to reopen it;
        // revealTreePane (revealPane) un-dismisses, adopts one a layout swap
        // dropped, un-hides it and un-collapses its zone — one call covers every
        // way it can be away.
        if (open && canHide) {
          host.dismissPane(PANE_ID)

          return
        }
        if (typeof host.revealPane === 'function') {
          host.revealPane(PANE_ID)
        } else {
          host.notify({ kind: 'info', message: 'Peripheral Vision — open the pane from the layout menu' })
        }
      },
      children: [
        jsx('span', { className: 'hermes-pv-chip-icon', children: jsx(running ? icons.Eye : icons.EyeOff, {}) }),
        jsx('span', { className: 'hermes-pv-chip-label', children: label })
      ]
    })
  })
}

export default {
  id: ID,
  name: 'Peripheral Vision',
  // Opt-in on purpose: this plugin captures screen content and camera frames, so it ships
  // off and the user turns it on in Settings → Plugins. An explicit user choice always wins
  // over this default (contrib/plugins-store.ts), so enabling it once stays enabled.
  defaultEnabled: false,
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CHIP_CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Peripheral Vision',
      data: { placement: 'right', width: '270px' },
      render: () => jsx(PeripheralVisionPane, { ctx })
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 20,
      render: () => jsx(VisionChip, { ctx })
    })
  }
}
