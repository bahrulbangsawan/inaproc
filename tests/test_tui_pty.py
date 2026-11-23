"""End-to-end TUI scenarios in a real pseudo-terminal: the exact bytes a terminal sends (mouse wheel in SGR mode,
PgUp/PgDn/Home/End, arrows, bracketed paste, Esc, Ctrl-D, window resizes), checked through the TUI's state file."""
import json
import os
import select
import signal
import struct
import sys
import tempfile
import time
import unittest

from tests._util import SCRIPTS

WHEEL_UP, WHEEL_DOWN = b"\x1b[<64;20;10M", b"\x1b[<65;20;10M"
PGUP, PGDN, HOME, END = b"\x1b[5~", b"\x1b[6~", b"\x1b[H", b"\x1b[F"
UP, DOWN, ESC, CTRL_D, ENTER = b"\x1b[A", b"\x1b[B", b"\x1b", b"\x04", b"\r"
BACKSPACE = b"\x7f"


@unittest.skipUnless(sys.platform != "win32", "needs a POSIX pty")
class TuiSession(unittest.TestCase):
    rows, cols = 40, 120

    def setUp(self):
        import fcntl
        import pty
        import termios
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        env = dict(os.environ, TERM="xterm-256color", INAPROC_AUTO_UPDATE="0", INAPROC_HOME=self.tmp.name,
                   INAPROC_TUI_STATE=self.state, INAPROC_NO_KEYCHAIN="1")
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
            env.pop(k, None)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(sys.executable, [sys.executable, os.path.join(SCRIPTS, "inaproc_app.py"), "tui"], env)
        self._fcntl, self._termios = fcntl, termios
        self.resize(self.rows, self.cols)
        self.wait(lambda s: True)

    def tearDown(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        os.close(self.fd)
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------------------------------------------
    def drain(self, secs=0.05):
        end = time.time() + secs
        while time.time() < end:
            if select.select([self.fd], [], [], 0.02)[0]:
                try:
                    os.read(self.fd, 65536)
                except OSError:
                    return

    def send(self, data, pause=0.03, deadline=30):
        """Write while keeping the TUI's output drained (a full pty buffer would deadlock both sides)."""
        view, end = memoryview(data), time.time() + deadline
        while view:
            if time.time() > end:
                self.fail(f"TUI stopped reading input ({len(view)} bytes left unsent)")
            _, writable, _ = select.select([], [self.fd], [], 0.05)
            if writable:
                os.set_blocking(self.fd, False)
                try:
                    view = view[os.write(self.fd, view[:256]):]
                except BlockingIOError:
                    pass
                finally:
                    os.set_blocking(self.fd, True)
            self.drain(0.005)
        self.drain(pause)

    def resize(self, rows, cols):
        self._fcntl.ioctl(self.fd, self._termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def read_state(self):
        try:
            with open(self.state, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def wait(self, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.drain(0.05)
            st = self.read_state()
            if st and pred(st):
                return st
        self.fail(f"state never matched; last: {self.read_state()}")

    def settle(self):
        """Wait until the TUI has processed everything sent so far (state stops changing)."""
        self.drain(0.25)
        return self.wait(lambda s: not s["busy"] and time.time() - s["t"] < 1)

    def fill(self, times=12):
        before = (self.read_state() or {}).get("rows", 0)
        self.send(b"/bantuan" + ENTER, 0.08)
        one = self.wait(lambda s: s["rows"] > before)["rows"] - before
        for _ in range(times - 1):
            self.send(b"/bantuan" + ENTER, 0.08)
        return self.wait(lambda s: s["rows"] >= before + one * times and not s["busy"])

    def max_offset(self, st):
        return st["rows"] - st["page"]

    # scenarios -------------------------------------------------------------------------------------------------
    def test_wheel_to_the_top_and_back_down(self):
        st = self.fill()
        self.send(WHEEL_UP * 400, 0)                                    # a long swipe: one row per event
        st = self.wait(lambda s: s["offset"] == self.max_offset(s))
        top_offset = st["offset"]
        self.assertTrue(st["topRow"].startswith("Tanya apa saja"))   # the very first line is visible
        self.settle()
        time.sleep(0.2)                                                # the swipe is over
        for _ in range(3):
            self.send(WHEEL_DOWN, 0.2)                                 # three separate notches: three rows each
        st = self.wait(lambda s: s["offset"] == top_offset - 9)       # the reported bug: it must come back down
        self.send(WHEEL_DOWN * 400, 0)
        self.wait(lambda s: s["offset"] == 0)

    def test_pages_home_end(self):
        st = self.fill()
        self.send(PGUP)
        st = self.wait(lambda s: s["offset"] > 0)
        one = st["offset"]
        self.send(PGUP)
        st = self.wait(lambda s: s["offset"] > one)
        self.send(PGDN)
        self.send(PGDN)
        self.wait(lambda s: s["offset"] == 0)
        self.send(HOME)
        self.wait(lambda s: s["offset"] == self.max_offset(s))
        self.send(END)
        self.wait(lambda s: s["offset"] == 0)

    def test_wheel_never_touches_the_input_or_history(self):
        self.fill(3)
        self.send(b"draft pertanyaan")
        self.wait(lambda s: s["input"] == "draft pertanyaan")
        for _ in range(20):
            self.send(WHEEL_UP, 0)
            self.send(WHEEL_DOWN, 0)
        st = self.settle()
        self.assertEqual(st["input"], "draft pertanyaan")
        self.assertIsNone(st["hpos"])

    def test_history_arrows_still_work(self):
        self.fill(2)
        self.send(b"/bersih" + ENTER)
        self.wait(lambda s: s["rows"] <= 3)
        self.send(UP)
        self.wait(lambda s: s["input"] == "/bersih")
        self.send(UP)
        self.wait(lambda s: s["input"] == "/bantuan")
        self.send(DOWN)
        self.send(DOWN)
        self.wait(lambda s: s["input"] == "" and s["hpos"] is None)

    def test_paste_goes_into_the_input_without_submitting(self):
        self.send(b"\x1b[200~baris satu\nbaris dua\r\nbaris tiga\x1b[201~")
        st = self.wait(lambda s: s["input"] == "baris satu baris dua baris tiga")
        self.assertEqual(st["history"], [])

    def test_long_input_and_lone_esc(self):
        self.send(b"x" * 400)
        self.wait(lambda s: len(s["input"]) == 400)
        self.send(ESC)
        time.sleep(0.2)
        st = self.settle()
        self.assertEqual(len(st["input"]), 400)
        self.assertIsNone(st["modal"])

    def test_resize_keeps_scrolling_valid(self):
        st = self.fill()
        self.send(HOME)
        self.wait(lambda s: s["offset"] == self.max_offset(s) and s["offset"] > 0)
        for rows, cols in ((20, 70), (60, 200), (12, 50), (40, 120)):
            self.resize(rows, cols)
            st = self.wait(lambda s: s["size"] == [rows, cols])
            st = self.settle()
            self.assertLessEqual(st["offset"], self.max_offset(st))
            self.assertGreaterEqual(st["offset"], 0)
        self.send(END)
        self.wait(lambda s: s["offset"] == 0)

    def test_popup_cancel_with_esc_and_scroll_after(self):
        self.fill(2)
        self.send(b"/keys add" + ENTER)
        self.wait(lambda s: s["modal"] == "choose")
        self.send(WHEEL_UP, 0.1)                     # wheel while a popup is open: harmless
        self.send(DOWN)
        self.send(ESC)
        st = self.wait(lambda s: s["modal"] is None and not s["busy"])
        self.send(HOME)
        self.wait(lambda s: s["offset"] == self.max_offset(s))
        self.send(END)
        self.wait(lambda s: s["offset"] == 0)

    def test_clicks_and_unknown_sequences_type_nothing(self):
        self.send(b"\x1b[<0;5;5M\x1b[<0;5;5m\x1b[1;5A\x1b[99Z")
        st = self.settle()
        self.assertEqual(st["input"], "")

    def test_typing_while_scrolled_and_submit_returns_to_the_bottom(self):
        self.fill()
        self.send(HOME)
        self.wait(lambda s: s["offset"] > 0)
        self.send(b"/bersih")
        st = self.wait(lambda s: s["input"] == "/bersih")
        self.assertGreater(st["offset"], 0)          # typing doesn't move the view
        self.send(ENTER)
        self.wait(lambda s: s["offset"] == 0 and s["rows"] <= 3)

    def test_ctrl_d_quits_cleanly(self):
        self.send(CTRL_D)
        end = time.time() + 5
        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                return
            self.drain(0.05)
        self.fail("TUI did not quit on Ctrl-D")


    def alive(self):
        return os.waitpid(self.pid, os.WNOHANG) == (0, 0)

    def test_tiny_terminal_does_not_crash(self):
        self.fill(3)
        self.send(HOME)
        for rows, cols in ((4, 12), (1, 1), (2, 30), (8, 8), (40, 120)):
            self.resize(rows, cols)
            self.drain(0.3)
            self.assertTrue(self.alive(), f"crashed at {rows}x{cols}")
            self.send(WHEEL_UP + WHEEL_DOWN + b"ab" + BACKSPACE * 2)
        st = self.wait(lambda s: s["size"] == [40, 120])
        self.assertLessEqual(st["offset"], self.max_offset(st))

    def test_wide_characters_and_emoji_in_the_input(self):
        text = "cari 笔记本 laptop 💻 murah — é"
        self.send(text.encode())
        self.wait(lambda s: s["input"] == text)
        self.send(BACKSPACE * 3)
        self.wait(lambda s: s["input"] == text[:-3])
        self.send("ä".encode() * 300)                       # longer than the line: scrolls sideways
        st = self.wait(lambda s: s["input"].endswith("ä" * 300))
        self.assertTrue(self.alive())


    def test_scrolling_stops_when_the_wheel_stops(self):
        """A trackpad swipe sends a burst of wheel events; the view must catch up at once, not keep scrolling."""
        self.fill()
        self.send(HOME)
        st = self.wait(lambda s: s["offset"] > 0)
        start = time.time()
        self.send(WHEEL_DOWN * 150, 0, deadline=3)
        sent = time.time()
        last, stable_since = None, time.time()
        while time.time() - stable_since < 0.4 and time.time() - sent < 10:
            self.drain(0.02)
            cur = (self.read_state() or {}).get("offset")
            if cur != last:
                last, stable_since = cur, time.time()
        lag = stable_since - start
        self.assertLess(lag, 0.5, f"view kept scrolling {lag:.2f}s after the wheel stopped")
        self.assertEqual(last, max(0, st["offset"] - 150))        # exactly one row per event of the swipe

if __name__ == "__main__":
    unittest.main()
