#!/usr/bin/env python3
"""
tmux-dashboard server
Usage: python3 tmux_server.py
Serves tmux state as JSON on http://localhost:8765/api/tmux
"""

import subprocess
import json
import shlex
import re
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

_ANSI = re.compile(r'\x1b\[[0-9;]*m')
HOME = os.path.expanduser("~")
# Cap on concurrent capture-pane subprocesses per request. Captures run in
# threads; run() spends its time in subprocess (GIL released), so this gives
# near-linear speedup and bounds total wall time to ~one slow capture.
MAX_CAPTURE_WORKERS = 16

PORT = 8765
PREVIEW_LINES = 5
# A single tmux call is capped well under the frontend's fetch-abort window
# (8s) so one stuck capture-pane can't blow the whole /api/tmux response.
RUN_TIMEOUT = 2
# How many trailing lines to capture per pane. Bounded on purpose: a heavy
# full-screen TUI (e.g. Claude Code) can fill the whole visible pane, and we
# only ever keep the last PREVIEW_LINES non-empty ones. Capturing a small
# bottom slice keeps each call cheap regardless of pane height / activity.
CAPTURE_LINES = 20


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        return r.stdout.strip()
    except Exception:
        return ""


def capture_cmd(target):
    # -S -N / -E -1 bounds the capture to the bottom N lines instead of the
    # full visible pane, so cost is independent of how much the app redraws.
    # shlex.quote(target) so a session name containing a quote/space can't
    # break out of the -t argument.
    return (
        f"tmux capture-pane -t {shlex.quote(target)} -p -J "
        f"-S -{CAPTURE_LINES} -E -1 2>/dev/null"
    )


def fetch_preview(target):
    # Capture the pane, keep the last PREVIEW_LINES non-empty lines, strip ANSI.
    # Must never raise: this runs inside a parallel map(), and an exception
    # there would abort the entire /api/tmux response (→ 500 → dashboard flash).
    try:
        raw = run(capture_cmd(target))
        lines = [l for l in raw.splitlines() if l.strip()][-PREVIEW_LINES:]
        return [_ANSI.sub('', l).rstrip() for l in lines]
    except Exception:
        return []


def parse_pane_fields(line, host):
    # Field order MUST match the list-panes -F format below. pane_title is
    # user-controlled and may itself contain the '|' delimiter, so it is the
    # LAST field and we split with maxsplit=6 to keep it intact.
    parts = line.split("|", 6)
    if len(parts) < 7:
        return None
    pidx, pactive, pcmd, ppath, pwidth, pheight, ptitle = parts
    # default pane_title is the hostname — treat that as "no custom title"
    # so we fall back to the running command in the UI.
    title = ptitle if (ptitle and ptitle != host) else ""
    return {
        "index": pidx,
        "active": pactive,
        "command": pcmd,
        "path": ppath,
        "width": pwidth,
        "height": pheight,
        "title": title,
    }


def get_tmux_state():
    sessions_raw = run("tmux list-sessions -F '#{session_name}|#{session_windows}|#{session_attached}'")
    if not sessions_raw:
        return {"error": "no tmux sessions found", "sessions": []}

    current_session = run("tmux display-message -p '#{session_name}'")
    current_window = run("tmux display-message -p '#{window_index}'")
    current_pane   = run("tmux display-message -p '#{pane_index}'")
    host           = run("tmux display-message -p '#{host_short}'")

    sessions = []
    preview_jobs = []   # (target, pane_dict) — previews fetched in parallel below
    for sline in sessions_raw.splitlines():
        parts = sline.split("|")
        if len(parts) < 3:
            continue
        sname, swin, sattached = parts[0], parts[1], parts[2]

        windows_raw = run(
            f"tmux list-windows -t '{sname}' "
            f"-F '#{{window_index}}|#{{window_name}}|#{{window_active}}|#{{window_panes}}|#{{window_layout}}'"
        )

        windows = []
        for wline in windows_raw.splitlines():
            wparts = wline.split("|")
            if len(wparts) < 4:
                continue
            widx, wname, wactive, wpanes = wparts[0], wparts[1], wparts[2], wparts[3]

            panes_raw = run(
                f"tmux list-panes -t '{sname}:{widx}' "
                f"-F '#{{pane_index}}|#{{pane_active}}|#{{pane_current_command}}|#{{pane_current_path}}|#{{pane_width}}|#{{pane_height}}|#{{pane_title}}'"
            )

            panes = []
            for pline in panes_raw.splitlines():
                f = parse_pane_fields(pline, host)
                if f is None:
                    continue
                pidx, pactive, pcmd, ppath, pwidth, pheight = (
                    f["index"], f["active"], f["command"],
                    f["path"], f["width"], f["height"],
                )

                short_path = ppath.replace(HOME, "~")

                is_current = (
                    sname == current_session
                    and widx == current_window
                    and pidx == current_pane
                )

                pane = {
                    "index": pidx,
                    "active": pactive == "1",
                    "current": is_current,
                    "command": pcmd,
                    "title": f["title"],
                    "path": short_path,
                    "size": f"{pwidth}×{pheight}",
                    "preview": [],   # filled in parallel after the tree is built
                }
                panes.append(pane)
                preview_jobs.append((f"{sname}:{widx}.{pidx}", pane))

            windows.append({
                "index": widx,
                "name": wname,
                "active": wactive == "1",
                "pane_count": int(wpanes),
                "panes": panes,
            })

        sessions.append({
            "name": sname,
            "window_count": int(swin),
            "attached": sattached == "1",
            "current": sname == current_session,
            "windows": windows,
        })

    # Fetch all pane previews in parallel — this is the expensive part (one
    # capture-pane subprocess each). Serial, ~14 panes under load could spike
    # past the frontend's abort window; parallel bounds it to ~one capture.
    if preview_jobs:
        workers = min(MAX_CAPTURE_WORKERS, len(preview_jobs))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for pane, preview in zip(
                (p for _, p in preview_jobs),
                ex.map(lambda job: fetch_preview(job[0]), preview_jobs),
            ):
                pane["preview"] = preview

    return {"sessions": sessions}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress access logs

    def do_GET(self):
        try:
            if self.path == "/api/tmux":
                payload = json.dumps(get_tmux_state()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            # client (dashboard) aborted the fetch before we finished writing
            # — e.g. AbortSignal.timeout on a slow poll. Harmless; drop quietly.
            self.close_connection = True


if __name__ == "__main__":
    print(f"  tmux dashboard server running → http://localhost:{PORT}/api/tmux")
    print(f"  Ctrl-C to stop\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
