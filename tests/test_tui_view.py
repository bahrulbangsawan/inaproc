"""Conversation area: row-based scrolling (View) and raw input decoding (mouse wheel, paste, Esc)."""
import unittest

import tests._util  # noqa: F401
import inaproc_tui as tui


def body_with(n, text="baris {}", kind="plain"):
    b = tui.Body()
    for i in range(n):
        b.add(text.format(i), kind)
    return b


class ViewTest(unittest.TestCase):
    def test_wheel_up_to_the_very_top_then_back_down(self):
        b, v = body_with(300), tui.View()
        v.layout(b, width=80)
        v.scroll(+10_000, height=40)
        top = v.window(40)
        self.assertEqual(len(top), 40)
        self.assertEqual(top[0][0], "baris 0")              # the very first line is reachable
        v.scroll(-3, height=40)                              # and you can come back down
        self.assertEqual(v.window(40)[0][0], "baris 3")
        v.scroll(-10_000, height=40)
        self.assertEqual(v.window(40)[-1][0], "baris 299")
        self.assertEqual(v.offset, 0)

    def test_wrapped_lines_count_as_rows_so_the_top_is_reachable(self):
        b = tui.Body()
        b.add("AWAL")
        for i in range(30):
            b.add("kata " * 60, "ai")                       # every line wraps into several rows
        v = tui.View()
        v.layout(b, width=60)
        v.scroll(+10_000, height=20)
        self.assertEqual(v.window(20)[0][0], "AWAL")

    def test_new_output_does_not_yank_a_reader_who_scrolled_up(self):
        b, v = body_with(200), tui.View()
        v.layout(b, width=80)
        v.scroll(+50, height=20)
        first = v.window(20)[0][0]
        for i in range(10):
            b.add(f"baru {i}")
        v.layout(b, width=80)
        self.assertEqual(v.window(20)[0][0], first)          # same view
        self.assertEqual(v.new_below, 10)                    # with a "new lines below" count
        v.to_bottom()
        self.assertEqual(v.window(20)[-1][0], "baru 9")
        self.assertEqual(v.new_below, 0)

    def test_at_the_bottom_new_output_follows(self):
        b, v = body_with(50), tui.View()
        v.layout(b, width=80)
        b.add("terbaru")
        v.layout(b, width=80)
        self.assertEqual(v.window(10)[-1][0], "terbaru")

    def test_resize_keeps_the_offset_valid(self):
        b, v = body_with(100), tui.View()
        v.layout(b, width=80)
        v.scroll(+10_000, height=10)
        v.layout(b, width=200)
        v.scroll(0, height=90)                                # taller window after a resize
        self.assertEqual(len(v.window(90)), 90)
        self.assertEqual(v.window(90)[0][0], "baris 0")

    def test_reader_stays_put_when_the_line_limit_drops_old_lines(self):
        b = tui.Body(limit=100)
        for i in range(100):
            b.add(f"baris {i}")
        v = tui.View()
        v.layout(b, width=80)
        v.scroll(+30, height=20)
        first = v.window(20)[0][0]
        for i in range(10):                                  # the oldest 10 lines are dropped at the top
            b.add(f"baru {i}")
        v.layout(b, width=80)
        self.assertEqual(v.window(20)[0][0], first)
        self.assertEqual(v.new_below, 10)

    def test_resize_while_reading_keeps_the_same_lines_on_screen(self):
        b = tui.Body()
        for i in range(200):
            b.add(f"baris {i} " + "kata " * 20, "ai")       # wraps at 60 columns, not at 200
        v = tui.View()
        v.layout(b, width=200)
        v.scroll(+100, height=20)
        bottom = v.window(20)[-1][0]
        v.layout(b, width=60)
        rows = [r for r, k in v.window(20)]
        self.assertTrue(any(r.startswith(bottom.split(" kata")[0]) for r in rows), rows)
        v.layout(b, width=200)
        self.assertEqual(v.window(20)[-1][0], bottom)        # and back again

    def test_short_conversation_never_scrolls(self):
        b, v = body_with(5), tui.View()
        v.layout(b, width=80)
        v.scroll(+50, height=40)
        self.assertEqual(v.offset, 0)
        self.assertEqual([r for r, k in v.window(40)], [f"baris {i}" for i in range(5)])

    def test_clear_and_line_limit(self):
        b = tui.Body(limit=100)
        for i in range(500):
            b.add(f"l{i}")
        v = tui.View()
        v.layout(b, width=80)
        v.scroll(+10_000, height=10)
        self.assertEqual(v.window(10)[0][0], "l400")
        b.clear()
        v.layout(b, width=80)
        self.assertEqual(v.window(10), [])
        self.assertEqual(v.offset, 0)


class DecoderTest(unittest.TestCase):
    def feed(self, s):
        d = tui.InputDecoder()
        out = []
        for ch in s:
            out += d.feed(ch)
        return out, d

    def test_sgr_wheel_up_and_down(self):
        out, _ = self.feed("\x1b[<64;10;5M\x1b[<65;10;5M\x1b[<65;1;1M")
        self.assertEqual(out, [("wheel", 1), ("wheel", -1), ("wheel", -1)])

    def test_clicks_are_ignored(self):
        out, _ = self.feed("\x1b[<0;3;4M\x1b[<0;3;4m")
        self.assertEqual(out, [])

    def test_plain_text_and_keys_pass_through(self):
        out, _ = self.feed("ab")
        self.assertEqual(out, [("key", "a"), ("key", "b")])

    def test_bracketed_paste_keeps_newlines_out_of_submit(self):
        out, _ = self.feed("\x1b[200~baris satu\nbaris dua\x1b[201~")
        self.assertEqual(out, [("paste", "baris satu baris dua")])

    def test_lone_escape_is_reported_after_a_pause(self):
        out, d = self.feed("\x1b")
        self.assertEqual(out, [])
        self.assertEqual(d.flush(), [("key", "\x1b")])

    def test_navigation_keys_in_both_terminal_forms(self):
        import curses
        cases = {"\x1b[A": curses.KEY_UP, "\x1bOA": curses.KEY_UP, "\x1b[B": curses.KEY_DOWN,
                 "\x1b[H": curses.KEY_HOME, "\x1bOH": curses.KEY_HOME, "\x1b[1~": curses.KEY_HOME,
                 "\x1b[F": curses.KEY_END, "\x1b[4~": curses.KEY_END, "\x1b[5~": curses.KEY_PPAGE,
                 "\x1b[6~": curses.KEY_NPAGE, "\x1b[3~": curses.KEY_DC, "\x1b[C": curses.KEY_RIGHT,
                 "\x1b[D": curses.KEY_LEFT}
        for seq, key in cases.items():
            out, _ = self.feed(seq)
            self.assertEqual(out, [("key", key)], repr(seq))

    def test_unknown_escape_sequence_does_not_swallow_text(self):
        out, d = self.feed("\x1b[99Zx")
        self.assertIn(("key", "x"), out)


if __name__ == "__main__":
    unittest.main()


class BusyTypingTest(unittest.TestCase):
    """While a job runs the reader can already type the next question; Enter waits until the job is done."""

    def app(self):
        import curses
        a = object.__new__(tui.App)
        a.c, a.body, a.view, a.page = curses, tui.Body(), tui.View(), 20
        a.input, a.history, a.hpos, a.modal, a.busy, a.quit, a.status = "", [], None, None, True, False, ""
        return a

    def test_typing_while_busy_is_kept(self):
        a = self.app()
        for ch in "harga laptop":
            a.key(ch)
        a.key("\x7f")
        self.assertEqual(a.input, "harga lapto")

    def test_enter_while_busy_does_not_send_and_keeps_the_text(self):
        a = self.app()
        for ch in "/bantuan":
            a.key(ch)
        a.key("\n")
        self.assertEqual(a.input, "/bantuan")
        self.assertEqual(a.history, [])

    def test_paste_while_busy_is_kept(self):
        a = self.app()
        a.event(("paste", "teks tempel"))
        self.assertEqual(a.input, "teks tempel")
