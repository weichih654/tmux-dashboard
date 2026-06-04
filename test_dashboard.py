#!/usr/bin/env python3
"""
Regression tests for the tmux-dashboard timeout-cascade fix.

Bug: launching a heavy TUI (Claude Code) in a tmux pane made the dashboard
flicker "cannot connect". Root cause was a timeout cascade —
  * server captured the FULL pane scrollback per pane, serially;
  * each capture had a 3s timeout (> the frontend's 2.5s abort);
  * the frontend polled every 2s and aborted at 2.5s with no in-flight guard,
    so slow polls overlapped and piled load on tmux.

These tests lock in the fix (#1 frontend timeout > server work,
#2 bounded capture, #3 no overlapping polls).
"""

import os
import re
import unittest

import tmux_server as srv

HTML = os.path.join(os.path.dirname(__file__), "tmux_dashboard.html")


class ServerCaptureTests(unittest.TestCase):
    def test_capture_cmd_bounds_scrollback(self):
        # #2 — capture must NOT walk the whole scrollback of a busy TUI pane.
        # It must bound the start line so the command is cheap regardless of
        # how much output Claude has produced.
        cmd = srv.capture_cmd("sess:0.0")
        self.assertIn("-S -", cmd, "capture must bound start line (-S -N)")
        self.assertIn("capture-pane", cmd)
        self.assertIn("sess:0.0", cmd)

    def test_capture_window_covers_preview_lines(self):
        # The bounded window must be >= the number of preview lines we keep,
        # otherwise the preview would lose lines.
        cmd = srv.capture_cmd("s:0.0")
        m = re.search(r"-S -(\d+)", cmd)
        self.assertIsNotNone(m, "expected -S -N flag")
        self.assertGreaterEqual(int(m.group(1)), srv.PREVIEW_LINES)

    def test_capture_cmd_quotes_target(self):
        # A session name with a single quote / space must not break out of the
        # -t argument (shell-injection safety).
        cmd = srv.capture_cmd("e'vil; rm -rf ~")
        self.assertNotIn("; rm -rf", cmd.replace("rm -rf ~", ""),
                         "unquoted target lets shell metachars escape")
        # the dangerous payload must be inside a single shlex-quoted token
        self.assertIn("'e'\"'\"'vil; rm -rf ~'", cmd)

    def test_capture_includes_visible_pane(self):
        # `-E -1` ends the capture at the LAST HISTORY LINE when scrollback
        # exists — the entire visible pane is excluded, so previews go stale
        # and waiting prompts (always on the visible screen) are invisible.
        # Verified live: seq 1 30 into a 10-row pane, `-S -20 -E -1`
        # captured only line "1". The end must stay at the visible bottom
        # (default when -E is omitted).
        cmd = srv.capture_cmd("sess:0.0")
        self.assertNotIn("-E -1", cmd,
                         "-E -1 cuts off the visible pane when history exists")
        zoom_cmd = srv.capture_cmd("sess:0.0", srv.ZOOM_LINES)
        self.assertNotIn("-E -1", zoom_cmd)

    def test_run_timeout_below_frontend_abort(self):
        # #1 — a single subprocess must time out well under the frontend's
        # abort window so one stuck call can't blow the whole response.
        self.assertLessEqual(srv.RUN_TIMEOUT, 2)


class PreviewTests(unittest.TestCase):
    def test_fetch_preview_strips_ansi_blanks_and_slices(self):
        # fetch_preview must: strip ANSI, drop blank lines, keep last
        # PREVIEW_LINES. It pulls raw output via the module-level run(), so
        # monkeypatch that.
        raw = "\n".join([
            "\x1b[31mline1\x1b[0m", "", "  ", "line2", "line3",
            "line4", "line5", "line6", "line7",
        ])
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            out = srv.fetch_preview("s:0.0")
        finally:
            srv.run = orig
        self.assertEqual(len(out), srv.PREVIEW_LINES)
        self.assertEqual(out[-1], "line7")
        self.assertTrue(all("\x1b" not in l for l in out))   # no ANSI
        self.assertNotIn("", out)                             # no blanks


    def test_fetch_preview_never_raises(self):
        # A capture failure must NOT propagate — it runs inside a parallel
        # map() and a raise there would abort the whole /api/tmux response.
        orig = srv.run
        srv.run = lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(srv.fetch_preview("s:0.0"), [])
        finally:
            srv.run = orig


class PaneTitleTests(unittest.TestCase):
    def test_parses_custom_title(self):
        d = srv.parse_pane_fields("0|1|nvim|/Users/x|96|42|%1|edit-config", host="mac")
        self.assertEqual(d["command"], "nvim")
        self.assertEqual(d["title"], "edit-config")

    def test_blank_title_when_equals_host(self):
        # default pane_title is the hostname — treat as "no custom title".
        d = srv.parse_pane_fields("0|1|nvim|/Users/x|96|42|%1|mac", host="mac")
        self.assertEqual(d["title"], "")

    def test_title_with_pipes_preserved(self):
        # pane_title is user-controlled and may contain our '|' delimiter;
        # it is the last field so the rest must stay intact.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|%1|a|b|c", host="h")
        self.assertEqual(d["title"], "a|b|c")
        self.assertEqual(d["command"], "nvim")
        self.assertEqual(d["width"], "96")

    def test_short_line_rejected(self):
        self.assertIsNone(srv.parse_pane_fields("0|1|nvim|/p|96|42", host="h"))


class ActivitySourceBackendTests(unittest.TestCase):
    """Tests for runtime detection of tmux pane_last_activity support."""

    def setUp(self):
        # Reset the cached capability probe before each test.
        srv._PANE_LAST_ACTIVITY_SUPPORT = None
        self._orig_run = srv.run

    def tearDown(self):
        srv.run = self._orig_run
        srv._PANE_LAST_ACTIVITY_SUPPORT = None

    def test_probe_function_exists(self):
        self.assertTrue(callable(getattr(srv, "tmux_supports_pane_last_activity", None)),
                        "tmux_supports_pane_last_activity() missing")

    def test_probe_true_when_digit(self):
        # A tmux that supports the variable expands it to an epoch number.
        srv.run = lambda cmd: "1780479431"
        self.assertTrue(srv.tmux_supports_pane_last_activity())

    def test_probe_false_when_empty(self):
        # tmux 3.5 expands an unknown #{...} variable to an empty string.
        srv.run = lambda cmd: ""
        self.assertFalse(srv.tmux_supports_pane_last_activity())

    def test_probe_false_when_nondigit(self):
        # Defensive: garbage / literal text means no support.
        srv.run = lambda cmd: "#{pane_last_activity}"
        self.assertFalse(srv.tmux_supports_pane_last_activity())

    def test_probe_is_cached(self):
        # A confirmed-positive probe must run tmux at most once — not on
        # every request.
        calls = []
        srv.run = lambda cmd: (calls.append(cmd), "1780479431")[1]
        srv.tmux_supports_pane_last_activity()
        srv.tmux_supports_pane_last_activity()
        srv.tmux_supports_pane_last_activity()
        self.assertEqual(len(calls), 1,
                         "confirmed support must be cached, not re-run each call")

    def test_probe_reprobes_after_negative(self):
        # A negative/empty probe must NOT be cached permanently — a transient
        # tmux failure (empty output) on the first probe must not lock the
        # server into hash mode forever. Once tmux later reports support, it
        # must be picked up.
        calls = []
        srv.run = lambda cmd: (calls.append(cmd), "")[1]   # transient failure
        self.assertFalse(srv.tmux_supports_pane_last_activity())
        # now tmux reports support — must re-probe and switch to True
        srv.run = lambda cmd: (calls.append(cmd), "1780479431")[1]
        self.assertTrue(srv.tmux_supports_pane_last_activity())
        self.assertGreaterEqual(len(calls), 2,
                                "must re-probe after a negative result")

    def test_parse_with_activity_8_fields(self):
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|1735000000|%2|title",
                                  host="h", with_activity=True)
        self.assertIsNotNone(d)
        self.assertEqual(d["last_activity"], 1735000000)
        self.assertEqual(d["command"], "nvim")
        self.assertEqual(d["title"], "title")

    def test_parse_with_activity_title_pipes_preserved(self):
        # pane_title stays LAST so embedded pipes survive even with activity field.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|1735000000|%2|a|b|c",
                                  host="h", with_activity=True)
        self.assertEqual(d["title"], "a|b|c")
        self.assertEqual(d["last_activity"], 1735000000)

    def test_parse_with_activity_short_rejected(self):
        # 7 fields when 8 are expected (activity mode) must be rejected.
        self.assertIsNone(srv.parse_pane_fields("0|1|nvim|/p|96|42|%2|title",
                                                host="h", with_activity=True))

    def test_parse_with_activity_zero_on_non_numeric(self):
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42||%2|title",
                                  host="h", with_activity=True)
        self.assertEqual(d["last_activity"], 0)

    def test_parse_without_activity_defaults_zero(self):
        # Hash-mode (7 fields) still works; last_activity defaults to 0.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|%1|title", host="h")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("last_activity", 0), 0)


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_abort_timeout_exceeds_server_work(self):
        # #1 — frontend abort must be generous enough to outlast a normal
        # (now-bounded) server response; 2500ms was too tight.
        m = re.search(r"AbortSignal\.timeout\((\d+)\)", self.html)
        self.assertIsNotNone(m, "fetch must use AbortSignal.timeout")
        self.assertGreaterEqual(int(m.group(1)), 6000)

    def test_no_overlapping_polls(self):
        # #3 — must guard against overlapping in-flight requests.
        self.assertIn("inFlight", self.html,
                      "fetchData must guard against overlapping polls")

    def test_renders_pane_title_and_proc(self):
        # pane title is the main label; running command shown as secondary tag.
        self.assertIn("pane.title", self.html)
        self.assertIn("pane-proc", self.html)

    def test_transient_failure_does_not_flush_ui(self):
        # A single failed/aborted poll must NOT immediately wipe the last good
        # render to the "cannot connect" state — only after consecutive fails.
        self.assertIn("failCount", self.html,
                      "fetchData must track consecutive failures")
        # the error empty-state render must be guarded by a >=2 threshold
        self.assertRegex(self.html, r"FAIL_THRESHOLD\s*=\s*2")
        self.assertRegex(self.html, r"failCount\s*>=\s*FAIL_THRESHOLD")


class ZoomBackendTests(unittest.TestCase):
    """Tests for the pane zoom feature (backend side)."""

    def test_zoom_lines_constant_exists_and_is_ample(self):
        # ZOOM_LINES must exist and be meaningfully larger than PREVIEW_LINES
        # so the modal actually shows more context than the card.
        self.assertTrue(hasattr(srv, 'ZOOM_LINES'), "ZOOM_LINES constant missing")
        self.assertGreaterEqual(srv.ZOOM_LINES, srv.PREVIEW_LINES * 3)

    def test_fetch_zoom_function_exists(self):
        self.assertTrue(callable(getattr(srv, 'fetch_zoom', None)),
                        "fetch_zoom function missing from tmux_server")

    def test_fetch_zoom_returns_list(self):
        raw = "\n".join(["line" + str(i) for i in range(60)])
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            out = srv.fetch_zoom("s:0.0")
        finally:
            srv.run = orig
        self.assertIsInstance(out, list)
        self.assertGreater(len(out), 0)

    def test_fetch_zoom_caps_at_zoom_lines(self):
        # Even with 200 lines of input, must return at most ZOOM_LINES.
        raw = "\n".join(["line" + str(i) for i in range(200)])
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            out = srv.fetch_zoom("s:0.0")
        finally:
            srv.run = orig
        self.assertLessEqual(len(out), srv.ZOOM_LINES)

    def test_fetch_zoom_strips_ansi(self):
        raw = "\x1b[32mhello\x1b[0m\n\x1b[1;31mbold red\x1b[0m"
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            out = srv.fetch_zoom("s:0.0")
        finally:
            srv.run = orig
        self.assertTrue(all("\x1b" not in l for l in out))
        self.assertIn("hello", out[0])
        self.assertIn("bold red", out[1])

    def test_fetch_zoom_never_raises(self):
        # Must not propagate exceptions — same contract as fetch_preview.
        orig = srv.run
        srv.run = lambda cmd: (_ for _ in ()).throw(RuntimeError("explode"))
        try:
            result = srv.fetch_zoom("s:0.0")
        finally:
            srv.run = orig
        self.assertEqual(result, [])

    def test_fetch_zoom_capture_cmd_quotes_dangerous_target(self):
        # The capture command used by fetch_zoom must quote the target safely.
        # We verify by checking that a dangerous target doesn't escape.
        dangerous = "sess'; rm -rf ~; echo '"
        orig = srv.run
        captured_cmd = []
        def fake_run(cmd):
            captured_cmd.append(cmd)
            return ""
        srv.run = fake_run
        try:
            srv.fetch_zoom(dangerous)
        finally:
            srv.run = orig
        self.assertTrue(len(captured_cmd) > 0)
        cmd = captured_cmd[0]
        # The dangerous payload must NOT appear unquoted in the command.
        self.assertNotIn("; rm -rf ~;", cmd.replace(shlex.quote(dangerous), ""))

    def test_fetch_zoom_uses_zoom_lines_not_preview_lines(self):
        # The capture window for zoom must be ZOOM_LINES, not the smaller
        # PREVIEW_LINES — otherwise the modal shows no more than the card.
        orig = srv.run
        captured_cmd = []
        srv.run = lambda cmd: (captured_cmd.append(cmd), "")[1]
        try:
            srv.fetch_zoom("s:0.0")
        finally:
            srv.run = orig
        self.assertTrue(len(captured_cmd) > 0)
        m = re.search(r"-S -(\d+)", captured_cmd[0])
        self.assertIsNotNone(m, "zoom capture must bound start line")
        self.assertGreaterEqual(int(m.group(1)), srv.ZOOM_LINES)


class ZoomFrontendTests(unittest.TestCase):
    """Tests for the pane zoom feature (frontend side)."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_zoom_modal_element_exists(self):
        self.assertIn('id="pane-zoom"', self.html,
                      "modal element #pane-zoom missing from HTML")

    def test_zoom_modal_hidden_by_default(self):
        # The modal must NOT be visible on load — it should only appear when
        # openZoom() is called. Check it has display:none or no 'open' class initially.
        # The 'open' class (or similar) must be absent from the static HTML.
        self.assertNotRegex(self.html, r'id="pane-zoom"[^>]*class="[^"]*\bopen\b',
                            "modal must not have 'open' class in static HTML")

    def test_open_zoom_function_defined(self):
        self.assertIn("function openZoom(", self.html,
                      "openZoom() function missing")

    def test_close_zoom_function_defined(self):
        self.assertIn("function closeZoom(", self.html,
                      "closeZoom() function missing")

    def test_fetch_zoom_function_defined(self):
        self.assertIn("function fetchZoom(", self.html,
                      "fetchZoom() function missing")

    def test_close_zoom_clears_interval(self):
        # closeZoom must clear the polling timer to avoid ghost updates.
        self.assertIn("clearInterval", self.html,
                      "closeZoom must clearInterval to stop ghost polls")

    def test_zoom_fetches_api_pane_endpoint(self):
        # The zoom must poll the dedicated /api/pane endpoint, NOT /api/tmux.
        self.assertIn("/api/pane", self.html,
                      "fetchZoom must poll /api/pane endpoint")

    def test_zoom_esc_key_closes_modal(self):
        # ESC must close the modal so users can dismiss without clicking.
        self.assertRegex(self.html, r"Escape",
                         "modal must handle Escape key to close")

    def test_pane_cards_call_open_zoom(self):
        # Every pane card must wire up openZoom so clicking zooms that pane.
        self.assertIn("openZoom(", self.html,
                      "pane cards must call openZoom on click")

    def test_zoom_backdrop_closes_modal(self):
        # Clicking the backdrop (outside the box) must close the modal.
        # We check closeZoom() is triggered on the overlay element itself.
        self.assertRegex(self.html, r"pane-zoom.*click|click.*pane-zoom",
                         "backdrop click must close modal")

    def test_pane_click_stops_propagation(self):
        # Pane onclick must call stopPropagation() so the click doesn't
        # bubble up to toggleWindow / toggleSession and collapse the window.
        self.assertIn("stopPropagation", self.html,
                      "pane onclick must call event.stopPropagation()")

    def test_esc_html_escapes_single_quotes(self):
        # escHtml must escape single quotes for safe use in JS string
        # attributes like onclick="openZoom('...')"
        self.assertIn("&#39;", self.html,
                      "escHtml must escape single quotes as &#39;")

    def test_zoom_reschedule_updates_zoom_timer(self):
        # reschedule() must restart the zoom timer when the modal is open.
        # A stale zoom timer keeps polling at the old interval.
        import re as _re
        m = _re.search(r"function reschedule\(\)(.*?)^}", self.html, _re.DOTALL|_re.MULTILINE)
        self.assertIsNotNone(m, "reschedule function not found")
        reschedule_body = m.group(1)
        self.assertIn("zoomTarget", reschedule_body,
                      "reschedule must restart zoom timer when modal is open")

    def test_zoom_body_horizontal_overflow(self):
        # zoom-body must handle long lines without silent cropping.
        self.assertRegex(self.html, r"overflow-x\s*:\s*auto",
                         "zoom-body must have overflow-x: auto for long lines")

    def test_zoom_encodes_target_in_url(self):
        # Target string is user/tmux-controlled; it must be URL-encoded before
        # being appended to the fetch URL to prevent header injection.
        self.assertIn("encodeURIComponent", self.html,
                      "fetch URL must encodeURIComponent(target)")


import shlex  # needed by ZoomBackendTests.test_fetch_zoom_capture_cmd_quotes_dangerous_target


class FadeOutTests(unittest.TestCase):
    """Tests for the slow busy-fade-out feature (hash-comparison based)."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_fade_duration_constant_exists(self):
        self.assertIn("FADE_DURATION", self.html,
                      "FADE_DURATION constant missing")

    def test_fade_duration_at_least_60s(self):
        m = re.search(r"FADE_DURATION\s*=\s*(\d+)", self.html)
        self.assertIsNotNone(m, "FADE_DURATION not assigned a value")
        self.assertGreaterEqual(int(m.group(1)), 60000,
                                "FADE_DURATION must be >= 60000 ms (1 minute)")

    def test_fade_start_map_declared(self):
        # fadeStart Map tracks when each pane began fading out.
        self.assertRegex(self.html, r"fadeStart\s*=\s*new Map",
                         "fadeStart Map missing")

    def test_busy_fade_out_keyframes_defined(self):
        # CSS animation for the slow fade.
        self.assertIn("busy-fade-out", self.html,
                      "@keyframes busy-fade-out missing from CSS")

    def test_is_fading_css_class_defined(self):
        self.assertIn("is-fading", self.html,
                      ".pane.is-fading CSS class missing")

    def test_is_fading_uses_fade_out_animation(self):
        # The CSS class must reference the fade-out keyframes (may span lines).
        self.assertRegex(self.html,
                         re.compile(r"is-fading.*busy-fade-out|busy-fade-out.*is-fading",
                                    re.DOTALL),
                         "is-fading class must use busy-fade-out animation")

    def test_negative_animation_delay_applied_for_fading(self):
        # Negative animation-delay is the trick that correctly positions the
        # animation at the right point after DOM reconstruction each render cycle.
        self.assertIn("animation-delay", self.html,
                      "animation-delay must be applied for fading panes")

    def test_fade_start_deleted_when_fade_completes(self):
        # Must clean up fadeStart to avoid memory growth over time.
        self.assertRegex(self.html, r"fadeStart\.delete\(",
                         "fadeStart must be deleted when fade completes")

    def test_fade_cancelled_on_new_activity(self):
        # If a fading pane gets new output, it must return to full busy state.
        # fadeStart must be cleared on hash change.
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "checkBusy function not found")
        body = m.group(1)
        self.assertIn("fadeStart.delete", body,
                      "checkBusy must cancel fade on new activity")

    def test_checkbusy_has_fading_state(self):
        # checkBusy must distinguish 'fading' from plain busy/not-busy.
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "checkBusy not found")
        self.assertIn("fading", m.group(1),
                      "checkBusy must return/emit a fading state")

    def test_pane_render_uses_is_fading_class(self):
        # The pane HTML template must conditionally add is-fading class.
        self.assertRegex(self.html, r"is-fading",
                         "render must apply is-fading class to fading panes")

    def test_window_busy_badge_not_shown_during_fade(self):
        # Window-level activity badge should only pulse for actively busy
        # panes, not for panes that are just slowly fading out.
        self.assertRegex(self.html, r"isBusy.*winBusy|winBusy.*isBusy",
                         "winBusy must only activate on isBusy, not on fading")


class ActivitySourceFrontendTests(unittest.TestCase):
    """Tests for the frontend dual-mode (tmux pane_last_activity vs hash)."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_activity_source_variable_declared(self):
        # A mutable activitySource flag must exist, defaulting to hash so an
        # old/unknown server still gets the safe fallback.
        self.assertRegex(self.html, r"activitySource\s*=\s*['\"]hash['\"]",
                         "activitySource must default to 'hash'")

    def test_reads_activity_source_from_response(self):
        # render/fetch must pick up data.activity_source from the server.
        self.assertIn("activity_source", self.html,
                      "frontend must read data.activity_source")

    def test_checkbusy_branches_on_activity_source(self):
        # checkBusy must have a tmux branch that uses pane_last_activity.
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "checkBusy not found")
        body = m.group(1)
        self.assertIn("activitySource", body,
                      "checkBusy must branch on activitySource")
        self.assertRegex(body, r"\*\s*1000",
                         "tmux branch must convert epoch seconds to ms (* 1000)")

    def test_checkbusy_keeps_hash_fallback(self):
        # Hash-comparison path must remain for unsupported tmux.
        self.assertIn("function simpleHash", self.html,
                      "simpleHash must remain for the hash fallback")
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIn("simpleHash", m.group(1),
                      "checkBusy hash branch must still call simpleHash")

    def test_checkbusy_returns_state_and_fade_elapsed(self):
        # checkBusy returns an object exposing both the state and the fade
        # elapsed time so render need not branch on mode for animation-delay.
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        body = m.group(1)
        self.assertIn("fadeElapsed", body,
                      "checkBusy must return fadeElapsed for animation-delay")


class WaitingDetectionTests(unittest.TestCase):
    """Tests for detect_waiting() — agent waiting-for-user detection.

    Scans only the LAST few non-empty lines of a pane capture so prompts
    that scrolled away do not count. Patterns cover Claude Code, Codex,
    Copilot CLI, Opencode, plus generic y/n prompts.
    """

    # ── plumbing ────────────────────────────────────────────

    def test_detect_waiting_exists(self):
        self.assertTrue(callable(getattr(srv, "detect_waiting", None)),
                        "detect_waiting() missing from tmux_server")

    def test_scan_window_constant(self):
        # The scan window must exist and be big enough for a multi-line
        # prompt UI (question + options + hint line) but bounded so old
        # scrollback can't trigger.
        self.assertTrue(hasattr(srv, "WAITING_SCAN_LINES"),
                        "WAITING_SCAN_LINES constant missing")
        self.assertGreaterEqual(srv.WAITING_SCAN_LINES, 4)
        self.assertLessEqual(srv.WAITING_SCAN_LINES, 12)

    # ── Claude Code ─────────────────────────────────────────

    def test_claude_permission_menu(self):
        lines = [
            "  Bash command",
            "  rm -rf node_modules",
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again for this command",
            "  3. No, and tell Claude what to do differently (esc)",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_claude_ask_user_question_enter_to_select(self):
        # AskUserQuestion UI ends with an "Enter to select" hint line.
        lines = [
            "  Which approach do you prefer?",
            "  ❯ 1. Option A",
            "    2. Option B",
            "  Enter to select",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_claude_esc_to_cancel_hint(self):
        lines = [
            "  Edit file src/app.py",
            "  Esc to cancel",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_claude_working_spinner_not_waiting(self):
        # While Claude works it shows "(esc to interrupt)" — must NOT count.
        lines = [
            "● Reading src/app.py…",
            "✶ Pondering… (esc to interrupt)",
        ]
        self.assertFalse(srv.detect_waiting(lines))

    # ── Codex ───────────────────────────────────────────────

    def test_codex_approval_prompt(self):
        lines = [
            "  Codex wants to run:",
            "  npm install",
            "  Allow command?",
            "▌ Yes (y)",
            "  No, and tell Codex what to do differently (n)",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_codex_press_enter_to_confirm(self):
        lines = [
            "  apply patch to src/main.rs?",
            "  Press Enter to confirm",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    # ── Copilot ─────────────────────────────────────────────

    def test_copilot_numbered_yes(self):
        lines = [
            "  Allow this command?",
            "  1. Yes",
            "  2. No",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_copilot_allow_this(self):
        lines = [
            "  git push origin main",
            "  Allow this command?",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    # ── Opencode ────────────────────────────────────────────

    def test_opencode_permission_dialog(self):
        lines = [
            "  bash: rm -rf build/",
            "  Permission required",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    # ── generic prompts ─────────────────────────────────────

    def test_generic_y_slash_n(self):
        self.assertTrue(srv.detect_waiting(["Overwrite existing file? (y/n)"]))

    def test_generic_bracket_Yn(self):
        self.assertTrue(srv.detect_waiting(["Apply changes? [Y/n]"]))

    def test_generic_bracket_yN(self):
        self.assertTrue(srv.detect_waiting(["Delete branch? [y/N]"]))

    def test_generic_proceed_case_insensitive(self):
        self.assertTrue(srv.detect_waiting(["PROCEED?"]))
        self.assertTrue(srv.detect_waiting(["proceed?"]))

    def test_generic_continue_question(self):
        self.assertTrue(srv.detect_waiting(["Continue?"]))

    # ── false positives / window bounds ─────────────────────

    def test_scrolled_away_prompt_ignored(self):
        # A prompt buried older than the scan window must NOT trigger —
        # the user already answered and output continued.
        lines = ["Do you want to proceed?", "❯ 1. Yes"]
        lines += [f"output line {i}" for i in range(srv.WAITING_SCAN_LINES + 1)]
        self.assertFalse(srv.detect_waiting(lines))

    def test_plain_shell_prompt_not_waiting(self):
        lines = [
            "$ ls -la",
            "total 48",
            "drwxr-xr-x  6 wade  staff  192 Jun  4 10:00 .",
            "$",
        ]
        self.assertFalse(srv.detect_waiting(lines))

    def test_vim_buffer_with_old_yn_text(self):
        # "(y/n)" mid-buffer with >scan-window lines after it must not count.
        lines = ['  confirm("delete? (y/n)")']
        lines += [f"    line{i} = {i}" for i in range(srv.WAITING_SCAN_LINES + 2)]
        self.assertFalse(srv.detect_waiting(lines))

    def test_empty_lines_list(self):
        self.assertFalse(srv.detect_waiting([]))

    def test_blank_only_lines(self):
        self.assertFalse(srv.detect_waiting(["", "   ", "\t"]))

    def test_blanks_interspersed_prompt_still_detected(self):
        # Blank lines must not eat the scan window — only non-empty count.
        lines = ["Do you want to proceed?", "", "❯ 1. Yes", "", "  2. No", ""]
        self.assertTrue(srv.detect_waiting(lines))

    def test_build_log_not_waiting(self):
        lines = [
            "Compiling foo v0.3.1",
            "Compiling bar v1.2.0",
            "Finished dev [unoptimized] target(s) in 4.21s",
        ]
        self.assertFalse(srv.detect_waiting(lines))

    # ── false positives found in review round 1 ─────────────

    def test_starship_shell_prompt_not_waiting(self):
        # ❯ is the prompt char of starship/pure/spaceship themes. An idle
        # shell whose last command starts digit-then-dot must NOT trigger.
        self.assertFalse(srv.detect_waiting(["❯ 1.2.3 release notes"]))
        self.assertFalse(srv.detect_waiting(["❯ 3.txt"]))

    def test_allow_in_log_output_not_waiting(self):
        self.assertFalse(srv.detect_waiting(["Allowed origins? checking config"]))
        self.assertFalse(srv.detect_waiting(["curl: Allow-Control header missing, retry?"]))

    def test_do_you_want_to_prose_not_waiting(self):
        # mid-line prose without a trailing '?' must NOT trigger
        self.assertFalse(srv.detect_waiting(
            ["git log: Do you want to merge these branches manually later"]))

    def test_numbered_yes_prose_not_waiting(self):
        self.assertFalse(srv.detect_waiting(["   1. Yes it compiled"]))
        self.assertFalse(srv.detect_waiting(["42. Yesterday we shipped"]))

    def test_enter_to_select_prose_not_waiting(self):
        self.assertFalse(srv.detect_waiting(
            ["# Enter to select the menu item in docs"]))

    def test_continue_in_code_not_waiting(self):
        # source code shown in a pane — '?' is not at end of line
        self.assertFalse(srv.detect_waiting(['print("continue?")']))

    def test_claude_hint_after_middot_still_waiting(self):
        # Claude footer hints are middot-separated, not line-anchored.
        self.assertTrue(srv.detect_waiting(
            ["↑/↓ to navigate · Enter to select · Esc to cancel"]))

    def test_codex_no_option_line_waiting(self):
        # design doc lists the Codex "No … (n)" option line as a pattern
        self.assertTrue(srv.detect_waiting(
            ["  No, and tell Codex what to do differently (n)"]))

    # ── findings from review round 2 ────────────────────────

    def test_agent_narration_prose_not_waiting(self):
        # Agents stream narration ending in "continue?"/"proceed?" — prose,
        # not a prompt. Only a standalone Proceed?/Continue? line counts.
        self.assertFalse(srv.detect_waiting(["Tests pass. Should I continue?"]))
        self.assertFalse(srv.detect_waiting(["The previous step did not proceed?"]))
        self.assertFalse(srv.detect_waiting(["Now I will continue?"]))

    def test_standalone_proceed_continue_still_waiting(self):
        self.assertTrue(srv.detect_waiting(["Proceed?"]))
        self.assertTrue(srv.detect_waiting(["  Continue?  "]))

    def test_claude_box_drawn_dialog_waiting(self):
        # Real Claude Code renders permission dialogs inside a border box —
        # line-start anchors must see through the box-drawing characters.
        lines = [
            "╭──────────────────────────────────────╮",
            "│ Bash command                         │",
            "│   rm -rf node_modules                │",
            "│ Do you want to proceed?              │",
            "│ ❯ 1. Yes                             │",
            "│   2. No, and tell Claude what to do  │",
            "╰──────────────────────────────────────╯",
        ]
        self.assertTrue(srv.detect_waiting(lines))

    def test_box_border_lines_do_not_eat_scan_window(self):
        # Border-only lines (╭───╮) must not count toward the scan window.
        lines = ["Do you want to proceed?", "│ ❯ 1. Yes │"]
        lines += ["╭──────╮", "╰──────╯"] * 3   # 6 border-only lines after
        self.assertTrue(srv.detect_waiting(lines))

    # ── answered-question echo (found in live use) ──────────

    def test_answered_question_echo_not_waiting(self):
        # After the user answers, Claude Code collapses the menu into a
        # one-line echo in the transcript ("❯ 1. A 2. B 3. C") and goes back
        # to work. Option-shaped lines WITHOUT a question/hint line in the
        # window must NOT keep the pane red. Real capture from live use:
        lines = [
            "❯ 1. A 2. 所有qts的語言都要 3. OK 4. A",
            "✢ Perusing… (1m 23s · almost done thinking with high effort)",
            "⎿ Tip: Did you know you can drag and drop image files?",
            "❯",
            "Opus 4.8 │ ✍️ 30% │ some-branch (some-branch*) │ ● high",
            "current ●●●●○○○○○○  48% ⟳ 4:00pm",
            "weekly  ●●●○○○○○○○  32% ⟳ jun 8, 6:00pm",
            "⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
        self.assertFalse(srv.detect_waiting(lines))

    def test_bare_option_lines_without_question_not_waiting(self):
        # Option-shaped lines in a doc/poll/checklist with no question or
        # hint line must not trigger (round-2 finding, same root cause).
        self.assertFalse(srv.detect_waiting(["Quick poll:", "1. Yes", "2. No"]))
        self.assertFalse(srv.detect_waiting(["❯ 1. first item in my list"]))

    def test_live_menu_with_hint_footer_still_waiting(self):
        # A LIVE menu always carries its hint footer at the very bottom —
        # weak option lines + strong hint line together must stay True.
        lines = [
            "多語儲存/選擇模型要用哪個?",
            "❯ 1. 單檔全語言(前端選)",
            "  2. 多檔每語言(serve 選)",
            "  Chat about this",
            "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel",
        ]
        self.assertTrue(srv.detect_waiting(lines))


class WaitingPaneStateTests(unittest.TestCase):
    """fetch_pane_state() returns preview + waiting from ONE capture."""

    def test_fetch_pane_state_exists(self):
        self.assertTrue(callable(getattr(srv, "fetch_pane_state", None)),
                        "fetch_pane_state() missing from tmux_server")

    def test_returns_preview_and_waiting(self):
        raw = "\n".join([
            "some earlier output",
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
        ])
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            st = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertIsInstance(st, dict)
        self.assertIn("preview", st)
        self.assertIn("waiting", st)
        self.assertTrue(st["waiting"])
        self.assertLessEqual(len(st["preview"]), srv.PREVIEW_LINES)

    def test_ansi_stripped_before_matching(self):
        # Prompt wrapped in color codes must still match after ANSI strip.
        raw = "\x1b[1mDo you want to proceed?\x1b[0m\n\x1b[36m❯ 1. Yes\x1b[0m"
        orig = srv.run
        srv.run = lambda cmd: raw
        try:
            st = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertTrue(st["waiting"])

    def test_not_waiting_on_plain_output(self):
        orig = srv.run
        srv.run = lambda cmd: "hello\nworld"
        try:
            st = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertFalse(st["waiting"])

    def test_never_raises(self):
        # Same contract as fetch_preview — runs inside the parallel map.
        orig = srv.run
        srv.run = lambda cmd: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            st = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertEqual(st["preview"], [])
        self.assertFalse(st["waiting"])

    # ── content hash for activity detection (hash mode) ─────

    def test_content_hash_present(self):
        orig = srv.run
        srv.run = lambda cmd: "hello\nworld"
        try:
            st = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertIn("content_hash", st)
        self.assertIsInstance(st["content_hash"], str)
        self.assertTrue(st["content_hash"])

    def test_content_hash_sees_change_above_preview_window(self):
        # Claude Code's spinner/timer line sits ABOVE the static status-bar
        # lines that fill the 5-line preview. A tick that only changes the
        # spinner line must still change the hash, or hash-mode activity
        # detection goes blind while the agent thinks.
        static_tail = ["❯", "Opus 4.8 | 35%", "current 53%", "weekly 33%",
                       "bypass permissions on"]
        cap1 = "\n".join(["✽ Booping… (11m 16s)"] + static_tail)
        cap2 = "\n".join(["✽ Booping… (11m 17s)"] + static_tail)
        orig = srv.run
        try:
            srv.run = lambda cmd: cap1
            st1 = srv.fetch_pane_state("s:0.0")
            srv.run = lambda cmd: cap2
            st2 = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertEqual(st1["preview"], st2["preview"])          # preview blind
        self.assertNotEqual(st1["content_hash"], st2["content_hash"])

    def test_content_hash_stable_for_identical_content(self):
        orig = srv.run
        srv.run = lambda cmd: "same\ncontent\nlines"
        try:
            st1 = srv.fetch_pane_state("s:0.0")
            st2 = srv.fetch_pane_state("s:0.0")
        finally:
            srv.run = orig
        self.assertEqual(st1["content_hash"], st2["content_hash"])

    def test_get_tmux_state_emits_content_hash(self):
        import inspect
        src = inspect.getsource(srv.get_tmux_state)
        self.assertIn('"content_hash"', src,
                      "get_tmux_state must put 'content_hash' on each pane")

    def test_get_tmux_state_emits_waiting_field(self):
        # Pane JSON must carry the waiting flag to the frontend.
        import inspect
        src = inspect.getsource(srv.get_tmux_state)
        self.assertIn('"waiting"', src,
                      "get_tmux_state must put 'waiting' on each pane")

    def test_every_pane_has_waiting_bool_end_to_end(self):
        # Full get_tmux_state() pass over a simulated tmux: EVERY pane must
        # carry a boolean waiting flag, and a prompt-showing pane must be True.
        orig_run = srv.run
        orig_flag = srv._PANE_LAST_ACTIVITY_SUPPORT
        srv._PANE_LAST_ACTIVITY_SUPPORT = None

        def fake_run(cmd):
            # order matters — later format strings embed the same variable
            # names used by display-message probes
            if "list-sessions" in cmd:
                return "main|1|1"
            if "list-windows" in cmd:
                return "0|sh|1|2|layout"
            if "list-panes" in cmd:
                return ("0|1|zsh|/tmp|80|24|%10|t1\n"
                        "1|0|claude|/tmp|80|24|%11|t2")
            if "capture-pane" in cmd:
                return "Do you want to proceed?\n❯ 1. Yes"
            if "pane_last_activity" in cmd:
                return ""          # probe → hash mode (7-field panes)
            if "session_name" in cmd:
                return "main"
            if "window_index" in cmd:
                return "0"
            if "pane_index" in cmd:
                return "0"
            if "host_short" in cmd:
                return "mac"
            return ""

        srv.run = fake_run
        try:
            state = srv.get_tmux_state()
        finally:
            srv.run = orig_run
            srv._PANE_LAST_ACTIVITY_SUPPORT = orig_flag

        panes = [p for s in state["sessions"]
                 for w in s["windows"] for p in w["panes"]]
        self.assertEqual(len(panes), 2)
        for p in panes:
            self.assertIn("waiting", p)
            self.assertIsInstance(p["waiting"], bool)
            self.assertTrue(p["waiting"])   # capture shows a live prompt


class CollapsedHeaderTests(unittest.TestCase):
    """Collapsed windows/sessions dim their headers; events re-light them."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_collapsed_window_title_dimmed(self):
        self.assertRegex(self.html,
                         r"\.window\.collapsed\s+\.win-name\s*\{[^}]*--muted",
                         "collapsed window title must dim to --muted")

    def test_collapsed_session_title_dimmed(self):
        self.assertRegex(self.html,
                         r"\.session\.collapsed\s+\.session-name\s*\{[^}]*--muted",
                         "collapsed session title must dim to --muted")

    def test_collapsed_title_stays_dim_even_with_events(self):
        # The badge beside the title signals events; the title itself must
        # stay dim — re-coloring it made collapsed state unreadable again.
        self.assertNotRegex(self.html,
                            r"\.window\.collapsed\.is-busy\s+\.win-name",
                            "collapsed busy window title must NOT change color")
        self.assertNotRegex(self.html,
                            r"\.window\.collapsed\.is-waiting\s+\.win-name",
                            "collapsed waiting window title must NOT change color")
        self.assertNotRegex(self.html,
                            r"\.session\.collapsed\.is-(busy|waiting)\s+\.session-name",
                            "collapsed session title must NOT change color")

    def test_window_div_gets_waiting_class(self):
        self.assertRegex(self.html, r"winWaiting\s*\?\s*' is-waiting'",
                         "window div must carry is-waiting for CSS")

    def test_rollup_badge_next_to_title_not_far_right(self):
        # Badge sits right AFTER the title; a flex spacer pushes the
        # pane-count/arrow to the right — so the eye finds the event beside
        # the name, not at the far edge of the row.
        m = re.search(r"\.win-activity\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(m)
        self.assertNotIn("margin-left: auto", m.group(1),
                         "badge must not be pushed to the far right")
        self.assertIn("win-spacer", self.html,
                      "header needs a flex spacer after the badge")
        # template order: win-name div, then the rollup badge, then spacer
        self.assertRegex(self.html,
                         re.compile(r"win-name.{0,200}winWaiting.{0,300}win-spacer",
                                    re.DOTALL),
                         "badge must render between title and spacer")

    def test_session_header_rolls_up_events_when_collapsed(self):
        # A collapsed SESSION hides its windows' badges entirely — the
        # session header must roll up waiting/activity itself.
        self.assertRegex(self.html, r"sessWaiting",
                         "session-level waiting rollup missing")
        self.assertRegex(self.html,
                         r"\.session:not\(\.collapsed\)\s+\.(win-waiting|win-activity)",
                         "session rollup badge must hide when expanded")


class AutoFoldTests(unittest.TestCase):
    """Windows with no events for AUTO_FOLD_AFTER auto-collapse."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_auto_fold_constant(self):
        m = re.search(r"AUTO_FOLD_AFTER\s*=\s*(\d+)", self.html)
        self.assertIsNotNone(m, "AUTO_FOLD_AFTER constant missing")
        self.assertEqual(int(m.group(1)), 180000, "spec says 3 minutes")

    def test_event_times_tracked_per_window(self):
        self.assertRegex(self.html, r"winLastEvent\s*=\s*new Map",
                         "must track last event time per window")

    def test_first_sighting_is_baseline(self):
        # Page load must not fold everything instantly — a window first
        # seen now has its baseline set to now.
        self.assertRegex(self.html, r"!winLastEvent\.has\(wkey\)",
                         "first sighting must seed the event baseline")

    def test_manual_expand_exempts_until_next_event(self):
        self.assertRegex(self.html, r"manualExpand\s*=\s*new Set",
                         "manual-expand exemption set missing")
        # expanding via toggleWindow must register the exemption
        m = re.search(r"function toggleWindow\((.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertIn("manualExpand.add", m.group(0),
                      "manual expand must exempt the window from auto-fold")
        # a new event re-arms auto-fold (clears the exemption)
        self.assertRegex(self.html, r"manualExpand\.delete\(wkey\)",
                         "events must clear the manual-expand exemption")

    def test_here_window_never_auto_folds(self):
        # The window containing the cursor (HERE pane) must be exempt.
        self.assertRegex(self.html, r"hasCurrent",
                         "auto-fold must check for the current (HERE) pane")

    def test_expanded_title_color_uniform(self):
        # Expanded window titles are all the same color — the active
        # window is marked by its indicator dot, not an amber title.
        m = re.search(r"\.win-name\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(m, ".win-name rule missing")
        self.assertIn("--text", m.group(1),
                      "win-name base color must be --text")
        self.assertNotIn("--window", m.group(1),
                         "win-name must not use the amber accent")
        self.assertNotRegex(self.html,
                            r"\.window:not\(\.is-active\)\s+\.win-name",
                            "per-active-window title color override must go")

    def test_window_state_maps_pruned(self):
        # Vanished windows must not leak entries (index reuse would
        # resurrect stale fold/exemption state).
        self.assertRegex(self.html, r"winLastEvent\.delete\(",
                         "winLastEvent must be pruned for vanished windows")


class PaneIdTests(unittest.TestCase):
    """Panes are keyed by tmux pane_id (%N — unique, never renumbered).

    Killing a pane renumbers the survivors' indexes, so an index-composed
    key suddenly points at a DIFFERENT pane's stale hash → false activity.
    """

    def test_parse_includes_pane_id(self):
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|%7|mytitle", host="h")
        self.assertIsNotNone(d)
        self.assertEqual(d["id"], "%7")
        self.assertEqual(d["title"], "mytitle")

    def test_parse_includes_pane_id_with_activity(self):
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|1735000000|%7|t",
                                  host="h", with_activity=True)
        self.assertIsNotNone(d)
        self.assertEqual(d["id"], "%7")
        self.assertEqual(d["last_activity"], 1735000000)

    def test_pane_fmt_requests_pane_id(self):
        import inspect
        src = inspect.getsource(srv.get_tmux_state)
        self.assertIn("#{pane_id}", src,
                      "list-panes format must request #{pane_id}")
        self.assertIn('"id"', src,
                      "pane JSON must carry the id")

    def test_frontend_keys_by_pane_id(self):
        with open(HTML, encoding="utf-8") as f:
            html = f.read()
        self.assertRegex(html, r"pane\.id\s*\|\|\s*paneKey",
                         "render must key by pane.id, composite key only as fallback")


class WaitingFrontendTests(unittest.TestCase):
    """Frontend: red waiting glow, badge, rollup, browser notification."""

    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_is_waiting_css_class_defined(self):
        self.assertRegex(self.html, r"\.pane\.is-waiting",
                         ".pane.is-waiting CSS class missing")

    def test_waiting_breathe_keyframes(self):
        self.assertIn("waiting-breathe", self.html,
                      "@keyframes waiting-breathe missing")

    def test_render_uses_pane_waiting(self):
        self.assertIn("pane.waiting", self.html,
                      "render must read pane.waiting from API data")

    def test_waiting_badge_in_template(self):
        self.assertIn("waiting-badge", self.html,
                      "pane card must show a waiting badge")

    def test_waiting_overrides_busy_glow(self):
        # A waiting pane must NOT also pulse amber — red wins.
        self.assertRegex(self.html, r"isBusy\s*&&\s*!\s*(pane\.waiting|isWaiting)|!\s*(pane\.waiting|isWaiting)\s*&&\s*isBusy",
                         "busy glow must be suppressed when waiting")

    def test_window_header_waiting_rollup(self):
        # Collapsed window must roll up a waiting indicator like win-activity.
        self.assertIn("win-waiting", self.html,
                      "collapsed window header must show waiting rollup")

    def test_notification_permission_requested(self):
        self.assertIn("Notification.requestPermission", self.html,
                      "must request Notification permission")

    def test_notification_fired(self):
        self.assertIn("new Notification", self.html,
                      "must fire a browser notification")

    def test_notification_deduped_per_pane(self):
        # Track panes already notified; only fire on not-waiting → waiting.
        self.assertRegex(self.html, r"notifiedWaiting\s*=\s*new Set",
                         "must dedupe notifications with a per-pane Set")
        self.assertRegex(self.html, r"notifiedWaiting\.delete\(",
                         "must clear the dedupe entry when pane stops waiting")

    def test_notification_body_mentions_waiting(self):
        self.assertIn("在等你回覆", self.html,
                      "notification body must say the pane is waiting")

    def test_active_pane_uses_background_not_border(self):
        # The focused pane of a window must be marked by a subtle background
        # tint only — accent bars/borders clash with the amber/red glow
        # rings (green left edge + amber other edges looked broken).
        self.assertNotIn(".pane.is-active::before", self.html,
                         "is-active must not draw an accent bar")
        self.assertNotIn(".pane.is-current::before", self.html,
                         "is-current must not draw an accent bar")
        # no border-color override inside the is-active / is-current rules,
        # and the tint must be a NEUTRAL shade of the base palette (no green
        # hue — user feedback), distinguished by lightness only.
        for cls in (".pane.is-active", ".pane.is-current"):
            m = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", self.html)
            self.assertIsNotNone(m, f"{cls} rule missing")
            self.assertNotIn("border-color", m.group(1),
                             f"{cls} must not override border-color")
            self.assertIn("background", m.group(1),
                          f"{cls} must distinguish via background tint")
            self.assertNotIn("216", m.group(1),
                             f"{cls} must not use the green accent color")

    def test_demo_mode_has_waiting_pane(self):
        # Demo data must include a simulated waiting pane for preview.
        self.assertRegex(self.html, r"waiting:\s*true",
                         "makeDemoData must include a waiting pane")

    # ── fixes from review round 1 ───────────────────────────

    def test_hash_maps_reaped_for_vanished_panes(self):
        # prevHash/lastChanged/fadeStart entries for killed panes must be
        # reaped — tmux reuses pane indexes, so a stale hash under the same
        # key makes a NEWLY split pane glow "activity" at birth (compared
        # against the dead pane's content).
        self.assertRegex(self.html, r"prevHash\.delete\(",
                         "stale prevHash entries must be reaped")
        # the reap must cover all three hash-mode maps in one sweep
        m = re.search(
            r"for\s*\(const k of \[\.\.\.prevHash\.keys\(\)\]\)(.{0,300})",
            self.html, re.DOTALL)
        self.assertIsNotNone(m, "reap loop over prevHash.keys() missing")
        block = m.group(1)
        self.assertIn("!seenKeys.has(k)", block)
        self.assertIn("prevHash.delete(k)", block)
        self.assertIn("lastChanged.delete(k)", block)
        self.assertIn("fadeStart.delete(k)", block)

    def test_resize_rewrap_not_treated_as_activity(self):
        # Splitting a window resizes the sibling pane → content rewraps →
        # capture hash changes with NO real output. The poll where a pane's
        # size changed must not mark it busy.
        self.assertRegex(self.html, r"prevSize\s*=\s*new Map",
                         "must track pane size between polls")
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "checkBusy not found")
        body = m.group(1)
        self.assertIn("pane.size", body,
                      "checkBusy must compare pane.size to suppress rewrap noise")

    def test_new_pane_birth_grace_period(self):
        # A freshly created pane draws its shell prompt over the first
        # seconds — that must not glow. Grace period after first sighting.
        self.assertRegex(self.html, r"paneBirth\s*=\s*new Map",
                         "must record when a pane key was first seen")
        m = re.search(r"BIRTH_GRACE\s*=\s*(\d+)", self.html)
        self.assertIsNotNone(m, "BIRTH_GRACE constant missing")
        self.assertGreaterEqual(int(m.group(1)), 3000)
        self.assertLessEqual(int(m.group(1)), 15000)

    def test_size_and_birth_maps_reaped(self):
        # The vanished-pane reap sweep must cover the new maps too, or
        # index reuse resurrects stale sizes/birth times.
        m = re.search(
            r"for\s*\(const k of \[\.\.\.prevHash\.keys\(\)\]\)(.{0,400})",
            self.html, re.DOTALL)
        self.assertIsNotNone(m, "reap loop over prevHash.keys() missing")
        self.assertIn("prevSize.delete(k)", m.group(1))
        self.assertIn("paneBirth.delete(k)", m.group(1))

    def test_notified_set_reaped_for_vanished_panes(self):
        # A pane killed WHILE waiting never hits the !isWaiting branch, so
        # its dedupe key must be reaped by comparing against the keys seen
        # in the current render — otherwise pane-index reuse suppresses a
        # future real notification.
        self.assertIn("seenKeys", self.html,
                      "render must track keys seen this cycle")
        self.assertRegex(self.html, r"!seenKeys\.has\(",
                         "must prune notifiedWaiting entries for vanished panes")

    def test_notification_label_sanitized(self):
        # pane.title is attacker-influenced (select-pane -T / OSC escapes)
        # and lands in an OS notification — control chars stripped, length
        # capped.
        self.assertIn("function sanitizeLabel", self.html,
                      "notification label must go through sanitizeLabel()")
        self.assertRegex(self.html, r"sanitizeLabel\(label\)|sanitizeLabel\(.*label",
                         "notifyWaiting must sanitize the label")

    def test_sanitize_covers_c1_controls(self):
        # C1 range (-) must be stripped too, not just C0+DEL.
        self.assertRegex(self.html, r"u007f-\\u009f",
                         "sanitizeLabel must strip the C1 control range")

    def test_checkbusy_prefers_server_content_hash(self):
        # Hash mode must compare the server's full-capture content_hash
        # (sees the spinner line above the status bar), falling back to
        # hashing the preview only for old servers without the field.
        m = re.search(r"function checkBusy\(.*?\)(.*?)^}", self.html,
                      re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "checkBusy not found")
        body = m.group(1)
        self.assertIn("content_hash", body,
                      "checkBusy hash branch must use pane.content_hash")
        self.assertIn("simpleHash", body,
                      "preview hashing must remain as fallback")

    def test_dedupe_key_only_recorded_when_notification_fired(self):
        # During the permission-prompt window Notification.permission is
        # 'default' — notifyWaiting() no-ops. The dedupe key must NOT be
        # recorded then, or the first real notification of the episode is
        # silently swallowed once permission is granted.
        self.assertRegex(self.html, r"if\s*\(notifyWaiting\(",
                         "notifiedWaiting.add must be gated on notifyWaiting() success")
        self.assertRegex(self.html, r"function notifyWaiting[\s\S]*?return true",
                         "notifyWaiting must report whether it actually fired")


if __name__ == "__main__":
    unittest.main(verbosity=2)
