"""TUI: slash-command registry shared with the chat, input handling, and a real terminal smoke test."""
import os
import select
import sys
import time
import unittest

from tests._util import SCRIPTS
import inaproc_tui as tui


class SlashTest(unittest.TestCase):
    def test_every_menu_has_a_plain_command_equivalent(self):
        for c in tui.COMMANDS:
            self.assertTrue(c.name.startswith("/"), c.name)
            self.assertTrue(c.equivalent, c.name)      # shown in /bantuan and README for AI agents

    def test_parse_maps_slash_commands_to_inaproc_argv(self):
        self.assertEqual(tui.parse("/cari kertas hvs"), ("run", ["search", "kertas hvs"]))
        self.assertEqual(tui.parse("/termurah laptop i5"), ("run", ["cheapest", "laptop i5", "--top", "10"]))
        self.assertEqual(tui.parse("/kategori surat"), ("run", ["penyedia", "categories", "--search", "surat"]))
        self.assertEqual(tui.parse("/kategori"), ("run", ["penyedia", "categories"]))
        self.assertEqual(tui.parse("/pesanan"), ("run", ["penyedia", "orders"]))
        self.assertEqual(tui.parse("/pesanan EP-123"), ("run", ["penyedia", "order", "EP-123"]))
        self.assertEqual(tui.parse("/nego"), ("run", ["penyedia", "negotiations"]))
        self.assertEqual(tui.parse("/produk akrilik"), ("run", ["penyedia", "products", "--search", "akrilik"]))
        self.assertEqual(tui.parse("/bandingkan a/b c/d"), ("run", ["compare", "a/b", "c/d"]))
        self.assertEqual(tui.parse("/run ppn --json"), ("run", ["ppn", "--json"]))
        self.assertEqual(tui.parse("/keluar"), ("quit", []))
        self.assertEqual(tui.parse("/keys add"), ("inapp", ["keys", "add"]))
        self.assertEqual(tui.parse("/model"), ("inapp", ["model"]))
        self.assertEqual(tui.parse("/doctor"), ("inapp", ["doctor"]))
        self.assertEqual(tui.parse("/login"), ("inapp", ["login"]))
        self.assertEqual(tui.parse("carikan kertas termurah"), ("ai", ["carikan kertas termurah"]))
        kind, msg = tui.parse("/tidakada")
        self.assertEqual(kind, "error")
        self.assertIn("/tidakada", msg[0])

    def test_missing_argument_is_a_hint_not_a_crash(self):
        kind, msg = tui.parse("/cari")
        self.assertEqual(kind, "error")
        self.assertIn("/cari", msg[0])

    def test_completion(self):
        self.assertEqual(tui.complete("/ter"), ["/termurah"])
        self.assertIn("/keys", tui.complete("/k"))
        self.assertEqual(tui.complete("halo"), [])


class InAppIOTest(unittest.TestCase):
    """/model, /doctor, /keys run inside the TUI: their questions become TUI popups, nothing leaves the screen."""

    def test_prompts_are_routed_to_the_tui_and_restored(self):
        import inaproc_ui as ui
        import inaproc_app as app
        before = (ui.choose, ui.ask, ui.confirm, ui.Steps, app._interactive)
        fake = tui.FakeIO(choose=[1], ask=["sk-secret"], confirm=[True])
        with tui.tui_io(fake, steps_factory=lambda *a, **k: tui.NullSteps()):
            self.assertEqual(ui.choose("Pilih", ["a", "b"]), 1)
            self.assertEqual(ui.ask("Key", secret=True), "sk-secret")
            self.assertTrue(ui.confirm("Kirim?"))
            self.assertTrue(app._interactive())
        self.assertEqual((ui.choose, ui.ask, ui.confirm, ui.Steps, app._interactive), before)
        self.assertEqual(fake.asked[1], ("ask", "Key", True))

    def test_streaming_writer_adds_lines_live(self):
        b = tui.Body()
        w = tui.BodyWriter(b, kind="plain")
        w.write("satu\ndu")
        self.assertEqual([t for t, k in b.lines], ["satu"])
        w.write("a\n")
        self.assertEqual([t for t, k in b.lines], ["satu", "dua"])


class WheelTest(unittest.TestCase):
    def test_wheel_scrolls_instead_of_history(self):
        import curses
        self.assertGreater(tui.wheel_delta(curses.BUTTON4_PRESSED), 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0x200000)
        self.assertLess(tui.wheel_delta(down), 0)
        self.assertEqual(tui.wheel_delta(curses.BUTTON1_CLICKED), 0)


class ConversationTest(unittest.TestCase):
    def test_turn_blocks(self):
        b = tui.Body()
        tui.add_user(b, "carikan kertas")
        tui.add_answer(b, "**Termurah**: Rp48.000\nbaris dua", footer="utama · claude-opus-5-5 · 3.2 dtk")
        kinds = [k for t, k in b.lines]
        self.assertIn("user", kinds)
        self.assertIn("ai-head", kinds)
        self.assertIn("ai", kinds)
        self.assertIn("ai-foot", kinds)
        self.assertNotIn("**", " ".join(t for t, k in b.lines))

    def test_answer_lines_keep_their_gutter_when_wrapped(self):
        rows = tui.screen_rows([("x" * 100, "ai")], width=40, height=10)
        self.assertTrue(all(r[0].startswith("│ ") for r in rows))
        self.assertEqual(rows[0][1], "ai")


class BufferTest(unittest.TestCase):
    def test_body_keeps_the_last_lines(self):
        b = tui.Body(limit=100)
        for i in range(150):
            b.add(f"baris {i}")
        self.assertEqual(len(b.lines), 100)
        self.assertEqual(b.lines[-1][0], "baris 149")


class WrapRowsTest(unittest.TestCase):
    def test_long_lines_wrap_instead_of_being_cut(self):
        url = "https://katalog.inaproc.id/neutech-solusi-indonesia/sinar-dunia-kertas-hvs-a4-80-gram-kso"
        rows = [r for r, k in tui.screen_rows(["│ URL │ " + url], width=40, height=10)]
        self.assertGreater(len(rows), 1)
        self.assertEqual("".join(r.strip() for r in rows).replace("│ URL │", "").strip(), url)
        self.assertTrue(all(len(r) <= 40 for r in rows))

    def test_prose_wraps_between_words(self):
        text = "Pesanan ini lewat negosiasi dan dikirim dengan Kurir Penyedia dan pesanan sebelumnya batal"
        rows = [r for r, k in tui.screen_rows([(text, "ai")], width=40, height=10)]
        words = set(text.split())
        for r in rows:
            for token in r[2:].split():
                self.assertIn(token, words, f"word split across rows: {r!r}")
        self.assertEqual(" ".join(r[2:].strip() for r in rows), text)

    def test_only_the_last_rows_that_fit(self):
        self.assertEqual([r for r, k in tui.screen_rows([f"l{i}" for i in range(50)], width=40, height=3)],
                         ["l47", "l48", "l49"])


@unittest.skipUnless(sys.platform != "win32", "needs a POSIX pty")
class TerminalSmokeTest(unittest.TestCase):
    def test_tui_starts_shows_the_sticky_header_and_quits(self):
        import fcntl
        import pty
        import struct
        import termios
        env = dict(os.environ, TERM="xterm-256color", INAPROC_AUTO_UPDATE="0", INAPROC_HOME=os.environ.get(
            "INAPROC_TEST_HOME", "/tmp/inaproc-tui-test"))
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, os.path.join(SCRIPTS, "inaproc_app.py"), "tui"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        out, deadline = b"", time.time() + 15
        while time.time() < deadline and b"/termurah" not in out:
            if select.select([fd], [], [], 0.3)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
        self.assertIn(b"/termurah", out)
        os.write(fd, b"/keluar\r")
        status = None
        deadline = time.time() + 10
        while time.time() < deadline:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    os.read(fd, 65536)
                except OSError:
                    pass
        if status is None:
            os.kill(pid, 9)
        self.assertEqual(os.waitstatus_to_exitcode(status) if status is not None else None, 0)


if __name__ == "__main__":
    unittest.main()
