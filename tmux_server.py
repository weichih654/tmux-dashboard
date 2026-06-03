#!/usr/bin/env python3
"""
tmux-dashboard server
Usage: python3 tmux_server.py
Serves tmux state as JSON on http://localhost:8765/api/tmux
"""

import subprocess
import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = 8765
PREVIEW_LINES = 5


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def get_tmux_state():
    sessions_raw = run("tmux list-sessions -F '#{session_name}|#{session_windows}|#{session_attached}'")
    if not sessions_raw:
        return {"error": "no tmux sessions found", "sessions": []}

    current_session = run("tmux display-message -p '#{session_name}'")
    current_window = run("tmux display-message -p '#{window_index}'")
    current_pane   = run("tmux display-message -p '#{pane_index}'")

    sessions = []
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
                f"-F '#{{pane_index}}|#{{pane_active}}|#{{pane_current_command}}|#{{pane_current_path}}|#{{pane_width}}|#{{pane_height}}'"
            )

            panes = []
            for pline in panes_raw.splitlines():
                pparts = pline.split("|")
                if len(pparts) < 6:
                    continue
                pidx, pactive, pcmd, ppath, pwidth, pheight = pparts

                # capture last N non-empty lines
                raw_preview = run(
                    f"tmux capture-pane -t '{sname}:{widx}.{pidx}' -p -J 2>/dev/null"
                )
                preview_lines = [
                    l for l in raw_preview.splitlines() if l.strip()
                ][-PREVIEW_LINES:]

                import re
                ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
                preview_lines = [ansi_escape.sub('', l).rstrip() for l in preview_lines]

                # shorten path
                import os
                home = os.path.expanduser("~")
                short_path = ppath.replace(home, "~")

                is_current = (
                    sname == current_session
                    and widx == current_window
                    and pidx == current_pane
                )

                panes.append({
                    "index": pidx,
                    "active": pactive == "1",
                    "current": is_current,
                    "command": pcmd,
                    "path": short_path,
                    "size": f"{pwidth}×{pheight}",
                    "preview": preview_lines,
                })

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
