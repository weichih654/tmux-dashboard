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
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_ANSI = re.compile(r'\x1b\[[0-9;]*m')
HOME = os.path.expanduser("~")
# Cap on concurrent capture-pane subprocesses per request. Captures run in
# threads; run() spends its time in subprocess (GIL released), so this gives
# near-linear speedup and bounds total wall time to ~one slow capture.
MAX_CAPTURE_WORKERS = 16

PORT = 8765
PREVIEW_LINES = 5
ZOOM_LINES = 50
# A single tmux call is capped well under the frontend's fetch-abort window
# (8s) so one stuck capture-pane can't blow the whole /api/tmux response.
RUN_TIMEOUT = 2
# How many trailing lines to capture per pane. Bounded on purpose: a heavy
# full-screen TUI (e.g. Claude Code) can fill the whole visible pane, and we
# only ever keep the last PREVIEW_LINES non-empty ones. Capturing a small
# bottom slice keeps each call cheap regardless of pane height / activity.
CAPTURE_LINES = 20
# How many trailing NON-EMPTY lines detect_waiting() scans. Big enough for a
# multi-line prompt UI (question + options + hint line), small enough that a
# prompt the user already answered — scrolled up by later output — can't
# re-trigger the waiting state.
WAITING_SCAN_LINES = 8

# Prompt patterns that mean an AI agent (or any CLI) stopped and is waiting
# for the user to decide. Matched per line against the ANSI-stripped tail of
# the capture. Sources: Claude Code, Codex, Copilot CLI, Opencode + generic
# y/n prompts.
#
# Pane content is arbitrary (logs, docs, code, shell prompts), so every
# pattern is anchored to the SHAPE of a real prompt line — substring-style
# matching false-positives on prose ("Do you want to merge later"), logs
# ("Allowed origins? checking config") and starship/pure shell prompts
# ("❯ 3.txt"). Note "esc to cancel" (waiting) vs "esc to interrupt"
# (working) — only the former may match.
#
# STRONG patterns prove a live prompt on their own: question lines, hint
# footers, y/n tails. A live dialog always shows at least one (Claude's
# menus keep their "Enter to select …" footer at the very bottom).
WAITING_STRONG = [re.compile(p) for p in (
    # Claude Code — question line: "Do you want to proceed?" (own line,
    # ends with ?)
    r"^\s*Do you want to .*\?\s*$",
    # Claude Code / Copilot — hint line, either line-start or after a
    # middot separator ("↑/↓ to navigate · Enter to select · Esc to cancel")
    r"(?:^\s*|·\s*)Enter to select",
    r"(?i)(?:^\s*|·\s*|\()esc to cancel",
    # Codex — option lines "▌ Yes (y)" / "No, and tell Codex … (n)"
    r"(?i)^\W*yes \(y\)\s*$",
    r"^\W*No,?\s.*\(n\)\s*$",
    r"(?:^\s*|·\s*)Press Enter to confirm",
    # Codex / Copilot — approval question: line starts with "Allow",
    # ends with "?" ("Allow command?", "Allow this command?")
    r"^\s*Allow .*\?\s*$",
    # Opencode — permission dialog title
    r"(?i)^\s*permission required",
    # generic — y/n prompts end the line
    r"(?i)\(y/n\)\s*\??\s*$",
    r"\[Y/n\]\s*$",
    r"\[y/N\]\s*$",
    # generic — standalone Proceed?/Continue? line. Must be the WHOLE line:
    # agents stream narration ending in "…continue?" ("Tests pass. Should I
    # continue?") which is prose, not a prompt.
    r"(?i)^(proceed|continue)\?$",
)]

# WEAK patterns are option-SHAPED lines that also appear outside live
# prompts — Claude Code collapses an ANSWERED question into a one-line echo
# ("❯ 1. A 2. B 3. C") that lingers in the transcript, and docs/checklists
# contain "1. Yes" lines. They carry no decision weight (a live dialog
# always has a STRONG line, so STRONG-only deciding loses nothing); kept
# for the flat debug view below.
WAITING_WEAK = [re.compile(p) for p in (
    # Claude Code — selected menu option: "❯ 1. Yes". \s after the dot
    # rejects "❯ 1.2.3" / "❯ 3.txt" typed at a ❯-themed shell prompt.
    r"^\s*❯\s*\d+\.\s+\S",
    # Copilot — bare menu option: "1. Yes" / "2. No" (nothing after, so
    # numbered prose like "1. Yes it compiled" doesn't count)
    r"^\s*\d+\.\s+(Yes|No)\s*$",
)]

# Backwards-compatible flat view (debug tooling iterates this).
WAITING_PATTERNS = WAITING_STRONG + WAITING_WEAK

# Box-drawing characters Claude Code (and other TUIs) draw dialog borders
# with. Stripped from line edges before matching so the line-start/-end
# anchors in WAITING_PATTERNS see "❯ 1. Yes", not "│ ❯ 1. Yes      │".
# Border-only lines (╭────╮) strip to empty and don't eat the scan window.
_BOX_EDGE = "│┃║┆┇┊┋╎╏|╭╮╰╯┌┐└┘╔╗╚╝─━═"


def detect_waiting(lines):
    # True when the tail of the pane looks like a prompt waiting for the
    # user. Only the last WAITING_SCAN_LINES non-empty lines count — blank
    # and border-only lines are skipped so a boxed prompt is still seen,
    # and anything older is treated as already-answered scrollback.
    # Only STRONG patterns decide — WEAK (option-shaped) lines linger in
    # transcripts after the question was answered; see the pattern comments.
    tail = []
    for l in lines:
        cleaned = l.strip().strip(_BOX_EDGE).strip()
        if cleaned:
            tail.append(cleaned)
    tail = tail[-WAITING_SCAN_LINES:]
    return any(p.search(l) for l in tail for p in WAITING_STRONG)


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        return r.stdout.strip()
    except Exception:
        return ""


# Cached result of the pane_last_activity capability probe. None = not yet
# confirmed; True once confirmed. tmux >= the version that added the variable
# expands it to an epoch number; older tmux (e.g. 3.5) leaves it empty.
# Only a confirmed-positive is cached — a negative/empty result (which a
# transient tmux failure also produces) is NOT cached, so one bad probe can't
# lock the server into hash mode forever, and a later tmux upgrade is picked up.
_PANE_LAST_ACTIVITY_SUPPORT = None


def tmux_supports_pane_last_activity():
    # Ask tmux to expand #{pane_last_activity}. A tmux that knows the variable
    # returns an epoch integer; an unknown format variable expands to empty.
    # Once confirmed, the result is cached so the probe stops running a
    # subprocess on every /api/tmux request.
    global _PANE_LAST_ACTIVITY_SUPPORT
    if _PANE_LAST_ACTIVITY_SUPPORT is None:
        out = run("tmux display-message -p '#{pane_last_activity}'")
        if out.strip().isdigit():
            _PANE_LAST_ACTIVITY_SUPPORT = True
    return _PANE_LAST_ACTIVITY_SUPPORT is True


def capture_cmd(target, nlines=None):
    # -S -N starts the capture N lines into the history; the end is left at
    # the default (bottom of the visible pane). NOT -E -1: when scrollback
    # exists, -E -1 ends at the last HISTORY line and excludes the entire
    # visible screen — previews go stale and prompts become invisible.
    # Cost stays bounded: N history lines + one screenful.
    # shlex.quote(target) so a session name containing a quote/space can't
    # break out of the -t argument.
    n = nlines if nlines is not None else CAPTURE_LINES
    return (
        f"tmux capture-pane -t {shlex.quote(target)} -p -J "
        f"-S -{n} 2>/dev/null"
    )


def fetch_pane_state(target):
    # ONE capture → the preview (last PREVIEW_LINES non-empty lines,
    # ANSI-stripped), the waiting flag (prompt detection on the tail) and a
    # content hash over the WHOLE capture for hash-mode activity detection.
    # The hash must cover more than the preview: Claude Code's spinner/timer
    # line ticks ABOVE the static status-bar lines that fill the preview, so
    # a preview-only hash goes blind while the agent thinks.
    # Must never raise: this runs inside a parallel map(), and an exception
    # there would abort the entire /api/tmux response (→ 500 → dashboard flash).
    try:
        raw = run(capture_cmd(target))
        lines = [_ANSI.sub('', l).rstrip() for l in raw.splitlines() if l.strip()]
        return {
            "preview": lines[-PREVIEW_LINES:],
            "waiting": detect_waiting(lines),
            "content_hash": format(zlib.crc32("\n".join(lines).encode()), "x"),
        }
    except Exception:
        return {"preview": [], "waiting": False, "content_hash": ""}


def fetch_preview(target):
    # Preview-only view of fetch_pane_state — kept for compatibility.
    return fetch_pane_state(target)["preview"]


def fetch_zoom(target):
    # Like the preview but captures ZOOM_LINES for the zoom modal (and does
    # its own strip/slice — no waiting detection needed here).
    # Must never raise — called from the HTTP handler on the main thread.
    try:
        raw = run(capture_cmd(target, ZOOM_LINES))
        lines = [l for l in raw.splitlines() if l.strip()][-ZOOM_LINES:]
        return [_ANSI.sub('', l).rstrip() for l in lines]
    except Exception:
        return []


def parse_pane_fields(line, host, with_activity=False):
    # Field order MUST match the list-panes -F format below. pane_title is
    # user-controlled and may itself contain the '|' delimiter, so it is the
    # LAST field and we split with a maxsplit that keeps it intact.
    # When with_activity is True, pane_last_activity (epoch seconds) is the
    # 7th field, before pane_title.
    last_activity = 0
    if with_activity:
        parts = line.split("|", 7)
        if len(parts) < 8:
            return None
        pidx, pactive, pcmd, ppath, pwidth, pheight, plastact, ptitle = parts
        last_activity = int(plastact) if plastact.isdigit() else 0
    else:
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
        "last_activity": last_activity,
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

    # Prefer tmux-native pane_last_activity when available; otherwise the
    # frontend falls back to hash-comparing pane previews.
    use_activity = tmux_supports_pane_last_activity()
    pane_fmt = (
        "#{pane_index}|#{pane_active}|#{pane_current_command}|#{pane_current_path}"
        "|#{pane_width}|#{pane_height}|#{pane_last_activity}|#{pane_title}"
        if use_activity else
        "#{pane_index}|#{pane_active}|#{pane_current_command}|#{pane_current_path}"
        "|#{pane_width}|#{pane_height}|#{pane_title}"
    )

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
                f"tmux list-panes -t '{sname}:{widx}' -F '{pane_fmt}'"
            )

            panes = []
            for pline in panes_raw.splitlines():
                f = parse_pane_fields(pline, host, with_activity=use_activity)
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
                    "last_activity": f["last_activity"],
                    "preview": [],        # filled in parallel after the tree is built
                    "waiting": False,     # filled alongside preview
                    "content_hash": "",   # full-capture hash for hash-mode activity
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
            for pane, state in zip(
                (p for _, p in preview_jobs),
                ex.map(lambda job: fetch_pane_state(job[0]), preview_jobs),
            ):
                pane["preview"] = state["preview"]
                pane["waiting"] = state["waiting"]
                pane["content_hash"] = state["content_hash"]

    return {
        "sessions": sessions,
        "activity_source": "tmux" if use_activity else "hash",
    }


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
            elif self.path.startswith("/api/pane"):
                params = parse_qs(urlparse(self.path).query)
                target = params.get("target", [""])[0]
                if not target:
                    self.send_response(400)
                    self.end_headers()
                    return
                payload = json.dumps({"lines": fetch_zoom(target)}).encode()
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
