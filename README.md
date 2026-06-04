# tmux.dash

A live web dashboard for tmux. See every session, window, and pane at a glance — with live previews, activity indicators, and a click-to-zoom modal.

![status](https://img.shields.io/badge/python-3.9%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live overview** — all sessions → windows → panes rendered as cards, auto-refreshing (1–10s, adjustable)
- **Pane previews** — last 5 lines of each pane, ANSI-stripped
- **Pane zoom** — click any pane card to open a floating modal with 50 lines of live, auto-updating content; close with `ESC`, backdrop click, or `✕`
- **Activity glow** — panes with new output light up amber, then fade back to normal slowly over 60 seconds; new activity instantly restores the glow
- **Dual-mode activity detection** — uses tmux-native `pane_last_activity` when the running tmux supports it (detected automatically); otherwise falls back to content-hash comparison between polls
- **HERE marker** — shows which pane your cursor actually sits in
- **Pane titles** — custom titles (`select-pane -T`) shown as the main label, running command as a tag
- **Collapsible** — fold sessions and windows you don't care about
- **Demo mode** — `?demo=true` renders simulated data when no server is running

## Quick start

```bash
# 1. start the server (requires a running tmux with at least one session)
python3 tmux_server.py

# 2. open the dashboard
open tmux_dashboard.html        # macOS
xdg-open tmux_dashboard.html    # Linux
```

The server listens on `127.0.0.1:8765` and the page polls it every 2 seconds by default.

## Requirements

- Python 3.9+ (standard library only — no pip installs)
- tmux (any modern version; ≥ the version that ships `pane_last_activity` enables native activity detection automatically)
- A browser

## How it works

```
┌──────────────────┐     GET /api/tmux      ┌─────────────────┐     list-sessions
│ tmux_dashboard   │ ─────────────────────▶ │ tmux_server.py  │ ──▶ list-windows
│ .html (browser)  │ ◀───────────────────── │ (port 8765)     │ ──▶ list-panes
└──────────────────┘     JSON state          └─────────────────┘ ──▶ capture-pane ×N
        │                                            ▲                (parallel)
        └──────────── GET /api/pane?target=… ────────┘
                      (zoom modal, 50 lines)
```

- Pane captures run in a thread pool (bounded at 16 workers) so response time stays ~one capture regardless of pane count.
- Captures are bounded to the bottom 20 lines (`-S -N`), so cost is independent of scrollback size or how busy a TUI is.
- The frontend guards against overlapping polls and tolerates transient failures (2 consecutive failures before showing an error).

### Activity detection

On startup the server probes whether the running tmux expands `#{pane_last_activity}`:

| tmux support | mode   | how activity is detected                          |
|--------------|--------|---------------------------------------------------|
| yes          | `tmux` | native per-pane activity timestamp (epoch)        |
| no           | `hash` | preview content hashed and compared between polls |

The response carries `activity_source` so the frontend picks the right mode automatically. Only a confirmed-positive probe is cached — a transient failure can't lock the server into hash mode.

To check what your tmux supports:

```bash
tmux display-message -p '#{?#{pane_last_activity},YES,NO}'
```

## API

| Endpoint                  | Response                                              |
|---------------------------|-------------------------------------------------------|
| `GET /api/tmux`           | full state: sessions / windows / panes + previews + `activity_source` |
| `GET /api/pane?target=S:W.P` | `{"lines": [...]}` — last 50 non-empty lines of that pane |

## Tests

```bash
python3 -m unittest test_dashboard -v
```

## License

[MIT](LICENSE)
