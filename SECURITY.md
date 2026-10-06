# Security policy

## Supported versions

The latest tagged release and `main`. Fixes land on `main` first and ship in a patch release.

## What this plugin can reach

Peripheral Vision captures pixels, so the honest frame is: **a running watch is screen sharing.**
Anything that can reach the Hermes dashboard can reach the plugin's routes, and those routes are
what serve the preview and the status text.

| Surface | Reality |
|---|---|
| Screen and window pixels | Captured only while a source is selected in the pane. Every changed frame is encoded and sent to the vision model in use — on a cloud provider, that is a third-party API. |
| Window titles | Listed as source metadata (the same class of information a task manager shows). They are readable through the source list whenever the dashboard is reachable, and are *not* gated on a pane selection. |
| Descriptions | Written to `log.jsonl` in the plugin's state directory, and injected into the session transcript (the host stores the exact bytes sent to the model). |
| Cameras | Opened only when explicitly selected; the device is released on stop so the indicator does not stay lit. |

## The content gate

Pixel-derived content is gated on an explicit pane selection:

- The pane re-publishes its selection every 10 seconds with a **45-second TTL**, so a closed or
  crashed pane closes the gate by itself.
- `/preview`, `/start`, `/snap/start`, `/snap/frame`, `/snap`, and the description text inside
  `/status` refuse with `selection_required` unless that selection is live. A dashboard session
  that never opened the pane gets an empty ring buffer, never your screen.
- Teardown stays reachable while gated (`stop`, interval, inject mode, vision settings) so a
  watch can always be stopped.
- Access control is inherited from the dashboard: if the dashboard is exposed, these routes are
  exposed with it. There is no second password.

**Deployment guidance:** do not expose the Hermes dashboard to an untrusted network while a watch
is running. Bind it to loopback or a private network (LAN/Tailscale), and treat the dashboard's
own auth as the outer boundary it is. Note that the gate is session-based and time-limited, not
per-user: any client that can reach the dashboard and open the pane can select a source.

## Secrets

The plugin stores no credentials. Model access comes from the Hermes provider configuration —
never put an API key in this plugin's environment, state directory, or an issue report.

## Reporting a vulnerability

Please **do not open a public issue** for anything that could expose captured content or weaken
the gate. Use GitHub's private reporting instead:

**https://github.com/MaJorDan383/peripheral-vision/security/advisories/new**

Include what you did, what you observed, and the versions involved (plugin version from
`plugin.yaml`, Hermes version from `hermes --version`). This is a single-maintainer project with
no SLA; expect an acknowledgement and a best-effort fix, and a credit in the release notes if you
would like one.

## Redacting a report

Before pasting anything publicly (issues, PRs, chat): strip API keys and tokens, and redact
window titles, file paths, or screen regions you would not want indexed. `log.jsonl` holds recent
descriptions — check it before attaching it.
