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
        d = srv.parse_pane_fields("0|1|nvim|/Users/x|96|42|edit-config", host="mac")
        self.assertEqual(d["command"], "nvim")
        self.assertEqual(d["title"], "edit-config")

    def test_blank_title_when_equals_host(self):
        # default pane_title is the hostname — treat as "no custom title".
        d = srv.parse_pane_fields("0|1|nvim|/Users/x|96|42|mac", host="mac")
        self.assertEqual(d["title"], "")

    def test_title_with_pipes_preserved(self):
        # pane_title is user-controlled and may contain our '|' delimiter;
        # it is the last field so the rest must stay intact.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|a|b|c", host="h")
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
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|1735000000|title",
                                  host="h", with_activity=True)
        self.assertIsNotNone(d)
        self.assertEqual(d["last_activity"], 1735000000)
        self.assertEqual(d["command"], "nvim")
        self.assertEqual(d["title"], "title")

    def test_parse_with_activity_title_pipes_preserved(self):
        # pane_title stays LAST so embedded pipes survive even with activity field.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|1735000000|a|b|c",
                                  host="h", with_activity=True)
        self.assertEqual(d["title"], "a|b|c")
        self.assertEqual(d["last_activity"], 1735000000)

    def test_parse_with_activity_short_rejected(self):
        # 7 fields when 8 are expected (activity mode) must be rejected.
        self.assertIsNone(srv.parse_pane_fields("0|1|nvim|/p|96|42|title",
                                                host="h", with_activity=True))

    def test_parse_with_activity_zero_on_non_numeric(self):
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42||title",
                                  host="h", with_activity=True)
        self.assertEqual(d["last_activity"], 0)

    def test_parse_without_activity_defaults_zero(self):
        # Hash-mode (7 fields) still works; last_activity defaults to 0.
        d = srv.parse_pane_fields("0|1|nvim|/p|96|42|title", host="h")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
